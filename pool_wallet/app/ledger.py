"""Core pool-wallet ledger operations.

Every balance mutation runs inside a single DB transaction holding a
``SELECT ... FOR UPDATE`` row lock on the wallet row, so concurrent withdraw
requests can never overspend the available balance.

Accounting model
----------------
Each ledger entry is a single wallet leg of a balanced journal. The counter
leg is the implied system account selected by ``journal_kind``
(e.g. ``payment_collect`` credits the pool against a per-connector clearing
account, ``payout_commit`` debits the pool against a payout clearing account).
The journal_id + connector fields provide the audit trail; reconciliation
compares posted credits/debits against processor settlement data.

Idempotency
-----------
``idempotency_key`` columns carry a UNIQUE constraint on both ledger_entries
and pool_payouts. A retried request hits the constraint and the original
record is returned instead of duplicating money movement.
"""

import uuid
from typing import Optional, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    EntryDirection,
    EntryStatus,
    JournalKind,
    LedgerEntry,
    PoolPayout,
    Wallet,
)


class LedgerError(Exception):
    """Domain error surfaced to the API layer."""


class InsufficientFunds(LedgerError):
    pass


def _jid() -> str:
    return f"jrn_{uuid.uuid4().hex[:24]}"


def get_or_create_wallet(
    db: Session, merchant_id: str, currency: str
) -> Wallet:
    wallet = (
        db.query(Wallet)
        .filter_by(merchant_id=merchant_id, currency=currency)
        .first()
    )
    if wallet is None:
        wallet = Wallet(
            wallet_id=f"pool_{merchant_id}_{currency.lower()}",
            merchant_id=merchant_id,
            currency=currency,
        )
        db.add(wallet)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            wallet = (
                db.query(Wallet)
                .filter_by(merchant_id=merchant_id, currency=currency)
                .first()
            )
    return wallet


def _lock_wallet(db: Session, wallet_id: str) -> Wallet:
    wallet = (
        db.query(Wallet)
        .filter_by(wallet_id=wallet_id)
        .with_for_update()
        .first()
    )
    if wallet is None:
        raise LedgerError(f"wallet {wallet_id} not found")
    return wallet


# ---------------------------------------------------------------------------
# Collection side (payments -> pool)
# ---------------------------------------------------------------------------

def credit_payment(
    db: Session,
    merchant_id: str,
    currency: str,
    amount_minor: int,
    payment_id: str,
    connector: Optional[str],
    idempotency_key: Optional[str],
    auto_settle: bool,
    memo: Optional[str] = None,
) -> LedgerEntry:
    """Record a collected/captured payment.

    ``auto_settle`` controls whether the funds count as available immediately
    (use only when settlement is guaranteed by the funding model) or sit in
    pending until an explicit settlement signal.
    """
    if amount_minor <= 0:
        raise LedgerError("amount must be positive")

    status = (
        EntryStatus.POSTED if auto_settle else EntryStatus.PENDING
    ).value
    entry = LedgerEntry(
        journal_id=_jid(),
        direction=EntryDirection.CREDIT.value,
        status=status,
        journal_kind=JournalKind.PAYMENT_COLLECT.value,
        amount_minor=amount_minor,
        currency=currency,
        payment_id=payment_id,
        connector=connector,
        idempotency_key=idempotency_key,
        memo=memo,
    )

    wallet = get_or_create_wallet(db, merchant_id, currency)
    wallet = _lock_wallet(db, wallet.wallet_id)

    entry.wallet_id = wallet.wallet_id
    db.add(entry)
    if auto_settle:
        wallet.available_balance += amount_minor
        entry.posted_at_ms = entry.created_at_ms
    else:
        wallet.pending_balance += amount_minor
    wallet.total_collected += amount_minor
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = (
                db.query(LedgerEntry)
                .filter_by(idempotency_key=idempotency_key)
                .first()
            )
            if existing is not None:
                return existing
        raise
    return entry


