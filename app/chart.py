from __future__ import annotations

import io
from dataclasses import dataclass

from matplotlib.patches import Rectangle
import matplotlib.dates as mdates
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


@dataclass(frozen=True)
class SignalLevels:
    entry: float
    take_profit: float
    stop_loss: float
    current: float | None = None


def render_signal_chart(levels: SignalLevels, title: str) -> bytes:
    values = [levels.entry, levels.take_profit, levels.stop_loss]
    if levels.current is not None:
        values.append(levels.current)

    low = min(values)
    high = max(values)
    padding = (high - low) * 0.15 if high != low else max(high * 0.05, 1.0)
    ymin = low - padding
    ymax = high + padding

    fig, ax = plt.subplots(figsize=(6, 4), dpi=150)
    ax.set_facecolor("#0f172a")
    fig.patch.set_facecolor("#0f172a")
    ax.set_xlim(0, 10)
    ax.set_ylim(ymin, ymax)
    ax.set_xticks([])
    ax.set_yticks([])

    def _line(y: float, label: str, color: str) -> None:
        ax.hlines(y, 0.5, 9.5, colors=color, linewidth=2)
        ax.text(0.6, y, label, color=color, fontsize=9, va="bottom")

    _line(levels.take_profit, "TP", "#22c55e")
    _line(levels.entry, "ENTRY", "#38bdf8")
    _line(levels.stop_loss, "SL", "#ef4444")

    if levels.current is not None:
        ax.hlines(levels.current, 0.5, 9.5, colors="#a855f7", linewidth=1, linestyles="--")
        ax.text(0.6, levels.current, "MARK", color="#a855f7", fontsize=8, va="bottom")

    ax.text(0.5, ymax - (ymax - ymin) * 0.07, title, color="white", fontsize=11, weight="bold")

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def render_candlestick_chart(
    candles: list[dict[str, float]],
    levels: SignalLevels,
    title: str,
    timestamps: list[int] | None = None,
) -> bytes:
    fig, ax = plt.subplots(figsize=(7, 4), dpi=160)
    ax.set_facecolor("#0f172a")
    fig.patch.set_facecolor("#0f172a")

    xs = range(len(candles))
    if timestamps and len(timestamps) == len(candles):
        norm_ts = []
        for ts in timestamps:
            ts_int = int(ts)
            if ts_int > 10**12:
                ts_int = ts_int // 1000
            norm_ts.append(ts_int)
        x_dates = [
            datetime.fromtimestamp(ts, tz=timezone.utc) for ts in norm_ts
        ]
        x_vals = [mdates.date2num(dt) for dt in x_dates]
    else:
        x_dates = None
        x_vals = None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    level_values = [levels.entry, levels.take_profit, levels.stop_loss]
    if levels.current is not None:
        level_values.append(levels.current)
    ymin = min(lows + level_values)
    ymax = max(highs + level_values)
    padding = (ymax - ymin) * 0.25 if ymax != ymin else max(ymax * 0.08, 1.0)
    ax.set_ylim(ymin - padding, ymax + padding)
    ax.set_xlim(-1, len(candles))
    if x_dates and x_vals:
        ax.set_xlim(x_vals[0], x_vals[-1])
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
        ax.tick_params(axis="x", colors="#94a3b8", labelsize=8)
    else:
        ax.set_xticks([])

    ax.tick_params(axis="y", colors="#94a3b8", labelsize=8)
    ax.yaxis.set_major_locator(plt.MaxNLocator(5))

    for i, candle in enumerate(candles):
        open_p = candle["open"]
        close_p = candle["close"]
        high_p = candle["high"]
        low_p = candle["low"]
        color = "#22c55e" if close_p >= open_p else "#ef4444"
        x_val = x_vals[i] if x_vals else i
        ax.vlines(x_val, low_p, high_p, color=color, linewidth=1)
        body_bottom = min(open_p, close_p)
        body_height = max(abs(close_p - open_p), 0.0000001)
        if x_vals:
            base_width = (x_vals[1] - x_vals[0]) if len(x_vals) > 1 else 0.01
            width = max(min(base_width * 0.7, 0.05), 0.001)
            x_left = x_val - (width / 2)
        else:
            width = 0.6
            x_left = x_val - 0.3
        ax.add_patch(
            Rectangle(
                (x_left, body_bottom),
                width,
                body_height,
                facecolor=color,
                edgecolor=color,
            )
        )

    def _line(y: float, label: str, color: str, style: str = "-") -> None:
        if x_vals:
            x_min = x_vals[0]
            x_max = x_vals[-1]
        else:
            x_min = -0.5
            x_max = len(candles) - 0.5
        ax.hlines(y, x_min, x_max, colors=color, linewidth=2, linestyles=style)
        ax.text(x_min, y, label, color=color, fontsize=9, va="bottom", weight="bold")

    _line(levels.take_profit, "TP", "#22c55e")
    _line(levels.entry, "ENTRY", "#38bdf8")
    _line(levels.stop_loss, "SL", "#ef4444")
    if levels.current is not None:
        _line(levels.current, "MARK", "#a855f7", "--")

    ax.text(0, ymax + padding * 0.2, title, color="white", fontsize=12, weight="bold")

    buf = io.BytesIO()
    fig.subplots_adjust(left=0.12, right=0.98, top=0.92, bottom=0.2)
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()
