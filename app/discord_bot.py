import asyncio
import io
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands

from app.chart import SignalLevels, render_candlestick_chart, render_signal_chart
from app.orderly import OrderlyClient, OrderlyOrder
from app.storage import Storage

logger = logging.getLogger(__name__)


def _extract_ticker(symbol: str) -> str:
    cleaned = symbol.upper()
    if cleaned.startswith("PERP_"):
        cleaned = cleaned[5:]
    return cleaned.split("_", 1)[0]


def _normalize_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not cleaned:
        return cleaned
    if cleaned.startswith("PERP_") or "_" in cleaned:
        return cleaned
    return f"PERP_{cleaned}_USDC"


def _format_signal_message(signal: dict) -> str:
    ticker = _extract_ticker(str(signal["symbol"]))
    side = str(signal["side"]).upper()
    side_icon = "🟢" if side == "BUY" else "🔴"
    title = f"{ticker} {side}"
    if signal["order_type"] == "MARKET":
        title = f"{ticker} - Market {side}"

    limit_line = ""
    mark_line = ""
    if signal["order_type"] == "LIMIT" and signal.get("limit_price") is not None:
        limit_line = f"**LIMIT:** ${signal['limit_price']}\n"
        if signal.get("market_price") is not None:
            mark_line = f"**Mark:** ${signal['market_price']}\n"

    return (
        f"{side_icon} **{title}** {side_icon}\n\n"
        f"{limit_line}"
        f"{mark_line}"
        f"**Size:** ${signal['notional_usd']}\n"
        f"**TP:** ${signal['take_profit']} | **SL:** ${signal['stop_loss']}\n\n"
        "*Press 'Copy' to proceed (TP/SL included).*"
    )


