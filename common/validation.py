"""Shared value validators for the dependency-free Trader services."""

from __future__ import annotations

import math
import re
from datetime import time as clock_time
from typing import Any, Optional, Type


SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


def is_symbol(value: Any) -> bool:
    return isinstance(value, str) and SYMBOL_PATTERN.fullmatch(value) is not None


def normalize_symbol(
    value: Any,
    name: str = "ticker",
    *,
    error: Type[Exception] = ValueError,
    message: Optional[str] = None,
) -> str:
    symbol = value.strip().upper() if isinstance(value, str) else ""
    if not is_symbol(symbol):
        raise error(message or "invalid %s: %r" % (name, value))
    return symbol


def require_clock(
    value: Any,
    name: str,
    *,
    error: Type[Exception] = ValueError,
) -> clock_time:
    if not isinstance(value, str):
        raise error("%s must be HH:MM" % name)
    try:
        parsed = clock_time.fromisoformat(value)
    except ValueError as exc:
        raise error("%s must be HH:MM" % name) from exc
    if parsed.second or parsed.microsecond:
        raise error("%s must be precise to the minute" % name)
    return parsed


def require_int(
    value: Any,
    name: str,
    minimum: int = 1,
    *,
    error: Type[Exception] = ValueError,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise error("%s must be an integer >= %d" % (name, minimum))
    return value


def require_float(
    value: Any,
    name: str,
    *,
    minimum: Optional[float] = None,
    nullable: bool = False,
    error: Type[Exception] = ValueError,
) -> Optional[float]:
    if value is None and nullable:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        suffix = " or null" if nullable else ""
        raise error("%s must be a finite number%s" % (name, suffix))
    number = float(value)
    if minimum is not None and number < minimum:
        raise error("%s must be finite and >= %g" % (name, minimum))
    return number
