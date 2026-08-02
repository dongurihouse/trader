"""Ingest Robinhood raw JSON dumps into the provider minute-bar store."""

from __future__ import annotations

from datetime import date
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from trader.provider import store


_BAR_COLUMNS = ["o", "h", "l", "c", "v"]
_PRICE_FIELDS = {
    "o": "open_price",
    "h": "high_price",
    "l": "low_price",
    "c": "close_price",
}
_DEFAULT_ETF_LEVERAGE_MAP = {"SNXX": 2.0, "SNDQ": -2.0}


def _empty_bar_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex([], dtype="datetime64[ns, UTC]", name="t")
    return pd.DataFrame(
        {column: pd.Series(dtype="float64") for column in _BAR_COLUMNS},
        index=index,
    )


def parse_result_block(result: dict) -> tuple[pd.DataFrame, int]:
    """Convert one vendor ``results[]`` block to canonical minute bars."""
    rows: list[dict[str, Any]] = []
    interpolated_dropped = 0
    for bar in result.get("bars", []) or []:
        if bar.get("interpolated"):
            interpolated_dropped += 1
            continue

        row: dict[str, Any] = {"t": bar["begins_at"]}
        for column, vendor_field in _PRICE_FIELDS.items():
            row[column] = float(bar[vendor_field])
        row["v"] = float(bar["volume"])
        rows.append(row)

    if not rows:
        return _empty_bar_frame(), interpolated_dropped

    frame = pd.DataFrame(rows)
    frame["t"] = pd.to_datetime(frame["t"], utc=True)
    frame = frame.set_index("t")[_BAR_COLUMNS].astype("float64").sort_index()
    return frame, interpolated_dropped


def _merge_frames(
    existing: pd.DataFrame | None, incoming: pd.DataFrame
) -> pd.DataFrame:
    combined = pd.concat([existing, incoming]) if existing is not None else incoming
    return combined.loc[~combined.index.duplicated(keep="last")].sort_index()


def is_bad_tick(
    prospective_frame: pd.DataFrame,
    position: int,
    fraction: float,
) -> str | None:
    """Return the anomalous price field at an interior bar, if any."""
    if position <= 0 or position >= len(prospective_frame) - 1:
        return None

    row = prospective_frame.iloc[position]
    previous_close = float(prospective_frame.iloc[position - 1]["c"])
    next_close = float(prospective_frame.iloc[position + 1]["c"])

    high = float(row["h"])
    if (
        abs(high - previous_close) / previous_close > fraction
        and abs(high - next_close) / next_close > fraction
    ):
        return "high"

    low = float(row["l"])
    if (
        abs(low - previous_close) / previous_close > fraction
        and abs(low - next_close) / next_close > fraction
    ):
        return "low"
    return None


def _quarantine_incoming(
    symbol: str,
    day: date,
    existing: pd.DataFrame | None,
    incoming: pd.DataFrame,
    bad_tick_neighbor_fraction: float,
) -> tuple[pd.DataFrame, list[dict]]:
    """Remove anomalous incoming rows while leaving existing store rows intact."""
    prospective = _merge_frames(existing, incoming)
    incoming_timestamps = set(incoming.index)
    quarantined_timestamps: list[pd.Timestamp] = []
    quarantined: list[dict] = []

    for position, timestamp in enumerate(prospective.index):
        if timestamp not in incoming_timestamps:
            continue
        offending_field = is_bad_tick(
            prospective,
            position,
            bad_tick_neighbor_fraction,
        )
        if offending_field is None:
            continue

        field_column = "h" if offending_field == "high" else "l"
        offending_value = float(prospective.iloc[position][field_column])
        quarantined_timestamps.append(timestamp)
        quarantined.append(
            {
                "timestamp": timestamp,
                "symbol": symbol,
                "day": day,
                "field": offending_field,
                "value": offending_value,
            }
        )

    surviving = incoming.drop(index=quarantined_timestamps)
    return surviving, quarantined


def _day_return(frame: pd.DataFrame) -> float:
    ordered = frame.sort_index()
    return float(ordered.iloc[-1]["c"] / ordered.iloc[0]["o"] - 1.0)


def etf_leverage_warning(
    symbol: str,
    day: date,
    underlying: pd.DataFrame | None,
    etf: pd.DataFrame | None,
    *,
    etf_leverage_factor: float,
    etf_tolerance: float,
    etf_leverage_map: dict[str, float] = _DEFAULT_ETF_LEVERAGE_MAP,
) -> dict | None:
    """Return one day-return leverage warning without mutating either frame."""
    if symbol not in etf_leverage_map:
        return None
    if underlying is None or underlying.empty or etf is None or etf.empty:
        return None

    underlying_return = _day_return(underlying)
    if abs(underlying_return) < 1e-9:
        return None

    ratio = _day_return(etf) / underlying_return
    configured_factor = etf_leverage_map[symbol]
    signed_factor = math.copysign(etf_leverage_factor, configured_factor)
    endpoint_a = signed_factor * (1.0 - etf_tolerance)
    endpoint_b = signed_factor * (1.0 + etf_tolerance)
    expected_range = (min(endpoint_a, endpoint_b), max(endpoint_a, endpoint_b))
    if expected_range[0] <= ratio <= expected_range[1]:
        return None

    return {
        "symbol": symbol,
        "day": day,
        "expected_range": expected_range,
        "actual_ratio": ratio,
    }


