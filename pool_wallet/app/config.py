"""Pool Wallet configuration — all values via environment variables."""
import os
import json


class Settings:
    def __init__(self) -> None:
        # SQLAlchemy URL. Defaults to a local sqlite file so the service and
        # tests run with zero infrastructure; point it at Postgres in
        # production, e.g.:
        #   postgresql+psycopg2://user:pass@postgres:5432/pool_wallet
        self.database_url = os.getenv(
            "DATABASE_URL", "sqlite:///./pool_wallet.db"
        )

        # Shared secret guarding every API call made by merchants/operators.
        self.api_key = os.getenv("POOL_WALLET_API_KEY", "dev-key")

        # Hyperswitch backend connection for payout orchestration.
        self.hyperswitch_base_url = os.getenv(
            "HYPERSWITCH_BASE_URL", "http://hyperswitch-server:8080"
        ).rstrip("/")
        self.hyperswitch_api_key = os.getenv("HYPERSWITCH_API_KEY", "")
        self.hyperswitch_profile_id = os.getenv("HYPERSWITCH_PROFILE_ID", "")
        self.hyperswitch_merchant_id = os.getenv("HYPERSWITCH_MERCHANT_ID", "")

        # Payout processor routing: currency_code -> connector name.
        # Indonesian rupiah defaults to the Flip disbursement connector; every
        # other currency is routed through the configured fallback connector.
        self.payout_connector_routing = json.loads(
            os.getenv("PAYOUT_CONNECTOR_ROUTING", '{"IDR": "flip"}')
        )
        self.default_payout_connector = os.getenv(
            "DEFAULT_PAYOUT_CONNECTOR", "flip"
        )

        # When true, a successful connector payment response is treated as
        # settled cash immediately. Keep false for honest accounting — use the
        # /settle endpoint or a settlement webhook to move pending -> available.
        self.auto_settle_payments = (
            os.getenv("POOL_WALLET_AUTO_SETTLE", "false").lower() == "true"
        )

        # Optional HMAC verification secret for Hyperswitch webhooks.
        self.webhook_secret = os.getenv("POOL_WALLET_WEBHOOK_SECRET", "")


settings = Settings()
