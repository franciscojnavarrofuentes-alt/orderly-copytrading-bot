import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO")
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = os.getenv("LOG_FILE")
    if log_file:
        from logging.handlers import RotatingFileHandler

        max_bytes = int(os.getenv("LOG_MAX_BYTES", "5242880"))
        backup_count = int(os.getenv("LOG_BACKUP_COUNT", "3"))
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )
        )
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_username: str
    orderly_base_url: str
    database_path: str
    master_key: str
    market_data_account_id: str | None
    market_data_key: str | None
    market_data_secret: str | None


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
    market_data_account_id = os.getenv("MARKET_DATA_ACCOUNT_ID")
    market_data_key = os.getenv("MARKET_DATA_KEY")
    market_data_secret = os.getenv("MARKET_DATA_SECRET")

    return Settings(
        telegram_token=telegram_token,
        telegram_username=telegram_username,
        orderly_base_url=orderly_base_url,
        database_path=database_path,
        master_key=master_key,
        market_data_account_id=market_data_account_id,
        market_data_key=market_data_key,
        market_data_secret=market_data_secret,
    )


@dataclass(frozen=True)
class DiscordSettings:
    discord_token: str
    orderly_base_url: str
    database_path: str
    master_key: str
    dev_guild_id: int | None


def load_discord_settings() -> DiscordSettings:
    load_dotenv()
    _configure_logging()

    discord_token = os.getenv("DISCORD_BOT_TOKEN")
    if not discord_token:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")
    dev_guild_id = os.getenv("DISCORD_DEV_GUILD_ID")

    master_key = os.getenv("MASTER_KEY")
    if not master_key:
        raise RuntimeError("Missing MASTER_KEY in environment.")

    orderly_base_url = os.getenv(
        "ORDERLY_BASE_URL", "https://api.orderly.org"
    )
    database_path = os.getenv("DATABASE_PATH", "orderly_copytrading.db")

    return DiscordSettings(
        discord_token=discord_token,
        orderly_base_url=orderly_base_url,
        database_path=database_path,
        master_key=master_key,
        dev_guild_id=int(dev_guild_id) if dev_guild_id else None,
    )
