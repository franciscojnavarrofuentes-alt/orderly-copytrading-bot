import logging

from app.discord_bot import OrderlyDiscordBot, register_discord_commands
from app.orderly import OrderlyClient
from app.settings import load_discord_settings
from app.storage import Storage


def main() -> None:
    settings = load_discord_settings()
    storage = Storage(settings.database_path, settings.master_key)
    storage.init_db()

    orderly_client = OrderlyClient(settings.orderly_base_url)
    bot = OrderlyDiscordBot(
        storage=storage,
        orderly_client=orderly_client,
        dev_guild_id=settings.dev_guild_id,
    )
    register_discord_commands(bot)

    logging.getLogger(__name__).info("Starting Discord bot")
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
