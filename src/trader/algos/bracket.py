"""Translate SNDK-space bracket levels onto the traded ETF."""

from __future__ import annotations

from datetime import datetime

from trader.contracts import MarketData


# These mirror config/trader.yaml's primary_symbol and instrument_map. Algos
# cannot read that file, so edits to either copy must be mirrored in the other.
UNDERLYING = "SNDK"
ETF_LONG = "SNXX"
ETF_SHORT = "SNDQ"
LEVERAGE: dict[str, float] = {"SNXX": 2.0, "SNDQ": -2.0}


def instrument_for(direction: str) -> str:
    """Return the long-share ETF used to express a candidate direction."""
    if direction == "long":
        return ETF_LONG
    if direction == "short":
        return ETF_SHORT
    raise ValueError(f"unknown direction {direction!r}; expected 'long' or 'short'")


def leverage_for(instrument: str) -> float:
    """Return an instrument's signed leverage."""
    if instrument not in LEVERAGE:
        raise ValueError(f"unknown leveraged instrument {instrument!r}")
    return LEVERAGE[instrument]


def etf_price(
    sndk_px: float, sndk_prev_close: float, etf_prev_close: float, lev: float
) -> float:
    """Translate one SNDK-space price onto an ETF's price axis."""
    return etf_prev_close * (1 + lev * (sndk_px / sndk_prev_close - 1))


def previous_close(symbol: str, data: MarketData, asof: datetime) -> float | None:
    """Return the most recent complete daily close strictly before ``asof``."""
    frame = data.bars_1d(symbol, asof=asof.date(), lookback_days=1)
    if frame.empty:
        return None
    return float(frame["c"].iloc[-1])


def translate(
    direction: str,
    entry_sndk: float,
    stop_sndk: float,
    target_sndk: float,
    signal_symbol: str,
    data: MarketData,
    asof: datetime,
) -> tuple[str, float, float, float] | None:
    """Resolve an ETF and translate SNDK-space entry, stop, and target prices."""
    instrument = instrument_for(direction)
    lev = leverage_for(instrument)
    sndk_prev = previous_close(signal_symbol, data, asof)
    etf_prev = previous_close(instrument, data, asof)
    if sndk_prev is None or etf_prev is None:
        return None
    # Defensive additions beyond dt: broken non-positive previous-close quotes
    # cannot be used to translate today's SNDK-space levels into a valid bracket.
    if sndk_prev <= 0 or etf_prev <= 0:
        return None

    entry = etf_price(entry_sndk, sndk_prev, etf_prev, lev)
    stop = etf_price(stop_sndk, sndk_prev, etf_prev, lev)
    target = etf_price(target_sndk, sndk_prev, etf_prev, lev)
    return instrument, round(entry, 2), round(stop, 2), round(target, 2)
