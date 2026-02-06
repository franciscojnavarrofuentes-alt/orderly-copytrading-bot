import asyncio
import logging
import re
import time
import uuid
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.chart import SignalLevels, render_candlestick_chart, render_signal_chart
from app.orderly import OrderlyClient, OrderlyOrder
from app.storage import Storage

logger = logging.getLogger(__name__)

ACCOUNT_ID_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
ACCOUNT_ID_32B_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
ORDERLY_KEY_RE = re.compile(r"^ed25519:[A-Za-z0-9+/=]+$")


def _format_register_help() -> str:
    return (
        "Correct format:\n"
        "/register <ACCOUNT_ID> <ORDERLY_KEY> <ORDERLY_SECRET>\n"
        "Example:\n"
        "/register 0xabc... ed25519:AAAA... ed25519:BBBB...\n\n"
        "Notes:\n"
        "- ACCOUNT_ID must be a 0x... 40-hex address.\n"
        "- ORDERLY_KEY and ORDERLY_SECRET start with ed25519:"
    )


def _validate_register_args(account_id: str, orderly_key: str, orderly_secret: str) -> Optional[str]:
    if not (ACCOUNT_ID_RE.match(account_id) or ACCOUNT_ID_32B_RE.match(account_id)):
        return "Invalid ACCOUNT_ID. It must be 0x + 40 hex (address) or 0x + 64 hex (account id)."
    if not ORDERLY_KEY_RE.match(orderly_key):
        return "Invalid ORDERLY_KEY. It must start with ed25519: and be base64."
    if not ORDERLY_KEY_RE.match(orderly_secret):
        return "Invalid ORDERLY_SECRET. It must start with ed25519: and be base64."
    if account_id.startswith("0x000000") or len(account_id) < 42:
        return "ACCOUNT_ID looks incomplete."
    return None


def _parse_signal_args(args: list[str]) -> dict[str, float | str | None]:
    if len(args) < 6:
        raise ValueError(
            "Format: /signal <TICKER> <SIDE> <TYPE> <NOTIONAL_USD> <TP> <SL> [LIMIT_PRICE]"
        )

    symbol = _normalize_symbol(args[0])
    side = args[1].upper()
    order_type = args[2].upper()
    notional_usd = float(args[3])
    take_profit = float(args[4])
    stop_loss = float(args[5])
    limit_price: Optional[float] = None

    if order_type == "LIMIT":
        if len(args) < 7:
            raise ValueError("For LIMIT, include LIMIT_PRICE.")
        limit_price = float(args[6])

    if order_type not in {"MARKET", "LIMIT"}:
        raise ValueError("Invalid order TYPE. Use MARKET or LIMIT.")
    if side not in {"BUY", "SELL"}:
        raise ValueError("Invalid SIDE. Use BUY or SELL.")
    if notional_usd <= 0:
        raise ValueError("NOTIONAL_USD must be greater than 0.")

    return {
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "notional_usd": notional_usd,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "limit_price": limit_price,
    }


def _signal_to_message(signal: dict[str, float | str | None]) -> str:
    ticker = _extract_ticker(str(signal["symbol"]))
    side = str(signal["side"]).upper()
    side_icon = "🟢" if side == "BUY" else "🔴"
    limit_line = ""
    price_line = ""
    if signal["order_type"] == "LIMIT" and signal["limit_price"] is not None:
        limit_line = f"<b>LIMIT:</b> ${signal['limit_price']}\n"
        if signal.get("market_price") is not None:
            price_line = f"<b>Mark:</b> ${signal['market_price']}\n"
    title = f"{ticker} {side}"
    if signal["order_type"] == "MARKET":
        title = f"{ticker} - Market {side}"
    return (
        f"{side_icon} <b>{title}</b> {side_icon}\n\n"
        f"{limit_line}"
        f"{price_line}"
        f"<b>Size:</b> ${signal['notional_usd']}\n"
        f"<b>TP:</b> ${signal['take_profit']} | <b>SL:</b> ${signal['stop_loss']}\n\n"
        "<i>Press 'Copy' to proceed (TP/SL included).</i>"
    )


