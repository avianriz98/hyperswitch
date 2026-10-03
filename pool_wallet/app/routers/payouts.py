"""Payout listing / inspection endpoints."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PoolPayout
from ..schemas import PayoutRecord

router = APIRouter(prefix="/payouts", tags=["payouts"])


@router.get("", response_model=list)
def list_payouts(
    wallet_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    limit = min(max(limit, 1), 200)
    q = db.query(PoolPayout).order_by(PoolPayout.created_at_ms.desc())
    if wallet_id:
        q = q.filter(PoolPayout.wallet_id == wallet_id)
    if status:
        q = q.filter(PoolPayout.status == status)
    return [_serialize(p) for p in q.limit(limit).all()]


@router.get("/{payout_id}", response_model=PayoutRecord)
def get_payout(payout_id: str, db: Session = Depends(get_db)):
    payout = db.query(PoolPayout).filter_by(payout_id=payout_id).first()
    if payout is None:
        raise HTTPException(404, "payout not found")
    return _serialize(payout)


def _serialize(p: PoolPayout) -> dict:
    return {
        "payout_id": p.payout_id,
        "wallet_id": p.wallet_id,
        "connector": p.connector,
        "connector_payout_id": p.connector_payout_id,
        "hyperswitch_payout_id": p.hyperswitch_payout_id,
        "amount_minor": p.amount_minor,
        "currency": p.currency,
        "beneficiary": p.beneficiary,
        "status": p.status,
        "error_code": p.error_code,
        "error_message": p.error_message,
        "created_at_ms": p.created_at_ms,
        "updated_at_ms": p.updated_at_ms,
    }
