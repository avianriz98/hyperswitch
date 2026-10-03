"""Reconciliation: pull authoritative payout status from Hyperswitch.

Webhooks can be lost. ``POST /reconcile/payouts`` walks every pool payout
stuck in a non-terminal state, asks Hyperswitch for the real status and
settles the ledger accordingly.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import hyperswitch, ledger
from ..db import get_db
from ..models import PoolPayout

router = APIRouter(prefix="/reconcile", tags=["reconcile"])

NON_TERMINAL = ("pending", "requires_fulfillment", "initiated", "processing")


@router.post("/payouts")
def reconcile_payouts(db: Session = Depends(get_db)):
    stale = (
        db.query(PoolPayout)
        .filter(PoolPayout.status.in_(NON_TERMINAL))
        .filter(PoolPayout.hyperswitch_payout_id.is_not(None))
        .all()
    )
    results = {"checked": 0, "committed": 0, "released": 0, "unchanged": 0, "errors": 0}
    for payout in stale:
        results["checked"] += 1
        try:
            remote = hyperswitch.retrieve_payout(payout.hyperswitch_payout_id)
        except Exception:
            results["errors"] += 1
            continue
        status = remote.get("status")
        payout.connector_payout_id = remote.get(
            "connector_payout_id", payout.connector_payout_id
        )
        if status == "success":
            ledger.commit_payout(db, payout.payout_id)
            results["committed"] += 1
        elif status in ("failed", "cancelled"):
            ledger.release_payout(
                db,
                payout.payout_id,
                error_code=str(remote.get("error_code") or "reconcile"),
                error_message=str(remote.get("error_message") or "")[:500],
                final_status=status,
            )
            results["released"] += 1
        else:
            payout.status = status or payout.status
            results["unchanged"] += 1
    db.commit()
    return results
