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


def _interior_references(
    frame: pd.DataFrame,
    position: int,
) -> tuple[float, float]:
    left = median(
        float(value)
        for value in frame.iloc[max(0, position - 2) : position]["c"]
    )
    right = median(
        float(value)
        for value in frame.iloc[position + 1 : position + 3]["c"]
    )
    return float(left), float(right)


def _bad_price_field(
    row: pd.Series,
    references: tuple[float, ...],
    fraction: float,
) -> str | None:
    high = float(row["h"])
    if all(_deviates(high, reference, fraction) for reference in references):
        return "high"

    low = float(row["l"])
    if all(_deviates(low, reference, fraction) for reference in references):
        return "low"
    return None


def bad_tick_fields(
    prospective_frame: pd.DataFrame,
    fraction: float,
) -> list[str | None]:
    """Classify anomalous high/low fields across one ordered trading day."""
    frame = prospective_frame
    size = len(frame)
    fields: list[str | None] = [None] * size
    if size < 3:
        return fields

    # Edges use closes as references, so track close trust separately from a
    # row's reportable high/low finding (a bad low does not taint a good close).
    bad_closes: set[int] = set()
    for position in range(1, size - 1):
        references = _interior_references(frame, position)
        row = frame.iloc[position]
        fields[position] = _bad_price_field(row, references, fraction)
        close = float(row["c"])
        if all(
            _deviates(close, reference, fraction) for reference in references
        ):
            bad_closes.add(position)

    edge_searches = (
        (0, range(1, size)),
        (size - 1, range(size - 2, -1, -1)),
    )
    for edge_position, candidates in edge_searches:
        # Skip close outliers found by the interior pass, then require a second
        # inward close to corroborate the reference before checking the edge.
        trustworthy = [
            position for position in candidates if position not in bad_closes
        ]
        if len(trustworthy) < 2:
            continue

        reference = float(frame.iloc[trustworthy[0]]["c"])
        corroborating_close = float(frame.iloc[trustworthy[1]]["c"])
        if _deviates(reference, corroborating_close, fraction):
            continue
        fields[edge_position] = _bad_price_field(
            frame.iloc[edge_position],
            (reference,),
            fraction,
        )

    return fields


def is_bad_tick(
    prospective_frame: pd.DataFrame,
    position: int,
    fraction: float,
) -> str | None:
    """Return one position's result from the per-day bad-tick classifier."""
    if position < 0 or position >= len(prospective_frame):
        return None
    return bad_tick_fields(prospective_frame, fraction)[position]


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
    fields = bad_tick_fields(prospective, bad_tick_neighbor_fraction)

    for position, timestamp in enumerate(prospective.index):
        if timestamp not in incoming_timestamps:
            continue
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
        daily_bar = _daily_bar(day, merged)
        if daily_bar is not None:
            existing_daily = store.read_1d(data_root, symbol)
            store.write_1d(
                data_root,
                symbol,
                _merge_frames(existing_daily, daily_bar),
            )
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
    "bad_tick_fields",
    "etf_leverage_warning",
    "ingest_all",
    "ingest_file",
    "is_bad_tick",
    "parse_result_block",
]