def _signalform_template() -> str:
    return (
        "Signalform template (multiline):\n"
        "/signalform\n"
        "SYMBOL=ETH\n"
        "SIDE=BUY\n"
        "TYPE=MARKET\n"
        "USD=200\n"
        "TP=2200\n"
        "SL=1950\n"
        "LIMIT=2050  # only if TYPE=LIMIT"
    )


def _parse_signalform(text: str) -> dict[str, float | str | None]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        raise ValueError("Incomplete format.\n" + _signalform_template())

    data: dict[str, str] = {}
    for line in lines[1:]:
        if "=" not in line:
            raise ValueError("Invalid line. Use KEY=VALUE.\n" + _signalform_template())
        key, value = line.split("=", 1)
        data[key.strip().upper()] = value.strip()

    symbol = data.get("SYMBOL")
    side = data.get("SIDE")
    order_type = data.get("TYPE")
    usd = data.get("USD")
    tp = data.get("TP")
    sl = data.get("SL")
    limit = data.get("LIMIT")

    if not all([symbol, side, order_type, usd, tp, sl]):
        raise ValueError("Missing required fields.\n" + _signalform_template())

    if order_type.upper() == "LIMIT" and not limit:
        raise ValueError("LIMIT is required when TYPE=LIMIT.\n" + _signalform_template())
    if order_type.upper() == "MARKET" and limit:
        raise ValueError("If TYPE=MARKET, LIMIT must be empty or omitted.\n" + _signalform_template())

    return {
        "symbol": _normalize_symbol(symbol),
        "side": side.upper(),
        "order_type": order_type.upper(),
        "notional_usd": float(usd),
        "take_profit": float(tp),
        "stop_loss": float(sl),
        "limit_price": float(limit) if limit else None,
    }


def _normalize_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not cleaned:
        return cleaned
    if cleaned.startswith("PERP_") or "_" in cleaned:
        return cleaned
    return f"PERP_{cleaned}_USDC"


def _extract_ticker(symbol: str) -> str:
    cleaned = symbol.upper()
    if cleaned.startswith("PERP_"):
        cleaned = cleaned[5:]
    return cleaned.split("_", 1)[0]


def _store_signal(context: ContextTypes.DEFAULT_TYPE, signal: dict[str, float | str | None]) -> str:
    signal_id = str(uuid.uuid4())[:8]
    context.bot_data.setdefault("signals", {})[signal_id] = signal
    return signal_id


