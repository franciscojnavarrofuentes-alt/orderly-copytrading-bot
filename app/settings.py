import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_username: str
    orderly_base_url: str
    database_path: str
    master_key: str


def load_settings() -> Settings:
    load_dotenv()
    _configure_logging()

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in environment.")

    telegram_username = os.getenv("TELEGRAM_BOT_USERNAME")
    if not telegram_username:
        raise RuntimeError("Missing TELEGRAM_BOT_USERNAME in environment.")

    master_key = os.getenv("MASTER_KEY")
    if not master_key:
        raise RuntimeError("Missing MASTER_KEY in environment.")

    orderly_base_url = os.getenv(
        "ORDERLY_BASE_URL", "https://api.orderly.org"
    )
    database_path = os.getenv("DATABASE_PATH", "orderly_copytrading.db")

    return Settings(
        telegram_token=telegram_token,
        telegram_username=telegram_username,
        orderly_base_url=orderly_base_url,
        database_path=database_path,
        master_key=master_key,
    )
