"""Trading strategy calculations and their registry."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from algo_types import (
    AlgoContext,
    AlgoSpec,
    ConfigError,
    EvaluationError,
    _no_parameters,
    _parameter_bool,
    _parameter_int,
    _parameter_keys,
    _parameter_number,
)
from common.validation import require_float, require_int


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise EvaluationError("percentile requires at least one value")
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    portion = position - lower
    return ordered[lower] + portion * (ordered[upper] - ordered[lower])


def _sentiment_pullback_volatility_unit(
    context: AlgoContext, entry_ts: int
) -> float:
    lookback_minutes = int(context.parameters["vol_lookback_minutes"])
    floor_pct = float(context.parameters["vol_floor_pct"])
    cap_pct = float(context.parameters["vol_cap_pct"])
    bars = context.read_bars(entry_ts - lookback_minutes * 60, entry_ts)
    returns = [
        abs((float(current["close"]) / float(previous["close"]) - 1.0) * 100.0)
        for previous, current in zip(bars, bars[1:])
        if int(current["ts"]) - int(previous["ts"]) == 60
        and float(previous["close"]) > 0.0
    ]
    if len(returns) < int(context.parameters["min_vol_bars"]):
        return floor_pct
    unit = _percentile(returns, float(context.parameters["vol_percentile"]))
    return max(floor_pct, min(cap_pct, unit))


def _algo_crossover(context: AlgoContext) -> tuple[bool, bool, int]:
    inputs = context.inputs
    previous = context.previous
    fast_name, slow_name = inputs
    fast = require_float(
        inputs[fast_name], "fast", nullable=True, error=EvaluationError
    )
    slow = require_float(
        inputs[slow_name], "slow", nullable=True, error=EvaluationError
    )
    old_fast = require_float(
        previous.get(fast_name), "previous fast", nullable=True, error=EvaluationError
    )
    old_slow = require_float(
        previous.get(slow_name), "previous slow", nullable=True, error=EvaluationError
    )
    if None in (fast, slow, old_fast, old_slow):
        return False, False, 0
    is_entry = old_fast <= old_slow and fast > slow
    is_close = old_fast >= old_slow and fast < slow
    direction = 1 if is_entry else (
        int(context.open_entries[0]["direction"])
        if is_close and context.open_entries
        else 1 if is_close else 0
    )
    return is_entry, is_close, direction



def _algo_range_breakout(context: AlgoContext) -> tuple[bool, bool, int]:
    inputs = context.inputs
    session_name, range_name, rvol_name, price_name = inputs
    session = inputs[session_name]
    opening_range = inputs[range_name]
    rvol = require_float(
        inputs[rvol_name], "rvol_open", nullable=True, error=EvaluationError
    )
    price = require_float(
        inputs[price_name], "last_close", nullable=True, error=EvaluationError
    )
    direction_param = context.parameters["direction"]
    target_r = float(context.parameters["target_r"])
    min_rvol = float(context.parameters["min_rvol"])
    minute_max = int(context.parameters["minute_max"])
    entry_cutoff = float(context.parameters["entry_cutoff_minutes"])
    flat_minutes = float(context.parameters["flat_minutes"])
    if session is None or opening_range is None or rvol is None or price is None:
        return False, False, 0

    if context.open_entries:
        entry = context.open_entries[0]
        direction = int(entry["direction"])
        entry_price = float(entry["price"])
        stop = float(opening_range["low" if direction == 1 else "high"])
        risk = entry_price - stop if direction == 1 else stop - entry_price
        target = entry_price + direction * target_r * risk
        hit_stop = price <= stop if direction == 1 else price >= stop
        hit_target = price >= target if direction == 1 else price <= target
        if (
            risk <= 0
            or hit_stop
            or hit_target
            or float(session["to_close"]) <= flat_minutes
        ):
            return False, True, direction
        return False, False, 0

    if any(_algo_output(row["output"])[0] for row in context.session_outputs):
        return False, False, 0
    if (
        int(session["minute"]) >= minute_max
        or float(session["to_close"]) < entry_cutoff
        or rvol <= min_rvol
    ):
        return False, False, 0
    high = float(opening_range["high"])
    low = float(opening_range["low"])
    if direction_param in ("both", "long") and price > high:
        return True, False, 1
    if direction_param in ("both", "short") and price < low:
        return True, False, -1
    return False, False, 0



def _algo_sentiment_pullback(context: AlgoContext) -> tuple[bool, bool, int]:
    session_name, sentiment_name, pullback_name, price_name = context.inputs
    session = context.inputs[session_name]
    sentiment = context.inputs[sentiment_name]
    pullback = context.inputs[pullback_name]
    price = require_float(
        context.inputs[price_name], "last_close", nullable=True, error=EvaluationError
    )
    early_minutes = int(context.parameters["early_minutes"])
    early_hold = int(context.parameters["early_hold_minutes"])
    late_hold = int(context.parameters["late_hold_minutes"])
    flat_minutes = float(context.parameters["flat_minutes"])
    pattern_exit = bool(context.parameters["pattern_exit"])
    if session is None or price is None:
        return False, False, 0

    if context.open_entries:
        entry = context.open_entries[0]
        direction = int(entry["direction"])
        entry_price = float(entry["price"])
        volatility_unit = _sentiment_pullback_volatility_unit(
            context, int(entry["ts"])
        )
        take_profit = float(context.parameters["tp_vol_mult"]) * volatility_unit
        giveback_pct = float(context.parameters["gb_vol_mult"]) * volatility_unit
        stop_loss = float(context.parameters["sl_vol_mult"]) * volatility_unit
        elapsed = (int(session["ts"]) - int(entry["ts"])) / 60.0
        entry_minute = (int(entry["ts"]) - int(session["open_ts"])) / 60.0
        hold_minutes = early_hold if entry_minute < early_minutes else late_hold
        pnl_pct = direction * (float(price) / entry_price - 1.0) * 100.0
        peak_pnl_pct = max(
            0.0,
            *(
                direction * (float(row["close"]) / entry_price - 1.0) * 100.0
                for row in context.read_bars(
                    int(entry["ts"]), int(session["ts"])
                )
            ),
        )
        gave_back = pnl_pct <= peak_pnl_pct - giveback_pct
        pattern_broke = (
            pattern_exit
            and (sentiment is None or not bool(sentiment["pattern_valid"]))
        )
        if (
            (take_profit > 0.0 and pnl_pct >= take_profit)
            or (stop_loss > 0.0 and pnl_pct <= -stop_loss)
            or gave_back
            or elapsed >= hold_minutes
            or pattern_broke
            or float(session["to_close"]) <= flat_minutes
        ):
            return False, True, direction
        return False, False, 0

    if any(_algo_output(row["output"])[0] for row in context.session_outputs):
        return False, False, 0
    if pullback is not None and bool(pullback["trigger"]):
        return True, False, int(pullback["direction"])
    return False, False, 0



def _algo_has_session_entry(context: AlgoContext) -> bool:
    return any(
        _algo_output(row["output"])[0] for row in context.session_outputs
    )



def _algo_window(
    session: Mapping[str, Any], minute_min: int, minute_max: int
) -> tuple[int, int]:
    total = int(session["total"])
    if total == 390:
        return minute_min, minute_max
    start = (
        minute_min
        if minute_min <= 120
        else round(
            120 + (total - 120) * (minute_min - 120) / (390 - 120)
        )
    )
    end = (
        minute_max
        if minute_max <= 120
        else total - (390 - minute_max)
    )
    return max(0, start), min(total, end)



def _fixed_atr_exit(
    context: AlgoContext,
    session: Mapping[str, Any],
    atr_session: float,
    price: float,
    risk_atr_frac: float,
    target_r: float,
    flat_minutes: float,
) -> tuple[bool, bool, int]:
    entry = context.open_entries[0]
    direction = int(entry["direction"])
    entry_price = float(entry["price"])
    risk = risk_atr_frac * atr_session * entry_price
    stop = entry_price - direction * risk
    target = entry_price + direction * target_r * risk
    hit_stop = price <= stop if direction == 1 else price >= stop
    hit_target = price >= target if direction == 1 else price <= target
    if (
        risk <= 0.0
        or hit_stop
        or hit_target
        or float(session["to_close"]) <= flat_minutes
    ):
        return False, True, direction
    return False, False, 0



def _algo_momentum_continuation(
    context: AlgoContext,
) -> tuple[bool, bool, int]:
    session_name, first30_name, atr_name, rvol_name, price_name = context.inputs
    session = context.inputs[session_name]
    first30 = require_float(
        context.inputs[first30_name],
        "first30_ret",
        nullable=True,
        error=EvaluationError,
    )
    atr_session = require_float(
        context.inputs[atr_name],
        "atr_session",
        nullable=True,
        error=EvaluationError,
    )
    rvol = require_float(
        context.inputs[rvol_name],
        "rvol_open",
        nullable=True,
        error=EvaluationError,
    )
    price = require_float(
        context.inputs[price_name],
        "last_close",
        nullable=True,
        error=EvaluationError,
    )
    first30_min = float(context.parameters["first30_min_pct"])
    risk_fraction = float(context.parameters["risk_atr_frac"])
    target_r = float(context.parameters["target_r"])
    min_rvol = float(context.parameters["min_rvol"])
    minute_min = int(context.parameters["minute_min"])
    minute_max = int(context.parameters["minute_max"])
    entry_cutoff = float(context.parameters["entry_cutoff_minutes"])
    flat_minutes = float(context.parameters["flat_minutes"])
    if (
        session is None
        or first30 is None
        or atr_session is None
        or rvol is None
        or price is None
    ):
        return False, False, 0
    if context.open_entries:
        return _fixed_atr_exit(
            context,
            session,
            float(atr_session),
            float(price),
            risk_fraction,
            target_r,
            flat_minutes,
        )
    if _algo_has_session_entry(context):
        return False, False, 0
    window_min, window_max = _algo_window(session, minute_min, minute_max)
    minute = int(session["minute"])
    if (
        minute < window_min
        or minute >= window_max
        or float(session["to_close"]) < entry_cutoff
        or float(rvol) <= min_rvol
        or abs(float(first30)) < first30_min
        or float(first30) == 0.0
    ):
        return False, False, 0
    return True, False, 1 if float(first30) > 0.0 else -1


def _algo_failed_gap(context: AlgoContext) -> tuple[bool, bool, int]:
    session_name, prior_name, atr_name, price_name = context.inputs
    session = context.inputs[session_name]
    prior = context.inputs[prior_name]
    atr_session = require_float(
        context.inputs[atr_name],
        "atr_session",
        nullable=True,
        error=EvaluationError,
    )
    price = require_float(
        context.inputs[price_name],
        "last_close",
        nullable=True,
        error=EvaluationError,
    )
    gap_min = float(context.parameters["gap_min_pct"])
    risk_fraction = float(context.parameters["risk_atr_frac"])
    target_r = float(context.parameters["target_r"])
    minute_min = int(context.parameters["minute_min"])
    minute_max = int(context.parameters["minute_max"])
    entry_cutoff = float(context.parameters["entry_cutoff_minutes"])
    flat_minutes = float(context.parameters["flat_minutes"])
    if (
        session is None
        or prior is None
        or atr_session is None
        or price is None
    ):
        return False, False, 0
    if context.open_entries:
        return _fixed_atr_exit(
            context,
            session,
            float(atr_session),
            float(price),
            risk_fraction,
            target_r,
            flat_minutes,
        )
    if _algo_has_session_entry(context):
        return False, False, 0
    window_min, window_max = _algo_window(session, minute_min, minute_max)
    minute = int(session["minute"])
    if (
        minute < window_min
        or minute >= window_max
        or float(session["to_close"]) < entry_cutoff
    ):
        return False, False, 0
    gap = float(prior["gap_pct"])
    if (
        gap < -gap_min
        and float(prior["open_vs_prior_low"]) < 0.0
        and float(prior["price_vs_prior_low"]) > 0.0
    ):
        return True, False, 1
    if (
        gap > gap_min
        and float(prior["open_vs_prior_high"]) > 0.0
        and float(prior["price_vs_prior_high"]) < 0.0
    ):
        return True, False, -1
    return False, False, 0


def _algo_gap_continuation(context: AlgoContext) -> tuple[bool, bool, int]:
    session_name, prior_name, range_name, atr_name, rvol_name, price_name = (
        context.inputs
    )
    session = context.inputs[session_name]
    prior = context.inputs[prior_name]
    opening_range = context.inputs[range_name]
    atr_session = require_float(
        context.inputs[atr_name],
        "atr_session",
        nullable=True,
        error=EvaluationError,
    )
    rvol = require_float(
        context.inputs[rvol_name],
        "rvol_open",
        nullable=True,
        error=EvaluationError,
    )
    price = require_float(
        context.inputs[price_name],
        "last_close",
        nullable=True,
        error=EvaluationError,
    )
    gap_min = float(context.parameters["gap_min_pct"])
    risk_fraction = float(context.parameters["risk_atr_frac"])
    target_r = float(context.parameters["target_r"])
    min_rvol = float(context.parameters["min_rvol"])
    minute_min = int(context.parameters["minute_min"])
    minute_max = int(context.parameters["minute_max"])
    entry_cutoff = float(context.parameters["entry_cutoff_minutes"])
    flat_minutes = float(context.parameters["flat_minutes"])
    if (
        session is None
        or prior is None
        or opening_range is None
        or atr_session is None
        or rvol is None
        or price is None
    ):
        return False, False, 0
    if context.open_entries:
        return _fixed_atr_exit(
            context,
            session,
            float(atr_session),
            float(price),
            risk_fraction,
            target_r,
            flat_minutes,
        )
    if _algo_has_session_entry(context):
        return False, False, 0
    window_min, window_max = _algo_window(session, minute_min, minute_max)
    minute = int(session["minute"])
    if (
        minute < window_min
        or minute >= window_max
        or float(session["to_close"]) < entry_cutoff
        or float(rvol) <= min_rvol
    ):
        return False, False, 0
    gap = float(prior["gap_pct"])
    if (
        gap > gap_min
        and float(prior["open_vs_prior_high"]) > 0.0
        and float(price) > float(opening_range["high"])
    ):
        return True, False, 1
    if (
        gap < -gap_min
        and float(prior["open_vs_prior_low"]) < 0.0
        and float(price) < float(opening_range["low"])
    ):
        return True, False, -1
    return False, False, 0


def _confirmed_extreme_direction(
    context: AlgoContext,
    session: Mapping[str, Any],
    atr_session: float,
    min_range_atr: float,
    confirmation_bars: int,
    window_min: int,
    window_max: int,
) -> int:
    """Confirm a recent session extreme without storing private algo state."""
    rows = context.read_bars(int(session["open_ts"]), None)
    if len(rows) <= confirmation_bars:
        return 0
    current_price = float(rows[-1]["close"])
    prior_rows = rows[:-1]
    first_candidate = max(0, len(prior_rows) - confirmation_bars)
    for index in range(len(prior_rows) - 1, first_candidate - 1, -1):
        candidate = prior_rows[index]
        candidate_minute = int(
            (int(candidate["ts"]) - int(session["open_ts"])) // 60
        )
        if candidate_minute < window_min or candidate_minute >= window_max:
            continue
        history = prior_rows[: index + 1]
        day_high = max(float(row["high"]) for row in history)
        day_low = min(float(row["low"]) for row in history)
        candidate_price = float(candidate["close"])
        denominator = atr_session * candidate_price
        if (
            denominator <= 0.0
            or (day_high - day_low) / denominator < min_range_atr
        ):
            continue
        if float(candidate["low"]) <= day_low:
            if current_price > float(candidate["high"]):
                return 1
            continue
        if (
            float(candidate["high"]) >= day_high
            and current_price < float(candidate["low"])
        ):
            return -1
    return 0


def _algo_extreme_fade(context: AlgoContext) -> tuple[bool, bool, int]:
    session_name, extremes_name, atr_name, rvol_name, price_name = context.inputs
    session = context.inputs[session_name]
    extremes = context.inputs[extremes_name]
    atr_session = require_float(
        context.inputs[atr_name],
        "atr_session",
        nullable=True,
        error=EvaluationError,
    )
    rvol = require_float(
        context.inputs[rvol_name],
        "rvol_open",
        nullable=True,
        error=EvaluationError,
    )
    price = require_float(
        context.inputs[price_name],
        "last_close",
        nullable=True,
        error=EvaluationError,
    )
    min_range_atr = float(context.parameters["min_range_atr"])
    stop_fraction = float(context.parameters["stop_atr_frac"])
    target_r = float(context.parameters["target_r"])
    confirmation_bars = int(context.parameters["confirmation_bars"])
    min_rvol = float(context.parameters["min_rvol"])
    minute_min = int(context.parameters["minute_min"])
    minute_max = int(context.parameters["minute_max"])
    entry_cutoff = float(context.parameters["entry_cutoff_minutes"])
    flat_minutes = float(context.parameters["flat_minutes"])
    if (
        session is None
        or extremes is None
        or atr_session is None
        or rvol is None
        or price is None
    ):
        return False, False, 0
    if context.open_entries:
        entry = context.open_entries[0]
        direction = int(entry["direction"])
        entry_price = float(entry["price"])
        entry_rows = context.read_bars(
            int(session["open_ts"]), int(entry["ts"])
        )
        if not entry_rows:
            return False, False, 0
        offset = stop_fraction * float(atr_session) * entry_price
        entry_high = max(float(row["high"]) for row in entry_rows)
        entry_low = min(float(row["low"]) for row in entry_rows)
        stop = entry_low - offset if direction == 1 else entry_high + offset
        risk = entry_price - stop if direction == 1 else stop - entry_price
        target = entry_price + direction * target_r * risk
        hit_stop = float(price) <= stop if direction == 1 else float(price) >= stop
        hit_target = (
            float(price) >= target if direction == 1 else float(price) <= target
        )
        if (
            risk <= 0.0
            or hit_stop
            or hit_target
            or float(session["to_close"]) <= flat_minutes
        ):
            return False, True, direction
        return False, False, 0
    if _algo_has_session_entry(context):
        return False, False, 0
    window_min, window_max = _algo_window(session, minute_min, minute_max)
    minute = int(session["minute"])
    if (
        minute < window_min
        or minute >= window_max
        or float(session["to_close"]) < entry_cutoff
        or float(rvol) <= min_rvol
    ):
        return False, False, 0
    if (
        float(extremes["day_range_atr"]) >= min_range_atr
        and (bool(extremes["new_day_low"]) or bool(extremes["new_day_high"]))
    ):
        return False, False, 0
    direction = _confirmed_extreme_direction(
        context,
        session,
        float(atr_session),
        min_range_atr,
        confirmation_bars,
        window_min,
        window_max,
    )
    if direction:
        return True, False, direction
    return False, False, 0



def _opening_drive_pct(
    context: AlgoContext,
    session: Mapping[str, Any],
    confirm_minutes: int,
) -> Optional[float]:
    open_ts = int(session["open_ts"])
    confirm_ts = open_ts + (confirm_minutes - 1) * 60
    rows = context.read_bars(open_ts, confirm_ts)
    if (
        not rows
        or int(rows[0]["ts"]) != open_ts
        or int(rows[-1]["ts"]) != confirm_ts
    ):
        return None
    open_price = float(rows[0]["open"])
    if open_price <= 0.0:
        return None
    return (float(rows[-1]["close"]) / open_price - 1.0) * 100.0


def _algo_opening_drive(context: AlgoContext) -> tuple[bool, bool, int]:
    session_name, atr_name, price_name = context.inputs
    session = context.inputs[session_name]
    atr_session = require_float(
        context.inputs[atr_name],
        "atr_session",
        nullable=True,
        error=EvaluationError,
    )
    price = require_float(
        context.inputs[price_name],
        "last_close",
        nullable=True,
        error=EvaluationError,
    )
    confirm_minutes = int(context.parameters["confirm_minutes"])
    min_drive_pct = float(context.parameters["min_drive_pct"])
    drive_atr_frac = float(context.parameters["drive_atr_frac"])
    target_frac = float(context.parameters["target_frac"])
    stop_frac = float(context.parameters["stop_frac"])
    minute_min = int(context.parameters["minute_min"])
    minute_max = int(context.parameters["minute_max"])
    entry_cutoff = float(context.parameters["entry_cutoff_minutes"])
    flat_minutes = float(context.parameters["flat_minutes"])
    if session is None or atr_session is None or price is None:
        return False, False, 0
    drive_pct = _opening_drive_pct(context, session, confirm_minutes)
    if context.open_entries:
        entry = context.open_entries[0]
        direction = int(entry["direction"])
        entry_price = float(entry["price"])
        if drive_pct is None:
            return False, True, direction
        move = abs(drive_pct) / 100.0 * entry_price
        target = entry_price + direction * target_frac * move
        stop = entry_price - direction * stop_frac * move
        hit_stop = (
            float(price) <= stop if direction == 1 else float(price) >= stop
        )
        hit_target = (
            float(price) >= target if direction == 1 else float(price) <= target
        )
        if (
            move <= 0.0
            or hit_stop
            or hit_target
            or float(session["to_close"]) <= flat_minutes
        ):
            return False, True, direction
        return False, False, 0
    if _algo_has_session_entry(context):
        return False, False, 0
    window_min, window_max = _algo_window(session, minute_min, minute_max)
    minute = int(session["minute"])
    if (
        minute < window_min
        or minute >= window_max
        or float(session["to_close"]) < entry_cutoff
        or drive_pct is None
        or drive_pct == 0.0
        or abs(drive_pct)
        < max(min_drive_pct, drive_atr_frac * float(atr_session) * 100.0)
    ):
        return False, False, 0
    return True, False, 1 if drive_pct > 0.0 else -1



def _normalize_range_breakout(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    names = {
        "direction",
        "target_r",
        "min_rvol",
        "minute_max",
        "entry_cutoff_minutes",
        "flat_minutes",
    }
    _parameter_keys(parameters, names)
    direction = parameters.get("direction")
    if direction not in ("both", "long", "short"):
        raise ConfigError("direction must be both, long, or short")
    return {
        "direction": direction,
        "target_r": _parameter_number(parameters, "target_r"),
        "min_rvol": _parameter_number(parameters, "min_rvol"),
        "minute_max": _parameter_int(parameters, "minute_max"),
        "entry_cutoff_minutes": _parameter_number(
            parameters, "entry_cutoff_minutes"
        ),
        "flat_minutes": _parameter_number(parameters, "flat_minutes"),
    }



def _normalize_sentiment_pullback(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    names = {
        "early_minutes",
        "early_hold_minutes",
        "late_hold_minutes",
        "tp_vol_mult",
        "gb_vol_mult",
        "sl_vol_mult",
        "vol_percentile",
        "vol_lookback_minutes",
        "vol_floor_pct",
        "vol_cap_pct",
        "min_vol_bars",
        "pattern_exit",
        "flat_minutes",
        "capital_fraction",
    }
    _parameter_keys(parameters, names)
    capital_fraction = _parameter_number(parameters, "capital_fraction")
    tp_vol_mult = _parameter_number(parameters, "tp_vol_mult")
    gb_vol_mult = _parameter_number(parameters, "gb_vol_mult")
    sl_vol_mult = _parameter_number(parameters, "sl_vol_mult")
    vol_percentile = _parameter_number(parameters, "vol_percentile")
    vol_floor_pct = _parameter_number(parameters, "vol_floor_pct")
    vol_cap_pct = _parameter_number(parameters, "vol_cap_pct")
    if capital_fraction <= 0.0 or capital_fraction > 0.5:
        raise ConfigError("capital_fraction must be > 0 and <= 0.5")
    if min(tp_vol_mult, gb_vol_mult, sl_vol_mult) <= 0.0:
        raise ConfigError("volatility multipliers must be > 0")
    if gb_vol_mult >= sl_vol_mult:
        raise ConfigError("gb_vol_mult must be less than sl_vol_mult")
    if vol_percentile <= 0.0 or vol_percentile > 1.0:
        raise ConfigError("vol_percentile must be > 0 and <= 1")
    if vol_floor_pct <= 0.0 or vol_floor_pct > vol_cap_pct:
        raise ConfigError("volatility bounds must satisfy 0 < floor <= cap")
    return {
        "early_minutes": _parameter_int(parameters, "early_minutes"),
        "early_hold_minutes": _parameter_int(parameters, "early_hold_minutes"),
        "late_hold_minutes": _parameter_int(parameters, "late_hold_minutes"),
        "tp_vol_mult": tp_vol_mult,
        "gb_vol_mult": gb_vol_mult,
        "sl_vol_mult": sl_vol_mult,
        "vol_percentile": vol_percentile,
        "vol_lookback_minutes": _parameter_int(
            parameters, "vol_lookback_minutes", minimum=1
        ),
        "vol_floor_pct": vol_floor_pct,
        "vol_cap_pct": vol_cap_pct,
        "min_vol_bars": _parameter_int(parameters, "min_vol_bars", minimum=1),
        "pattern_exit": _parameter_bool(parameters, "pattern_exit"),
        "flat_minutes": _parameter_number(parameters, "flat_minutes"),
        "capital_fraction": capital_fraction,
    }



def _normalize_algo_window(
    parameters: Mapping[str, Any],
    tickers: tuple[str, ...],
    float_names: tuple[str, ...],
) -> dict[str, Any]:
    _parameter_keys(parameters, {"minute_min", "minute_max", *float_names})
    result: dict[str, Any] = {
        "minute_min": _parameter_int(parameters, "minute_min", minimum=0),
        "minute_max": _parameter_int(parameters, "minute_max"),
    }
    if result["minute_min"] >= result["minute_max"]:
        raise ConfigError("minute_min must be less than minute_max")
    result.update(
        (name, _parameter_number(parameters, name)) for name in float_names
    )
    return result



def _normalize_momentum_continuation(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    return _normalize_algo_window(
        parameters,
        tickers,
        (
            "first30_min_pct",
            "risk_atr_frac",
            "target_r",
            "min_rvol",
            "entry_cutoff_minutes",
            "flat_minutes",
        ),
    )



def _normalize_failed_gap(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    return _normalize_algo_window(
        parameters,
        tickers,
        (
            "gap_min_pct",
            "risk_atr_frac",
            "target_r",
            "entry_cutoff_minutes",
            "flat_minutes",
        ),
    )



def _normalize_gap_continuation(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    return _normalize_algo_window(
        parameters,
        tickers,
        (
            "gap_min_pct",
            "risk_atr_frac",
            "target_r",
            "min_rvol",
            "entry_cutoff_minutes",
            "flat_minutes",
        ),
    )



def _normalize_extreme_fade(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    base_parameters = dict(parameters)
    confirmation_bars = base_parameters.pop("confirmation_bars", None)
    result = _normalize_algo_window(
        base_parameters,
        tickers,
        (
            "min_range_atr",
            "stop_atr_frac",
            "target_r",
            "min_rvol",
            "entry_cutoff_minutes",
            "flat_minutes",
        ),
    )
    result["confirmation_bars"] = require_int(
        confirmation_bars, "confirmation_bars", error=ConfigError
    )
    return result


def _normalize_opening_drive(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    base_parameters = dict(parameters)
    confirm_minutes = base_parameters.pop("confirm_minutes", None)
    result = _normalize_algo_window(
        base_parameters,
        tickers,
        (
            "min_drive_pct",
            "drive_atr_frac",
            "target_frac",
            "stop_frac",
            "entry_cutoff_minutes",
            "flat_minutes",
        ),
    )
    result["confirm_minutes"] = require_int(
        confirm_minutes, "confirm_minutes", error=ConfigError
    )
    if result["minute_min"] < result["confirm_minutes"]:
        raise ConfigError("minute_min must be at least confirm_minutes")
    return result


ALGO_FUNCTIONS: dict[str, AlgoSpec] = {
    "crossover": AlgoSpec(_algo_crossover, 2, _no_parameters),
    "range_breakout": AlgoSpec(
        _algo_range_breakout, 4, _normalize_range_breakout
    ),
    "sentiment_pullback": AlgoSpec(
        _algo_sentiment_pullback, 4, _normalize_sentiment_pullback
    ),
    "momentum_continuation": AlgoSpec(
        _algo_momentum_continuation, 5, _normalize_momentum_continuation
    ),
    "failed_gap": AlgoSpec(_algo_failed_gap, 4, _normalize_failed_gap),
    "gap_continuation": AlgoSpec(
        _algo_gap_continuation, 6, _normalize_gap_continuation
    ),
    "extreme_fade": AlgoSpec(_algo_extreme_fade, 5, _normalize_extreme_fade),
    "opening_drive": AlgoSpec(
        _algo_opening_drive, 3, _normalize_opening_drive
    ),
}



def _algo_output(value: Any) -> tuple[bool, bool, int]:
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise EvaluationError("algo output must be [is_entry,is_close_all,direction]")
    is_entry, is_close = value[:2]
    if (
        not isinstance(is_entry, bool)
        or not isinstance(is_close, bool)
        or (is_entry and is_close)
    ):
        raise EvaluationError("algo actions must be exclusive booleans")
    direction = value[2] if len(value) == 3 else (1 if is_entry or is_close else 0)
    if (
        isinstance(direction, bool)
        or not isinstance(direction, int)
        or direction not in (-1, 0, 1)
    ):
        raise EvaluationError("algo direction must be -1, 0, or 1")
    if (is_entry or is_close) and direction == 0:
        raise EvaluationError("an algo action must include direction")
    if not (is_entry or is_close) and direction != 0:
        raise EvaluationError("a quiet algo output must use direction 0")
    return is_entry, is_close, direction
