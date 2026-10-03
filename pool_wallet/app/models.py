"""Pool wallet storage model.

Tables
------
wallets            one row per (merchant, currency) pool with materialised
                   balances. All mutations go through a row lock so balance
                   math is serialised per wallet.
ledger_entries     append-only journal. Money only moves via balanced pairs of
                   entries (credit/debit legs sharing a `journal_id`).
pool_payouts       withdrawal record created before the Hyperswitch payout call
                   and reconciled by webhooks / the reconcile worker.
"""

import enum
import time
import uuid

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# JSONB on Postgres, portable JSON elsewhere (sqlite for tests/local).
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class EntryStatus(str, enum.Enum):
    PENDING = "pending"      # collected but not settled — not spendable
    POSTED = "posted"        # settled — affects available balance
    RESERVED = "reserved"    # reserved for an in-flight payout
    RELEASED = "released"    # a reservation that was released (payout failed)
    VOIDED = "voided"        # cancelled before posting (payment reversal)
    REVERSED = "reversed"    # posted then reversed (chargeback/refund)


class EntryDirection(str, enum.Enum):
    CREDIT = "credit"   # money into the pool
    DEBIT = "debit"     # money out of the pool


class JournalKind(str, enum.Enum):
    PAYMENT_COLLECT = "payment_collect"     # collected/captured payment
    PAYMENT_SETTLE = "payment_settle"       # pending -> available settlement
    PAYOUT_RESERVE = "payout_reserve"       # reserve funds for a payout
    PAYOUT_COMMIT = "payout_commit"         # payout succeeded, funds leave
    PAYOUT_RELEASE = "payout_release"       # payout failed/cancelled
    REVERSAL = "reversal"                   # refund/chargeback clawback
    ADJUSTMENT = "adjustment"               # manual operator adjustment


class Wallet(Base):
    __tablename__ = "wallets"

    wallet_id = Column(String(64), primary_key=True)
    merchant_id = Column(String(64), nullable=False)
    currency = Column(String(8), nullable=False)
    # all amounts in minor units (cents / smallest denomination)
    available_balance = Column(BigInteger, nullable=False, default=0)
    pending_balance = Column(BigInteger, nullable=False, default=0)
    reserved_balance = Column(BigInteger, nullable=False, default=0)
    total_collected = Column(BigInteger, nullable=False, default=0)
    total_paid_out = Column(BigInteger, nullable=False, default=0)
    created_at_ms = Column(BigInteger, nullable=False, default=_now_ms)
    updated_at_ms = Column(BigInteger, nullable=False, default=_now_ms, onupdate=_now_ms)
    version = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("merchant_id", "currency", name="uq_wallet_merchant_currency"),
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    entry_id = Column(String(64), primary_key=True, default=lambda: _id("le"))
    journal_id = Column(String(64), nullable=False, index=True)
    wallet_id = Column(
        String(64), ForeignKey("wallets.wallet_id"), nullable=False, index=True
    )
    direction = Column(String(8), nullable=False)      # credit | debit
    status = Column(String(16), nullable=False)        # EntryStatus
    journal_kind = Column(String(32), nullable=False)  # JournalKind
    amount_minor = Column(BigInteger, nullable=False)
    currency = Column(String(8), nullable=False)

    # tracing back to hyperswitch objects
    payment_id = Column(String(64), nullable=True, index=True)
    payout_id = Column(String(64), nullable=True, index=True)
    connector = Column(String(64), nullable=True)
    memo = Column(Text, nullable=True)
    metadata_json = Column(JSONType, nullable=True)

    # idempotent ingestion — same key never posts twice
    idempotency_key = Column(String(128), nullable=True, unique=True)
    created_at_ms = Column(BigInteger, nullable=False, default=_now_ms)
    posted_at_ms = Column(BigInteger, nullable=True)


class PoolPayout(Base):
    __tablename__ = "pool_payouts"

    payout_id = Column(String(64), primary_key=True, default=lambda: _id("pp"))
    wallet_id = Column(
        String(64), ForeignKey("wallets.wallet_id"), nullable=False, index=True
    )
    connector = Column(String(64), nullable=False)
    connector_payout_id = Column(String(64), nullable=True)
    hyperswitch_payout_id = Column(String(64), nullable=True, index=True)
    amount_minor = Column(BigInteger, nullable=False)
    currency = Column(String(8), nullable=False)
    beneficiary = Column(JSONType, nullable=False)
    status = Column(String(32), nullable=False, default="requires_fulfillment")
    idempotency_key = Column(String(128), nullable=True, unique=True)
    error_code = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at_ms = Column(BigInteger, nullable=False, default=_now_ms)
    updated_at_ms = Column(BigInteger, nullable=False, default=_now_ms, onupdate=_now_ms)


Index("ix_ledger_wallet_journal", LedgerEntry.wallet_id, LedgerEntry.journal_id)