def _parse_signalform(text: str) -> dict:
    raw = text.strip()
    if not raw:
        raise ValueError("Empty template.")

    data: dict[str, str] = {}
    parts = [p for p in raw.replace("\n", " ").replace(",", " ").split(" ") if p.strip()]
    for part in parts:
        if "=" not in part:
            raise ValueError(
                "Invalid format. Use KEY=VALUE separated by spaces."
            )
        key, value = part.split("=", 1)
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
    def __init__(self, bot: "OrderlyDiscordBot", signal_id: str, signal: dict) -> None:
        super().__init__(title="Custom Order")
        self.bot = bot
        self.signal_id = signal_id
        self.signal = signal
        self.usd = discord.ui.TextInput(
            label="USD size",
            placeholder="e.g. 100",
            default=str(signal.get("notional_usd", "")),
        )
        self.limit = discord.ui.TextInput(
            label="Limit price (optional)",
            placeholder="e.g. 2050",
            required=False,
            default=str(signal.get("limit_price") or ""),
        )
        self.tp = discord.ui.TextInput(
            label="Take Profit",
            placeholder="e.g. 2200",
            default=str(signal.get("take_profit", "")),
        )
        self.sl = discord.ui.TextInput(
            label="Stop Loss",
            placeholder="e.g. 1950",
            default=str(signal.get("stop_loss", "")),
        )
        self.add_item(self.usd)
        self.add_item(self.limit)
        self.add_item(self.tp)
        self.add_item(self.sl)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            usd = float(str(self.usd.value))
            tp = float(str(self.tp.value))
            sl = float(str(self.sl.value))
        except ValueError:
            await interaction.response.send_message(
                "Invalid number in one of the fields.", ephemeral=True
            )
            return
        limit_val = None
        if str(self.limit.value).strip():
            try:
                limit_val = float(str(self.limit.value))
            except ValueError:
                await interaction.response.send_message(
                    "Invalid limit price.", ephemeral=True
                )
                return

        await self.bot.copy_with_custom(
            interaction,
            self.signal_id,
            usd,
            limit_val=limit_val,
            tp=tp,
            sl=sl,
        )


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

    @discord.ui.button(label="Custom", style=discord.ButtonStyle.success)
    async def copy_custom(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        signal = self.bot.signals.get(self.signal_id)
        if not signal:
            await interaction.response.send_message(
                "Signal not found or expired.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            CopyAmountModal(self.bot, self.signal_id, signal)
        )


class CopyButtonView(discord.ui.View):
    def __init__(self, bot: "OrderlyDiscordBot", signal_id: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.signal_id = signal_id

    @discord.ui.button(label="Copy", style=discord.ButtonStyle.primary)
    async def copy_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.bot.send_copy_options(interaction, self.signal_id)



class OrderlyDiscordBot(discord.Client):
    def __init__(
        self,
        storage: Storage,
        orderly_client: OrderlyClient,
        dev_guild_id: int | None = None,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.storage = storage
        self.orderly_client = orderly_client
        self.signals: dict[str, dict] = {}
        self.dev_guild_id = dev_guild_id

    async def setup_hook(self) -> None:
        if self.dev_guild_id:
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        logger.info("Discord bot logged in as %s", self.user)

    def user_can_signal(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        allowed_roles = self.storage.get_allowed_role_ids(interaction.guild.id)
        if not allowed_roles:
            return False
        user_role_ids = {role.id for role in interaction.user.roles}
        return any(role_id in user_role_ids for role_id in allowed_roles)

    async def send_copy_options(self, interaction: discord.Interaction, signal_id: str) -> None:
        signal = self.signals.get(signal_id)
        if not signal:
            await interaction.response.send_message(
                "Signal not found or expired.", ephemeral=True
            )
            return

        if not self.storage.get_user(interaction.user.id):
            try:
                await interaction.user.send(
                    "Please register first with /register in DM."
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    "Please register first with /register in DM.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                "I sent you a DM with the registration step.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            _format_signal_message(signal),
            ephemeral=True,
            view=CopyOptionsView(self, signal_id),
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

    async def copy_with_custom(
        self,
        interaction: discord.Interaction,
        signal_id: str,
        usd: float,
        limit_val: Optional[float],
        tp: float,
        sl: float,
    ) -> None:
        signal = self.signals.get(signal_id)
        if not signal:
            await interaction.response.send_message(
                "Signal not found or expired.", ephemeral=True
            )
            return

        updated = dict(signal)
        updated["take_profit"] = tp
        updated["stop_loss"] = sl
        if updated["order_type"] == "LIMIT" and limit_val is not None:
            updated["limit_price"] = limit_val

        price_ref = (
            float(updated["limit_price"])
            if updated["order_type"] == "LIMIT"
            else float(updated.get("market_price") or 0)
        )
        if updated["order_type"] == "MARKET" and price_ref <= 0:
            price_ref = await asyncio.to_thread(
                self.orderly_client.get_mark_price, updated["symbol"]
            )

        side = str(updated["side"]).upper()
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

        self.signals[signal_id] = updated
        await self.copy_with_usd(interaction, signal_id, usd)


def register_discord_commands(bot: OrderlyDiscordBot) -> None:
    @bot.tree.command(name="help", description="Show available commands")
    async def help_cmd(interaction: discord.Interaction) -> None:
        message = (
            "Commands:\n"
            "/register (DM only) - save your Orderly keys\n"
            "/signalform - create a signal (admins only)\n"
            "/ping - check bot status\n\n"
            "Signalform fields:\n"
            "Symbol (ticker only), Side, Type, Size (USD), TP, SL, Limit (only if Type=LIMIT)"
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

    @bot.tree.command(name="signalform", description="Create a signal with structured fields")
    @app_commands.describe(
        symbol="Ticker only, e.g. ETH",
        side="BUY or SELL",
        order_type="LIMIT or MARKET",
        size="Position size in USD",
        tp="Take profit price",
        sl="Stop loss price",
        limit="Limit price (only if Type=LIMIT)",
    )
    @app_commands.choices(
        side=[
            app_commands.Choice(name="BUY", value="BUY"),
            app_commands.Choice(name="SELL", value="SELL"),
        ],
        order_type=[
            app_commands.Choice(name="LIMIT", value="LIMIT"),
            app_commands.Choice(name="MARKET", value="MARKET"),
        ],
    )
    async def signalform(
        interaction: discord.Interaction,
        symbol: str,
        side: app_commands.Choice[str],
        order_type: app_commands.Choice[str],
        size: float,
        tp: float,
        sl: float,
        limit: Optional[float] = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use /signalform inside a server.",
                ephemeral=True,
            )
            return

        if not bot.user_can_signal(interaction):
            await interaction.response.send_message(
                "Only server admins or allowed roles can create signals.",
                ephemeral=True,
            )
            return

        signal = {
            "symbol": _normalize_symbol(symbol),
            "side": side.value.upper(),
            "order_type": order_type.value.upper(),
            "notional_usd": float(size),
            "take_profit": float(tp),
            "stop_loss": float(sl),
            "limit_price": float(limit) if limit is not None else None,
        }

        if signal["order_type"] == "LIMIT" and signal["limit_price"] is None:
            await interaction.response.send_message(
                "LIMIT price is required when Type=LIMIT.",
                ephemeral=True,
            )
            return
        if signal["order_type"] == "MARKET" and signal["limit_price"] is not None:
            await interaction.response.send_message(
                "Do not provide LIMIT when Type=MARKET.",
                ephemeral=True,
            )
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
            try:
                signal["market_price"] = await asyncio.to_thread(
                    bot.orderly_client.get_mark_price, signal["symbol"]
                )
            except Exception:  # noqa: BLE001
                signal["market_price"] = None

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

        title = f"{_extract_ticker(str(signal['symbol']))} {signal['side']}"
        levels = SignalLevels(
            entry=price_ref,
            take_profit=float(signal["take_profit"]),
            stop_loss=float(signal["stop_loss"]),
            current=signal.get("market_price") if signal["order_type"] == "LIMIT" else None,
        )
        chart_bytes = None
        try:
            rows = await asyncio.to_thread(
                bot.orderly_client.get_tv_history,
                str(signal["symbol"]),
                "1h",
                100,
                int(datetime.now(tz=timezone.utc).timestamp()),
            )
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
            logger.exception("Discord candlestick render failed: %s", exc)
            chart_bytes = None

        if chart_bytes is None:
            chart_bytes = await asyncio.to_thread(
                render_signal_chart, levels, title
            )

        file = discord.File(fp=io.BytesIO(chart_bytes), filename="signal.png")
        await interaction.response.send_message(
            _format_signal_message(signal),
            view=CopyButtonView(bot, signal_id),
            file=file,
        )

    @bot.tree.command(name="signalrole_add", description="Allow a role to publish signals (admins only)")
    async def signalrole_add(interaction: discord.Interaction, role: discord.Role) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use this command inside a server.",
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server admins can manage signal roles.",
                ephemeral=True,
            )
            return
        allowed = bot.storage.get_allowed_role_ids(interaction.guild.id)
        if role.id not in allowed:
            allowed.append(role.id)
            bot.storage.set_allowed_role_ids(interaction.guild.id, allowed)
        await interaction.response.send_message(
            f"Role allowed to publish signals: {role.name}",
            ephemeral=True,
        )

    @bot.tree.command(name="signalrole_remove", description="Remove a role from signal publishers (admins only)")
    async def signalrole_remove(interaction: discord.Interaction, role: discord.Role) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use this command inside a server.",
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server admins can manage signal roles.",
                ephemeral=True,
            )
            return
        allowed = bot.storage.get_allowed_role_ids(interaction.guild.id)
        if role.id in allowed:
            allowed = [rid for rid in allowed if rid != role.id]
            bot.storage.set_allowed_role_ids(interaction.guild.id, allowed)
        await interaction.response.send_message(
            f"Role removed from signal publishers: {role.name}",
            ephemeral=True,
        )

    @bot.tree.command(name="signalrole_list", description="List roles that can publish signals (admins only)")
    async def signalrole_list(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use this command inside a server.",
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server admins can manage signal roles.",
                ephemeral=True,
            )
            return
        allowed = bot.storage.get_allowed_role_ids(interaction.guild.id)
        if not allowed:
            await interaction.response.send_message(
                "No additional roles configured.",
                ephemeral=True,
            )
            return
        role_names = []
        for role_id in allowed:
            role = interaction.guild.get_role(role_id)
            if role:
                role_names.append(role.name)
        await interaction.response.send_message(
            "Allowed roles: " + ", ".join(role_names) if role_names else "No valid roles found.",
            ephemeral=True,
        )