async def _publish_signal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    signal: dict[str, float | str | None],
) -> None:
    price_ref = None
    client: OrderlyClient = context.bot_data["orderly_client"]
    if signal.get("order_type") == "MARKET":
        if signal.get("market_price") is None:
            try:
                market_price = await asyncio.to_thread(
                    client.get_mark_price, str(signal["symbol"])
                )
                signal["market_price"] = market_price
            except Exception as exc:  # noqa: BLE001
                signal["market_price"] = None
                await update.message.reply_text(
                    f"Could not fetch mark price for {signal['symbol']}: {exc}. "
                    "Signal was not sent."
                )
                return
        price_ref = float(signal["market_price"])
    else:
        price_ref = float(signal["limit_price"])
        if signal.get("market_price") is None:
            try:
                signal["market_price"] = await asyncio.to_thread(
                    client.get_mark_price, str(signal["symbol"])
                )
            except Exception:  # noqa: BLE001
                signal["market_price"] = None

    side = str(signal["side"]).upper()
    tp = float(signal["take_profit"])
    sl = float(signal["stop_loss"])
    if side == "BUY":
        if not (tp > price_ref and sl < price_ref):
            await update.message.reply_text(
                "Invalid TP/SL for BUY. TP must be above price and SL below price."
            )
            return
    elif side == "SELL":
        if not (tp < price_ref and sl > price_ref):
            await update.message.reply_text(
                "Invalid TP/SL for SELL. TP must be below price and SL above price."
            )
            return

    signal_id = _store_signal(context, signal)
    bot_username = context.bot_data["bot_username"]
    link = f"https://t.me/{bot_username}?start=copy_{signal_id}"
    button = InlineKeyboardButton("Copy", url=link)
    keyboard = InlineKeyboardMarkup([[button]])
    caption = _signal_to_message(signal)
    try:
        levels = SignalLevels(
            entry=price_ref,
            take_profit=tp,
            stop_loss=sl,
            current=signal.get("market_price") if signal["order_type"] == "LIMIT" else None,
        )
        title = f"{_extract_ticker(str(signal['symbol']))} {signal['side']}"
        chart_bytes = None
        try:
            rows = await asyncio.to_thread(
                context.bot_data["orderly_client"].get_tv_history,
                str(signal["symbol"]),
                "1h",
                100,
                int(time.time()),
            )
            timestamps = []
            candles = [
                {
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                }
                for r in rows
            ]
            timestamps = [int(r.get("ts")) for r in rows]
            chart_bytes = await asyncio.to_thread(
                render_candlestick_chart, candles, levels, title, timestamps
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Candlestick fetch/render failed: %s", exc)
            chart_bytes = None

        if chart_bytes is None:
            chart_bytes = await asyncio.to_thread(render_signal_chart, levels, title)
        await update.message.reply_photo(
            photo=chart_bytes,
            caption=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        await update.message.reply_text(
            caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )


async def _try_delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    try:
        await context.bot.delete_message(
            chat_id=update.message.chat_id,
            message_id=update.message.message_id,
        )
    except (Forbidden, BadRequest):
        # Lacking permissions or message already gone.
        return


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return False
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return False
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in {"administrator", "creator"}


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if context.args and context.args[0].startswith("copy_"):
        signal_id = context.args[0].split("_", 1)[1]
        signals = context.bot_data.get("signals", {})
        signal = signals.get(signal_id)
        if not signal:
            await update.message.reply_text("This signal is no longer available.")
            return

        ticker = _extract_ticker(str(signal["symbol"]))
        side = str(signal["side"]).upper()
        side_icon = "🟢" if side == "BUY" else "🔴"
        limit_line = ""
        price_line = ""
        if signal["order_type"] == "LIMIT" and signal.get("limit_price") is not None:
            limit_line = f"<b>LIMIT:</b> ${signal['limit_price']}\n"
            if signal.get("market_price") is not None:
                price_line = f"<b>Mark:</b> ${signal['market_price']}\n"
        title = f"{ticker} {side}"
        if signal["order_type"] == "MARKET":
            title = f"{ticker} - Market {side}"
        text = (
            "Signal received:\n"
            f"{side_icon} <b>{title}</b> {side_icon}\n\n"
            f"{limit_line}"
            f"{price_line}"
            f"<b>Size:</b> ${signal['notional_usd']}\n"
            f"<b>TP:</b> ${signal['take_profit']} | <b>SL:</b> ${signal['stop_loss']}\n\n"
            "<i>Press 'Copy' to proceed (TP/SL included).</i>"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Copiar cantidad sugerida",
                        callback_data=f"percent:{signal_id}:100",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Copiar 25%",
                        callback_data=f"percent:{signal_id}:25",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Copiar 50%",
                        callback_data=f"percent:{signal_id}:50",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Copiar 75%",
                        callback_data=f"percent:{signal_id}:75",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Otra cantidad ($)",
                        callback_data=f"custom:{signal_id}",
                    )
                ],
            ]
        )
        await update.message.reply_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    await update.message.reply_text("Hi! Use /help to see available commands.")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos:\n"
        "/start - saludo inicial\n"
        "/help - ver ayuda\n"
        "/register <ACCOUNT_ID> <ORDERLY_KEY> <ORDERLY_SECRET> (solo en privado)\n"
        "/follow - copy signals in the group\n"
        "/unfollow - stop copying signals\n"
        "/signal <TICKER> <SIDE> <TYPE> <NOTIONAL_USD> <TP> <SL> [LIMIT_PRICE] (solo admins)\n"
        "/signalform (plantilla multilinea, solo admins)\n"
        "/signalwizard - guided signal (admins only)\n"
        "/cancel - cancelar flujo guiado\n"
        "/copyusd <SIGNAL_ID> <USD> (solo en privado)\n"
        "/ping - comprobar que estoy vivo"
    )


