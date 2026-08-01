"""Deterministic synthetic OHLCV fixtures."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


_EASTERN = ZoneInfo("America/New_York")
_MINUTES_PER_DAY = 12 * 60


def synthetic_day(symbol: str, day: date, seed: int) -> pd.DataFrame:
    """Return a reproducible 04:00-16:00 ET minute-bar session.

    ``symbol`` identifies the fixture to callers but does not alter the random
    state; the supplied ``seed`` alone controls the generated price path.
    """
    del symbol
    rng = np.random.default_rng(seed)
    eastern_start = datetime.combine(day, time(4), tzinfo=_EASTERN)
    index = pd.date_range(
        eastern_start,
        periods=_MINUTES_PER_DAY,
        freq="min",
    ).tz_convert(timezone.utc)
    index.name = "ts"

    opens = np.empty(_MINUTES_PER_DAY, dtype=np.float64)
    highs = np.empty(_MINUTES_PER_DAY, dtype=np.float64)
    lows = np.empty(_MINUTES_PER_DAY, dtype=np.float64)
    closes = np.empty(_MINUTES_PER_DAY, dtype=np.float64)

    price = float(rng.uniform(40.0, 60.0))
    for position in range(_MINUTES_PER_DAY):
        opens[position] = price
        move = float(np.clip(rng.normal(0.0, 0.0008), -0.003, 0.003))
        close = max(price * (1.0 + move), 1.0)
        upper_wick = price * float(rng.uniform(0.0001, 0.0015))
        lower_wick = price * float(rng.uniform(0.0001, 0.0015))
        highs[position] = max(price, close) + upper_wick
        lows[position] = max(0.01, min(price, close) - lower_wick)
        closes[position] = close
        price = close

    volumes = rng.integers(250, 5_001, size=_MINUTES_PER_DAY, dtype=np.int64)
    return pd.DataFrame(
        {"o": opens, "h": highs, "l": lows, "c": closes, "v": volumes},
        index=index,
    )


__all__ = ["synthetic_day"]
