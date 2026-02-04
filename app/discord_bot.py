import asyncio
import logging
import uuid
from typing import Optional

import discord
from discord import app_commands

from app.orderly import OrderlyClient, OrderlyOrder
from app.storage import Storage

logger = logging.getLogger(__name__)


def _extract_ticker(symbol: str) -> str:
    cleaned = symbol.upper()
    if cleaned.startswith("PERP_"):
        cleaned = cleaned[5:]
    return cleaned.split("_", 1)[0]


def _format_signal_message(signal: dict) -> str:
    ticker = _extract_ticker(str(signal["symbol"]))
    side = str(signal["side"]).upper()
    side_icon = "🟢" if side == "BUY" else "🔴"
    title = f"{ticker} {side}"
    if signal["order_type"] == "MARKET":
        title = f"{ticker} - Market {side}"

    limit_line = ""
    if signal["order_type"] == "LIMIT" and signal.get("limit_price") is not None:
        limit_line = f"**LIMIT:** ${signal['limit_price']}\n"

    price_line = ""
    if signal["order_type"] == "MARKET" and signal.get("market_price") is not None:
        price_line = f"**Price:** ${signal['market_price']}\n"

    return (
        f"{side_icon} **{title}** {side_icon}\n\n"
        f"{limit_line}"
        f"{price_line}"
        f"**Size:** ${signal['notional_usd']}\n"
        f"**TP:** ${signal['take_profit']} | **SL:** ${signal['stop_loss']}\n\n"
        "*Press 'Copy' to proceed (TP/SL included).*"
    )


