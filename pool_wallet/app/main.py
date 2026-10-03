"""Pool Wallet — collection ledger + payout orchestration companion for
Hyperswitch.

Run:  uvicorn pool_wallet.app.main:app --host 0.0.0.0 --port 8090
"""
import logging

from fastapi import Depends, FastAPI, Header, HTTPException

from .config import settings
from .db import engine
from .models import Base
from .routers import payouts, reconcile, wallets, webhooks

logging.basicConfig(level=logging.INFO)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Hyperswitch Pool Wallet",
    version="0.1.0",
    description=(
        "Aggregates funds collected through Hyperswitch payment methods "
        "(cards, e-wallets, custom connectors) into a single ledgered pool "
        "and disburses them through Hyperswitch payout connectors (Flip for "
        "Indonesian banks by default)."
    ),
)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != settings.api_key:
        raise HTTPException(401, "invalid api key")


@app.get("/health")
def health():
    return {"status": "ok", "service": "pool-wallet"}


# webhooks are unauthenticated (connector-signed or network-restricted);
# everything else requires the operator API key.
app.include_router(webhooks.router)
app.include_router(
    wallets.router, dependencies=[Depends(require_api_key)]
)
app.include_router(
    payouts.router, dependencies=[Depends(require_api_key)]
)
app.include_router(
    reconcile.router, dependencies=[Depends(require_api_key)]
)