def _etf_warnings(
    data_root: Path,
    touched: set[tuple[str, date]],
    *,
    etf_leverage_factor: float,
    etf_tolerance: float,
    etf_leverage_map: dict[str, float],
    underlying_symbol: str,
) -> list[dict]:
    warnings: list[dict] = []
    for symbol, day in sorted(touched):
        if symbol not in etf_leverage_map:
            continue

        warning = etf_leverage_warning(
            symbol,
            day,
            store.read_1m_day(data_root, underlying_symbol, day),
            store.read_1m_day(data_root, symbol, day),
            etf_leverage_factor=etf_leverage_factor,
            etf_tolerance=etf_tolerance,
            etf_leverage_map=etf_leverage_map,
        )
        if warning is not None:
            warnings.append(warning)
    return warnings


def ingest_file(
    path: Path,
    data_root: Path,
    *,
    etf_leverage_factor: float = 2.0,
    etf_tolerance: float = 0.25,
    bad_tick_neighbor_fraction: float = 0.05,
    etf_leverage_map: dict[str, float] = _DEFAULT_ETF_LEVERAGE_MAP,
    underlying_symbol: str = "SNDK",
) -> dict:
    """Merge one raw vendor dump into the store and return its ingest summary."""
    payload = json.loads(Path(path).read_text())
    results = (payload.get("data") or {}).get("results") or []
    summaries: dict[str, dict] = {}
    incoming_by_day: dict[tuple[str, date], pd.DataFrame] = {}

    for result in results:
        symbol = result["symbol"]
        summary = summaries.setdefault(
            symbol,
            {"bars": 0, "interpolated_dropped": 0, "days": set()},
        )
        frame, interpolated_dropped = parse_result_block(result)
        summary["bars"] += len(frame)
        summary["interpolated_dropped"] += interpolated_dropped
        for day, day_frame in frame.groupby(frame.index.date):
            key = (symbol, day)
            previous_incoming = incoming_by_day.get(key)
            incoming_by_day[key] = _merge_frames(previous_incoming, day_frame)
            summary["days"].add(day)

    quarantined: list[dict] = []
    for (symbol, day), incoming in incoming_by_day.items():
        existing = store.read_1m_day(data_root, symbol, day)
        surviving, day_quarantine = _quarantine_incoming(
            symbol,
            day,
            existing,
            incoming,
            bad_tick_neighbor_fraction,
        )
        merged = _merge_frames(existing, surviving)
        store.write_1m_day(data_root, symbol, day, merged)
        quarantined.extend(day_quarantine)

    touched = set(incoming_by_day)
    result_summary = {
        symbol: {
            "bars": summary["bars"],
            "interpolated_dropped": summary["interpolated_dropped"],
            "days": sorted(summary["days"]),
        }
        for symbol, summary in summaries.items()
    }
    result_summary["quarantined"] = quarantined
    result_summary["etf_warnings"] = _etf_warnings(
        data_root,
        touched,
        etf_leverage_factor=etf_leverage_factor,
        etf_tolerance=etf_tolerance,
        etf_leverage_map=etf_leverage_map,
        underlying_symbol=underlying_symbol,
    )
    return result_summary


def ingest_all(
    raw_root: Path,
    data_root: Path,
    *,
    etf_leverage_factor: float = 2.0,
    etf_tolerance: float = 0.25,
    bad_tick_neighbor_fraction: float = 0.05,
    etf_leverage_map: dict[str, float] = _DEFAULT_ETF_LEVERAGE_MAP,
    underlying_symbol: str = "SNDK",
) -> dict:
    """Ingest all nested JSON dumps in sorted path order and aggregate results."""
    aggregate: dict[str, dict] = {}
    quarantined: list[dict] = []
    warnings: list[dict] = []

    for path in sorted(Path(raw_root).rglob("*.json")):
        file_summary = ingest_file(
            path,
            data_root,
            etf_leverage_factor=etf_leverage_factor,
            etf_tolerance=etf_tolerance,
            bad_tick_neighbor_fraction=bad_tick_neighbor_fraction,
            etf_leverage_map=etf_leverage_map,
            underlying_symbol=underlying_symbol,
        )
        quarantined.extend(file_summary["quarantined"])
        warnings.extend(file_summary["etf_warnings"])

        for symbol, summary in file_summary.items():
            if symbol in {"quarantined", "etf_warnings"}:
                continue
            symbol_aggregate = aggregate.setdefault(
                symbol,
                {"bars": 0, "interpolated_dropped": 0, "days": set()},
            )
            symbol_aggregate["bars"] += summary["bars"]
            symbol_aggregate["interpolated_dropped"] += summary[
                "interpolated_dropped"
            ]
            symbol_aggregate["days"].update(summary["days"])

    result: dict[str, Any] = {}
    for symbol, summary in aggregate.items():
        days = sorted(summary["days"])
        result[symbol] = {
            "bars": summary["bars"],
            "interpolated_dropped": summary["interpolated_dropped"],
            "days": len(days),
            "min_date": days[0] if days else None,
            "max_date": days[-1] if days else None,
        }
    result["quarantined"] = quarantined
    result["etf_warnings"] = warnings
    return result


__all__ = [
    "etf_leverage_warning",
    "ingest_all",
    "ingest_file",
    "is_bad_tick",
    "parse_result_block",
]
