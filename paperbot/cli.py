"""Command-line interface for the paper trader."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .data import load_candles
from .engine import Config, run_backtest


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperbot",
        description="Simulate a long-only crypto strategy on historical OHLC candles. No exchange connection is made.",
    )
    parser.add_argument("--prices", required=True, help="CSV with timestamp, open, high, low, close columns")
    parser.add_argument("--output-dir", default="run-output", help="folder for trades.csv and equity_curve.csv")
    parser.add_argument("--fast", type=int, default=5, help="fast moving-average window (default: 5)")
    parser.add_argument("--slow", type=int, default=20, help="slow moving-average window (default: 20)")
    parser.add_argument("--starting-cash", type=float, default=10_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.001, help="fee per fill as a fraction (default: 0.001)")
    parser.add_argument("--slippage-rate", type=float, default=0.0005, help="adverse slippage per fill (default: 0.0005)")
    parser.add_argument("--max-position-fraction", type=float, default=0.10)
    parser.add_argument("--stop-loss-fraction", type=float, default=0.03)
    parser.add_argument("--max-daily-loss-fraction", type=float, default=0.03)
    parser.add_argument("--max-drawdown-fraction", type=float, default=0.10)
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = Config(
        starting_cash=args.starting_cash,
        fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate,
        max_position_fraction=args.max_position_fraction,
        stop_loss_fraction=args.stop_loss_fraction,
        max_daily_loss_fraction=args.max_daily_loss_fraction,
        max_drawdown_fraction=args.max_drawdown_fraction,
        fast_window=args.fast,
        slow_window=args.slow,
    )
    candles = load_candles(args.prices)
    trades, curve = run_backtest(candles, config)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "trades.csv", trades)
    _write_csv(output / "equity_curve.csv", curve)

    final_equity = curve[-1]["equity"]
    net_pnl = final_equity - config.starting_cash
    wins = sum(1 for trade in trades if trade["pnl"] > 0)
    max_drawdown = max((row["drawdown_fraction"] for row in curve), default=0.0)
    print("Paper simulation complete (no live orders were sent).")
    print(f"Candles:       {len(candles)}")
    print(f"Closed trades: {len(trades)}")
    print(f"Starting cash: {config.starting_cash:.2f}")
    print(f"Final equity:  {final_equity:.2f}")
    print(f"Net P&L:       {net_pnl:.2f}")
    print(f"Win rate:      {wins / len(trades):.1%}" if trades else "Win rate:      n/a")
    print(f"Max drawdown:  {max_drawdown:.1%}")
    print(f"Reports:       {output.resolve()}")


if __name__ == "__main__":
    main()
