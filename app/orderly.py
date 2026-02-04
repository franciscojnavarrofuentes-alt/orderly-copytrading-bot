import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any
from decimal import Decimal, ROUND_DOWN

import base58
import nacl.signing
import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderlyOrder:
    symbol: str
    order_type: str
    side: str
    order_quantity: float
    order_price: float | None = None
    reduce_only: bool | None = None
    slippage: float | None = None


class OrderlyClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def _build_message(
        self, timestamp_ms: int, method: str, path: str, body: str | None
    ) -> str:
        parts = [str(timestamp_ms), method.upper(), path]
        if body is not None:
            parts.append(body)
        return "".join(parts)

    def _sign(self, secret: str, message: str) -> str:
        secret_key = secret.replace("ed25519:", "")
        seed = base58.b58decode(secret_key)
        signing_key = nacl.signing.SigningKey(seed)
        signature = signing_key.sign(message.encode("utf-8")).signature
        return base64.urlsafe_b64encode(signature).decode("utf-8")

    def get_mark_price(self, symbol: str) -> float:
        # Try direct symbol endpoint first.
        path = f"/v1/public/futures/{symbol}"
        url = f"{self._base_url}{path}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        rows = data.get("data", {}).get("rows", [])
        if rows:
            return float(rows[0]["mark_price"])

        # Fallback: fetch all markets and try to match exact or suffix variants.
        fallback_url = f"{self._base_url}/v1/public/futures"
        response = requests.get(fallback_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        rows = data.get("data", {}).get("rows", [])
        if not rows:
            raise RuntimeError("No market data rows returned.")

        symbol_upper = symbol.upper()
        for row in rows:
            row_symbol = str(row.get("symbol", "")).upper()
            if row_symbol == symbol_upper or row_symbol.startswith(symbol_upper + "."):
                return float(row["mark_price"])

        raise RuntimeError(f"Symbol not found in market data: {symbol}")

    def get_order_rules(self, symbol: str) -> dict[str, Any]:
        path = f"/v1/public/info/{symbol}"
        url = f"{self._base_url}{path}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        rules = data.get("data", {})
        if not rules or rules.get("symbol") is None:
            raise RuntimeError("No order rules returned.")
        return rules

    def round_quantity(self, quantity: float, base_tick: float) -> float:
        q = Decimal(str(quantity))
        tick = Decimal(str(base_tick))
        rounded = (q / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return float(rounded)

    def create_order(
        self,
        orderly_account_id: str,
        orderly_key: str,
        orderly_secret: str,
        order: OrderlyOrder,
    ) -> dict[str, Any]:
        path = "/v1/order"
        url = f"{self._base_url}{path}"
        timestamp_ms = int(time.time() * 1000)
        payload: dict[str, Any] = {
            "symbol": order.symbol,
            "client_order_id": str(uuid.uuid4()),
            "order_type": order.order_type,
            "side": order.side,
            "order_quantity": order.order_quantity,
        }

        if order.order_price is not None:
            payload["order_price"] = order.order_price
        if order.reduce_only is not None:
            payload["reduce_only"] = order.reduce_only
        if order.slippage is not None:
            payload["slippage"] = order.slippage

        body_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        message = self._build_message(timestamp_ms, "POST", path, body_json)
        signature = self._sign(orderly_secret, message)

        headers = {
            "Content-Type": "application/json",
            "orderly-account-id": orderly_account_id,
            "orderly-key": orderly_key,
            "orderly-signature": signature,
            "orderly-timestamp": str(timestamp_ms),
        }

        logger.info("Sending order for %s", order.symbol)
        response = requests.post(url, data=body_json, headers=headers, timeout=15)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            raise RuntimeError(
                f"Unexpected response ({response.status_code}): {response.text}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid JSON response ({response.status_code}): {response.text}"
            ) from exc

    def create_algo_order(
        self,
        orderly_account_id: str,
        orderly_key: str,
        orderly_secret: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        path = "/v1/algo/order"
        url = f"{self._base_url}{path}"
        timestamp_ms = int(time.time() * 1000)
        body_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        message = self._build_message(timestamp_ms, "POST", path, body_json)
        signature = self._sign(orderly_secret, message)

        headers = {
            "Content-Type": "application/json",
            "orderly-account-id": orderly_account_id,
            "orderly-key": orderly_key,
            "orderly-signature": signature,
            "orderly-timestamp": str(timestamp_ms),
        }

        logger.info("Sending algo order for %s", payload.get("symbol"))
        response = requests.post(url, data=body_json, headers=headers, timeout=15)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            raise RuntimeError(
                f"Unexpected response ({response.status_code}): {response.text}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid JSON response ({response.status_code}): {response.text}"
            ) from exc

    def get_kline_history(
        self,
        orderly_account_id: str,
        orderly_key: str,
        orderly_secret: str,
        symbol: str,
        resolution: str,
        limit: int,
        to_ts: int,
    ) -> list[dict[str, Any]]:
        seconds_per_candle = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
        }
        if resolution not in seconds_per_candle:
            raise RuntimeError("Unsupported resolution.")

        from_ts = to_ts - seconds_per_candle[resolution] * limit
        path = "/v1/tv/kline_history"
        query = (
            f"symbol={symbol}&resolution={resolution}"
            f"&from={from_ts}&to={to_ts}&limit={limit}"
        )
        url = f"{self._base_url}{path}?{query}"
        timestamp_ms = int(time.time() * 1000)
        message = self._build_message(
            timestamp_ms, "GET", f"{path}?{query}", None
        )
        signature = self._sign(orderly_secret, message)
        headers = {
            "Content-Type": "application/json",
            "orderly-account-id": orderly_account_id,
            "orderly-key": orderly_key,
            "orderly-signature": signature,
            "orderly-timestamp": str(timestamp_ms),
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        rows = data.get("data", {}).get("rows", [])
        if not rows:
            raise RuntimeError("No kline rows returned.")
        return rows

    def get_tv_history(
        self,
        symbol: str,
        resolution: str,
        limit: int,
        to_ts: int,
    ) -> list[dict[str, Any]]:
        seconds_per_candle = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
            "1w": 604800,
        }
        tv_resolution = {
            "1m": "1",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "4h": "240",
            "1d": "1D",
            "1w": "1W",
        }
        if resolution not in seconds_per_candle:
            raise RuntimeError("Unsupported resolution.")

        from_ts = to_ts - seconds_per_candle[resolution] * limit
        path = "/v1/tv/history"

        def _fetch(tv_symbol: str) -> list[dict[str, Any]] | None:
            query = (
                f"symbol={tv_symbol}&resolution={tv_resolution[resolution]}"
                f"&from={from_ts}&to={to_ts}"
            )
            url = f"{self._base_url}{path}?{query}"
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            if data.get("s") != "ok":
                return None
            timestamps = data.get("t", [])
            if not timestamps:
                return None
            opens = data.get("o", [])
            highs = data.get("h", [])
            lows = data.get("l", [])
            closes = data.get("c", [])
            rows = []
            for idx, ts in enumerate(timestamps):
                rows.append(
                    {
                        "ts": ts,
                        "open": opens[idx],
                        "high": highs[idx],
                        "low": lows[idx],
                        "close": closes[idx],
                    }
                )
            return rows

        candidates = [symbol]
        if not symbol.endswith(".e"):
            candidates.append(f"{symbol}.e")
        if symbol.startswith("PERP_"):
            base = symbol.replace("PERP_", "")
            candidates.append(base)
            candidates.append(f"{base}.e")

        for candidate in candidates:
            rows = _fetch(candidate)
            if rows:
                return rows

        raise RuntimeError("No kline rows returned.")
