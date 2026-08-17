"""No-lookahead intraday path forecast and whole-session shape signal."""

from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
SHAPES = (
    "trend_up",
    "trend_down",
    "v_recovery",
    "inverted_v",
    "drop_then_flat",
    "rise_then_flat",
    "range",
    "multi_reversal",
)
_PARAMETERS = {
    "history_sessions",
    "min_sessions",
    "stride_minutes",
    "shape_base_rate_w",
    "age_halflife_days",
    "support_k",
}
_CACHE: dict[tuple[str, str, str, str], tuple["_Session", ...]] = {}


@dataclass(frozen=True)
class _Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    interpolated: bool


@dataclass(frozen=True)
class _Session:
    day: date
    duration: int
    bars: tuple[_Bar, ...]
    minutes: tuple[float, ...]
    returns: tuple[float, ...]
    positions: Mapping[float, int]
    quality: float
    context: Mapping[str, float]
    normalized: tuple[float, ...]
    shape: str


def clear_shape_cache() -> None:
    """Drop the immutable per-cycle bar/session index."""
    _CACHE.clear()


def _template(name: str, x: float) -> float:
    if name == "trend_up":
        return x
    if name == "trend_down":
        return -x
    if name == "v_recovery":
        return -x / 0.4 if x <= 0.4 else -1.0 + 1.5 * (x - 0.4) / 0.6
    if name == "inverted_v":
        return x / 0.4 if x <= 0.4 else 1.0 - 1.5 * (x - 0.4) / 0.6
    if name == "drop_then_flat":
        return -min(x / 0.25, 1.0)
    if name == "rise_then_flat":
        return min(x / 0.25, 1.0)
    if name == "multi_reversal":
        return math.sin(3.0 * math.pi * x)
    return 0.0


def classify_shape(
    minutes: Sequence[float],
    returns: Sequence[float],
    duration: int,
    exante_daily_vol: float,
) -> str:
    """Classify one return curve with the exact DT template rules."""
    points = [
        (float(minute), float(value))
        for minute, value in zip(minutes, returns)
        if math.isfinite(float(minute))
        and math.isfinite(float(value))
        and 0.0 <= float(minute) <= float(duration) + 1e-9
    ]
    if len(points) < 2:
        return "range"
    sample_minutes = [point[0] for point in points]
    path = [point[1] for point in points]
    amplitude = max(path) - min(path)
    if amplitude <= max(0.0025, 0.22 * float(exante_daily_vol or 0.0)):
        return "range"
    centered = [value - path[0] for value in path]
    scale = max(max(abs(value) for value in centered), 1e-9)
    normalized = [value / scale for value in centered]
    candidates = [name for name in SHAPES if name != "range"]

    def mse(name: str) -> float:
        errors = [
            (value - _template(name, minute / max(float(duration), 1.0))) ** 2
            for minute, value in zip(sample_minutes, normalized)
        ]
        return sum(errors) / len(errors)

    return min(candidates, key=mse)


def _weighted_quantile(
    values: Sequence[float], weights: Sequence[float], quantile: float
) -> float:
    pairs = sorted(
        (float(value), float(weight))
        for value, weight in zip(values, weights)
        if math.isfinite(float(value))
        and math.isfinite(float(weight))
        and float(weight) > 0.0
    )
    if not pairs:
        return 0.0
    total = sum(weight for _, weight in pairs)
    target = float(quantile)
    previous_fraction = 0.0
    previous_value = pairs[0][0]
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        fraction = cumulative / total
        if target <= fraction:
            if fraction <= previous_fraction + 1e-15:
                return value
            portion = (target - previous_fraction) / (fraction - previous_fraction)
            return previous_value + portion * (value - previous_value)
        previous_fraction = fraction
        previous_value = value
    return pairs[-1][0]


def _at(session: _Session, minute: float, normalized: bool = False) -> float:
    position = session.positions.get(round(float(minute), 6))
    if position is None:
        raise ValueError("session has no bar endpoint at minute %g" % minute)
    values = session.normalized if normalized else session.returns
    return float(values[position])


def _supports(session: _Session, minute: float) -> bool:
    return round(float(minute), 6) in session.positions


