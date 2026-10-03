"""Wallet endpoints: balance inspection, collection hooks, withdrawals."""
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import hyperswitch, ledger
from ..config import settings
from ..db import get_db
from ..models import LedgerEntry, PoolPayout, Wallet
from ..schemas import (
    ConnectorTotalsResponse,
    CreditRequest,
    LedgerListResponse,
    ReverseRequest,
    SettleRequest,
    WalletBalanceResponse,
    WithdrawRequest,
    WithdrawResponse,
)

router = APIRouter(prefix="/wallets", tags=["wallets"])


@router.get("/{wallet_id}", response_model=WalletBalanceResponse)
def get_wallet(wallet_id: str, db: Session = Depends(get_db)):
    try:
        return ledger.get_balances(db, wallet_id)
    except ledger.LedgerError as e:
        raise HTTPException(404, str(e))


@router.get(
    "/{wallet_id}/connectors", response_model=ConnectorTotalsResponse
)
def get_wallet_connector_totals(
    wallet_id: str, db: Session = Depends(get_db)
):
    """Accumulated credits/debits split by source connector/e-wallet."""
    return ConnectorTotalsResponse(
        wallet_id=wallet_id,
        totals_by_connector=ledger.get_by_connector_totals(db, wallet_id),
    )


@router.get("/{wallet_id}/entries", response_model=LedgerListResponse)
def list_entries(
    wallet_id: str,
    limit: int = 50,
    cursor: Optional[str] = None,
    db: Session = Depends(get_db),
):
    limit = min(max(limit, 1), 200)
    q = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.wallet_id == wallet_id)
        .order_by(LedgerEntry.created_at_ms.desc(), LedgerEntry.entry_id)
    )
    if cursor:
        q = q.filter(LedgerEntry.created_at_ms < int(cursor))
    rows = q.limit(limit + 1).all()
    entries = rows[:limit]
    next_cursor = (
        str(entries[-1].created_at_ms) if len(rows) > limit else None
    )
    return LedgerListResponse(
        wallet_id=wallet_id,
        entries=[
            {
                "entry_id": e.entry_id,
                "journal_id": e.journal_id,
                "wallet_id": e.wallet_id,
                "direction": e.direction,
                "status": e.status,
                "journal_kind": e.journal_kind,
                "amount_minor": e.amount_minor,
                "currency": e.currency,
                "payment_id": e.payment_id,
                "payout_id": e.payout_id,
                "connector": e.connector,
                "memo": e.memo,
                "created_at_ms": e.created_at_ms,
            }
            for e in entries
        ],
        next_cursor=next_cursor,
    )


@router.post("/credit")
def credit_collection(req: CreditRequest, db: Session = Depends(get_db)):
    """Credit the pool from a collected/captured payment (API-driven path)."""
    try:
        entry = ledger.credit_payment(
            db,
            merchant_id=req.merchant_id,
            currency=req.currency,
            amount_minor=req.amount_minor,
            payment_id=req.payment_id,
            connector=req.connector,
            idempotency_key=req.idempotency_key,
            auto_settle=req.settle_immediately,
            memo=req.memo,
        )
    except ledger.LedgerError as e:
        raise HTTPException(400, str(e))
    return {"entry_id": entry.entry_id, "status": entry.status}


@router.post("/settle")
def settle(req: SettleRequest, db: Session = Depends(get_db)):
    """Mark collected funds for a payment as settled -> available."""
    count, amount = ledger.settle_payment(db, req.payment_id)
    return {"settled_entries": count, "settled_amount_minor": amount}


@router.post("/reverse")
def reverse(req: ReverseRequest, db: Session = Depends(get_db)):
    """Refund/chargeback: claw back a previously posted collection."""
    amount = ledger.reverse_payment(
        db, req.payment_id, req.amount_minor, req.reason
    )
    return {"reversed_amount_minor": amount}


@router.post("/{wallet_id}/withdraw", response_model=WithdrawResponse)
def withdraw(
    wallet_id: str, req: WithdrawRequest, db: Session = Depends(get_db)
):
    """Reserve pool funds and dispatch a payout through Hyperswitch."""
    connector = req.connector or settings.payout_connector_routing.get(
        req_currency(db, wallet_id), settings.default_payout_connector
    )
    beneficiary = req.beneficiary.model_dump()

    # idempotent replay: same key returns the original payout record
    if req.idempotency_key:
        existing = (
            db.query(PoolPayout)
            .filter_by(idempotency_key=req.idempotency_key)
            .first()
        )
        if existing is not None:
            return _to_response(existing)

    try:
        payout, _entry = ledger.reserve_for_payout(
            db,
            wallet_id=wallet_id,
            amount_minor=req.amount_minor,
            connector=connector,
            beneficiary=beneficiary,
            idempotency_key=req.idempotency_key,
        )
    except ledger.InsufficientFunds as e:
        raise HTTPException(409, str(e))
    except ledger.LedgerError as e:
        raise HTTPException(404, str(e))
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(PoolPayout)
            .filter_by(idempotency_key=req.idempotency_key)
            .first()
        )
        if existing is not None:
            return _to_response(existing)
        raise HTTPException(409, "duplicate payout idempotency key")

    # Dispatch to Hyperswitch *after* the reservation commits — never hold
    # the wallet row lock across an outbound network call.
    try:
        resp = hyperswitch.create_payout(
            amount_minor=req.amount_minor,
            currency=payout.currency,
            beneficiary=beneficiary,
            connector=connector,
            idempotency_key=req.idempotency_key or payout.payout_id,
            remark=req.remark,
        )
        status = resp.get("status", "initiated")
        # persist provider ids first; terminal states are written by
        # commit/release so the ledger state transition stays consistent.
        payout = ledger.mark_payout_dispatched(
            db,
            payout.payout_id,
            hyperswitch_payout_id=resp.get("payout_id"),
            connector_payout_id=resp.get("connector_payout_id"),
            status=status
            if status not in ("success", "failed", "cancelled")
            else "initiated",
        )
        # synchronous terminal states returned inline
        if status == "success":
            ledger.commit_payout(db, payout.payout_id)
        elif status in ("failed", "cancelled"):
            ledger.release_payout(
                db,
                payout.payout_id,
                error_code=resp.get("error_code"),
                error_message=resp.get("error_message"),
                final_status=status,
            )
    except hyperswitch.HyperswitchError as e:
        ledger.release_payout(
            db,
            payout.payout_id,
            error_code=str(e.status),
            error_message=str(e.body)[:500],
        )
        raise HTTPException(502, f"payout dispatch failed: {e.body}")
    except httpx.TimeoutException:
        # payout may or may not have been created upstream — keep it
        # "pending" and let the reconcile worker resolve via retrieve.
        pass

    return _to_response(payout)


def req_currency(db: Session, wallet_id: str) -> str:
    w = db.query(Wallet).filter_by(wallet_id=wallet_id).first()
    return w.currency if w else "IDR"


def _to_response(payout) -> WithdrawResponse:
    return WithdrawResponse(
        payout_id=payout.payout_id,
        status=payout.status,
        connector=payout.connector,
        amount_minor=payout.amount_minor,
        currency=payout.currency,
        hyperswitch_payout_id=payout.hyperswitch_payout_id,
        connector_payout_id=payout.connector_payout_id,
        error=payout.error_message,
    )