async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong")


async def register_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type != ChatType.PRIVATE:
        await update.message.reply_text(
            "For security, register your keys in a private chat with me."
        )
        return

    if len(context.args) != 3:
        await update.message.reply_text(_format_register_help())
        return

    account_id, orderly_key, orderly_secret = context.args
    validation_error = _validate_register_args(account_id, orderly_key, orderly_secret)
    if validation_error:
        await update.message.reply_text(f"{validation_error}\n\n{_format_register_help()}")
        return

    storage: Storage = context.bot_data["storage"]
    storage.upsert_user(
        user_id=update.effective_user.id,
        orderly_account_id=account_id,
        orderly_key=orderly_key,
        orderly_secret=orderly_secret,
    )
    await update.message.reply_text("Keys saved. You can now use /follow in the group.")


async def follow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.message.reply_text("Use /follow inside the group.")
        return

    storage: Storage = context.bot_data["storage"]
    user_id = update.effective_user.id
    if not storage.get_user(user_id):
        await update.message.reply_text(
            "Please register first with /register in private."
        )
        return

    storage.set_following(chat.id, user_id, True)
    await update.message.reply_text("Done! You will copy the next signals.")


async def unfollow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.message.reply_text("Use /unfollow inside the group.")
        return

    storage: Storage = context.bot_data["storage"]
    user_id = update.effective_user.id
    storage.set_following(chat.id, user_id, False)
    await update.message.reply_text("You stopped copying signals.")


async def signal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.message.reply_text("Use /signal inside the group.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text(
            "Only group admins can send signals."
        )
        return

    try:
        signal = _parse_signal_args(context.args)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    await _publish_signal(update, context, signal)


async def signalform_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.message.reply_text("Use /signalform inside the group.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only group admins can create signals.")
        return

    text = update.message.text or ""
    if text.strip() == "/signalform":
        await update.message.reply_text(_signalform_template())
        return

    try:
        signal = _parse_signalform(text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    await _publish_signal(update, context, signal)
    await _try_delete_message(update, context)


def _get_wizard_key(update: Update) -> tuple[int, int]:
    return update.effective_chat.id, update.effective_user.id


async def signalwizard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.message.reply_text("Use /signalwizard inside the group.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only group admins can create signals.")
        return

    key = _get_wizard_key(update)
    context.bot_data.setdefault("signal_wizard", {})[key] = {
        "step": "symbol",
        "data": {},
    }
    await update.message.reply_text(
        "Signal wizard started.\n"
        "Step 1: type the SYMBOL (e.g., PERP_ETH_USDC)."
    )


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = _get_wizard_key(update)
    wizard = context.bot_data.get("signal_wizard", {})
    if key in wizard:
        wizard.pop(key, None)
        await update.message.reply_text("Wizard cancelled.")
    else:
        await update.message.reply_text("No active wizard.")


async def wizard_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    key = _get_wizard_key(update)
    wizard = context.bot_data.get("signal_wizard", {}).get(key)
    if not wizard:
        return

    step = wizard["step"]
    text = update.message.text.strip()

    if step == "symbol":
        wizard["data"]["symbol"] = text.upper()
        wizard["step"] = "side"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("BUY", callback_data="wiz:side:BUY"),
              InlineKeyboardButton("SELL", callback_data="wiz:side:SELL")]]
        )
        await update.message.reply_text("Step 2: choose SIDE.", reply_markup=keyboard)
        return

    if step == "notional":
        try:
            wizard["data"]["notional_usd"] = float(text)
        except ValueError:
            await update.message.reply_text("Invalid NOTIONAL_USD. Send a number.")
            return
        wizard["step"] = "tp"
        await update.message.reply_text("Step 4: enter TP (absolute price).")
        return

    if step == "tp":
        try:
            wizard["data"]["take_profit"] = float(text)
        except ValueError:
            await update.message.reply_text("Invalid TP. Send a number.")
            return
        wizard["step"] = "sl"
        await update.message.reply_text("Step 5: enter SL (absolute price).")
        return

    if step == "sl":
        try:
            wizard["data"]["stop_loss"] = float(text)
        except ValueError:
            await update.message.reply_text("Invalid SL. Send a number.")
            return
        if wizard["data"]["order_type"] == "LIMIT":
            wizard["step"] = "limit_price"
            await update.message.reply_text("Step 6: enter LIMIT_PRICE.")
        else:
            await _finalize_wizard(update, context, wizard["data"])
            context.bot_data["signal_wizard"].pop(key, None)
        return

    if step == "limit_price":
        try:
            wizard["data"]["limit_price"] = float(text)
        except ValueError:
            await update.message.reply_text("Invalid LIMIT_PRICE. Send a number.")
            return
        await _finalize_wizard(update, context, wizard["data"])
        context.bot_data["signal_wizard"].pop(key, None)