def _curve(
    bars: Sequence[_Bar], session_open: int
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if not bars or not math.isfinite(bars[0].open) or bars[0].open <= 0.0:
        return (), ()
    opening = bars[0].open
    points: dict[float, float] = {0.0: 0.0}
    for bar in bars:
        minute = float((bar.ts - session_open) // 60 + 1)
        if minute <= 0.0 or not math.isfinite(bar.close) or bar.close <= 0.0:
            continue
        points.setdefault(minute, math.log(bar.close / opening))
    ordered = sorted(points.items())
    return tuple(point[0] for point in ordered), tuple(point[1] for point in ordered)


def _log_return(left: Optional[float], right: Optional[float]) -> float:
    if left and right and left > 0.0 and right > 0.0:
        return math.log(right / left)
    return 0.0


def _trend(closes: Sequence[float], sessions: int) -> float:
    if len(closes) <= sessions:
        return 0.0
    return _log_return(closes[-sessions - 1], closes[-1])


def _atr(prior: Sequence[_Session], count: int = 14) -> float:
    values: list[float] = []
    previous: Optional[float] = None
    for session in prior:
        if not session.bars:
            continue
        close = session.bars[-1].close
        if previous and previous > 0.0:
            high = max(bar.high for bar in session.bars)
            low = min(bar.low for bar in session.bars)
            values.append(
                max(high - low, abs(high - previous), abs(low - previous))
                / previous
            )
        previous = close
    tail = values[-count:]
    return sum(tail) / len(tail) if tail else 0.0


def _context(day: date, prior: Sequence[_Session], current_open: float) -> dict[str, float]:
    earlier = sorted((session for session in prior if session.day < day), key=lambda s: s.day)
    closes = [
        session.bars[-1].close
        for session in earlier
        if session.bars and session.bars[-1].close > 0.0
    ]
    tail = closes[-21:]
    returns = [
        math.log(right / left)
        for left, right in zip(tail, tail[1:])
        if left > 0.0 and right > 0.0
    ]
    vol20 = (
        math.sqrt(sum(value * value for value in returns) / len(returns))
        * math.sqrt(252.0)
        if returns
        else 0.0
    )
    atr14 = _atr(earlier)
    daily_scale = vol20 / math.sqrt(252.0) if vol20 > 0.0 else atr14
    weekday = float(day.weekday())
    return {
        "gap": _log_return(closes[-1] if closes else None, current_open),
        "trend_1": _trend(closes, 1),
        "trend_5": _trend(closes, 5),
        "trend_20": _trend(closes, 20),
        "vol20_annualized": vol20,
        "daily_atr14": atr14,
        "exante_daily_vol": max(float(daily_scale or 0.0), 0.005),
        "weekday_sin": math.sin(2.0 * math.pi * weekday / 5.0),
        "weekday_cos": math.cos(2.0 * math.pi * weekday / 5.0),
    }


def _history_index(
    connection: sqlite3.Connection,
    ticker: str,
    settings: Any,
    today: date,
    session_open: int,
    session_window: Callable[[Any, int], tuple[date, int, int]],
) -> tuple[_Session, ...]:
    key = (str(settings.database), ticker, settings.content, today.isoformat())
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    rows = connection.execute(
        """
        SELECT ts,open,high,low,close,interpolated
        FROM bars WHERE ticker=? AND ts<? ORDER BY ts
        """,
        (ticker, session_open),
    ).fetchall()
    grouped: dict[date, list[_Bar]] = {}
    for row in rows:
        ts = int(row["ts"])
        day = datetime.fromtimestamp(ts, tz=EASTERN).date()
        grouped.setdefault(day, []).append(
            _Bar(
                ts=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                interpolated=bool(row["interpolated"]),
            )
        )
    sessions: list[_Session] = []
    for day in sorted(grouped):
        probe = grouped[day][0].ts
        _, session_open, session_close = session_window(settings, probe)
        bars = tuple(
            bar for bar in grouped[day] if session_open <= bar.ts < session_close
        )
        if len(bars) < 3:
            continue
        minutes, returns = _curve(bars, session_open)
        if len(minutes) < 2:
            continue
        context = _context(day, sessions, bars[0].open)
        vol = context["exante_daily_vol"]
        duration = int((session_close - session_open) // 60)
        quality = max(
            sum(1 for bar in bars if not bar.interpolated) / len(bars), 0.05
        )
        session = _Session(
            day=day,
            duration=duration,
            bars=bars,
            minutes=minutes,
            returns=returns,
            positions={round(minute, 6): index for index, minute in enumerate(minutes)},
            quality=quality,
            context=context,
            normalized=tuple(value / vol for value in returns),
            shape=classify_shape(minutes, returns, duration, vol),
        )
        sessions.append(session)
    cached = tuple(sessions)
    _CACHE[key] = cached
    return cached


def _prefix(minutes: Sequence[float], values: Sequence[float], elapsed: float) -> dict[float, float]:
    result: dict[float, float] = {}
    for minute, value in zip(minutes, values):
        minute = float(minute)
        if minute <= 0.0 or minute > elapsed + 1e-9:
            continue
        if elapsed >= 5.0 and not math.isclose(
            minute / 5.0, round(minute / 5.0), abs_tol=1e-6, rel_tol=0.0
        ):
            continue
        result[round(minute, 6)] = float(value)
    return result


def _distance(
    current_minutes: Sequence[float],
    current_normalized: Sequence[float],
    candidate: _Session,
    elapsed: float,
) -> float:
    left = _prefix(current_minutes, current_normalized, elapsed)
    right = _prefix(candidate.minutes, candidate.normalized, elapsed)
    common = sorted(set(left).intersection(right))
    if not common:
        prefix_distance = 0.0
    else:
        prefix_distance = math.sqrt(
            sum((left[minute] - right[minute]) ** 2 for minute in common)
            / len(common)
        )
    fraction = min(1.0, elapsed / max(float(candidate.duration), 1.0))
    return (0.25 + 3.0 * fraction) * prefix_distance


def _effective(weights: Sequence[float]) -> float:
    total = sum(weights)
    squared = sum(weight * weight for weight in weights)
    return total * total / squared if total > 0.0 and squared > 0.0 else 0.0


def _normalized(values: Sequence[float]) -> list[float]:
    total = sum(max(float(value), 0.0) for value in values)
    if total <= 0.0:
        return [1.0 / len(values) for _ in values] if values else []
    return [max(float(value), 0.0) / total for value in values]


def _probabilities(values: Mapping[str, float]) -> dict[str, float]:
    normalized = _normalized([values.get(name, 0.0) for name in SHAPES])
    result = {name: normalized[position] for position, name in enumerate(SHAPES)}
    if result:
        result[SHAPES[-1]] += 1.0 - sum(result.values())
    return result


def _parameters(parameters: Mapping[str, Any]) -> tuple[int, int, int, float, float, float]:
    unknown = set(parameters) - _PARAMETERS
    if unknown:
        raise ValueError("unknown shape params: %s" % ", ".join(sorted(unknown)))

    def integer(name: str, default: int, minimum: int) -> int:
        value = parameters.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError("%s must be an integer >= %d" % (name, minimum))
        return value

    def number(name: str, default: float, minimum: float) -> float:
        value = parameters.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("%s must be a number" % name)
        value = float(value)
        if not math.isfinite(value) or value < minimum:
            raise ValueError("%s must be >= %g" % (name, minimum))
        return value

    history_sessions = integer("history_sessions", 120, 1)
    min_sessions = integer("min_sessions", 8, 1)
    stride_minutes = integer("stride_minutes", 5, 1)
    shape_base_rate_w = number("shape_base_rate_w", 0.1, 0.0)
    if shape_base_rate_w > 1.0:
        raise ValueError("shape_base_rate_w must be <= 1")
    age_halflife_days = number("age_halflife_days", 365.0, 1e-9)
    support_k = number("support_k", 8.0, 1e-9)
    return (
        history_sessions,
        min_sessions,
        stride_minutes,
        shape_base_rate_w,
        age_halflife_days,
        support_k,
    )


def _preopen_shape(
    candidates: Sequence[_Session],
    today: date,
    duration: int,
    shape_base_rate_w: float,
    age_halflife_days: float,
    support_k: float,
) -> dict[str, Any]:
    raw_weights = [
        candidate.quality
        * math.exp(-float((today - candidate.day).days) / age_halflife_days)
        for candidate in candidates
    ]
    effective_n = _effective(raw_weights)
    support = effective_n / (effective_n + support_k)
    analog = _normalized(raw_weights)
    prior = _normalized([candidate.quality for candidate in candidates])
    weights = [
        support * analog_weight + (1.0 - support) * prior_weight
        for analog_weight, prior_weight in zip(analog, prior)
    ]
    votes = {
        name: sum(
            weight
            for candidate, weight in zip(candidates, weights)
            if candidate.shape == name
        )
        for name in SHAPES
    }
    completed = _probabilities(votes)
    counts = {
        name: sum(candidate.shape == name for candidate in candidates)
        for name in SHAPES
    }
    base_rate = _probabilities(counts)
    probabilities = _probabilities(
        {
            name: (1.0 - shape_base_rate_w) * completed[name]
            + shape_base_rate_w * base_rate[name]
            for name in SHAPES
        }
    )
    top = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)[:3]
    quality_mean = sum(
        weight * candidate.quality for candidate, weight in zip(candidates, weights)
    )
    return {
        "session_minute": None,
        "remaining_minutes": duration,
        "session_open": None,
        "current_return": None,
        "funnel": [],
        "shapes": probabilities,
        "top_shapes": [
            {"shape": name, "probability": probability}
            for name, probability in top
        ],
        "reliability": support,
        "evidence": {
            "analogs": len(candidates),
            "effective_n": effective_n,
            "bandwidth": None,
            "shrinkage": support,
            "quality_mean": quality_mean,
            "dropped_weight": 0.0,
            "shape_base_rate_w": shape_base_rate_w,
            "shape_method": "preopen_prior",
            "proximity": "not_measured",
        },
    }


def _shape_v1(
    connection: sqlite3.Connection,
    ticker: str,
    ts: int,
    parameters: Mapping[str, Any],
    inputs: Mapping[str, Any],
    settings: Any,
    session_window: Callable[[Any, int], tuple[date, int, int]],
) -> Optional[dict[str, Any]]:
    if list(inputs) != ["bars"]:
        raise ValueError("shape_v1 inputs must be ['bars']")
    (
        history_sessions,
        min_sessions,
        stride_minutes,
        shape_base_rate_w,
        age_halflife_days,
        support_k,
    ) = _parameters(parameters)
    today, session_open, session_close = session_window(settings, ts)
    if ts >= session_close:
        return None
    duration = int((session_close - session_open) // 60)

    prior_all = list(
        _history_index(
            connection,
            ticker,
            settings,
            today,
            session_open,
            session_window,
        )
    )
    candidates = prior_all[-history_sessions:]
    if len(candidates) < min_sessions:
        return None
    if ts < session_open:
        return _preopen_shape(
            candidates,
            today,
            duration,
            shape_base_rate_w,
            age_halflife_days,
            support_k,
        )

    session_minute = int((ts - session_open) // 60 + 1)
    if session_minute % stride_minutes != 0:
        return None
    remaining = duration - session_minute
    if remaining <= 0:
        return None

    rows = connection.execute(
        """
        SELECT ts,open,high,low,close,interpolated
        FROM bars
        WHERE ticker=? AND ts>=? AND ts<=? AND ts<?
        ORDER BY ts
        """,
        (ticker, session_open, ts, session_close),
    ).fetchall()
    observed_bars = tuple(
        _Bar(
            ts=int(row["ts"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            interpolated=bool(row["interpolated"]),
        )
        for row in rows
    )
    if len(observed_bars) < 2:
        return None
    observed_minutes, observed_returns = _curve(observed_bars, session_open)
    if len(observed_minutes) < 3:
        return None
    elapsed = float(observed_minutes[-1])
    if not math.isclose(
        elapsed, float(session_minute), abs_tol=1e-6, rel_tol=0.0
    ):
        return None

    current_return = float(observed_returns[-1])
    current_context = _context(today, prior_all, observed_bars[0].open)
    current_vol = current_context["exante_daily_vol"]
    current_normalized = tuple(value / current_vol for value in observed_returns)
    distances = [
        _distance(observed_minutes, current_normalized, candidate, elapsed)
        for candidate in candidates
    ]
    ranked = sorted(distances)
    position = min(len(ranked) - 1, max(0, int(math.sqrt(len(ranked))) - 1))
    bandwidth = max(ranked[position], statistics.median(ranked) * 0.35, 1e-6)
    raw_weights = [
        candidate.quality
        * math.exp(-0.5 * (distance / bandwidth) ** 2)
        * math.exp(-float((today - candidate.day).days) / age_halflife_days)
        for candidate, distance in zip(candidates, distances)
    ]
    effective_n = _effective(raw_weights)
    support = effective_n / (effective_n + support_k)
    analog = _normalized(raw_weights)
    prior = _normalized([candidate.quality for candidate in candidates])
    weights = [
        support * analog_weight + (1.0 - support) * prior_weight
        for analog_weight, prior_weight in zip(analog, prior)
    ]

    funnel: list[dict[str, float | int]] = []
    dropped: list[float] = []
    horizons = list(range(5, remaining + 1, 5))
    if not horizons or horizons[-1] != remaining:
        horizons.append(remaining)
    opening = observed_bars[0].open
    for horizon in horizons:
        positions = [
            index
            for index, candidate in enumerate(candidates)
            if _supports(candidate, elapsed)
            and _supports(candidate, elapsed + float(horizon))
        ]
        kept_weights = [weights[index] for index in positions]
        dropped.append(max(0.0, 1.0 - sum(kept_weights)))
        if positions:
            continuations = [
                _at(candidates[index], elapsed + float(horizon))
                - _at(candidates[index], elapsed)
                for index in positions
            ]
            center = _weighted_quantile(continuations, kept_weights, 0.5)
            values = [current_return + value - center for value in continuations]
            quantiles = {
                name: _weighted_quantile(values, kept_weights, quantile)
                for name, quantile in (
                    ("q10", 0.10),
                    ("q25", 0.25),
                    ("median", 0.50),
                    ("q75", 0.75),
                    ("q90", 0.90),
                )
            }
        else:
            quantiles = {
                "q10": current_return,
                "q25": current_return,
                "median": current_return,
                "q75": current_return,
                "q90": current_return,
            }
        funnel.append(
            {
                "minute": session_minute + horizon,
                **quantiles,
                "price_q10": opening * math.exp(quantiles["q10"]),
                "price_q25": opening * math.exp(quantiles["q25"]),
                "price_median": opening * math.exp(quantiles["median"]),
                "price_q75": opening * math.exp(quantiles["q75"]),
                "price_q90": opening * math.exp(quantiles["q90"]),
            }
        )

    votes = {name: 0.0 for name in SHAPES}
    kept_shape = 0.0
    dropped_shape = 0.0
    for candidate, weight in zip(candidates, weights):
        if weight <= 0.0 or not _supports(candidate, elapsed):
            dropped_shape += max(weight, 0.0)
            continue
        anchor = _at(candidate, elapsed, normalized=True)
        tail = [
            (minute, value)
            for minute, value in zip(candidate.minutes, candidate.normalized)
            if elapsed < minute <= duration
        ]
        if not tail:
            dropped_shape += weight
            continue
        completed_minutes = list(observed_minutes) + [minute for minute, _ in tail]
        completed_returns = list(observed_returns) + [
            current_return + (value - anchor) * current_vol for _, value in tail
        ]
        label = classify_shape(
            completed_minutes, completed_returns, duration, current_vol
        )
        votes[label] += weight
        kept_shape += weight
    shape_method = "prefix_completed"
    if kept_shape > 0.0:
        completed = _probabilities(votes)
        counts = {name: 0.0 for name in SHAPES}
        for candidate in candidates:
            counts[candidate.shape] += 1.0
        base_rate = _probabilities(counts)
        probabilities = _probabilities(
            {
                name: (1.0 - shape_base_rate_w) * completed[name]
                + shape_base_rate_w * base_rate[name]
                for name in SHAPES
            }
        )
    else:
        shape_method = "analog_label"
        probabilities = _probabilities(
            {
                name: sum(
                    weight
                    for candidate, weight in zip(candidates, weights)
                    if candidate.shape == name
                )
                for name in SHAPES
            }
        )
    dropped.append(
        dropped_shape / (kept_shape + dropped_shape)
        if kept_shape + dropped_shape > 0.0
        else 0.0
    )
    top = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)[:3]
    quality_mean = sum(
        weight * candidate.quality for candidate, weight in zip(candidates, weights)
    )
    return {
        "session_minute": session_minute,
        "remaining_minutes": remaining,
        "session_open": opening,
        "current_return": current_return,
        "funnel": funnel,
        "shapes": probabilities,
        "top_shapes": [
            {"shape": name, "probability": probability}
            for name, probability in top
        ],
        "reliability": support,
        "evidence": {
            "analogs": len(candidates),
            "effective_n": effective_n,
            "bandwidth": bandwidth,
            "shrinkage": support,
            "quality_mean": quality_mean,
            "dropped_weight": sum(dropped) / len(dropped) if dropped else 0.0,
            "shape_base_rate_w": shape_base_rate_w,
            "shape_method": shape_method,
            "proximity": "not_measured",
        },
    }


def shape_v1(
    connection: sqlite3.Connection,
    ticker: str,
    ts: int,
    parameters: Mapping[str, Any],
    inputs: Mapping[str, Any],
    settings: Any,
    session_window: Callable[[Any, int], tuple[date, int, int]],
) -> Optional[dict[str, Any]]:
    """Return the shape forecast, or null for unusable snapshots."""
    try:
        return _shape_v1(
            connection,
            ticker,
            ts,
            parameters,
            inputs,
            settings,
            session_window,
        )
    except Exception:
        return None
