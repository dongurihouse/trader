"""Canonical provider-indicator configuration and metadata lookup keys."""

from __future__ import annotations

import json
from typing import Any, Mapping, Type

from common.validation import require_float, require_int


_DEFAULTS: dict[str, dict[str, Any]] = {
    "ema": {"period": 9},
    "sma": {"period": 9},
    "rsi": {"period": 14},
    "momentum": {"period": 12},
    "roc": {"period": 14},
    "cci": {"period": 14},
    "williams_r": {"period": 10},
    "atr": {"period": 14},
    "mfi": {"period": 14},
    "adx": {"period": 10},
    "donchian_channels": {"period": 20},
    "bollinger_bands": {"period": 20, "num_std": 2},
    "macd": {"fast_period": 12, "slow_period": 26, "signal_period": 9},
    "keltner_channels": {"period": 20, "multiplier": 2},
    "supertrend": {"period": 10, "multiplier": 3},
    "vwap": {},
    "obv": {},
    "pivot_points": {"method": "classic"},
}


def normalize_provider_indicator(
    configured: Mapping[str, Any],
    *,
    error: Type[Exception] = ValueError,
) -> tuple[str, str]:
    """Return the indicator name and its canonical metadata lookup key."""
    name = configured.get("name")
    if not isinstance(name, str) or name not in _DEFAULTS:
        raise error("name must be a supported provider indicator")

    supplied = {key: value for key, value in configured.items() if key != "name"}
    unknown = set(supplied) - set(_DEFAULTS[name])
    if unknown:
        raise error(
            "unsupported provider parameters: %s" % ", ".join(sorted(unknown))
        )

    normalized = dict(_DEFAULTS[name])
    normalized.update(supplied)
    for key, value in normalized.items():
        if key.endswith("period") or key == "period":
            try:
                require_int(value, key, error=error)
            except error as exc:
                raise error("%s must be a positive integer" % key) from exc
        elif key in ("num_std", "multiplier"):
            try:
                number = require_float(value, key, error=error)
            except error as exc:
                raise error("%s must be a positive number" % key) from exc
            if number is None or number <= 0:
                raise error("%s must be a positive number" % key)
        elif key == "method" and value != "classic":
            raise error("method must be 'classic'")

    if name == "macd" and normalized["fast_period"] >= normalized["slow_period"]:
        raise error("fast_period must be less than slow_period")

    query_key = json.dumps(
        normalized,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return name, query_key