def settle_payment(db: Session, payment_id: str) -> Tuple[int, int]:
    """Move all pending credit entries for a payment to posted/available.

    Returns (settled_count, settled_amount).
    """
    pending = (
        db.query(LedgerEntry)
        .filter_by(payment_id=payment_id, status=EntryStatus.PENDING.value)
        .all()
    )
    if not pending:
        return (0, 0)

    wallet = _lock_wallet(db, pending[0].wallet_id)
    total = 0
    for entry in pending:
        entry.status = EntryStatus.POSTED.value
        wallet.pending_balance -= entry.amount_minor
        wallet.available_balance += entry.amount_minor
        total += entry.amount_minor
        entry.posted_at_ms = _now()
    db.commit()
    return (len(pending), total)


def void_payment_pending(db: Session, payment_id: str) -> int:
    """Void pending entries for a payment that failed after collection.

    Returns voided amount.
    """
    pending = (
        db.query(LedgerEntry)
        .filter_by(payment_id=payment_id, status=EntryStatus.PENDING.value)
        .all()
    )
    total = 0
    if not pending:
        return 0
    wallet = _lock_wallet(db, pending[0].wallet_id)
    for entry in pending:
        entry.status = EntryStatus.VOIDED.value
        wallet.pending_balance -= entry.amount_minor
        total += entry.amount_minor
    db.commit()
    return total


def reverse_payment(
    db: Session,
    payment_id: str,
    amount_minor: Optional[int] = None,
    reason: str = "reversal",
) -> int:
    """Reverse posted credits (refund/chargeback). Returns reversed amount."""
    posted = (
        db.query(LedgerEntry)
        .filter_by(payment_id=payment_id, status=EntryStatus.POSTED.value)
        .all()
    )
    if not posted:
        return 0
    wallet = _lock_wallet(db, posted[0].wallet_id)
    total = 0
    for entry in posted:
        amt = amount_minor or entry.amount_minor
        amt = min(amt, entry.amount_minor)
        entry.status = EntryStatus.REVERSED.value
        wallet.available_balance -= amt
        wallet.total_collected -= amt
        total += amt
        # balanced counter-leg marking the reversal
        db.add(
            LedgerEntry(
                journal_id=_jid(),
                wallet_id=wallet.wallet_id,
                direction=EntryDirection.DEBIT.value,
                status=EntryStatus.POSTED.value,
                journal_kind=JournalKind.REVERSAL.value,
                amount_minor=amt,
                currency=entry.currency,
                payment_id=payment_id,
                connector=entry.connector,
                memo=reason,
            )
        )
    # note: available_balance may go negative — that is a real deficit the
    # merchant owes back to the pool and it must stay visible.
    db.commit()
    return total


# ---------------------------------------------------------------------------
# Withdrawal side (pool -> payout processor)
# ---------------------------------------------------------------------------

def reserve_for_payout(
    db: Session,
    wallet_id: str,
    amount_minor: int,
    connector: str,
    beneficiary: dict,
    idempotency_key: Optional[str],
) -> Tuple[PoolPayout, LedgerEntry]:
    """Atomically reserve funds and create the payout record.

    Raises InsufficientFunds when available < amount.
    """
    if amount_minor <= 0:
        raise LedgerError("amount must be positive")

    wallet = _lock_wallet(db, wallet_id)
    if wallet.available_balance < amount_minor:
        raise InsufficientFunds(
            f"insufficient available balance: {wallet.available_balance} < {amount_minor}"
        )

    payout = PoolPayout(
        wallet_id=wallet.wallet_id,
        connector=connector,
        amount_minor=amount_minor,
        currency=wallet.currency,
        beneficiary=beneficiary,
        status="pending",
        idempotency_key=idempotency_key,
    )
    entry = LedgerEntry(
        journal_id=_jid(),
        wallet_id=wallet.wallet_id,
        direction=EntryDirection.DEBIT.value,
        status=EntryStatus.RESERVED.value,
        journal_kind=JournalKind.PAYOUT_RESERVE.value,
        amount_minor=amount_minor,
        currency=wallet.currency,
        connector=connector,
        idempotency_key=(
            f"payout-reserve:{idempotency_key}" if idempotency_key else None
        ),
        memo="payout reservation",
    )
    db.add(payout)
    db.add(entry)
    db.flush()

    entry.payout_id = payout.payout_id

    wallet.available_balance -= amount_minor
    wallet.reserved_balance += amount_minor
    db.commit()
    return payout, entry


