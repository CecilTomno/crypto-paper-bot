"""Long-only Binance Spot bot. Preview and testnet are the safe starting modes."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .strategy import crossover_signal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperbot-live",
        description="Run an automated Binance Spot moving-average bot.",
    )
    parser.add_argument("--mode", choices=("preview", "testnet", "live"), default="preview")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--fast", type=int, default=5)
    parser.add_argument("--slow", type=int, default=20)
    parser.add_argument("--capital-limit", type=float, default=None,
                        help="virtual quote-currency capital allocated to this bot")
    parser.add_argument("--max-position-fraction", type=float, default=0.10)
    parser.add_argument("--max-quote-per-trade", type=float, default=None)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--max-slippage-fraction", type=float, default=0.002)
    parser.add_argument("--stop-loss-fraction", type=float, default=0.03)
    parser.add_argument("--max-daily-loss-fraction", type=float, default=0.03)
    parser.add_argument("--max-drawdown-fraction", type=float, default=0.10)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--confirm-live", action="store_true",
                        help="required acknowledgement before using production Binance")
    return parser


def _make_exchange(mode: str):
    try:
        import ccxt
    except ImportError as exc:
        raise RuntimeError("Install the live dependency with: python -m pip install -r requirements-live.txt") from exc

    config = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if mode in ("testnet", "live"):
        key_name, secret_name = (
            ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_SECRET")
            if mode == "testnet"
            else ("BINANCE_API_KEY", "BINANCE_SECRET")
        )
        api_key, secret = os.getenv(key_name), os.getenv(secret_name)
        if not api_key or not secret:
            raise RuntimeError(f"Set {key_name} and {secret_name} in your local environment")
        config.update({"apiKey": api_key, "secret": secret})

    exchange = ccxt.binance(config)
    if mode == "testnet":
        # CCXT requires sandbox selection before any other exchange call.
        exchange.set_sandbox_mode(True)
    exchange.load_markets()
    return exchange


def _atomic_save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _initial_state(capital: float, mode: str, symbol: str, timeframe: str) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "mode": mode,
        "symbol": symbol,
        "timeframe": timeframe,
        "quote_cash": capital,
        "position_amount": 0.0,
        "entry_price": 0.0,
        "entry_cost": 0.0,
        "entry_time": None,
        "peak_equity": capital,
        "utc_day": today,
        "day_start_equity": capital,
        "daily_halted": False,
        "drawdown_halted": False,
        "last_processed_candle": None,
        "pending_order": None,
        "exit_retry": "",
    }


def _client_order_id() -> str:
    return "pb" + str(int(time.time() * 1000)) + uuid.uuid4().hex[:8]


def _filled_order(exchange, symbol: str, side: str, amount: float, slippage: float,
                  state: dict, state_path: Path, fee_rate: float = 0.0,
                  max_quote: float | None = None) -> dict:
    """Submit a bounded IOC limit order and fail closed if its outcome is uncertain."""
    book = exchange.fetch_order_book(symbol, limit=5)
    levels = book.get("asks") if side == "buy" else book.get("bids")
    if not levels:
        raise RuntimeError("Order book is empty; no order was submitted")
    reference = float(levels[0][0])
    raw_limit = reference * (1 + slippage) if side == "buy" else reference * (1 - slippage)
    limit_price = float(exchange.price_to_precision(symbol, raw_limit))
    if (side == "buy" and limit_price > raw_limit) or (side == "sell" and limit_price < raw_limit):
        raise RuntimeError("Price precision would exceed the configured slippage cap; no order was submitted")
    if side == "buy" and max_quote is not None:
        amount = min(amount, max_quote / (limit_price * (1 + fee_rate)))
    amount = float(exchange.amount_to_precision(symbol, amount))
    if amount <= 0:
        raise RuntimeError("Order amount rounds to zero; no order was submitted")
    limits = exchange.market(symbol).get("limits") or {}
    min_amount = (limits.get("amount") or {}).get("min")
    min_cost = (limits.get("cost") or {}).get("min")
    if min_amount and amount < float(min_amount):
        raise RuntimeError("Order amount is below Binance's minimum; no order was submitted")
    if min_cost and amount * limit_price < float(min_cost):
        raise RuntimeError("Order value is below Binance's minimum; no order was submitted")

    client_id = _client_order_id()
    state["pending_order"] = {"client_order_id": client_id, "side": side, "amount": amount}
    _atomic_save(state_path, state)
    try:
        order = exchange.create_order(
            symbol, "limit", side, amount, limit_price,
            {"timeInForce": "IOC", "clientOrderId": client_id},
        )
        if order.get("id") and (order.get("filled") is None or order.get("cost") is None):
            order = exchange.fetch_order(order["id"], symbol)
        if not order.get("id") or order.get("filled") is None:
            raise RuntimeError("Exchange returned no definitive order status")
        if order.get("status") not in ("closed", "canceled", "expired"):
            raise RuntimeError("IOC order did not reach a final status")
    except Exception as exc:
        # Leave pending_order in the state file. A timeout may mean Binance accepted it.
        raise RuntimeError(
            f"Order result is uncertain ({type(exc).__name__}). Trading is halted; inspect Binance order history "
            "before clearing pending_order in the state file."
        ) from None

    filled = float(order.get("filled") or 0.0)
    cost = float(order.get("cost") or 0.0)
    average = float(order.get("average") or (cost / filled if filled and cost else limit_price))
    if filled and not cost:
        cost = filled * average
    return {"filled": filled, "cost": cost, "average": average, "order_id": order.get("id")}


def _equity(state: dict, price: float) -> float:
    return float(state["quote_cash"]) + float(state["position_amount"]) * price


def _update_risk_state(state: dict, price: float, options) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    marked = _equity(state, price)
    if state["utc_day"] != today:
        state["utc_day"] = today
        state["day_start_equity"] = marked
        state["daily_halted"] = False
    state["peak_equity"] = max(float(state["peak_equity"]), marked)
    if marked <= float(state["day_start_equity"]) * (1 - options.max_daily_loss_fraction):
        state["daily_halted"] = True
    if marked <= float(state["peak_equity"]) * (1 - options.max_drawdown_fraction):
        state["drawdown_halted"] = True
    if state["daily_halted"]:
        return "daily_loss_limit"
    if state["drawdown_halted"]:
        return "max_drawdown_limit"
    return ""


def _stop_exit(exchange, options, symbol: str, base: str, state: dict, state_path: Path,
               last_price: float) -> None:
    amount = float(state["position_amount"])
    if amount <= 0:
        return
    stop = float(state["entry_price"]) * (1 - options.stop_loss_fraction)
    if last_price > stop:
        return

    balance = exchange.fetch_balance()
    free_base = float((balance.get(base) or {}).get("free") or 0.0)
    if free_base < amount * 0.98:
        raise RuntimeError(
            f"Tracked position and available {base} balance differ. Trading halted for manual reconciliation."
        )
    order = _filled_order(exchange, symbol, "sell", min(amount, free_base),
                          options.max_slippage_fraction, state, state_path)
    if order["filled"] <= 0:
        state["pending_order"] = None
        state["exit_retry"] = "Stop-loss"
        print("Stop reached, but the IOC limit order did not fill; retrying while the bot remains online.")
        _atomic_save(state_path, state)
        return
    sold = min(order["filled"], amount)
    entry_cost_sold = float(state["entry_cost"]) * sold / amount
    state["quote_cash"] += order["cost"] * sold / order["filled"] * (1 - options.fee_rate)
    remaining = amount - sold
    state["entry_cost"] = max(0.0, float(state["entry_cost"]) - entry_cost_sold)
    state["position_amount"] = remaining
    if remaining <= max(amount * 1e-8, 1e-12):
        state["position_amount"] = 0.0
        state["entry_price"] = 0.0
        state["entry_cost"] = 0.0
        state["entry_time"] = None
        state["exit_retry"] = ""
    else:
        state["exit_retry"] = "Stop-loss"
    state["pending_order"] = None
    _atomic_save(state_path, state)
    print(f"Stop exit filled {sold:.8g} {symbol.split('/')[0]} at average {order['average']:.8g}.")


def _place_entry(exchange, options, symbol: str, quote: str, state: dict,
                 state_path: Path) -> None:
    budget = min(
        float(state["quote_cash"]) * options.max_position_fraction,
        options.max_quote_per_trade,
    )
    balance = exchange.fetch_balance()
    free_quote = float((balance.get(quote) or {}).get("free") or 0.0)
    if budget <= 0 or free_quote < budget:
        print(f"Buy signal ignored: available {quote} balance is below the bot's {budget:.4f} order budget.")
        return
    orderbook = exchange.fetch_order_book(symbol, limit=5)
    asks = orderbook.get("asks") or []
    if not asks:
        print("Buy signal ignored: order book is empty.")
        return
    cap_price = float(asks[0][0]) * (1 + options.max_slippage_fraction)
    amount = budget / (cap_price * (1 + options.fee_rate))
    amount = float(exchange.amount_to_precision(symbol, amount))
    limits = exchange.market(symbol).get("limits") or {}
    min_cost = ((limits.get("cost") or {}).get("min"))
    if amount <= 0 or (min_cost and amount * cap_price < float(min_cost)):
        print("Buy signal ignored: order is below Binance's current market minimum.")
        return

    order = _filled_order(exchange, symbol, "buy", amount,
                          options.max_slippage_fraction, state, state_path,
                          fee_rate=options.fee_rate, max_quote=budget)
    if order["filled"] <= 0:
        state["pending_order"] = None
        _atomic_save(state_path, state)
        print("Buy signal produced no fill; the unfilled IOC remainder was canceled.")
        return
    fee_estimate = order["cost"] * options.fee_rate
    state["quote_cash"] -= order["cost"] + fee_estimate
    state["position_amount"] = order["filled"]
    state["entry_price"] = order["average"]
    state["entry_cost"] = order["cost"] + fee_estimate
    state["entry_time"] = datetime.now(timezone.utc).isoformat()
    state["pending_order"] = None
    _atomic_save(state_path, state)
    print(f"Buy filled {order['filled']:.8g} {symbol.split('/')[0]} at average {order['average']:.8g}.")


def _place_exit(exchange, options, symbol: str, base: str, state: dict,
                state_path: Path, reason: str) -> None:
    amount = float(state["position_amount"])
    if amount <= 0:
        return
    balance = exchange.fetch_balance()
    free_base = float((balance.get(base) or {}).get("free") or 0.0)
    if free_base < amount * 0.98:
        raise RuntimeError(
            f"Tracked position and available {base} balance differ. Trading halted for manual reconciliation."
        )
    order = _filled_order(exchange, symbol, "sell", min(amount, free_base),
                          options.max_slippage_fraction, state, state_path)
    if order["filled"] <= 0:
        state["pending_order"] = None
        state["exit_retry"] = reason
        _atomic_save(state_path, state)
        print(f"{reason} signal produced no fill; the unfilled IOC remainder was canceled.")
        return
    sold = min(order["filled"], amount)
    entry_cost_sold = float(state["entry_cost"]) * sold / amount
    proceeds = order["cost"] * sold / order["filled"]
    state["quote_cash"] += proceeds * (1 - options.fee_rate)
    state["position_amount"] = max(0.0, amount - sold)
    state["entry_cost"] = max(0.0, float(state["entry_cost"]) - entry_cost_sold)
    if state["position_amount"] <= max(amount * 1e-8, 1e-12):
        state["position_amount"] = 0.0
        state["entry_price"] = 0.0
        state["entry_cost"] = 0.0
        state["entry_time"] = None
        state["exit_retry"] = ""
    else:
        state["exit_retry"] = reason
    state["pending_order"] = None
    _atomic_save(state_path, state)
    print(f"{reason} exit filled {sold:.8g} {base} at average {order['average']:.8g}.")


def _validate_options(parser, options) -> None:
    if options.mode == "live" and not options.confirm_live:
        parser.error("real trading requires --confirm-live; use --mode testnet first")
    if options.mode == "live" and (
        not options.symbol or options.capital_limit is None or options.max_quote_per_trade is None
    ):
        parser.error("live mode requires explicit --symbol, --capital-limit, and --max-quote-per-trade values")
    if options.symbol is None:
        options.symbol = "BTC/USDT"
    if options.capital_limit is None:
        options.capital_limit = 1000.0
    if options.max_quote_per_trade is None:
        options.max_quote_per_trade = 100.0
    if options.fast < 1 or options.slow <= options.fast:
        parser.error("moving-average windows must satisfy 1 <= fast < slow")
    if (not math.isfinite(options.capital_limit) or not math.isfinite(options.max_quote_per_trade)
            or options.capital_limit <= 0 or options.max_quote_per_trade <= 0):
        parser.error("capital and per-trade quote limits must be positive")
    if not 0 < options.max_position_fraction < 1:
        parser.error("max-position-fraction must be between zero and one")
    for name in ("fee_rate", "max_slippage_fraction", "stop_loss_fraction",
                 "max_daily_loss_fraction", "max_drawdown_fraction"):
        if not 0 < getattr(options, name) < 1:
            parser.error(f"{name.replace('_', '-')} must be between zero and one")
    if not math.isfinite(options.poll_seconds) or options.poll_seconds < 5:
        parser.error("poll-seconds must be at least 5 to respect exchange rate limits")


def main() -> None:
    parser = _parser()
    options = parser.parse_args()
    _validate_options(parser, options)

    exchange = _make_exchange(options.mode)
    symbol = options.symbol
    market = exchange.market(symbol)
    if not market.get("spot") or market.get("contract"):
        raise RuntimeError("This bot only supports Binance Spot markets; margin and derivatives are disabled")
    base, quote = market["base"], market["quote"]
    timeframe_ms = int(exchange.parse_timeframe(options.timeframe) * 1000)

    if options.state_file is None:
        state_key = symbol.replace("/", "_").replace(":", "_")
        state_path = Path("state") / f"binance_{options.mode}_{state_key}_{options.timeframe}.json"
    else:
        state_path = options.state_file

    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"Bot lock exists at {lock_path}; another instance may be running") from None
    os.write(lock_handle, str(os.getpid()).encode("ascii"))
    os.close(lock_handle)

    try:
        state = _initial_state(options.capital_limit, options.mode, symbol, options.timeframe)
        if options.mode != "preview" and state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if (state.get("mode"), state.get("symbol"), state.get("timeframe")) != (
                options.mode, symbol, options.timeframe
            ):
                raise RuntimeError("State file belongs to a different mode, symbol, or timeframe")
            if state.get("pending_order"):
                raise RuntimeError(
                    "State contains an unresolved order. Inspect Binance order history and reconcile the state "
                    "before restarting; the bot will not risk submitting a duplicate."
                )
        elif options.mode != "preview":
            _atomic_save(state_path, state)

        print(f"Binance Spot {options.mode} mode: {symbol}, {options.timeframe}; long-only.")
        if options.mode == "live":
            print("REAL ORDERS ENABLED. Check the symbol, limits, API permissions, and state file before continuing.")

        last_preview_candle = None
        while True:
            ticker = exchange.fetch_ticker(symbol)
            last_price = float(ticker.get("last") or ticker.get("close") or 0.0)
            if last_price <= 0:
                raise RuntimeError("Binance returned no valid last price")

            if options.mode != "preview":
                halt_reason = _update_risk_state(state, last_price, options)
                if state["position_amount"] > 0 and state.get("exit_retry"):
                    _place_exit(exchange, options, symbol, base, state, state_path,
                                state["exit_retry"])
                elif state["position_amount"] > 0:
                    _stop_exit(exchange, options, symbol, base, state, state_path, last_price)
                _atomic_save(state_path, state)
            else:
                halt_reason = ""

            now_ms = exchange.milliseconds()
            raw = exchange.fetch_ohlcv(symbol, timeframe=options.timeframe,
                                       limit=max(options.slow + 3, 30))
            closed = [row for row in raw if int(row[0]) + timeframe_ms <= now_ms]
            if len(closed) >= options.slow + 1:
                candle_timestamp = int(closed[-1][0])
                previous_timestamp = state.get("last_processed_candle") if options.mode != "preview" else last_preview_candle
                if candle_timestamp != previous_timestamp:
                    closes = [float(row[4]) for row in closed]
                    signal = crossover_signal(closes, len(closes) - 1, options.fast, options.slow)
                    if signal:
                        print(f"{datetime.fromtimestamp(candle_timestamp / 1000, timezone.utc).isoformat()} signal: {signal}")
                        if options.mode == "preview":
                            pass
                        elif signal == "BUY" and state["position_amount"] == 0 and not halt_reason:
                            _place_entry(exchange, options, symbol, quote, state, state_path)
                        elif signal == "SELL" and state["position_amount"] > 0:
                            _place_exit(exchange, options, symbol, base, state, state_path,
                                        "Moving-average")
                    if options.mode != "preview":
                        state["last_processed_candle"] = candle_timestamp
                        _atomic_save(state_path, state)
                    else:
                        last_preview_candle = candle_timestamp
            if halt_reason:
                print(f"New entries halted by {halt_reason}; open-position stop monitoring continues.")
            time.sleep(options.poll_seconds)
    except KeyboardInterrupt:
        print("Stopped by user. Any open position remains on Binance; this local stop monitor is now offline.")
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        close = getattr(exchange, "close", None)
        if close:
            close()


if __name__ == "__main__":
    main()
