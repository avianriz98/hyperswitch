"""Hyperswitch webhook ingestion.

Payments:
  payment_intent.succeeded  -> credit pool (pending or settled per config)
  payment_intent.failed     -> void pending credit for that payment
  refund / dispute events   -> reverse posted credits

Payouts:
  payout success            -> commit the reserved funds (pool is debited)
  payout failed/cancelled   -> release the reservation back to available

Configure the webhook endpoint on the Hyperswitch merchant profile pointing
to `<service>/webhooks/hyperswitch`.
"""
import hashlib
import hmac as hmac_mod
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .. import ledger
from ..config import settings
from ..db import get_db

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger("pool_wallet.webhooks")


def _verify_signature(body: bytes, signature: Optional[str]) -> None:
    """Optional HMAC-SHA256 check of the raw webhook body."""
    if not settings.webhook_secret:
        return
    if not signature:
        raise HTTPException(401, "missing webhook signature")
    expected = hmac_mod.new(
        settings.webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac_mod.compare_digest(expected, signature):
        raise HTTPException(401, "invalid webhook signature")


@router.post("/hyperswitch")
async def hyperswitch_webhook(
    request: Request,
    x_signature: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    raw = await request.body()
    _verify_signature(raw, x_signature)

    payload = await request.json()
    event_type = payload.get("event_type") or payload.get("event_class", "")
    object_id = payload.get("object_id") or payload.get("object_id_v2")
    connector = payload.get("connector")
    content = payload.get("content") or {}
    merchant_id = (
        payload.get("merchant_id") or content.get("merchant_id") or "default"
    )

    logger.info("webhook event=%s object=%s", event_type, object_id)

    if event_type in ("payment_intent.succeeded", "payment.succeeded"):
        _on_payment_succeeded(db, merchant_id, object_id, connector, content)
    elif event_type in ("payment_intent.failed", "payment.failed"):
        ledger.void_payment_pending(db, object_id)
    elif event_type in (
        "payment_intent.partially_refunded",
        "refund.succeeded",
        "dispute_opened",
        "dispute.lost",
    ):
        ledger.reverse_payment(db, object_id, reason=event_type)
    elif event_type in ("payout.success", "payout_succeeded", "payouts.success"):
        _on_payout_success(db, object_id, content)
    elif event_type in (
        "payout.failed",
        "payout_failed",
        "payout.cancelled",
        "payout_cancelled",
        "payouts.failed",
        "payouts.cancelled",
        "payout.reversed",
    ):
        _on_payout_terminal(db, object_id, content)

    return {"received": True, "event_type": event_type}


def _on_payment_succeeded(db, merchant_id, payment_id, connector, content):
    amount = (
        content.get("amount")
        or content.get("net_amount")
        or content.get("amount_capturable")
    )
    currency = content.get("currency", "IDR")
    if amount is None or payment_id is None:
        return
    ledger.credit_payment(
        db,
        merchant_id=merchant_id,
        currency=currency,
        amount_minor=int(amount),
        payment_id=payment_id,
        connector=connector,
        idempotency_key=f"payment:{payment_id}:collect",
        auto_settle=settings.auto_settle_payments,
        memo="payment collected via hyperswitch",
    )


def _on_payout_success(db, object_id, content):
    target = _find_payout(db, object_id, content)
    if target is not None:
        ledger.commit_payout(db, target.payout_id)


def _on_payout_terminal(db, object_id, content):
    target = _find_payout(db, object_id, content)
    if target is not None:
        ledger.release_payout(
            db,
            target.payout_id,
            error_code=str(content.get("error_code") or "provider"),
            error_message=str(
                content.get("error_message") or "payout failed"
            )[:500],
            final_status="failed",
        )


def _find_payout(db, object_id, content):
    """Locate the pool payout by hyperswitch payout_id / connector id / our id."""
    from ..models import PoolPayout

    hs_id = object_id or content.get("payout_id")
    conn_id = content.get("connector_payout_id")

    if hs_id:
        p = (
            db.query(PoolPayout)
            .filter(PoolPayout.hyperswitch_payout_id == hs_id)
            .first()
        )
        if p:
            return p
        p = db.query(PoolPayout).filter_by(payout_id=hs_id).first()
        if p:
            return p
    if conn_id:
        return (
            db.query(PoolPayout)
            .filter_by(connector_payout_id=conn_id)
            .first()
        )
    return None
