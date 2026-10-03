"""Pool wallet tests — sqlite-backed, no external services needed.

Run: python3 -m unittest discover -s tests -v   (from pool_wallet/)
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("POOL_WALLET_API_KEY", "test-key")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# in-memory sqlite shared across connections
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

import app.db as db_mod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402

Base.metadata.create_all(bind=engine)
db_mod.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)
HEADERS = {"x-api-key": "test-key"}


def _credit(amount=100_000, payment_id="pay_1", connector="stripe", settle=True):
    return client.post(
        "/wallets/credit",
        json={
            "merchant_id": "merch_1",
            "currency": "IDR",
            "amount_minor": amount,
            "payment_id": payment_id,
            "connector": connector,
            "settle_immediately": settle,
        },
        headers=HEADERS,
    )


class WalletFlowTest(unittest.TestCase):
    def test_credit_pending_then_settle(self):
        r = _credit(amount=50_000, payment_id="pay_pending", settle=False)
        assert r.status_code == 200
        bal = client.get("/wallets/pool_merch_1_idr", headers=HEADERS).json()
        assert bal["pending_balance"] >= 50_000
        before = bal["available_balance"]
        r = client.post(
            "/wallets/settle", json={"payment_id": "pay_pending"}, headers=HEADERS
        )
        assert r.json()["settled_amount_minor"] == 50_000
        bal = client.get("/wallets/pool_merch_1_idr", headers=HEADERS).json()
        assert bal["available_balance"] == before + 50_000

    def test_credit_idempotent(self):
        body = {
            "merchant_id": "merch_1",
            "currency": "IDR",
            "amount_minor": 10_000,
            "payment_id": "pay_idem",
            "connector": "custom_ewallet",
            "idempotency_key": "k-dup-1",
            "settle_immediately": True,
        }
        r1 = client.post("/wallets/credit", json=body, headers=HEADERS)
        r2 = client.post("/wallets/credit", json=body, headers=HEADERS)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["entry_id"] == r2.json()["entry_id"]

    def test_insufficient_withdraw(self):
        _credit(amount=5_000, payment_id="pay_small")
        r = client.post(
            "/wallets/pool_merch_1_idr/withdraw",
            json={
                "amount_minor": 999_999_999,
                "beneficiary": {
                    "bank_code": "bca",
                    "account_number": "12345",
                    "account_holder_name": "Test",
                },
            },
            headers=HEADERS,
        )
        assert r.status_code == 409

    def test_withdraw_reserve_dispatch_commit(self):
        _credit(amount=200_000, payment_id="pay_wd")
        before = client.get(
            "/wallets/pool_merch_1_idr", headers=HEADERS
        ).json()

        with patch(
            "app.routers.wallets.hyperswitch.create_payout"
        ) as mock_hs:
            mock_hs.return_value = {
                "payout_id": "pout_hs_1",
                "status": "initiated",
                "connector_payout_id": "1234567890123456789",
            }
            r = client.post(
                "/wallets/pool_merch_1_idr/withdraw",
                json={
                    "amount_minor": 100_000,
                    "beneficiary": {
                        "bank_code": "bca",
                        "account_number": "0012345678",
                        "account_holder_name": "Jane",
                    },
                    "idempotency_key": "wd-1",
                },
                headers=HEADERS,
            )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "initiated"
        assert data["connector"] == "flip"

        mid = client.get("/wallets/pool_merch_1_idr", headers=HEADERS).json()
        assert mid["reserved_balance"] == before["reserved_balance"] + 100_000
        assert mid["available_balance"] == before["available_balance"] - 100_000

        # provider webhook: success -> commit (reserved drains, paid_out rises)
        r = client.post(
            "/webhooks/hyperswitch",
            json={
                "event_type": "payout.success",
                "object_id": "pout_hs_1",
            },
        )
        assert r.status_code == 200
        after = client.get(
            "/wallets/pool_merch_1_idr", headers=HEADERS
        ).json()
        assert after["reserved_balance"] == mid["reserved_balance"] - 100_000
        assert after["total_paid_out"] >= 100_000

    def test_withdraw_release_on_provider_failure(self):
        _credit(amount=80_000, payment_id="pay_wd_fail")
        before = client.get(
            "/wallets/pool_merch_1_idr", headers=HEADERS
        ).json()
        with patch(
            "app.routers.wallets.hyperswitch.create_payout"
        ) as mock_hs:
            mock_hs.return_value = {"status": "failed", "error_message": "bad acct"}
            r = client.post(
                "/wallets/pool_merch_1_idr/withdraw",
                json={
                    "amount_minor": 80_000,
                    "beneficiary": {
                        "bank_code": "bni",
                        "account_number": "999",
                        "account_holder_name": "Bob",
                    },
                },
                headers=HEADERS,
            )
        assert r.status_code == 200
        after = client.get(
            "/wallets/pool_merch_1_idr", headers=HEADERS
        ).json()
        # reservation released — funds back to available
        assert after["available_balance"] == before["available_balance"]
        assert after["reserved_balance"] == before["reserved_balance"]

    def test_withdraw_idempotent(self):
        with patch(
            "app.routers.wallets.hyperswitch.create_payout"
        ) as mock_hs:
            mock_hs.return_value = {"status": "initiated", "payout_id": "pout_x"}
            body = {
                "amount_minor": 10_000,
                "beneficiary": {
                    "bank_code": "bri",
                    "account_number": "1",
                    "account_holder_name": "A",
                },
                "idempotency_key": "wd-idem",
            }
            r1 = client.post(
                "/wallets/pool_merch_1_idr/withdraw", json=body, headers=HEADERS
            )
            r2 = client.post(
                "/wallets/pool_merch_1_idr/withdraw", json=body, headers=HEADERS
            )
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["payout_id"] == r2.json()["payout_id"]
        assert mock_hs.call_count == 1  # hyperswitch hit once only

    def test_connector_totals(self):
        _credit(amount=30_000, payment_id="pay_totals", connector="stripe")
        r = client.get(
            "/wallets/pool_merch_1_idr/connectors", headers=HEADERS
        )
        assert r.status_code == 200
        totals = r.json()["totals_by_connector"]
        assert "stripe" in totals
        assert totals["stripe"]["credit"] >= 30_000

    def test_unauthorized(self):
        r = client.get("/wallets/pool_merch_1_idr")
        assert r.status_code == 401


if __name__ == "__main__":
    unittest.main()
