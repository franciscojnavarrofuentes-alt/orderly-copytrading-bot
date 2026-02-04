import logging

from app.bot import build_application
from app.orderly import OrderlyClient
from app.settings import load_settings
from app.storage import Storage


def main() -> None:
    settings = load_settings()
    storage = Storage(settings.database_path, settings.master_key)
    storage.init_db()

    orderly_client = OrderlyClient(settings.orderly_base_url)
    application = build_application(
        settings.telegram_token,
        storage=storage,
        orderly_client=orderly_client,
        bot_username=settings.telegram_username,
    )

    logging.getLogger(__name__).info("Starting bot in polling mode")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