def _parse_signalform(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Empty template.")

    data: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            raise ValueError("Invalid line. Use KEY=VALUE.")
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
        raise ValueError("Missing required fields.")
    if order_type.upper() == "LIMIT" and not limit:
        raise ValueError("LIMIT is required when TYPE=LIMIT.")
    if order_type.upper() == "MARKET" and limit:
        raise ValueError("If TYPE=MARKET, LIMIT must be empty or omitted.")

    return {
        "symbol": symbol.upper(),
        "side": side.upper(),
        "order_type": order_type.upper(),
        "notional_usd": float(usd),
        "take_profit": float(tp),
        "stop_loss": float(sl),
        "limit_price": float(limit) if limit else None,
        "market_price": None,
    }


class CopyAmountModal(discord.ui.Modal):
    def __init__(self, bot: "OrderlyDiscordBot", signal_id: str) -> None:
        super().__init__(title="Custom USD Amount")
        self.bot = bot
        self.signal_id = signal_id
        self.usd = discord.ui.TextInput(label="USD amount", placeholder="e.g. 100")
        self.add_item(self.usd)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            usd = float(str(self.usd.value))
        except ValueError:
            await interaction.response.send_message(
                "Invalid USD amount.", ephemeral=True
            )
            return

        await self.bot.copy_with_usd(interaction, self.signal_id, usd)


class CopyOptionsView(discord.ui.View):
    def __init__(self, bot: "OrderlyDiscordBot", signal_id: str) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.signal_id = signal_id

    @discord.ui.button(label="Copy suggested", style=discord.ButtonStyle.primary)
    async def copy_100(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.bot.copy_percent(interaction, self.signal_id, 100)

    @discord.ui.button(label="Copy 25%", style=discord.ButtonStyle.secondary)
    async def copy_25(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.bot.copy_percent(interaction, self.signal_id, 25)

    @discord.ui.button(label="Copy 50%", style=discord.ButtonStyle.secondary)
    async def copy_50(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.bot.copy_percent(interaction, self.signal_id, 50)

    @discord.ui.button(label="Copy 75%", style=discord.ButtonStyle.secondary)
    async def copy_75(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.bot.copy_percent(interaction, self.signal_id, 75)

    @discord.ui.button(label="Custom USD", style=discord.ButtonStyle.success)
    async def copy_custom(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CopyAmountModal(self.bot, self.signal_id))


class CopyButtonView(discord.ui.View):
    def __init__(self, bot: "OrderlyDiscordBot", signal_id: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.signal_id = signal_id

    @discord.ui.button(label="Copy", style=discord.ButtonStyle.primary)
    async def copy_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.bot.send_copy_options(interaction, self.signal_id)


class OrderlyDiscordBot(discord.Client):
    def __init__(self, storage: Storage, orderly_client: OrderlyClient) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.storage = storage
        self.orderly_client = orderly_client
        self.signals: dict[str, dict] = {}

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def on_ready(self) -> None:
        logger.info("Discord bot logged in as %s", self.user)

    async def send_copy_options(self, interaction: discord.Interaction, signal_id: str) -> None:
        signal = self.signals.get(signal_id)
        if not signal:
            await interaction.response.send_message(
                "Signal not found or expired.", ephemeral=True
            )
            return

        if not self.storage.get_user(interaction.user.id):
            await interaction.response.send_message(
                "Please register first with /register in DM.",
                ephemeral=True,
            )
            return

        try:
            await interaction.user.send(
                _format_signal_message(signal),
                view=CopyOptionsView(self, signal_id),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot send you DMs. Please enable DMs and try again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Check your DMs to copy this signal.",
            ephemeral=True,
        )

    async def copy_percent(self, interaction: discord.Interaction, signal_id: str, percent: float) -> None:
        signal = self.signals.get(signal_id)
        if not signal:
            await interaction.response.send_message(
                "Signal not found or expired.", ephemeral=True
            )
            return
        usd = float(signal["notional_usd"]) * (percent / 100.0)
        await self.copy_with_usd(interaction, signal_id, usd)

    async def copy_with_usd(self, interaction: discord.Interaction, signal_id: str, usd: float) -> None:
        signal = self.signals.get(signal_id)
        if not signal:
            await interaction.response.send_message(
                "Signal not found or expired.", ephemeral=True
            )
            return

        if usd <= 0:
            await interaction.response.send_message(
                "USD must be greater than 0.", ephemeral=True
            )
            return

        creds = self.storage.get_user(interaction.user.id)
        if not creds:
            await interaction.response.send_message(
                "Please register first with /register in DM.",
                ephemeral=True,
            )
            return

        try:
            rules = await asyncio.to_thread(
                self.orderly_client.get_order_rules, signal["symbol"]
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.response.send_message(
                f"Could not fetch market rules: {exc}", ephemeral=True
            )
            return

        base_tick = float(rules.get("base_tick", 0) or 0)
        if base_tick <= 0:
            await interaction.response.send_message(
                "Invalid tick size for this symbol.", ephemeral=True
            )
            return

        if signal["order_type"] == "LIMIT":
            price_ref = float(signal["limit_price"])
        else:
            price_ref = await asyncio.to_thread(
                self.orderly_client.get_mark_price, signal["symbol"]
            )

        if price_ref <= 0:
            await interaction.response.send_message(
                "Invalid reference price.", ephemeral=True
            )
            return

        order_quantity = usd / price_ref
        order_quantity = self.orderly_client.round_quantity(order_quantity, base_tick)
        if order_quantity <= 0:
            await interaction.response.send_message(
                "Order size is too small for this symbol's tick.",
                ephemeral=True,
            )
            return

        order = OrderlyOrder(
            symbol=signal["symbol"],
            side=signal["side"],
            order_type=signal["order_type"],
            order_quantity=order_quantity,
            order_price=signal.get("limit_price"),
        )

        try:
            if order.order_type == "LIMIT":
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
                                    "side": "SELL" if order.side == "BUY" else "BUY",
                                    "type": "CLOSE_POSITION",
                                    "trigger_price": signal["take_profit"],
                                    "reduce_only": True,
                                },
                                {
                                    "symbol": order.symbol,
                                    "algo_type": "STOP_LOSS",
                                    "side": "SELL" if order.side == "BUY" else "BUY",
                                    "type": "CLOSE_POSITION",
                                    "trigger_price": signal["stop_loss"],
                                    "reduce_only": True,
                                },
                            ],
                        }
                    ],
                }
                await asyncio.to_thread(
                    self.orderly_client.create_algo_order,
                    creds.orderly_account_id,
                    creds.orderly_key,
                    creds.orderly_secret,
                    algo_payload,
                )
            else:
                await asyncio.to_thread(
                    self.orderly_client.create_order,
                    creds.orderly_account_id,
                    creds.orderly_key,
                    creds.orderly_secret,
                    order,
                )
                algo_payload = {
                    "symbol": order.symbol,
                    "algo_type": "TP_SL",
                    "quantity": order.order_quantity,
                    "trigger_price_type": "MARK_PRICE",
                    "child_orders": [
                        {
                            "algo_type": "TAKE_PROFIT",
                            "symbol": order.symbol,
                            "side": "SELL" if order.side == "BUY" else "BUY",
                            "type": "MARKET",
                            "trigger_price": signal["take_profit"],
                            "reduce_only": True,
                        },
                        {
                            "algo_type": "STOP_LOSS",
                            "symbol": order.symbol,
                            "side": "SELL" if order.side == "BUY" else "BUY",
                            "type": "MARKET",
                            "trigger_price": signal["stop_loss"],
                            "reduce_only": True,
                        },
                    ],
                }
                await asyncio.to_thread(
                    self.orderly_client.create_algo_order,
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
            await interaction.response.send_message(
                f"Copy failed: {error_detail}", ephemeral=True
            )
            return

        confirmation = (
            "Order sent successfully.\n"
            f"Symbol: {signal['symbol']}\n"
            f"Side: {signal['side']}\n"
            f"Type: {signal['order_type']}\n"
            f"USD: {usd}\n"
            f"Qty: {order_quantity}\n"
            f"Price ref: {price_ref}\n"
            f"TP: {signal['take_profit']} | SL: {signal['stop_loss']}"
        )
        await interaction.response.send_message(confirmation, ephemeral=True)


def register_discord_commands(bot: OrderlyDiscordBot) -> None:
    @bot.tree.command(name="help", description="Show available commands")
    async def help_cmd(interaction: discord.Interaction) -> None:
        message = (
            "Commands:\n"
            "/register (DM only) - save your Orderly keys\n"
            "/signalform - create a signal (admins only)\n"
            "/ping - check bot status\n\n"
            "Signalform template:\n"
            "SYMBOL=PERP_ETH_USDC\n"
            "SIDE=BUY\n"
            "TYPE=MARKET\n"
            "USD=400\n"
            "TP=2200\n"
            "SL=1950\n"
            "LIMIT=2050  (only if TYPE=LIMIT)"
        )
        await interaction.response.send_message(message, ephemeral=True)

    @bot.tree.command(name="ping", description="Check if the bot is alive")
    async def ping(interaction: discord.Interaction) -> None:
        await interaction.response.send_message("pong", ephemeral=True)

    @bot.tree.command(name="register", description="Register your Orderly credentials (DM only)")
    async def register(
        interaction: discord.Interaction,
        account_id: str,
        orderly_key: str,
        orderly_secret: str,
    ) -> None:
        if interaction.guild is not None:
            await interaction.response.send_message(
                "Please use /register in a DM with me.",
                ephemeral=True,
            )
            return

        bot.storage.upsert_user(
            user_id=interaction.user.id,
            orderly_account_id=account_id,
            orderly_key=orderly_key,
            orderly_secret=orderly_secret,
        )
        await interaction.response.send_message(
            "Keys saved. You can now copy signals in the server."
        )

    @bot.tree.command(name="signalform", description="Create a signal using the multiline template")
    async def signalform(interaction: discord.Interaction, template: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use /signalform inside a server.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server admins can create signals.",
                ephemeral=True,
            )
            return

        try:
            signal = _parse_signalform(template)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        price_ref: Optional[float] = None
        if signal["order_type"] == "MARKET":
            try:
                price_ref = await asyncio.to_thread(
                    bot.orderly_client.get_mark_price, signal["symbol"]
                )
                signal["market_price"] = price_ref
            except Exception as exc:  # noqa: BLE001
                await interaction.response.send_message(
                    f"Could not fetch mark price: {exc}. Signal was not sent.",
                    ephemeral=True,
                )
                return
        else:
            price_ref = float(signal["limit_price"])

        tp = float(signal["take_profit"])
        sl = float(signal["stop_loss"])
        side = str(signal["side"]).upper()
        if side == "BUY":
            if not (tp > price_ref and sl < price_ref):
                await interaction.response.send_message(
                    "Invalid TP/SL for BUY. TP must be above price and SL below price.",
                    ephemeral=True,
                )
                return
        elif side == "SELL":
            if not (tp < price_ref and sl > price_ref):
                await interaction.response.send_message(
                    "Invalid TP/SL for SELL. TP must be below price and SL above price.",
                    ephemeral=True,
                )
                return

        signal_id = str(uuid.uuid4())[:8]
        bot.signals[signal_id] = signal
        await interaction.response.send_message(
            _format_signal_message(signal),
            view=CopyButtonView(bot, signal_id),
        )
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
