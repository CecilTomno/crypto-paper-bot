"""Input parsing and validation for candle CSV files."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


def _parse_timestamp(value: str, line_number: int) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(f"line {line_number}: timestamp must be ISO 8601") from exc


def load_candles(path: str | Path) -> list[Candle]:
    """Load validated OHLC candles from a CSV file."""
    source = Path(path)
    candles: list[Candle] = []
    with source.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError("CSV is empty or has no header row")
        names = {name.strip().lower() for name in reader.fieldnames if name}
        required = {"timestamp", "open", "high", "low", "close"}
        missing = required - names
        if missing:
            raise ValueError("CSV is missing required columns: " + ", ".join(sorted(missing)))

        for line_number, row in enumerate(reader, start=2):
            try:
                normalized = {key.strip().lower(): value for key, value in row.items() if key}
                timestamp = _parse_timestamp(normalized["timestamp"], line_number)
                prices = {
                    key: float(normalized[key])
                    for key in ("open", "high", "low", "close")
                }
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, ValueError) and str(exc).startswith("line "):
                    raise
                raise ValueError(f"line {line_number}: invalid or missing candle value") from exc

            if not all(math.isfinite(price) and price > 0 for price in prices.values()):
                raise ValueError(f"line {line_number}: all prices must be finite and greater than zero")
            if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
                raise ValueError(f"line {line_number}: high is below another candle price")
            if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
                raise ValueError(f"line {line_number}: low is above another candle price")
            if candles and timestamp <= candles[-1].timestamp:
                raise ValueError(f"line {line_number}: timestamps must be strictly increasing")

            candles.append(Candle(timestamp=timestamp, **prices))

    if not candles:
        raise ValueError("CSV contains no candle rows")
    return candles
