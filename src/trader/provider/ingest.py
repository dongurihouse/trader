"""Ingest Robinhood raw JSON dumps into the provider minute-bar store."""

from __future__ import annotations

from datetime import date
import json
import math
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

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
_NEW_YORK = ZoneInfo("America/New_York")
_RTH_OPEN_MINUTE = 9 * 60 + 30
_RTH_CLOSE_MINUTE = 16 * 60


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


def _daily_bar(day: date, minute_bars: pd.DataFrame) -> pd.DataFrame | None:
    ordered = minute_bars.sort_index()
    local_index = ordered.index.tz_convert(_NEW_YORK)
    local_minutes = local_index.hour * 60 + local_index.minute
    rth = ordered.loc[
        (local_minutes >= _RTH_OPEN_MINUTE)
        & (local_minutes < _RTH_CLOSE_MINUTE)
    ]
    if rth.empty:
        return None

    return pd.DataFrame(
        {
            "o": [float(rth.iloc[0]["o"])],
            "h": [float(rth["h"].max())],
            "l": [float(rth["l"].min())],
            "c": [float(rth.iloc[-1]["c"])],
            "v": [float(rth["v"].sum())],
        },
        index=pd.DatetimeIndex(
            [pd.Timestamp(day).tz_localize("UTC")],
            name="t",
        ),
        dtype="float64",
    )


def _deviates(value: float, reference: float, fraction: float) -> bool:
    if reference == 0.0:
        return value != 0.0
    return abs(value - reference) / abs(reference) > fraction


def _gather_references(
    position: int,
    size: int,
    closes: list[float],
    excluded: set[int],
) -> list[float]:
    """Return up to two close references per side, skipping exclusions."""
    references: list[float] = []
    step = 1
    while len(references) < 2 and position - step >= 0:
        candidate = position - step
        if candidate not in excluded:
            references.append(closes[candidate])
        step += 1

    left_count = len(references)
    step = 1
    while len(references) - left_count < 2 and position + step < size:
        candidate = position + step
        if candidate not in excluded:
            references.append(closes[candidate])
        step += 1
    return references


def bad_tick_fields(
    prospective_frame: pd.DataFrame,
    fraction: float,
) -> list[str | None]:
    """Classify anomalous high/low fields across one ordered trading day."""
    frame = prospective_frame
    size = len(frame)
    closes = [float(value) for value in frame["c"]]
    highs = [float(value) for value in frame["h"]]
    lows = [float(value) for value in frame["l"]]
    flagged_field: dict[int, str] = {}

    for _ in range(10):
        excluded = set(flagged_field)
        new_flagged_field: dict[int, str] = {}
        for position in range(size):
            references = _gather_references(
                position,
                size,
                closes,
                excluded,
            )
            if len(references) < 2:
                if position in flagged_field:
                    new_flagged_field[position] = flagged_field[position]
                continue

            reference = median(references)
            if _deviates(highs[position], reference, fraction):
                new_flagged_field[position] = "high"
            elif _deviates(lows[position], reference, fraction):
                new_flagged_field[position] = "low"

        # If every position is excluded, sticky deferral cannot recover false
        # positives caused by a short split-level frame. Re-anchor that fully
        # starved state to the day's close median before continuing iteration.
        if size and len(new_flagged_field) == size:
            day_reference = median(closes)
            new_flagged_field = {}
            for position in range(size):
                if _deviates(highs[position], day_reference, fraction):
                    new_flagged_field[position] = "high"
                elif _deviates(lows[position], day_reference, fraction):
                    new_flagged_field[position] = "low"

        if new_flagged_field == flagged_field:
            flagged_field = new_flagged_field
            break
        flagged_field = new_flagged_field

    return [flagged_field.get(position) for position in range(size)]


def is_bad_tick(
    prospective_frame: pd.DataFrame,
    position: int,
    fraction: float,
) -> str | None:
    """Return one position's result from the per-day bad-tick classifier."""
    if position < 0 or position >= len(prospective_frame):
        return None
    return bad_tick_fields(prospective_frame, fraction)[position]


def _quarantine_day(
    symbol: str,
    day: date,
    existing: pd.DataFrame | None,
    incoming: pd.DataFrame,
    bad_tick_neighbor_fraction: float,
) -> tuple[pd.DataFrame, list[dict], dict | None]:
    """Classify a merged day and return the frame that is safe to store."""
    prospective = _merge_frames(existing, incoming)
    fields = bad_tick_fields(prospective, bad_tick_neighbor_fraction)
    flagged_count = sum(field is not None for field in fields)
    flagged_fraction = flagged_count / len(prospective)
    if flagged_fraction > 0.5:
        return (
            prospective,
            [],
            {
                "symbol": symbol,
                "day": day,
                "reason": (
                    "bad-tick classifier flagged more than half the day's bars "
                    f"-- {flagged_count} of {len(prospective)} positions"
                ),
            },
        )

    quarantined_timestamps: list[pd.Timestamp] = []
    quarantined: list[dict] = []

    for position, timestamp in enumerate(prospective.index):
        offending_field = fields[position]
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

    surviving = prospective.drop(index=quarantined_timestamps)
    return surviving, quarantined, None


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
    validation_errors: list[dict] = []
    for (symbol, day), incoming in incoming_by_day.items():
        existing = store.read_1m_day(data_root, symbol, day)
        merged, day_quarantine, validation_error = _quarantine_day(
            symbol,
            day,
            existing,
            incoming,
            bad_tick_neighbor_fraction,
        )
        store.write_1m_day(data_root, symbol, day, merged)
        daily_bar = _daily_bar(day, merged)
        if daily_bar is not None:
            existing_daily = store.read_1d(data_root, symbol)
            store.write_1d(
                data_root,
                symbol,
                _merge_frames(existing_daily, daily_bar),
            )
        quarantined.extend(day_quarantine)
        if validation_error is not None:
            validation_errors.append(validation_error)

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
    result_summary["validation_errors"] = validation_errors
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
    validation_errors: list[dict] = []
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
        validation_errors.extend(file_summary["validation_errors"])
        warnings.extend(file_summary["etf_warnings"])

        for symbol, summary in file_summary.items():
            if symbol in {
                "quarantined",
                "validation_errors",
                "etf_warnings",
            }:
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
    result["validation_errors"] = validation_errors
    result["etf_warnings"] = warnings
    return result


__all__ = [
    "bad_tick_fields",
    "etf_leverage_warning",
    "ingest_all",
    "ingest_file",
    "is_bad_tick",
    "parse_result_block",
]