async def wizard_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    if not query.data.startswith("wiz:"):
        return

    await query.answer()
    _, action, value = query.data.split(":", 2)
    key = (query.message.chat_id, query.from_user.id)
    wizard = context.bot_data.get("signal_wizard", {}).get(key)
    if not wizard:
        await query.edit_message_text("Wizard not active.")
        return

    if action == "side":
        wizard["data"]["side"] = value
        wizard["step"] = "type"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("MARKET", callback_data="wiz:type:MARKET"),
              InlineKeyboardButton("LIMIT", callback_data="wiz:type:LIMIT")]]
        )
        await query.edit_message_text("Step 3: choose TYPE.", reply_markup=keyboard)
        return

    if action == "type":
        wizard["data"]["order_type"] = value
        wizard["data"]["limit_price"] = None
        wizard["step"] = "notional"
        await query.edit_message_text("Step 4: enter NOTIONAL_USD.")
        return


async def _finalize_wizard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: dict,
) -> None:
    signal = {
        "symbol": data["symbol"],
        "side": data["side"],
        "order_type": data["order_type"],
        "notional_usd": data["notional_usd"],
        "take_profit": data["take_profit"],
        "stop_loss": data["stop_loss"],
        "limit_price": data.get("limit_price"),
    }
    await _publish_signal(update, context, signal)


