# Orderly Copy Trading Bot (Telegram + Discord)

Telegram bot to copy signals on Orderly. Only group admins can publish signals; registered members can copy them with their own Orderly credentials.

## Requirements
- Python 3.10+
- Telegram bot token
- Discord bot token (for Discord deployment)
- Orderly credentials per user

## Project hygiene
- `.env` is ignored by Git and should never be committed.
- Use `/signalform` for the simplest signal creation UX.

## Configuration
1. Create `.env` in the project root:
```env
TELEGRAM_BOT_TOKEN=tu_token
TELEGRAM_BOT_USERNAME=Orderly_copy_bot
DISCORD_BOT_TOKEN=your_discord_token
MASTER_KEY=tu_fernet_key
ORDERLY_BASE_URL=https://api.orderly.org
DATABASE_PATH=orderly_copytrading.db
LOG_LEVEL=INFO
LOG_FILE=
LOG_MAX_BYTES=5242880
LOG_BACKUP_COUNT=3
BACKUP_DIR=backups
BACKUP_KEEP_DAYS=7
```

2. Generate `MASTER_KEY`:
```bash
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

## Install dependencies
```bash
pip install -r requirements.txt
```

## Run
```bash
python -m app
```

## Run (Discord)
```bash
python -m app.discord_main
```

## Discord setup (quick)
1. Create a Discord application and bot.
2. Enable the **Message Content Intent** if you want to use message-based commands (not required for slash commands).
3. Invite the bot with these scopes:
   - `bot`
   - `applications.commands`
4. Recommended bot permissions:
   - Send Messages
   - Read Message History
   - Use Slash Commands
   - Manage Messages (optional, only if you want the bot to delete the command message)

Example invite URL format:
```
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=274877990912&scope=bot%20applications.commands
```

## Telegram usage
1. Add the bot to your group.
2. An admin sends a signal:
```
/signal ETH BUY MARKET 200 2200 1950
```
or
```
/signal ETH BUY LIMIT 200 2200 1950 2050
```
Or use the multiline template:
```
/signalform
SYMBOL=ETH
SIDE=BUY
TYPE=LIMIT
USD=200
TP=2200
SL=1950
LIMIT=2050
```
3. Each user registers their keys in private:
```
/register <ACCOUNT_ID> <ORDERLY_KEY> <ORDERLY_SECRET>
```
4. In the group, each user enables copy:
```
/follow
```
5. To copy, press the `Copy` button in the group.
   - The bot opens in private with the prefilled signal.
   - You can copy the suggested amount or a percentage.
   - For a custom amount, send `/copyusd <SIGNAL_ID> <USD>`.
6. Status:
```
/status
```

## Candlestick chart (Telegram)
The bot fetches public TradingView-style kline data to render the candlestick chart.

## Security notes
- Keys are stored encrypted in SQLite using `MASTER_KEY`.
- Register your keys only in a private chat.

## Maintenance (recommended)
### Create a daily DB backup (server)
```
0 3 * * * cd /root/orderly-copytrading-bot && /root/orderly-copytrading-bot/.venv/bin/python scripts/backup_db.py >/var/log/orderly_backup.log 2>&1
```

### Log rotation
Set `LOG_FILE` in `.env` to enable rotating logs on disk.
