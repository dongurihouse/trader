"""Small shared value validators for the dependency-free services."""

from __future__ import annotations

import math
from typing import Any, Optional, Type


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
