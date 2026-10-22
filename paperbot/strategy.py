"""A minimal long-only moving-average crossover strategy."""

from __future__ import annotations

def crossover_signal(closes: list[float], index: int, fast: int, slow: int) -> str | None:
    """Return BUY/SELL on a crossover visible at this candle's close."""
    # A crossover needs both the current and prior slow-window averages.
    if index < slow:
        return None

    fast_now = sum(closes[index - fast + 1 : index + 1]) / fast
    slow_now = sum(closes[index - slow + 1 : index + 1]) / slow
    fast_before = sum(closes[index - fast : index]) / fast
    slow_before = sum(closes[index - slow : index]) / slow

    if fast_before <= slow_before and fast_now > slow_now:
        return "BUY"
    if fast_before >= slow_before and fast_now < slow_now:
        return "SELL"
    return None
