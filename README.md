# Orderly Copy Trading Bot (Telegram)

Telegram bot to copy signals on Orderly. Only group admins can publish signals; registered members can copy them with their own Orderly credentials.

## Requirements
- Python 3.10+
- Telegram bot token
- Orderly credentials per user

## Configuration
1. Create `.env` in the project root:
```env
TELEGRAM_BOT_TOKEN=tu_token
TELEGRAM_BOT_USERNAME=Orderly_copy_bot
MASTER_KEY=tu_fernet_key
ORDERLY_BASE_URL=https://api.orderly.org
DATABASE_PATH=orderly_copytrading.db
LOG_LEVEL=INFO
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

## Telegram usage
1. Add the bot to your group.
2. An admin sends a signal:
```
/signal PERP_ETH_USDC BUY MARKET 200 2200 1950
```
or
```
/signal PERP_ETH_USDC BUY LIMIT 200 2200 1950 2050
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

## Security notes
- Keys are stored encrypted in SQLite using `MASTER_KEY`.
- Register your keys only in a private chat.
