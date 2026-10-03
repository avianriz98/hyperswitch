"""Database engine/session wiring."""
import logging
import os
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

logger = logging.getLogger("pool_wallet.db")

_is_sqlite = settings.database_url.startswith("sqlite")


def ensure_database_exists() -> None:
    """Create the target database when POOL_WALLET_CREATE_DB=1.

    Only meaningful for Postgres deployments (docker-compose sets this so the
    service owns a dedicated `pool_wallet` database on the shared pg server).
    Runs before engine creation so a first-boot bring-up just works.
    """
    if _is_sqlite or os.getenv("POOL_WALLET_CREATE_DB") != "1":
        return
    url = urlparse(settings.database_url.replace("+psycopg2", ""))
    target_db = url.path.lstrip("/")
    admin_url = url._replace(path="/postgres").geturl()
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db"),
                {"db": target_db},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{target_db}"'))
                logger.info("created database %s", target_db)
    finally:
        admin.dispose()


ensure_database_exists()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    # sqlite needs this for multi-threaded FastAPI workers
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