async def _execute_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order: OrderlyOrder,
    take_profit: float,
    stop_loss: float,
    confirmation_text: str,
) -> None:
    async def _send_reply(text: str) -> None:
        message = update.effective_message
        if message is not None:
            await message.reply_text(text)
            return
        user = update.effective_user
        if user is not None:
            await context.bot.send_message(chat_id=user.id, text=text)

    storage: Storage = context.bot_data["storage"]
    client: OrderlyClient = context.bot_data["orderly_client"]
    creds = storage.get_user(update.effective_user.id)
    if not creds:
        await _send_reply("Please register first with /register.")
        return

    try:
        if order.order_type == "LIMIT" and order.order_price is not None:
            try:
                mark_price = await asyncio.to_thread(
                    client.get_mark_price, order.symbol
                )
            except Exception:  # noqa: BLE001
                mark_price = None
            if mark_price is not None:
                if order.side == "BUY" and order.order_price > mark_price:
                    await _send_reply(
                        "Heads up: LIMIT price is above the mark price. "
                        "Your order will execute at market."
                    )
                elif order.side == "SELL" and order.order_price < mark_price:
                    await _send_reply(
                        "Heads up: LIMIT price is below the mark price. "
                        "Your order will execute at market."
                    )

        if order.order_type == "LIMIT":
            close_side = "SELL" if order.side == "BUY" else "BUY"
            algo_payload = {
                "symbol": order.symbol,
                "algo_type": "BRACKET",
                "type": "LIMIT",
                "price": order.order_price,
                "quantity": order.order_quantity,
                "side": order.side,
                "child_orders": [
                    {
                        "symbol": order.symbol,
                        "algo_type": "POSITIONAL_TP_SL",
                        "child_orders": [
                            {
                                "symbol": order.symbol,
                                "algo_type": "TAKE_PROFIT",
                                "side": close_side,
                                "type": "CLOSE_POSITION",
                                "trigger_price": take_profit,
                                "reduce_only": True,
                            },
                            {
                                "symbol": order.symbol,
                                "algo_type": "STOP_LOSS",
                                "side": close_side,
                                "type": "CLOSE_POSITION",
                                "trigger_price": stop_loss,
                                "reduce_only": True,
                            },
                        ],
                    }
                ],
            }
            await asyncio.to_thread(
                client.create_algo_order,
                creds.orderly_account_id,
                creds.orderly_key,
                creds.orderly_secret,
                algo_payload,
            )
        else:
            await asyncio.to_thread(
                client.create_order,
                creds.orderly_account_id,
                creds.orderly_key,
                creds.orderly_secret,
                order,
            )
            close_side = "SELL" if order.side == "BUY" else "BUY"
            algo_payload = {
                "symbol": order.symbol,
                "algo_type": "TP_SL",
                "quantity": order.order_quantity,
                "trigger_price_type": "MARK_PRICE",
                "child_orders": [
                    {
                        "algo_type": "TAKE_PROFIT",
                        "symbol": order.symbol,
                        "side": close_side,
                        "type": "MARKET",
                        "trigger_price": take_profit,
                        "reduce_only": True,
                    },
                    {
                        "algo_type": "STOP_LOSS",
                        "symbol": order.symbol,
                        "side": close_side,
                        "type": "MARKET",
                        "trigger_price": stop_loss,
                        "reduce_only": True,
                    },
                ],
            }
            await asyncio.to_thread(
                client.create_algo_order,
                creds.orderly_account_id,
                creds.orderly_key,
                creds.orderly_secret,
                algo_payload,
            )
    except Exception as exc:  # noqa: BLE001
        response = getattr(exc, "response", None)
        if response is not None:
            error_detail = f"HTTP {response.status_code}: {response.text}"
        else:
            error_detail = str(exc)
        await _send_reply(f"Copy failed: {error_detail}")
        return

    await _send_reply("Order sent successfully.\n" + confirmation_text)


async def copy_choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    await query.answer()
    parts = query.data.split(":")
    action = parts[0]
    signal_id = parts[1]
    signals = context.bot_data.get("signals", {})
    signal = signals.get(signal_id)
    if not signal:
        await query.edit_message_text("This signal is no longer available.")
        return

    if action == "custom":
        await query.message.reply_text(
            "Envía en este chat:\n"
            "/copyusd <USD>\n"
            "Ejemplo:\n"
            "/copyusd 50"
        )
        context.user_data["pending_signal_id"] = signal_id
        return

    if action == "percent":
        try:
            percent = float(parts[2])
        except (IndexError, ValueError):
            await query.edit_message_text("Porcentaje invalido.")
            return

        await query.edit_message_text("Calculating quantity...")
        await _copy_with_usd(
            update,
            context,
            signal,
            usd=signal["notional_usd"] * (percent / 100.0),
        )
        return