def mark_payout_dispatched(
    db: Session,
    payout_id: str,
    hyperswitch_payout_id: Optional[str],
    connector_payout_id: Optional[str],
    status: str,
) -> PoolPayout:
    payout = db.query(PoolPayout).filter_by(payout_id=payout_id).first()
    if payout is None:
        raise LedgerError(f"payout {payout_id} not found")
    payout.hyperswitch_payout_id = hyperswitch_payout_id
    payout.connector_payout_id = connector_payout_id
    payout.status = status
    db.commit()
    return payout


def commit_payout(db: Session, payout_id: str) -> Optional[PoolPayout]:
    """Provider confirmed success: reserved funds leave the pool for good."""
    payout = (
        db.query(PoolPayout)
        .filter_by(payout_id=payout_id)
        .with_for_update()
        .first()
    )
    if payout is None or payout.status in ("success", "failed", "cancelled"):
        return payout

    wallet = _lock_wallet(db, payout.wallet_id)
    entry = (
        db.query(LedgerEntry)
        .filter_by(
            payout_id=payout_id,
            status=EntryStatus.RESERVED.value,
            journal_kind=JournalKind.PAYOUT_RESERVE.value,
        )
        .first()
    )
    if entry is not None:
        entry.status = EntryStatus.POSTED.value
        entry.journal_kind = JournalKind.PAYOUT_COMMIT.value
        entry.posted_at_ms = _now()
        wallet.reserved_balance -= entry.amount_minor
        wallet.total_paid_out += entry.amount_minor
    payout.status = "success"
    db.commit()
    return payout


def release_payout(
    db: Session,
    payout_id: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    final_status: str = "failed",
) -> Optional[PoolPayout]:
    """Payout failed/cancelled before dispatch or afterwards — release funds."""
    payout = (
        db.query(PoolPayout)
        .filter_by(payout_id=payout_id)
        .with_for_update()
        .first()
    )
    if payout is None or payout.status in ("success", "failed", "cancelled"):
        return payout

    wallet = _lock_wallet(db, payout.wallet_id)
    entry = (
        db.query(LedgerEntry)
        .filter_by(
            payout_id=payout_id,
            status=EntryStatus.RESERVED.value,
            journal_kind=JournalKind.PAYOUT_RESERVE.value,
        )
        .first()
    )
    if entry is not None:
        entry.status = EntryStatus.RELEASED.value
        entry.journal_kind = JournalKind.PAYOUT_RELEASE.value
        wallet.reserved_balance -= entry.amount_minor
        wallet.available_balance += entry.amount_minor
    payout.status = final_status
    payout.error_code = error_code
    payout.error_message = error_message
    db.commit()
    return payout


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def get_balances(db: Session, wallet_id: str) -> dict:
    wallet = (
        db.query(Wallet).filter_by(wallet_id=wallet_id).first()
    )
    if wallet is None:
        raise LedgerError(f"wallet {wallet_id} not found")
    return {
        "wallet_id": wallet.wallet_id,
        "merchant_id": wallet.merchant_id,
        "currency": wallet.currency,
        "available_balance": wallet.available_balance,
        "pending_balance": wallet.pending_balance,
        "reserved_balance": wallet.reserved_balance,
        "total_collected": wallet.total_collected,
        "total_paid_out": wallet.total_paid_out,
        "created_at_ms": wallet.created_at_ms,
        "updated_at_ms": wallet.updated_at_ms,
    }


def get_by_connector_totals(db: Session, wallet_id: str) -> dict:
    """Per-connector accumulated totals — shows where pooled funds came from."""
    rows = (
        db.query(
            LedgerEntry.connector,
            LedgerEntry.direction,
            func.sum(LedgerEntry.amount_minor),
        )
        .filter(
            LedgerEntry.wallet_id == wallet_id,
            LedgerEntry.status.in_(
                [EntryStatus.POSTED.value, EntryStatus.PENDING.value]
            ),
        )
        .group_by(LedgerEntry.connector, LedgerEntry.direction)
        .all()
    )
    out: dict = {}
    for connector, direction, total in rows:
        key = connector or "unknown"
        bucket = out.setdefault(key, {"credit": 0, "debit": 0})
        bucket[direction] += int(total or 0)
    return out


def _now() -> int:
    import time

    return int(time.time() * 1000)
