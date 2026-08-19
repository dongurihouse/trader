"""Signal calculations and their registry."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import date, datetime, time as clock_time, timedelta
from typing import Any, Iterator, Mapping, Optional, Sequence

from algo_types import (
    ConfigError,
    EASTERN,
    EvaluationError,
    Settings,
    SignalSpec,
    _no_parameters,
    _parameter_bool,
    _parameter_int,
    _parameter_keys,
    _parameter_number,
    _session_window,
)
from common.validation import require_float
from shape_signal import normalize_shape_parameters, shape_v1
from storage import (
    _bars,
    _exact_bar,
    _latest_close,
    _session_bars,
)

BAR_FIELDS = {"open", "high", "low", "close", "volume"}
_RVOL_BASELINES: dict[
    tuple[str, str, date, int, int], Optional[tuple[float, ...]]
] = {}
_SESSION_SUMMARIES: dict[
    tuple[str, str, date], Optional[Mapping[str, float]]
] = {}
_ATR_SESSION_VALUES: dict[tuple[str, str, date, int], Optional[float]] = {}
_RELATIVE_MOMENTUM_SESSION_FEATURES: dict[
    tuple[str, str, date, int, float],
    Optional[tuple[Optional[Mapping[str, float]], ...]],
] = {}
_RELATIVE_MOMENTUM_BASELINES: dict[
    tuple[str, str, date, int, int, int, float, int],
    Optional[Mapping[int, tuple[tuple[float, ...], tuple[float, ...]]]],
] = {}



def clear_signal_caches() -> None:
    """Clear signal data cached for one service cycle."""
    _RVOL_BASELINES.clear()
    _SESSION_SUMMARIES.clear()
    _ATR_SESSION_VALUES.clear()
    _RELATIVE_MOMENTUM_SESSION_FEATURES.clear()
    _RELATIVE_MOMENTUM_BASELINES.clear()


def _rows_form_uniform_grid(
    rows: Sequence[Mapping[str, Any]],
    start: int,
    end_exclusive: int,
    accepted_cadences: Sequence[int],
) -> bool:
    duration = end_exclusive - start
    if not rows or duration <= 0:
        return False
    timestamps = [int(row["ts"]) for row in rows]
    return any(
        cadence > 0
        and duration % cadence == 0
        and len(timestamps) == duration // cadence
        and all(
            timestamp == start + index * cadence
            for index, timestamp in enumerate(timestamps)
        )
        for cadence in accepted_cadences
    )


def _normalize_sma(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    _parameter_keys(parameters, {"field", "period", "include_interpolated"})
    field = parameters.get("field")
    if field not in BAR_FIELDS:
        raise ConfigError("field must name a stored bar field")
    return {
        "field": field,
        "period": _parameter_int(parameters, "period"),
        "include_interpolated": _parameter_bool(
            parameters, "include_interpolated"
        ),
    }



def _normalize_int_parameter(
    parameters: Mapping[str, Any], name: str, minimum: int = 1
) -> Mapping[str, Any]:
    _parameter_keys(parameters, {name})
    return {name: _parameter_int(parameters, name, minimum)}



def _normalize_last_close(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    _parameter_keys(parameters, {"include_interpolated"})
    return {
        "include_interpolated": _parameter_bool(
            parameters, "include_interpolated"
        )
    }



def _normalize_rvol(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    _parameter_keys(parameters, {"cap_bars", "baseline_sessions"})
    return {
        "cap_bars": _parameter_int(parameters, "cap_bars"),
        "baseline_sessions": _parameter_int(parameters, "baseline_sessions"),
    }



def _normalize_relative_momentum(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    _parameter_keys(
        parameters,
        {
            "window_minutes",
            "baseline_sessions",
            "min_sessions",
            "time_tolerance_minutes",
            "min_momentum_pct",
            "strong_percentile",
        },
    )
    result = {
        "window_minutes": _parameter_int(parameters, "window_minutes"),
        "baseline_sessions": _parameter_int(parameters, "baseline_sessions"),
        "min_sessions": _parameter_int(parameters, "min_sessions"),
        "time_tolerance_minutes": _parameter_int(
            parameters, "time_tolerance_minutes", minimum=0
        ),
        "min_momentum_pct": _parameter_number(parameters, "min_momentum_pct"),
        "strong_percentile": _parameter_number(parameters, "strong_percentile"),
    }
    if result["min_sessions"] > result["baseline_sessions"]:
        raise ConfigError("min_sessions cannot exceed baseline_sessions")
    if result["strong_percentile"] > 100.0:
        raise ConfigError("strong_percentile must be <= 100")
    return result




def _normalize_shape(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    return normalize_shape_parameters(parameters, ConfigError)



def _signal_sma(
    connection, ticker, ts, parameters, inputs, settings=None
) -> Optional[float]:
    field = str(parameters["field"])
    period = int(parameters["period"])
    include = bool(parameters["include_interpolated"])
    rows = _bars(connection, ticker, ts, period, include)
    if len(rows) < period:
        return None
    return sum(float(row[field]) for row in rows) / period



def _signal_session(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[dict[str, Any]]:
    local_day, session_open, session_close = _session_window(settings, ts)
    if ts < session_open or ts >= session_close:
        return None
    return {
        "date": local_day.isoformat(),
        "minute": int((ts - session_open) // 60),
        "to_close": (session_close - ts) / 60.0,
        "total": int((session_close - session_open) // 60),
        "ts": ts,
        "open_ts": session_open,
    }



def _complete_session_summary(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    local_day: date,
) -> Optional[Mapping[str, float]]:
    key = (str(settings.database), ticker, local_day)
    if key in _SESSION_SUMMARIES:
        return _SESSION_SUMMARIES[key]
    probe = int(
        datetime.combine(local_day, clock_time(12), tzinfo=EASTERN).timestamp()
    )
    _, session_open, session_close = _session_window(settings, probe)
    rows = _session_bars(
        connection,
        ticker,
        session_open,
        session_close,
        session_close - 1,
        include_interpolated=False,
    )
    if not _rows_form_uniform_grid(
        rows, session_open, session_close, (60, 120, 300)
    ):
        _SESSION_SUMMARIES[key] = None
        return None
    summary: Mapping[str, float] = {
        "open": float(rows[0]["open"]),
        "high": max(float(row["high"]) for row in rows),
        "low": min(float(row["low"]) for row in rows),
        "close": float(rows[-1]["close"]),
    }
    _SESSION_SUMMARIES[key] = summary
    return summary



def _prior_complete_session_summaries(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    local_day: date,
    count: int,
) -> list[Mapping[str, float]]:
    summaries: list[Mapping[str, float]] = []
    for candidate in _prior_days(local_day, count):
        summary = _complete_session_summary(
            connection, settings, ticker, candidate
        )
        if summary is not None:
            summaries.append(summary)
            if len(summaries) == count:
                break
    return summaries


def _prior_days(local_day: date, count: int) -> Iterator[date]:
    for offset in range(1, count * 4 + 61):
        yield local_day - timedelta(days=offset)



def _signal_atr_session(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[float]:
    sessions = int(parameters["sessions"])
    session = inputs["session"]
    if session is None:
        return None
    local_day = date.fromisoformat(session["date"])
    key = (str(settings.database), ticker, local_day, sessions)
    if key in _ATR_SESSION_VALUES:
        return _ATR_SESSION_VALUES[key]
    summaries = _prior_complete_session_summaries(
        connection, settings, ticker, local_day, sessions + 1
    )
    if len(summaries) < sessions + 1:
        _ATR_SESSION_VALUES[key] = None
        return None
    ordered = list(reversed(summaries))
    ranges: list[float] = []
    for previous, current in zip(ordered, ordered[1:]):
        previous_close = float(previous["close"])
        if previous_close <= 0.0:
            _ATR_SESSION_VALUES[key] = None
            return None
        true_range = max(
            float(current["high"]) - float(current["low"]),
            abs(float(current["high"]) - previous_close),
            abs(float(current["low"]) - previous_close),
        )
        ranges.append(true_range / previous_close)
    value = float(statistics.mean(ranges))
    _ATR_SESSION_VALUES[key] = value
    return value



def _signal_prior_session(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[dict[str, float]]:
    session = inputs["session"]
    if session is None:
        return None
    local_day = date.fromisoformat(session["date"])
    summaries = _prior_complete_session_summaries(
        connection, settings, ticker, local_day, 1
    )
    if not summaries:
        return None
    opening = _exact_bar(connection, ticker, int(session["open_ts"]))
    current = _exact_bar(connection, ticker, ts)
    if opening is None or current is None:
        return None
    previous = summaries[0]
    previous_close = float(previous["close"])
    if previous_close <= 0.0:
        return None
    session_open = float(opening["open"])
    price = float(current["close"])
    previous_high = float(previous["high"])
    previous_low = float(previous["low"])
    return {
        "prev_close": previous_close,
        "prev_high": previous_high,
        "prev_low": previous_low,
        "gap_pct": (session_open / previous_close - 1.0) * 100.0,
        "open_vs_prior_high": session_open - previous_high,
        "open_vs_prior_low": session_open - previous_low,
        "price_vs_prior_high": price - previous_high,
        "price_vs_prior_low": price - previous_low,
    }



def _signal_first30_ret(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[float]:
    bars = int(parameters["bars"])
    session = inputs["session"]
    if session is None or int(session["minute"]) < bars - 1:
        return None
    local_day = date.fromisoformat(session["date"])
    summaries = _prior_complete_session_summaries(
        connection, settings, ticker, local_day, 1
    )
    if not summaries or float(summaries[0]["close"]) <= 0.0:
        return None
    closing = _exact_bar(
        connection, ticker, int(session["open_ts"]) + (bars - 1) * 60
    )
    if closing is None:
        return None
    return (
        float(closing["close"]) / float(summaries[0]["close"]) - 1.0
    ) * 100.0



def _signal_session_extremes(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[dict[str, Any]]:
    session = inputs["session"]
    atr_session = require_float(
        inputs["atr_session"],
        "atr_session",
        nullable=True,
        error=EvaluationError,
    )
    if session is None or atr_session is None:
        return None
    rows = _session_bars(
        connection,
        ticker,
        int(session["open_ts"]),
        int(session["open_ts"]) + int(session["total"]) * 60,
        ts,
        include_interpolated=False,
    )
    if not rows or int(rows[-1]["ts"]) != ts:
        return None
    current = rows[-1]
    day_high = max(float(row["high"]) for row in rows)
    day_low = min(float(row["low"]) for row in rows)
    price = float(current["close"])
    denominator = float(atr_session) * price
    return {
        "day_high": day_high,
        "day_low": day_low,
        "new_day_high": float(current["high"]) >= day_high,
        "new_day_low": float(current["low"]) <= day_low,
        "day_range_atr": (day_high - day_low) / denominator
        if denominator > 0.0
        else 0.0,
    }



def _signal_opening_range(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[dict[str, float]]:
    minutes = int(parameters["minutes"])
    session = inputs["session"]
    if session is None or session.get("minute", -1) < minutes:
        return None
    _, session_open, session_close = _session_window(settings, ts)
    rows = _session_bars(
        connection,
        ticker,
        session_open,
        session_close,
        ts,
        limit=minutes,
        include_interpolated=False,
    )
    if len(rows) < minutes:
        return None
    high = max(float(row["high"]) for row in rows)
    low = min(float(row["low"]) for row in rows)
    return {"high": high, "low": low, "range": high - low}



def _prior_volume_baseline(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    local_day: date,
    cap_bars: int,
    baseline_sessions: int,
) -> Optional[tuple[float, ...]]:
    key = (str(settings.database), ticker, local_day, cap_bars, baseline_sessions)
    if key in _RVOL_BASELINES:
        return _RVOL_BASELINES[key]
    sessions: list[list[float]] = []
    for candidate in _prior_days(local_day, baseline_sessions):
        probe = int(
            datetime.combine(candidate, clock_time(12), tzinfo=EASTERN).timestamp()
        )
        _, session_open, session_close = _session_window(settings, probe)
        rows = _session_bars(
            connection,
            ticker,
            session_open,
            session_close,
            session_close - 1,
            limit=cap_bars,
            include_interpolated=False,
        )
        if _rows_form_uniform_grid(
            rows, session_open, session_open + cap_bars * 60, (60,)
        ):
            sessions.append([float(row["volume"]) for row in rows])
            if len(sessions) == baseline_sessions:
                break
    if len(sessions) < baseline_sessions:
        _RVOL_BASELINES[key] = None
        return None
    baseline = tuple(
        float(statistics.median(session[slot] for session in sessions))
        for slot in range(cap_bars)
    )
    _RVOL_BASELINES[key] = baseline
    return baseline



def _signal_rvol_open(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[float]:
    cap_bars = int(parameters["cap_bars"])
    baseline_sessions = int(parameters["baseline_sessions"])
    session = inputs["session"]
    if session is None:
        return None
    local_day, session_open, session_close = _session_window(settings, ts)
    rows = _session_bars(
        connection,
        ticker,
        session_open,
        session_close,
        ts,
        limit=cap_bars,
        include_interpolated=False,
    )
    if not rows:
        return None
    baseline = _prior_volume_baseline(
        connection, settings, ticker, local_day, cap_bars, baseline_sessions
    )
    if baseline is None or len(baseline) < len(rows):
        return None
    actual = sum(float(row["volume"]) for row in rows)
    expected = sum(baseline[: len(rows)]) or 1.0
    return actual / expected



def _relative_momentum_features(
    rows: Sequence[Mapping[str, Any]],
    window_minutes: int,
    min_momentum_pct: float,
) -> tuple[Optional[Mapping[str, float]], ...]:
    if not rows:
        return ()
    prices = [float(rows[0]["open"])] + [float(row["close"]) for row in rows]
    features: list[Optional[Mapping[str, float]]] = []
    previous_direction = 0
    run_length = 0
    for index, row in enumerate(rows):
        elapsed = index + 1
        effective_window = min(window_minutes, elapsed)
        reference = prices[elapsed - effective_window]
        price = float(row["close"])
        if reference <= 0.0 or price <= 0.0:
            features.append(None)
            previous_direction = 0
            run_length = 0
            continue
        momentum_pct = (price / reference - 1.0) * 100.0
        direction = _momentum_direction(momentum_pct, min_momentum_pct)
        if direction == 0:
            run_length = 0
        elif direction == previous_direction:
            run_length += 1
        else:
            run_length = 1
        previous_direction = direction
        duration = (
            min(elapsed, effective_window + run_length - 1) if direction else 0
        )
        if direction:
            start = elapsed - duration
            trend_move_pct = (price / prices[start] - 1.0) * 100.0
            path = sum(
                abs(prices[position] - prices[position - 1])
                for position in range(start + 1, elapsed + 1)
            )
            efficiency = abs(price - prices[start]) / path if path else 0.0
        else:
            trend_move_pct = 0.0
            efficiency = 0.0
        features.append(
            {
                "direction": float(direction),
                "momentum_pct": momentum_pct,
                "magnitude_pct": abs(momentum_pct),
                "duration_minutes": float(duration),
                "trend_move_pct": trend_move_pct,
                "efficiency": efficiency,
            }
        )
    return tuple(features)



def _momentum_direction(momentum_pct: float, minimum_pct: float) -> int:
    if momentum_pct >= minimum_pct:
        return 1
    if momentum_pct <= -minimum_pct:
        return -1
    return 0



def _momentum_direction_alignment(
    rows: Sequence[Mapping[str, Any]],
    direction: int,
    windows: Sequence[int],
    min_momentum_pct: float,
) -> Optional[float]:
    if not rows:
        return None
    if direction == 0:
        return 0.0
    prices = [float(rows[0]["open"])] + [float(row["close"]) for row in rows]
    elapsed = len(rows)
    aligned = 0
    for window in windows:
        reference = prices[elapsed - window]
        price = prices[elapsed]
        if reference <= 0.0 or price <= 0.0:
            return None
        momentum_pct = (price / reference - 1.0) * 100.0
        aligned += _momentum_direction(momentum_pct, min_momentum_pct) == direction
    return aligned / len(windows)



def _momentum_persistence_score(
    magnitude_percentile: float,
    duration_percentile: float,
    direction_alignment: Optional[float],
) -> Optional[float]:
    if direction_alignment is None:
        return None
    joint_strength = math.sqrt(magnitude_percentile * duration_percentile)
    return 0.8 * joint_strength + 0.2 * direction_alignment * 100.0



def _complete_relative_momentum_session(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    local_day: date,
    window_minutes: int,
    min_momentum_pct: float,
) -> Optional[tuple[Optional[Mapping[str, float]], ...]]:
    key = (
        str(settings.database),
        ticker,
        local_day,
        window_minutes,
        min_momentum_pct,
    )
    if key in _RELATIVE_MOMENTUM_SESSION_FEATURES:
        return _RELATIVE_MOMENTUM_SESSION_FEATURES[key]
    probe = int(
        datetime.combine(local_day, clock_time(12), tzinfo=EASTERN).timestamp()
    )
    _, session_open, session_close = _session_window(settings, probe)
    rows = _session_bars(
        connection,
        ticker,
        session_open,
        session_close,
        session_close - 1,
        include_interpolated=False,
    )
    if not _rows_form_uniform_grid(
        rows, session_open, session_close, (60,)
    ):
        result = None
    else:
        result = _relative_momentum_features(
            rows, window_minutes, min_momentum_pct
        )
    _RELATIVE_MOMENTUM_SESSION_FEATURES[key] = result
    return result



def _relative_momentum_baseline(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    local_day: date,
    window_minutes: int,
    baseline_sessions: int,
    time_tolerance_minutes: int,
    min_momentum_pct: float,
    target_minutes: int,
) -> Optional[Mapping[int, tuple[tuple[float, ...], tuple[float, ...]]]]:
    key = (
        str(settings.database),
        ticker,
        local_day,
        window_minutes,
        baseline_sessions,
        time_tolerance_minutes,
        min_momentum_pct,
        target_minutes,
    )
    if key in _RELATIVE_MOMENTUM_BASELINES:
        return _RELATIVE_MOMENTUM_BASELINES[key]
    sessions: list[tuple[Optional[Mapping[str, float]], ...]] = []
    for candidate in _prior_days(local_day, baseline_sessions):
        features = _complete_relative_momentum_session(
            connection,
            settings,
            ticker,
            candidate,
            window_minutes,
            min_momentum_pct,
        )
        if features is not None and len(features) >= target_minutes:
            sessions.append(features)
            if len(sessions) == baseline_sessions:
                break
    if not sessions:
        _RELATIVE_MOMENTUM_BASELINES[key] = None
        return None

    curve: dict[int, tuple[tuple[float, ...], tuple[float, ...]]] = {}
    for minute in range(target_minutes):
        magnitudes: list[float] = []
        durations: list[float] = []
        start = max(0, minute - time_tolerance_minutes)
        end = min(target_minutes, minute + time_tolerance_minutes + 1)
        for features in sessions:
            nearby = [feature for feature in features[start:end] if feature is not None]
            if not nearby:
                continue
            magnitudes.append(
                float(statistics.median(feature["magnitude_pct"] for feature in nearby))
            )
            durations.append(
                float(
                    statistics.median(
                        feature["duration_minutes"] for feature in nearby
                    )
                )
            )
        curve[minute] = (tuple(magnitudes), tuple(durations))
    _RELATIVE_MOMENTUM_BASELINES[key] = curve
    return curve



def _midrank_percentile(value: float, samples: Sequence[float]) -> float:
    lower = sum(sample < value for sample in samples)
    equal = sum(sample == value for sample in samples)
    return (lower + equal * 0.5) / len(samples) * 100.0



def _signal_relative_momentum(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[dict[str, Any]]:
    session = inputs["session"]
    if session is None:
        return None
    window_minutes = int(parameters["window_minutes"])
    session_minute = int(session["minute"])
    session_open = int(session["open_ts"])
    rows = _session_bars(
        connection,
        ticker,
        session_open,
        session_open + int(session["total"]) * 60,
        ts,
        include_interpolated=False,
    )
    if not _rows_form_uniform_grid(
        rows,
        session_open,
        session_open + (session_minute + 1) * 60,
        (60,),
    ):
        return None
    current = _relative_momentum_features(
        rows, window_minutes, float(parameters["min_momentum_pct"])
    )[-1]
    if current is None:
        return None
    baseline = _relative_momentum_baseline(
        connection,
        settings,
        ticker,
        date.fromisoformat(session["date"]),
        window_minutes,
        int(parameters["baseline_sessions"]),
        int(parameters["time_tolerance_minutes"]),
        float(parameters["min_momentum_pct"]),
        int(session["total"]),
    )
    if baseline is None:
        return None
    magnitudes, durations = baseline.get(session_minute, ((), ()))
    minimum = int(parameters["min_sessions"])
    if len(magnitudes) < minimum or len(durations) < minimum:
        return None
    magnitude_percentile = _midrank_percentile(
        float(current["magnitude_pct"]), magnitudes
    )
    duration_percentile = _midrank_percentile(
        float(current["duration_minutes"]), durations
    )
    strength_percentile = max(magnitude_percentile, duration_percentile)
    difference = abs(magnitude_percentile - duration_percentile)
    basis = (
        "both"
        if difference < 0.000001
        else "magnitude"
        if magnitude_percentile > duration_percentile
        else "duration"
    )
    direction_value = int(current["direction"])
    direction = (
        "up" if direction_value > 0 else "down" if direction_value < 0 else "neutral"
    )
    alignment_windows = (
        max(1, window_minutes // 2),
        window_minutes * 2,
        window_minutes * 3,
    )
    alignment_windows = tuple(
        sorted({min(window, len(rows)) for window in alignment_windows})
    )
    direction_alignment = _momentum_direction_alignment(
        rows,
        direction_value,
        alignment_windows,
        float(parameters["min_momentum_pct"]),
    )
    persistence_score = (
        0.0
        if direction_value == 0
        else _momentum_persistence_score(
            magnitude_percentile,
            duration_percentile,
            direction_alignment,
        )
    )
    strong = (
        direction_value != 0
        and strength_percentile >= float(parameters["strong_percentile"])
    )
    persistent = (
        direction_value != 0
        and persistence_score is not None
        and persistence_score >= float(parameters["strong_percentile"])
    )
    return {
        "direction": direction,
        "direction_value": direction_value,
        "momentum_pct": round(float(current["momentum_pct"]), 6),
        "magnitude_pct": round(float(current["magnitude_pct"]), 6),
        "trend_duration_minutes": int(current["duration_minutes"]),
        "trend_move_pct": round(float(current["trend_move_pct"]), 6),
        "efficiency": round(float(current["efficiency"]), 6),
        "magnitude_percentile": round(magnitude_percentile, 3),
        "duration_percentile": round(duration_percentile, 3),
        "strength_percentile": round(strength_percentile, 3),
        "signed_strength": round(
            direction_value * strength_percentile / 100.0, 6
        ),
        "direction_alignment": round(direction_alignment, 6)
        if direction_alignment is not None
        else None,
        "persistence_score": round(persistence_score, 3)
        if persistence_score is not None
        else None,
        "signed_persistence": round(
            direction_value * persistence_score / 100.0, 6
        )
        if persistence_score is not None
        else None,
        "persistent": persistent,
        "alignment_windows": list(alignment_windows),
        "strength_basis": basis,
        "strong": strong,
        "sample_sessions": len(magnitudes),
        "session_minute": session_minute,
        "window_minutes": window_minutes,
    }



def _signal_last_close(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[float]:
    return _latest_close(
        connection, ticker, ts, bool(parameters["include_interpolated"])
    )




def _signal_shape_v1(connection, ticker, ts, parameters, inputs, settings) -> Any:
    return shape_v1(
        connection,
        ticker,
        ts,
        parameters,
        inputs,
        settings,
        _session_window,
        _session_bars,
    )


SIGNAL_FUNCTIONS: dict[str, SignalSpec] = {
    "sma": SignalSpec(_signal_sma, ("bars",), _normalize_sma),
    "session": SignalSpec(_signal_session, (), _no_parameters),
    "atr_session": SignalSpec(
        _signal_atr_session,
        ("bars", "session"),
        lambda parameters, tickers: _normalize_int_parameter(
            parameters, "sessions"
        ),
    ),
    "prior_session": SignalSpec(
        _signal_prior_session, ("bars", "session"), _no_parameters
    ),
    "first30_ret": SignalSpec(
        _signal_first30_ret,
        ("bars", "session"),
        lambda parameters, tickers: _normalize_int_parameter(parameters, "bars"),
    ),
    "session_extremes": SignalSpec(
        _signal_session_extremes,
        ("bars", "session", "atr_session"),
        _no_parameters,
    ),
    "opening_range": SignalSpec(
        _signal_opening_range,
        ("bars", "session"),
        lambda parameters, tickers: _normalize_int_parameter(parameters, "minutes"),
    ),
    "rvol_open": SignalSpec(
        _signal_rvol_open, ("bars", "session"), _normalize_rvol
    ),
    "relative_momentum": SignalSpec(
        _signal_relative_momentum,
        ("bars", "session"),
        _normalize_relative_momentum,
    ),
    "last_close": SignalSpec(
        _signal_last_close, ("bars",), _normalize_last_close
    ),
    "shape_v1": SignalSpec(_signal_shape_v1, ("bars",), _normalize_shape),
}
