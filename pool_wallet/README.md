# Pool Wallet

A companion ledger service for Hyperswitch that aggregates funds collected
through any payment method / e-wallet / custom connector into a single
per-merchant pool, and withdraws that pool through any Hyperswitch payout
connector (Flip for Indonesian bank accounts by default).

## Important: what "balance" means

Hyperswitch orchestrates payment and payout API calls — it does **not** move
money between unrelated processor accounts. A connector's payment "success"
does not mean cash is sitting in a bank account you control.

This service therefore keeps an honest ledger:

| Bucket      | Meaning                                                        |
|-------------|----------------------------------------------------------------|
| `pending`   | Payment collected/captured at the connector, **not settled**.  |
| `available` | Settled funds — the only spendable bucket for withdrawals.     |
| `reserved`  | Locked for in-flight payouts; released back if payout fails.   |

Funds move `pending → available` only on a settlement signal:

- `POST /wallets/settle {payment_id}` (operator/settlement-report driven), or
- set `POOL_WALLET_AUTO_SETTLE=true` to treat connector success as settled —
  use only when your funding model guarantees it (e.g. you operate a central
  acquiring/settlement account the processors settle into).

Withdrawals follow `available → reserved → committed` (or `reserved → released`
on failure). All reservations are atomic (`SELECT ... FOR UPDATE` on the wallet
row) and idempotent (UNIQUE idempotency keys on both `pool_payouts` and
`ledger_entries`).

## Data model

- `wallets` — one row per `(merchant_id, currency)` with materialised balances
- `ledger_entries` — append-only journal; every row carries `journal_id`,
  `journal_kind`, `direction`, `status`, plus `payment_id` / `payout_id` /
  `connector` for full traceability
- `pool_payouts` — withdrawal records linked to the reservation entry and to
  the Hyperswitch payout id

## Flow

```
collect:   POST /wallets/credit          (or payment_intent.succeeded webhook)
settle:    POST /wallets/settle          pending -> available
inspect:   GET  /wallets/{id}            balances
           GET  /wallets/{id}/entries    ledger journal
           GET  /wallets/{id}/connectors accumulated totals per connector
withdraw:  POST /wallets/{id}/withdraw   reserve -> POST /payouts (hyperswitch)
webhooks:  POST /webhooks/hyperswitch    payout.success/failed, payment events
reconcile: POST /reconcile/payouts       re-pulls stale payout statuses
```

Withdraw example (Indonesian bank via Flip):

```bash
curl -X POST localhost:8090/wallets/pool_merch_1_idr/withdraw \
  -H "x-api-key: $POOL_WALLET_API_KEY" \
  -d '{
    "amount_minor": 100000,
    "beneficiary": {
      "bank_code": "bca",
      "account_number": "0012345678",
      "account_holder_name": "Jane Doe"
    },
    "idempotency_key": "wd-2024-001"
  }'
```

`bank_code` accepts the Flip bank codes: `bca, bni, bri, mandiri, cimb,
permata, danamon, bsi, ovo, gopay, dana, linkaja, shopeepay, ...`.

## Payout processor routing

`PAYOUT_CONNECTOR_ROUTING` maps wallet currency → connector, e.g.
`{"IDR": "flip", "USD": "wise", "EUR": "wise"}`. Any payout connector
registered in Hyperswitch can be used (`flip`, `wise`, `stripe`, `adyenplatform`,
`trustly`, ...). Per-request override: `"connector": "wise"` in the withdraw body.

## Running

```bash
# local (sqlite, zero infra)
cd pool_wallet && pip install -r requirements.txt
uvicorn app.main:app --port 8090

# tests
python3 -m unittest discover -s tests -v

# with docker-compose (uses the shared pg container, own `pool_wallet` db)
docker compose up -d pool-wallet
```

## Environment

| Variable                    | Default                        | Purpose                              |
|-----------------------------|--------------------------------|--------------------------------------|
| `DATABASE_URL`              | `sqlite:///./pool_wallet.db`   | SQLAlchemy URL                       |
| `POOL_WALLET_CREATE_DB`     | unset                          | `1` = create the Postgres db on boot |
| `POOL_WALLET_API_KEY`       | `dev-key`                      | `x-api-key` for all merchant APIs    |
| `HYPERSWITCH_BASE_URL`      | `http://hyperswitch-server:8080` | router API                       |
| `HYPERSWITCH_API_KEY`       | —                              | api-key for `/payouts`               |
| `HYPERSWITCH_PROFILE_ID`    | —                              | optional profile to route payouts    |
| `DEFAULT_PAYOUT_CONNECTOR`  | `flip`                         | fallback connector                   |
| `PAYOUT_CONNECTOR_ROUTING`  | `{"IDR": "flip"}`              | currency → connector map             |
| `POOL_WALLET_AUTO_SETTLE`   | `false`                        | treat collection as settled cash     |
| `POOL_WALLET_WEBHOOK_SECRET`| unset                          | HMAC-SHA256 verify for webhooks      |

## Webhook wiring

Point the Hyperswitch merchant webhook URL at
`http://<service>/webhooks/hyperswitch`. Payment success events credit the
pool (`pending` unless auto-settle); payout events commit or release the
reserved funds. `POST /reconcile/payouts` heals any missed webhook by polling
`GET /payouts/{id}` on Hyperswitch.

## Security notes

- All amounts are integer minor units; never floats.
- `POOL_WALLET_API_KEY` guards every endpoint except `/webhooks/*` and
  `/health`. Restrict webhook ingress at the network layer and/or set
  `POOL_WALLET_WEBHOOK_SECRET`.
- Bank account numbers and beneficiary names are stored as returned by
  Hyperswitch; do not log them.
