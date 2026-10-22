"""Paper execution engine with position sizing and loss controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math

from .data import Candle
from .strategy import crossover_signal


@dataclass(frozen=True)
class Config:
    starting_cash: float = 10_000.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    max_position_fraction: float = 0.10
    stop_loss_fraction: float = 0.03
    max_daily_loss_fraction: float = 0.03
    max_drawdown_fraction: float = 0.10
    fast_window: int = 5
    slow_window: int = 20


def _validate_config(config: Config) -> None:
    if not math.isfinite(config.starting_cash) or config.starting_cash <= 0:
        raise ValueError("starting cash must be greater than zero")
    if not 0 <= config.fee_rate < 1 or not 0 <= config.slippage_rate < 1:
        raise ValueError("fee and slippage rates must be between zero and one")
    for name, value in (
        ("max position fraction", config.max_position_fraction),
        ("stop loss", config.stop_loss_fraction),
        ("max daily loss", config.max_daily_loss_fraction),
        ("max drawdown", config.max_drawdown_fraction),
    ):
        if not 0 < value < 1:
            raise ValueError(f"{name} must be greater than zero and less than one")
    if config.fast_window < 1 or config.slow_window < 2 or config.fast_window >= config.slow_window:
        raise ValueError("moving-average windows must satisfy 1 <= fast < slow")


def run_backtest(candles: list[Candle], config: Config) -> tuple[list[dict], list[dict]]:
    """Run the paper simulation and return (trades, equity curve) rows."""
    _validate_config(config)
    if not candles:
        raise ValueError("at least one candle is required")

    cash = config.starting_cash
    quantity = 0.0
    entry_price = 0.0
    entry_fee = 0.0
    entry_time = None
    peak_equity = config.starting_cash
    day_start_equity = config.starting_cash
    last_close_equity = config.starting_cash
    current_day: date | None = None
    daily_halted = False
    drawdown_halted = False
    pending_signal: str | None = None
    closes = [candle.close for candle in candles]
    trades: list[dict] = []
    curve: list[dict] = []

    def equity(mark_price: float) -> float:
        return cash + quantity * mark_price

    def close_position(timestamp, raw_price: float, reason: str) -> None:
        nonlocal cash, quantity, entry_price, entry_fee, entry_time
        fill = raw_price * (1 - config.slippage_rate)
        exit_fee = quantity * fill * config.fee_rate
        cash += quantity * fill - exit_fee
        pnl = (fill - entry_price) * quantity - entry_fee - exit_fee
        trades.append(
            {
                "entry_timestamp": entry_time.isoformat(),
                "exit_timestamp": timestamp.isoformat(),
                "quantity": round(quantity, 10),
                "entry_price": round(entry_price, 8),
                "exit_price": round(fill, 8),
                "fees": round(entry_fee + exit_fee, 8),
                "pnl": round(pnl, 8),
                "exit_reason": reason,
            }
        )
        quantity = 0.0
        entry_price = 0.0
        entry_fee = 0.0
        entry_time = None

    def open_position(timestamp, raw_price: float, marked_equity: float) -> None:
        nonlocal cash, quantity, entry_price, entry_fee, entry_time
        fill = raw_price * (1 + config.slippage_rate)
        target_value = marked_equity * config.max_position_fraction
        quantity = min(
            target_value / fill,
            cash / (fill * (1 + config.fee_rate)),
        )
        if quantity <= 0:
            return
        entry_price = fill
        entry_fee = quantity * fill * config.fee_rate
        entry_time = timestamp
        cash -= quantity * fill + entry_fee

    for index, candle in enumerate(candles):
        candle_day = candle.timestamp.date()
        if current_day != candle_day:
            current_day = candle_day
            day_start_equity = last_close_equity
            daily_halted = False

        marked_at_open = equity(candle.open)
        if marked_at_open <= day_start_equity * (1 - config.max_daily_loss_fraction):
            daily_halted = True
        if marked_at_open <= peak_equity * (1 - config.max_drawdown_fraction):
            drawdown_halted = True
        entry_halt_reason = "daily_loss_limit" if daily_halted else (
            "max_drawdown_limit" if drawdown_halted else ""
        )

        if quantity > 0:
            stop_price = entry_price * (1 - config.stop_loss_fraction)
            if candle.open <= stop_price:
                close_position(candle.timestamp, candle.open, "stop_loss_gap")
            elif pending_signal == "SELL":
                close_position(candle.timestamp, candle.open, "moving_average_exit")
        elif pending_signal == "BUY" and not entry_halt_reason:
            open_position(candle.timestamp, candle.open, marked_at_open)

        # The intrabar low can trigger a stop after an entry or while holding.
        if quantity > 0:
            stop_price = entry_price * (1 - config.stop_loss_fraction)
            if candle.low <= stop_price:
                close_position(candle.timestamp, min(candle.open, stop_price), "stop_loss")

        marked_equity = equity(candle.close)
        peak_equity = max(peak_equity, marked_equity)
        if marked_equity <= day_start_equity * (1 - config.max_daily_loss_fraction):
            daily_halted = True
        if marked_equity <= peak_equity * (1 - config.max_drawdown_fraction):
            drawdown_halted = True
        halt_reason = "daily_loss_limit" if daily_halted else (
            "max_drawdown_limit" if drawdown_halted else ""
        )
        curve.append(
            {
                "timestamp": candle.timestamp.isoformat(),
                "cash": round(cash, 8),
                "position_quantity": round(quantity, 10),
                "equity": round(marked_equity, 8),
                "drawdown_fraction": round((peak_equity - marked_equity) / peak_equity, 8),
                "entries_halted": bool(halt_reason),
                "halt_reason": halt_reason,
            }
        )
        last_close_equity = marked_equity
        pending_signal = crossover_signal(closes, index, config.fast_window, config.slow_window)

    if quantity > 0:
        last = candles[-1]
        close_position(last.timestamp, last.close, "end_of_data")
        final_equity = cash
        curve[-1]["cash"] = round(cash, 8)
        curve[-1]["position_quantity"] = 0.0
        curve[-1]["equity"] = round(final_equity, 8)
        curve[-1]["drawdown_fraction"] = round(
            (peak_equity - final_equity) / peak_equity, 8
        )

    return trades, curve