async def _copy_with_usd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    signal: dict,
    usd: float,
) -> None:
    if usd <= 0:
        await update.message.reply_text("USD must be greater than 0.")
        return

    client: OrderlyClient = context.bot_data["orderly_client"]
    try:
        rules = await asyncio.to_thread(client.get_order_rules, signal["symbol"])
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Could not fetch market rules: {exc}")
        return

    base_tick = float(rules.get("base_tick", 0) or 0)
    if base_tick <= 0:
        await update.message.reply_text("Invalid tick size for this symbol.")
        return

    if signal["order_type"] == "LIMIT":
        price_ref = float(signal["limit_price"])
        try:
            mark_price = await asyncio.to_thread(
                client.get_mark_price, signal["symbol"]
            )
        except Exception:  # noqa: BLE001
            mark_price = None
        if mark_price is not None:
            side = str(signal["side"]).upper()
            crosses = (
                side == "BUY" and price_ref > mark_price
            ) or (
                side == "SELL" and price_ref < mark_price
            )
            if crosses:
                price_tick = float(
                    rules.get("price_tick")
                    or rules.get("quote_tick")
                    or 0
                )
                adjusted = mark_price
                if price_tick > 0:
                    adjusted = client.round_price(adjusted, price_tick, side)
                signal["limit_price"] = adjusted
                price_ref = adjusted
                await update.message.reply_text(
                    "Heads up: LIMIT price crosses the mark price. "
                    f"Adjusted limit to ${adjusted} to avoid rejection."
                )
    else:
        price_ref = await asyncio.to_thread(
            client.get_mark_price, signal["symbol"]
        )

    if price_ref <= 0:
        await update.message.reply_text("Invalid reference price.")
        return

    order_quantity = usd / price_ref
    order_quantity = client.round_quantity(order_quantity, base_tick)
    if order_quantity <= 0:
        await update.message.reply_text(
            "Order size is too small for this symbol's tick."
        )
        return
    order = OrderlyOrder(
        symbol=signal["symbol"],
        side=signal["side"],
        order_type=signal["order_type"],
        order_quantity=order_quantity,
        order_price=signal.get("limit_price"),
    )
    confirmation = (
        "Resumen:\n"
        f"Symbol: {signal['symbol']}\n"
        f"Side: {signal['side']}\n"
        f"Type: {signal['order_type']}\n"
        f"USD: {usd}\n"
        f"Qty: {order_quantity}\n"
        f"Precio ref: {price_ref}\n"
        f"{'Limit: ' + str(signal['limit_price']) if signal['limit_price'] else ''}\n"
        f"TP: {signal['take_profit']} | SL: {signal['stop_loss']}"
    )
    await _execute_order(
        update,
        context,
        order,
        take_profit=signal["take_profit"],
        stop_loss=signal["stop_loss"],
        confirmation_text=confirmation,
    )


async def copy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Use /copyusd in a private chat with me.")
        return

    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /copyusd <USD>"
        )
        return

    if len(context.args) == 1:
        signal_id = context.user_data.get("pending_signal_id")
        usd_value = context.args[0]
        if not signal_id:
            await update.message.reply_text(
                "Please click 'Custom USD' first so I know which signal to use."
            )
            return
    else:
        signal_id = context.args[0]
        usd_value = context.args[1]

    signals = context.bot_data.get("signals", {})
    signal = signals.get(signal_id)
    if not signal:
        await update.message.reply_text("Signal not found or expired.")
        return

    try:
        usd = float(usd_value)
    except ValueError:
        await update.message.reply_text("Invalid USD amount.")
        return

    await _copy_with_usd(update, context, signal, usd)


def build_application(
    token: str, storage: Storage, orderly_client: OrderlyClient, bot_username: str
) -> Application:
    application = Application.builder().token(token).build()

    application.bot_data["storage"] = storage
    application.bot_data["orderly_client"] = orderly_client
    application.bot_data["bot_username"] = bot_username

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("ping", ping_handler))
    application.add_handler(CommandHandler("register", register_handler))
    application.add_handler(CommandHandler("follow", follow_handler))
    application.add_handler(CommandHandler("unfollow", unfollow_handler))
    application.add_handler(CommandHandler("signal", signal_handler))
    application.add_handler(CommandHandler("signalform", signalform_handler))
    application.add_handler(
        CallbackQueryHandler(copy_choice_handler, pattern="^(percent|custom):")
    )
    application.add_handler(CallbackQueryHandler(wizard_button_handler, pattern="^wiz:"))
    application.add_handler(CommandHandler("copyusd", copy_handler))
    application.add_handler(CommandHandler("signalwizard", signalwizard_handler))
    application.add_handler(CommandHandler("cancel", cancel_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_text_handler)
    )

    return application
