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

from trader.provider.badticks import classify_bad_ticks
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


def _distance_from_reference(value: float, reference: float) -> float:
    if reference == 0.0:
        return abs(value - reference)
    return abs(value - reference) / abs(reference)


def _contiguous_runs(positions: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for position in positions:
        if not runs or position != runs[-1][-1] + 1:
            runs.append([position])
            continue
        runs[-1].append(position)
    return runs


def _close_reference_for_run(
    frame: pd.DataFrame,
    run: list[int],
    quarantined_positions: set[int],
) -> float:
    start = run[0]
    stop = run[-1] + 1

    left_start = start
    while left_start > 0 and left_start - 1 not in quarantined_positions:
        left_start -= 1
    left = frame.iloc[left_start:start]

    right_stop = stop
    while right_stop < len(frame) and right_stop not in quarantined_positions:
        right_stop += 1
    right = frame.iloc[stop:right_stop]

    references: list[float] = []
    if not left.empty:
        references.append(float(median(left["c"])))
    if not right.empty:
        references.append(float(median(right["c"])))
    if not references:
        return float(frame.iloc[start]["c"])
    return sum(references) / len(references)


def bad_tick_manifest_entries(
    symbol: str,
    day: date,
    merged_frame: pd.DataFrame,
    quarantined_timestamps: list[pd.Timestamp],
) -> list[dict]:
    """Build backward-compatible per-bar quarantine manifest entries."""
    if not quarantined_timestamps:
        return []

    position_by_timestamp = {
        timestamp: position for position, timestamp in enumerate(merged_frame.index)
    }
    quarantined_positions = {
        position_by_timestamp[timestamp]
        for timestamp in quarantined_timestamps
        if timestamp in position_by_timestamp
    }
    entries: list[dict] = []
    for run in _contiguous_runs(sorted(quarantined_positions)):
        reference = _close_reference_for_run(merged_frame, run, quarantined_positions)
        for position in run:
            row = merged_frame.iloc[position]
            high_distance = _distance_from_reference(float(row["h"]), reference)
            low_distance = _distance_from_reference(float(row["l"]), reference)
            field = "high" if high_distance >= low_distance else "low"
            column = "h" if field == "high" else "l"
            entries.append(
                {
                    "timestamp": merged_frame.index[position],
                    "symbol": symbol,
                    "day": day,
                    "field": field,
                    "value": float(row[column]),
                }
            )
    return entries


def _quarantine_day(
    symbol: str,
    day: date,
    existing: pd.DataFrame | None,
    incoming: pd.DataFrame,
    bad_tick_neighbor_fraction: float,
    max_bad_run_bars: int,
    quarantine_abort_fraction: float,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    """Classify a merged day and return the frame that is safe to store."""
    prospective = _merge_frames(existing, incoming)
    quarantined_timestamps, validation_errors = classify_bad_ticks(
        prospective,
        bad_tick_neighbor_fraction=bad_tick_neighbor_fraction,
        max_bad_run_bars=max_bad_run_bars,
        quarantine_abort_fraction=quarantine_abort_fraction,
        symbol=symbol,
        day=day,
    )
    quarantined = bad_tick_manifest_entries(
        symbol,
        day,
        prospective,
        quarantined_timestamps,
    )

    surviving = prospective.drop(index=quarantined_timestamps)
    return surviving, quarantined, validation_errors


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
    max_bad_run_bars: int = 3,
    quarantine_abort_fraction: float = 0.05,
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
        merged, day_quarantine, day_validation_errors = _quarantine_day(
            symbol,
            day,
            existing,
            incoming,
            bad_tick_neighbor_fraction,
            max_bad_run_bars,
            quarantine_abort_fraction,
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
        validation_errors.extend(day_validation_errors)

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
    max_bad_run_bars: int = 3,
    quarantine_abort_fraction: float = 0.05,
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
            max_bad_run_bars=max_bad_run_bars,
            quarantine_abort_fraction=quarantine_abort_fraction,
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
    "bad_tick_manifest_entries",
    "etf_leverage_warning",
    "ingest_all",
    "ingest_file",
    "parse_result_block",
]
