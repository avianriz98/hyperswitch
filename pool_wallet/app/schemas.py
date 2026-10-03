"""Pydantic request/response schemas."""
from typing import Optional, Dict, Any, List

from pydantic import BaseModel, Field


class WalletBalanceResponse(BaseModel):
    wallet_id: str
    merchant_id: str
    currency: str
    available_balance: int
    pending_balance: int
    reserved_balance: int
    total_collected: int
    total_paid_out: int
    created_at_ms: int
    updated_at_ms: int


class ConnectorTotalsResponse(BaseModel):
    wallet_id: str
    totals_by_connector: Dict[str, Dict[str, int]]


class CreditRequest(BaseModel):
    merchant_id: str
    currency: str = "IDR"
    amount_minor: int = Field(gt=0)
    payment_id: str
    connector: Optional[str] = None
    idempotency_key: Optional[str] = None
    settle_immediately: bool = False
    memo: Optional[str] = None


class SettleRequest(BaseModel):
    payment_id: str


class ReverseRequest(BaseModel):
    payment_id: str
    amount_minor: Optional[int] = None
    reason: str = "reversal"


class Beneficiary(BaseModel):
    """Beneficiary bank account.

    `bank_code` is the Flip bank code (bca, bni, bri, mandiri, cimb, permata,
    gopay, dana, ovo, linkaja, shopeepay, ...). `account_holder_name` is used
    only for display — Flip validates the name server-side.
    """

    bank_code: str
    account_number: str
    account_holder_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    country_code: str = "ID"


class WithdrawRequest(BaseModel):
    amount_minor: int = Field(gt=0)
    beneficiary: Beneficiary
    connector: Optional[str] = None  # override routing
    idempotency_key: Optional[str] = None
    remark: Optional[str] = None


class WithdrawResponse(BaseModel):
    payout_id: str
    status: str
    connector: str
    amount_minor: int
    currency: str
    hyperswitch_payout_id: Optional[str] = None
    connector_payout_id: Optional[str] = None
    error: Optional[str] = None


class PayoutRecord(BaseModel):
    payout_id: str
    wallet_id: str
    connector: str
    connector_payout_id: Optional[str]
    hyperswitch_payout_id: Optional[str]
    amount_minor: int
    currency: str
    beneficiary: Dict[str, Any]
    status: str
    error_code: Optional[str]
    error_message: Optional[str]
    created_at_ms: int
    updated_at_ms: int


class LedgerEntryRecord(BaseModel):
    entry_id: str
    journal_id: str
    wallet_id: str
    direction: str
    status: str
    journal_kind: str
    amount_minor: int
    currency: str
    payment_id: Optional[str]
    payout_id: Optional[str]
    connector: Optional[str]
    memo: Optional[str]
    created_at_ms: int


class LedgerListResponse(BaseModel):
    wallet_id: str
    entries: List[LedgerEntryRecord]
    next_cursor: Optional[str] = None


class HyperswitchWebhook(BaseModel):
    """Subset of the Hyperswitch webhook envelope we act on."""

    event_type: str
    object_id: Optional[str] = None           # payment_id / payout_id
    connector: Optional[str] = None
    merchant_id: Optional[str] = None
    content: Optional[Dict[str, Any]] = None  # raw payload (payment/payout)
