"""Thin client for the Hyperswitch router API used to execute payouts.

The pool wallet never moves money itself — withdrawal execution is delegated
to Hyperswitch `POST /payouts`, which routes to the chosen payout connector
(Flip for Indonesian bank accounts by default).
"""
from typing import Any, Dict, Optional

import httpx

from .config import settings


class HyperswitchError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"hyperswitch error {status}: {body}")


def _headers(idempotency_key: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "api-key": settings.hyperswitch_api_key,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def create_payout(
    *,
    amount_minor: int,
    currency: str,
    beneficiary: Dict[str, Any],
    connector: str,
    idempotency_key: Optional[str],
    remark: Optional[str],
) -> Dict[str, Any]:
    """Create + confirm a bank payout via Hyperswitch.

    Indonesian local-bank payouts use the `bank_transfer/ach` payout method —
    Hyperswitch maps `bank_routing_number` to the Flip `bank_code`.
    """
    payload: Dict[str, Any] = {
        "amount": amount_minor,
        "currency": currency,
        "payout_type": "bank",
        "connector": [connector],
        "auto_fulfill": True,
        "confirm": True,
        "description": remark or "pool wallet withdrawal",
        "payout_method_data": {
            "bank_transfer": {
                "ach": {
                    "bank_account_number": beneficiary["account_number"],
                    "bank_routing_number": beneficiary["bank_code"],
                    "account_holder_name": beneficiary["account_holder_name"],
                    "bank_country_code": beneficiary.get("country_code", "ID"),
                    "bank_city": beneficiary.get("city"),
                }
            }
        },
    }
    if settings.hyperswitch_profile_id:
        payload["profile_id"] = settings.hyperswitch_profile_id

    resp = httpx.post(
        f"{settings.hyperswitch_base_url}/payouts",
        json=payload,
        headers=_headers(idempotency_key),
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise HyperswitchError(resp.status_code, resp.text)
    return resp.json()


def retrieve_payout(hyperswitch_payout_id: str) -> Dict[str, Any]:
    resp = httpx.get(
        f"{settings.hyperswitch_base_url}/payouts/{hyperswitch_payout_id}",
        headers=_headers(),
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise HyperswitchError(resp.status_code, resp.text)
    return resp.json()
