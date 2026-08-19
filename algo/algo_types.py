"""Shared algo service types and small validation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from common.config import EASTERN, MarketSchedule
from common.validation import require_float, require_int

UTC = timezone.utc
CYCLE_PAIR_LIMIT = 2_000
ALERT_CLOSE_GRACE_SECONDS = 5 * 60


class ConfigError(ValueError):
    pass



class EvaluationError(RuntimeError):
    pass



@dataclass(frozen=True)
class BrokerSettings:
    enabled: bool
    dollar_amount: str
    shadow_options: bool
    account_env: str
    execution_tickers: Mapping[str, Mapping[str, str]]



@dataclass(frozen=True)
class Settings:
    database: Path
    tickers: tuple[str, ...]
    evaluation_days: int
    poll_seconds: int
    api_port: int
    schedule: MarketSchedule
    signals: Mapping[str, Mapping[str, Any]]
    algos: Mapping[str, Mapping[str, Any]]
    signal_order: tuple[str, ...]
    algo_order: tuple[str, ...]
    algo_requirements: Mapping[str, frozenset[str]]
    broker: BrokerSettings
    content: str

    def enabled_signals(self) -> tuple[str, ...]:
        return tuple(self.signals)

    def enabled_algos(self) -> tuple[str, ...]:
        return tuple(self.algos)

    def output_kinds(self) -> tuple[str, ...]:
        return self.enabled_signals() + self.enabled_algos()



def _json(value: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ConfigError("value is not finite JSON") from exc



@dataclass(frozen=True)
class SignalSpec:
    function: Callable[..., Any]
    inputs: tuple[str, ...]
    normalize: Callable[[Mapping[str, Any], tuple[str, ...]], Mapping[str, Any]]



@dataclass(frozen=True)
class AlgoSpec:
    function: Callable[..., tuple[bool, bool, int]]
    input_count: int
    normalize: Callable[[Mapping[str, Any], tuple[str, ...]], Mapping[str, Any]]



@dataclass(frozen=True)
class AlgoContext:
    ticker: str
    ts: int
    parameters: Mapping[str, Any]
    inputs: Mapping[str, Any]
    previous: Mapping[str, Any]
    open_entries: tuple[Mapping[str, Any], ...]
    session_outputs: tuple[Mapping[str, Any], ...]
    read_bars: Callable[
        [int, Optional[int]], tuple[Mapping[str, Any], ...]
    ]


def _parameter_keys(parameters: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(parameters) - allowed
    if unknown:
        raise ConfigError("unknown parameters: %s" % ", ".join(sorted(unknown)))



def _no_parameters(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    _parameter_keys(parameters, set())
    return {}



def _configured_tickers(
    value: Any, name: str, tickers: tuple[str, ...]
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("%s must be a non-empty ticker list" % name)
    result: list[str] = []
    for item in value:
        ticker = item.strip().upper() if isinstance(item, str) else ""
        if ticker not in tickers:
            raise ConfigError("%s contains an unconfigured ticker: %r" % (name, item))
        if ticker not in result:
            result.append(ticker)
    return tuple(result)



def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("%s must be boolean" % name)
    return value



def _number(value: Any, name: str, minimum: float = 0.0) -> float:
    return float(require_float(value, name, minimum=minimum, error=ConfigError))



def _parameter_int(
    parameters: Mapping[str, Any], name: str, minimum: int = 1
) -> int:
    return require_int(
        parameters.get(name), name, minimum=minimum, error=ConfigError
    )



def _parameter_number(
    parameters: Mapping[str, Any], name: str, minimum: float = 0.0
) -> float:
    return _number(parameters.get(name), name, minimum=minimum)



def _parameter_bool(parameters: Mapping[str, Any], name: str) -> bool:
    return _boolean(parameters.get(name), name)


def _session_window(settings: Settings, ts: int) -> tuple[date, int, int]:
    local_day = datetime.fromtimestamp(ts, tz=EASTERN).date()
    session_open, session_close = settings.schedule.session_window(local_day)
    return local_day, session_open, session_close
