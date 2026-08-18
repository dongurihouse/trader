#!/usr/bin/env python3
"""Small deterministic algo core and SQLite polling service."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from broker import send_broker_orders
from notify import send_trade_alerts
from shape_signal import clear_shape_cache, normalize_shape_parameters, shape_v1
from validation import require_float, require_int


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT.parent / "config" / "config.json"
SCHEMA = ROOT.parent / "config" / "schema.sql"
UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
BAR_FIELDS = {"open", "high", "low", "close", "volume"}
CYCLE_PAIR_LIMIT = 2_000
CYCLE_PROGRESS_INTERVAL = 1_000
ALERT_CLOSE_GRACE_SECONDS = 5 * 60
METADATA_DEFAULTS = {
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
_RVOL_BASELINES: dict[
    tuple[str, str, date, int, int], Optional[tuple[float, ...]]
] = {}
_PULLBACK_BASELINES: dict[
    tuple[str, str, date, int, int, int, int, int, int, float],
    Optional[tuple[float, float]],
] = {}
_OUTPUT_SESSION_SEEDS: dict[
    tuple[str, str, str, int], tuple[tuple[int, float, int], ...]
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
_RELATIVE_MOMENTUM_REPAIR_ATTEMPTS: set[tuple[str, str, int]] = set()
_INITIALIZED_DATABASES: set[Path] = set()
_DATABASE_INIT_LOCK = threading.Lock()


class ConfigError(ValueError):
    pass


class EvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerSettings:
    enabled: bool
    quantity: str
    account_env: str
    execution_tickers: Mapping[str, Mapping[str, str]]


@dataclass(frozen=True)
class Settings:
    database: Path
    tickers: tuple[str, ...]
    evaluation_days: int
    poll_seconds: int
    api_port: int
    regular_open: clock_time
    regular_close: clock_time
    early_close: clock_time
    early_close_days: frozenset[date]
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


def _clock(value: Any, name: str) -> clock_time:
    if not isinstance(value, str):
        raise ConfigError("%s must be HH:MM" % name)
    try:
        parsed = clock_time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("%s must be HH:MM" % name) from exc
    if parsed.second or parsed.microsecond:
        raise ConfigError("%s must be precise to the minute" % name)
    return parsed


def _nodes(document: Mapping[str, Any], key: str) -> dict[str, Mapping[str, Any]]:
    raw = document.get(key, {})
    if not isinstance(raw, dict):
        raise ConfigError("%s must be an object keyed by node name" % key)
    nodes: dict[str, Mapping[str, Any]] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or not isinstance(value, dict):
            raise ConfigError("every %s entry must be a named object" % key)
        params = value.get("params", {})
        inputs = value.get("inputs", [])
        function = value.get("function")
        if function is None:
            function = "metadata" if key == "signals" and inputs == ["bar_metadata"] else name
        if "trades" in value:
            raise ConfigError("%s.%s.trades is not supported" % (key, name))
        if not isinstance(function, str) or not function:
            raise ConfigError("%s.%s.function must be a name" % (key, name))
        if not isinstance(params, dict) or not isinstance(inputs, list):
            raise ConfigError("%s.%s params must be an object and inputs must be a list" % (key, name))
        if not all(isinstance(target, str) and target for target in inputs):
            raise ConfigError("%s.%s inputs must contain node names" % (key, name))
        nodes[name] = {
            "function": function,
            "params": params,
            "inputs": inputs,
        }
    return nodes


def _broker_settings(
    document: Mapping[str, Any], tickers: tuple[str, ...]
) -> BrokerSettings:
    raw = document.get("broker", {})
    if not isinstance(raw, dict):
        raise ConfigError("broker must be an object")
    unknown = set(raw) - {
        "enabled",
        "quantity",
        "account_env",
        "execution_tickers",
    }
    if unknown:
        raise ConfigError("unknown broker settings: %s" % ", ".join(sorted(unknown)))
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("broker.enabled must be boolean")
    quantity = raw.get("quantity", "0")
    if not isinstance(quantity, str) or not re.fullmatch(
        r"\d+(?:\.\d{1,6})?", quantity
    ):
        raise ConfigError("broker.quantity must be a non-negative decimal string")
    account_env = raw.get("account_env", "TRADER_ROBINHOOD_ACCOUNT")
    if not isinstance(account_env, str) or not re.fullmatch(
        r"[A-Z_][A-Z0-9_]*", account_env
    ):
        raise ConfigError("broker.account_env must be an environment variable name")

    configured = raw.get("execution_tickers", {})
    if not isinstance(configured, dict):
        raise ConfigError("broker.execution_tickers must be an object")
    execution_tickers: dict[str, Mapping[str, str]] = {}
    for raw_ticker, raw_mapping in configured.items():
        ticker = raw_ticker.strip().upper() if isinstance(raw_ticker, str) else ""
        if ticker not in tickers:
            raise ConfigError(
                "broker.execution_tickers contains an unconfigured ticker: %r"
                % raw_ticker
            )
        if ticker in execution_tickers:
            raise ConfigError("duplicate broker execution ticker: %s" % ticker)
        if not isinstance(raw_mapping, dict) or set(raw_mapping) != {"long", "short"}:
            raise ConfigError(
                "broker.execution_tickers.%s must contain long and short" % ticker
            )
        mapping: dict[str, str] = {}
        for direction in ("long", "short"):
            value = raw_mapping[direction]
            symbol = value.strip().upper() if isinstance(value, str) else ""
            if not SYMBOL.fullmatch(symbol):
                raise ConfigError(
                    "broker.execution_tickers.%s.%s must be a ticker"
                    % (ticker, direction)
                )
            mapping[direction] = symbol
        if mapping["long"] == mapping["short"]:
            raise ConfigError(
                "broker.execution_tickers.%s long and short must differ" % ticker
            )
        execution_tickers[ticker] = mapping
    return BrokerSettings(
        enabled=enabled,
        quantity=quantity,
        account_env=account_env,
        execution_tickers=execution_tickers,
    )


def _compile_dependencies(
    signals: Mapping[str, Mapping[str, Any]],
    algos: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, frozenset[str]]]:
    visiting: set[tuple[str, str]] = set()
    complete: set[tuple[str, str]] = set()
    signal_order: list[str] = []
    algo_order: list[str] = []
    algo_requirements: dict[str, frozenset[str]] = {}

    def visit(layer: str, name: str) -> None:
        key = (layer, name)
        if key in complete:
            return
        if key in visiting:
            raise ConfigError("dependency cycle at %s" % name)
        nodes = signals if layer == "signal" else algos
        node = nodes[name]
        visiting.add(key)
        for target in node["inputs"]:
            if layer == "signal" and target in ("bars", "bar_metadata", "events"):
                continue
            if target in signals:
                visit("signal", target)
            elif layer == "algo" and target in algos:
                visit("algo", target)
            else:
                raise ConfigError("%s references unknown node %s" % (name, target))
        visiting.remove(key)
        complete.add(key)
        if layer == "signal":
            signal_order.append(name)
        else:
            dependencies = {name}
            for target in node["inputs"]:
                if target in algos:
                    dependencies.update(algo_requirements[target])
            algo_requirements[name] = frozenset(dependencies)
            algo_order.append(name)

    for name in signals:
        visit("signal", name)
    for name in algos:
        visit("algo", name)
    return tuple(signal_order), tuple(algo_order), algo_requirements


def load_settings(
    path: Path,
    document: Optional[Mapping[str, Any]] = None,
    database_override: Optional[Path] = None,
) -> Settings:
    if document is None:
        try:
            document = json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise ConfigError("config file does not exist: %s" % path) from exc
        except json.JSONDecodeError as exc:
            raise ConfigError("invalid JSON in %s: %s" % (path, exc)) from exc
    if not isinstance(document, dict):
        raise ConfigError("config must be a JSON object")

    tickers = document.get("tickers")
    if not isinstance(tickers, list) or not tickers:
        raise ConfigError("tickers must be a non-empty list")
    normalized: list[str] = []
    for value in tickers:
        ticker = value.strip().upper() if isinstance(value, str) else ""
        if not SYMBOL.fullmatch(ticker):
            raise ConfigError("invalid ticker: %r" % value)
        if ticker not in normalized:
            normalized.append(ticker)

    algo = document.get("algo", {})
    if not isinstance(algo, dict):
        raise ConfigError("algo must be an object")
    polling = document.get("live_polling", {})
    if not isinstance(polling, dict):
        raise ConfigError("live_polling must be an object")
    early_closes = document.get("early_closes", [])
    if not isinstance(early_closes, list):
        raise ConfigError("early_closes must be a list")
    try:
        early_close_days = frozenset(date.fromisoformat(value) for value in early_closes)
    except (TypeError, ValueError) as exc:
        raise ConfigError("early_closes must contain YYYY-MM-DD strings") from exc
    database_value = document.get("database", "../data/trader.sqlite3")
    if not isinstance(database_value, str) or not database_value:
        raise ConfigError("database must be a path string")
    database = Path(database_value).expanduser()
    if not database.is_absolute():
        database = (path.parent / database).resolve()
    if database_override is not None:
        database = database_override

    signals = _nodes(document, "signals")
    algos = _nodes(document, "algos")
    if set(signals) & set(algos):
        raise ConfigError("signal and algo names must be unique")
    unknown_signals = {
        node["function"] for node in signals.values()
    } - set(SIGNAL_FUNCTIONS)
    unknown_algos = {
        node["function"] for node in algos.values()
    } - set(ALGO_FUNCTIONS)
    if unknown_signals or unknown_algos:
        raise ConfigError(
            "unknown functions: %s" % ", ".join(sorted(unknown_signals | unknown_algos))
        )
    for name, node in signals.items():
        spec = SIGNAL_FUNCTIONS[node["function"]]
        required = spec.inputs
        if tuple(node["inputs"]) != required:
            raise ConfigError(
                "signals.%s inputs must be %s" % (name, list(required))
            )
        try:
            params = spec.normalize(node["params"], tuple(normalized))
        except ConfigError as exc:
            raise ConfigError("signals.%s.params: %s" % (name, exc)) from exc
        signals[name] = {**node, "params": params}
    for name, node in algos.items():
        spec = ALGO_FUNCTIONS[node["function"]]
        required = spec.input_count
        if len(node["inputs"]) != required:
            raise ConfigError(
                "algos.%s inputs must contain %d nodes" % (name, required)
            )
        try:
            params = spec.normalize(node["params"], tuple(normalized))
        except ConfigError as exc:
            raise ConfigError("algos.%s.params: %s" % (name, exc)) from exc
        algos[name] = {**node, "params": params}
    for name, node in algos.items():
        if node["function"] == "gap_continuation":
            range_name = node["inputs"][2]
            range_node = signals.get(range_name)
            if range_node is None or range_node["function"] != "opening_range":
                raise ConfigError(
                    "algos.%s third input must be an opening_range signal" % name
                )
            if node["params"].get("minute_min") != range_node["params"].get(
                "minutes"
            ):
                raise ConfigError(
                    "algos.%s minute_min must equal %s minutes" % (name, range_name)
                )
    signal_order, algo_order, algo_requirements = _compile_dependencies(
        signals, algos
    )
    settings = Settings(
        database=database,
        tickers=tuple(normalized),
        evaluation_days=require_int(
            algo.get("evaluation_days", 60), "algo.evaluation_days", error=ConfigError
        ),
        poll_seconds=require_int(
            algo.get("poll_seconds", 30), "algo.poll_seconds", error=ConfigError
        ),
        api_port=require_int(
            algo.get("api_port", 8791), "algo.api_port", 1024, error=ConfigError
        ),
        regular_open=_clock(
            polling.get("regular_open", "09:30"), "live_polling.regular_open"
        ),
        regular_close=_clock(
            polling.get("regular_close", "16:00"), "live_polling.regular_close"
        ),
        early_close=_clock(
            polling.get("early_close", "13:00"), "live_polling.early_close"
        ),
        early_close_days=early_close_days,
        signals=signals,
        algos=algos,
        signal_order=signal_order,
        algo_order=algo_order,
        algo_requirements=algo_requirements,
        broker=_broker_settings(document, tuple(normalized)),
        content=_json(document),
    )
    return settings


def _connect(path: Path, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        if not path.is_file():
            raise ConfigError("database does not exist: %s" % path)
        connection = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _document_mapping(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    return dict(value) if isinstance(value, Mapping) else {}


def _algo_snapshots(
    document: Mapping[str, Any],
) -> dict[str, tuple[str, str]]:
    """Return each raw algo definition and its effective config dependencies."""
    signals = _document_mapping(document, "signals")
    algos = _document_mapping(document, "algos")
    schedule = {
        "early_closes": document.get("early_closes", []),
        "live_polling": document.get("live_polling", {}),
    }
    snapshots: dict[str, tuple[str, str]] = {}

    for name, definition in algos.items():
        signal_names: set[str] = set()
        algo_names: set[str] = set()

        def visit_signal(target: str) -> None:
            if target in signal_names or target not in signals:
                return
            signal_names.add(target)
            node = signals[target]
            if not isinstance(node, Mapping):
                return
            for dependency in node.get("inputs", []):
                if isinstance(dependency, str):
                    visit_signal(dependency)

        def visit_algo(target: str) -> None:
            if target in algo_names or target not in algos:
                return
            algo_names.add(target)
            node = algos[target]
            if not isinstance(node, Mapping):
                return
            for dependency in node.get("inputs", []):
                if not isinstance(dependency, str):
                    continue
                if dependency in algos:
                    visit_algo(dependency)
                else:
                    visit_signal(dependency)

        visit_algo(name)
        algo_names.discard(name)
        dependencies = {
            "signals": {key: signals[key] for key in sorted(signal_names)},
            "algos": {key: algos[key] for key in sorted(algo_names)},
            "schedule": schedule,
        }
        snapshots[str(name)] = (_json(definition), _json(dependencies))
    return snapshots


def _remove_definition_history(connection: sqlite3.Connection) -> bool:
    """Remove database-backed config history retained by older releases."""
    changed = _table_exists(connection, "algo_history") or _table_exists(
        connection, "configs"
    )
    connection.execute("DROP TRIGGER IF EXISTS archive_algo_update")
    connection.execute("DROP TRIGGER IF EXISTS archive_algo_delete")
    connection.execute("DROP TABLE IF EXISTS algo_history")
    connection.execute("DROP TABLE IF EXISTS configs")
    return changed


def _migrate_outputs(connection: sqlite3.Connection) -> bool:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(outputs)")
    }
    if "config" not in columns:
        return False
    connection.execute("DROP TABLE outputs")
    connection.execute(
        """
        CREATE TABLE outputs (
            ticker      TEXT    NOT NULL,
            ts          INTEGER NOT NULL,
            kind        TEXT    NOT NULL,
            output      TEXT    NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (ticker, ts, kind)
        )
        """
    )
    return True


def _init_database(path: Path) -> None:
    path = path.resolve()
    if path in _INITIALIZED_DATABASES:
        return
    with _DATABASE_INIT_LOCK:
        if path in _INITIALIZED_DATABASES:
            return
        try:
            schema = SCHEMA.read_text()
        except FileNotFoundError as exc:
            raise ConfigError("schema file does not exist: %s" % SCHEMA) from exc
        connection = _connect(path)
        try:
            connection.executescript(schema)
            connection.execute("BEGIN IMMEDIATE")
            trade_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(trades)")
            }
            if "direction" not in trade_columns:
                connection.execute(
                    "ALTER TABLE trades ADD COLUMN direction INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(direction IN (-1, 1))"
                )
            removed_history = _remove_definition_history(connection)
            migrated_outputs = _migrate_outputs(connection)
            if removed_history:
                _log(
                    connection,
                    "info",
                    "removed database definition history; config and git are authoritative",
                )
            if migrated_outputs:
                _log(
                    connection,
                    "info",
                    "migrated live storage to unversioned outputs",
                )
            connection.commit()
        finally:
            connection.close()
        _INITIALIZED_DATABASES.add(path)


def _bars(
    connection: sqlite3.Connection,
    ticker: str,
    ts: int,
    period: int,
    include_interpolated: bool,
) -> list[sqlite3.Row]:
    quality = "" if include_interpolated else "AND interpolated=0"
    rows = connection.execute(
        """
        SELECT ts, open, high, low, close, volume, interpolated
        FROM bars WHERE ticker=? AND ts<=? %s
        ORDER BY ts DESC LIMIT ?
        """ % quality,
        (ticker, ts, period),
    ).fetchall()
    return list(reversed(rows))


def _session_bars(
    connection: sqlite3.Connection,
    ticker: str,
    session_open: int,
    session_close: int,
    through: int,
    *,
    limit: Optional[int] = None,
    include_interpolated: bool = True,
) -> list[sqlite3.Row]:
    quality = "" if include_interpolated else "AND interpolated=0"
    limit_sql = "" if limit is None else "LIMIT ?"
    arguments: tuple[Any, ...] = (ticker, session_open, session_close, through)
    if limit is not None:
        arguments += (limit,)
    return connection.execute(
        """
        SELECT ts,open,high,low,close,volume,interpolated
        FROM bars
        WHERE ticker=? AND ts>=? AND ts<? AND ts<=? %s
        ORDER BY ts %s
        """ % (quality, limit_sql),
        arguments,
    ).fetchall()


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


def _normalize_sma(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    _parameter_keys(parameters, {"field", "period", "include_interpolated"})
    field = parameters.get("field")
    period = parameters.get("period")
    include = parameters.get("include_interpolated")
    if field not in BAR_FIELDS:
        raise ConfigError("field must name a stored bar field")
    return {
        "field": field,
        "period": require_int(period, "period", error=ConfigError),
        "include_interpolated": _boolean(include, "include_interpolated"),
    }


def _normalize_metadata(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    name = parameters.get("name")
    if name not in METADATA_DEFAULTS:
        raise ConfigError("name must be a supported provider indicator")
    supplied = {key: value for key, value in parameters.items() if key != "name"}
    unknown = set(supplied) - set(METADATA_DEFAULTS[name])
    if unknown:
        raise ConfigError(
            "unsupported provider parameters: %s" % ", ".join(sorted(unknown))
        )
    normalized = dict(METADATA_DEFAULTS[name])
    normalized.update(supplied)
    return {"name": name, "query_key": _json(normalized)}


def _normalize_int_parameter(
    parameters: Mapping[str, Any], name: str, minimum: int = 1
) -> Mapping[str, Any]:
    _parameter_keys(parameters, {name})
    return {name: require_int(parameters.get(name), name, minimum, error=ConfigError)}


def _normalize_last_close(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    _parameter_keys(parameters, {"include_interpolated"})
    return {
        "include_interpolated": _boolean(
            parameters.get("include_interpolated"), "include_interpolated"
        )
    }


def _normalize_rvol(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    _parameter_keys(parameters, {"cap_bars", "baseline_sessions"})
    return {
        "cap_bars": require_int(
            parameters.get("cap_bars"), "cap_bars", error=ConfigError
        ),
        "baseline_sessions": require_int(
            parameters.get("baseline_sessions"),
            "baseline_sessions",
            error=ConfigError,
        ),
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
        "window_minutes": require_int(
            parameters.get("window_minutes"),
            "window_minutes",
            error=ConfigError,
        ),
        "baseline_sessions": require_int(
            parameters.get("baseline_sessions"),
            "baseline_sessions",
            error=ConfigError,
        ),
        "min_sessions": require_int(
            parameters.get("min_sessions"),
            "min_sessions",
            error=ConfigError,
        ),
        "time_tolerance_minutes": require_int(
            parameters.get("time_tolerance_minutes"),
            "time_tolerance_minutes",
            minimum=0,
            error=ConfigError,
        ),
        "min_momentum_pct": _number(
            parameters.get("min_momentum_pct"), "min_momentum_pct"
        ),
        "strong_percentile": _number(
            parameters.get("strong_percentile"), "strong_percentile"
        ),
    }
    if result["min_sessions"] > result["baseline_sessions"]:
        raise ConfigError("min_sessions cannot exceed baseline_sessions")
    if result["strong_percentile"] > 100.0:
        raise ConfigError("strong_percentile must be <= 100")
    return result


def _normalize_opening_sentiment(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    _parameter_keys(
        parameters,
        {
            "market_tickers",
            "minutes",
            "min_market_move_pct",
            "require_ticker_agreement",
        },
    )
    return {
        "market_tickers": _configured_tickers(
            parameters.get("market_tickers"), "market_tickers", tickers
        ),
        "minutes": require_int(parameters.get("minutes"), "minutes", error=ConfigError),
        "min_market_move_pct": _number(
            parameters.get("min_market_move_pct"), "min_market_move_pct"
        ),
        "require_ticker_agreement": _boolean(
            parameters.get("require_ticker_agreement"), "require_ticker_agreement"
        ),
    }


def _normalize_pullback(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    names = {
        "early_minutes",
        "early_window_minutes",
        "late_window_minutes",
        "late_market_strength_ratio",
        "max_threshold_ratio",
        "entry_cutoff_minutes",
        "baseline_sessions",
        "min_baseline_sessions",
        "percentile",
        "min_extreme_distance_pct",
    }
    _parameter_keys(parameters, names)
    result: dict[str, Any] = {
        "early_minutes": require_int(
            parameters.get("early_minutes"), "early_minutes", error=ConfigError
        ),
        "early_window_minutes": require_int(
            parameters.get("early_window_minutes"),
            "early_window_minutes",
            error=ConfigError,
        ),
        "late_window_minutes": require_int(
            parameters.get("late_window_minutes"),
            "late_window_minutes",
            error=ConfigError,
        ),
        "late_market_strength_ratio": _number(
            parameters.get("late_market_strength_ratio"),
            "late_market_strength_ratio",
        ),
        "max_threshold_ratio": _number(
            parameters.get("max_threshold_ratio"),
            "max_threshold_ratio",
            minimum=1.0,
        ),
        "entry_cutoff_minutes": require_int(
            parameters.get("entry_cutoff_minutes"),
            "entry_cutoff_minutes",
            minimum=0,
            error=ConfigError,
        ),
        "baseline_sessions": require_int(
            parameters.get("baseline_sessions"),
            "baseline_sessions",
            error=ConfigError,
        ),
        "min_baseline_sessions": require_int(
            parameters.get("min_baseline_sessions"),
            "min_baseline_sessions",
            error=ConfigError,
        ),
        "percentile": _number(parameters.get("percentile"), "percentile"),
        "min_extreme_distance_pct": _number(
            parameters.get("min_extreme_distance_pct"),
            "min_extreme_distance_pct",
        ),
    }
    if result["min_baseline_sessions"] > result["baseline_sessions"]:
        raise ConfigError("min_baseline_sessions cannot exceed baseline_sessions")
    if result["percentile"] > 1.0:
        raise ConfigError("percentile must be <= 1")
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


def _signal_metadata(
    connection, ticker, ts, parameters, inputs, settings=None
) -> Any:
    name = parameters["name"]
    row = connection.execute(
        """
        SELECT value FROM bar_metadata
        WHERE ticker=? AND ts=? AND name=? AND params=?
        """,
        (ticker, ts, name, parameters["query_key"]),
    ).fetchone()
    return json.loads(row["value"]) if row else None


def _session_window(settings: Settings, ts: int) -> tuple[date, int, int]:
    local_day = datetime.fromtimestamp(ts, tz=EASTERN).date()
    session_close_time = (
        settings.early_close
        if local_day in settings.early_close_days
        else settings.regular_close
    )
    session_open = datetime.combine(
        local_day, settings.regular_open, tzinfo=EASTERN
    )
    session_close = datetime.combine(
        local_day, session_close_time, tzinfo=EASTERN
    )
    return local_day, int(session_open.timestamp()), int(session_close.timestamp())


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
    timestamps = [int(row["ts"]) for row in rows]
    cadence = timestamps[1] - timestamps[0] if len(timestamps) > 1 else 0
    expected = (session_close - session_open) // cadence if cadence else 0
    if (
        cadence not in (60, 120, 300)
        or len(rows) != expected
        or timestamps[0] != session_open
        or timestamps[-1] != session_close - cadence
        or any(
            timestamp != session_open + index * cadence
            for index, timestamp in enumerate(timestamps)
        )
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
    candidate = local_day - timedelta(days=1)
    for _ in range(count * 4 + 60):
        summary = _complete_session_summary(
            connection, settings, ticker, candidate
        )
        if summary is not None:
            summaries.append(summary)
            if len(summaries) == count:
                break
        candidate -= timedelta(days=1)
    return summaries


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
    opening = connection.execute(
        "SELECT open FROM bars WHERE ticker=? AND ts=? AND interpolated=0",
        (ticker, int(session["open_ts"])),
    ).fetchone()
    current = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts=? AND interpolated=0",
        (ticker, ts),
    ).fetchone()
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
    closing = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts=? AND interpolated=0",
        (ticker, int(session["open_ts"]) + (bars - 1) * 60),
    ).fetchone()
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
    candidate = local_day - timedelta(days=1)
    for _ in range(baseline_sessions * 4 + 60):
        probe = int(datetime.combine(candidate, clock_time(12), tzinfo=EASTERN).timestamp())
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
        if len(rows) == cap_bars and all(
            int(row["ts"]) == session_open + slot * 60
            for slot, row in enumerate(rows)
        ):
            sessions.append([float(row["volume"]) for row in rows])
            if len(sessions) == baseline_sessions:
                break
        candidate -= timedelta(days=1)
    if len(sessions) < baseline_sessions:
        _RVOL_BASELINES[key] = None
        return None
    baseline = tuple(
        float(statistics.median(session[slot] for session in sessions))
        for slot in range(cap_bars)
    )
    _RVOL_BASELINES[key] = baseline
    return baseline


def _signal_rvol_open(connection, ticker, ts, parameters, inputs, settings) -> Optional[float]:
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
    expected = (session_close - session_open) // 60
    if len(rows) != expected or any(
        int(row["ts"]) != session_open + index * 60
        for index, row in enumerate(rows)
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
    candidate = local_day - timedelta(days=1)
    for _ in range(baseline_sessions * 4 + 60):
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
        candidate -= timedelta(days=1)
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
    if len(rows) != session_minute + 1 or any(
        int(row["ts"]) != session_open + index * 60
        for index, row in enumerate(rows)
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


def _signal_last_close(connection, ticker, ts, parameters, inputs, settings) -> Optional[float]:
    include = bool(parameters["include_interpolated"])
    quality = "" if include else "AND interpolated=0"
    row = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts<=? %s ORDER BY ts DESC LIMIT 1"
        % quality,
        (ticker, ts),
    ).fetchone()
    return float(row["close"]) if row else None


def _session_return_pct(
    connection: sqlite3.Connection,
    ticker: str,
    session_open: int,
    through: int,
) -> Optional[float]:
    opening = connection.execute(
        "SELECT open FROM bars WHERE ticker=? AND ts=? AND interpolated=0",
        (ticker, session_open),
    ).fetchone()
    latest = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts=? AND interpolated=0",
        (ticker, through),
    ).fetchone()
    if opening is None or latest is None or float(opening["open"]) <= 0.0:
        return None
    return (float(latest["close"]) / float(opening["open"]) - 1.0) * 100.0


def _signal_opening_sentiment(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[dict[str, Any]]:
    session = inputs["session"]
    if session is None:
        return None
    markets = parameters["market_tickers"]
    minutes = int(parameters["minutes"])
    min_market_move = float(parameters["min_market_move_pct"])
    require_agreement = bool(parameters["require_ticker_agreement"])
    if int(session["minute"]) < minutes:
        return None

    session_open = int(session["open_ts"])
    opening_end = session_open + (minutes - 1) * 60
    market_opening_returns = [
        _session_return_pct(connection, market, session_open, opening_end)
        for market in markets
    ]
    ticker_return = _session_return_pct(
        connection, ticker, session_open, opening_end
    )
    if ticker_return is None or any(
        value is None for value in market_opening_returns
    ):
        return None
    market_return = float(statistics.median(market_opening_returns))
    market_direction = (
        1
        if market_return >= min_market_move and market_return > 0.0
        else -1
        if market_return <= -min_market_move and market_return < 0.0
        else 0
    )
    agreed = market_direction != 0 and ticker_return * market_direction > 0.0
    direction = market_direction if agreed or not require_agreement else 0

    current_returns = [
        _session_return_pct(connection, market, session_open, ts)
        for market in markets
    ]
    current_market_return = (
        float(statistics.median(current_returns))
        if all(value is not None for value in current_returns)
        else None
    )
    pattern_valid = bool(
        direction
        and current_market_return is not None
        and current_market_return * direction > 0.0
    )
    return {
        "direction": direction,
        "market_direction": market_direction,
        "market_return_pct": market_return,
        "ticker_return_pct": ticker_return,
        "ticker_agreed": agreed,
        "current_market_return_pct": current_market_return,
        "pattern_valid": pattern_valid,
        "minutes": minutes,
    }


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise EvaluationError("percentile requires at least one value")
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    portion = position - lower
    return ordered[lower] + portion * (ordered[upper] - ordered[lower])


def _prior_pullback_baseline(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    local_day: date,
    early_window: int,
    late_window: int,
    early_minutes: int,
    entry_cutoff: int,
    baseline_sessions: int,
    min_baseline_sessions: int,
    percentile: float,
) -> Optional[tuple[float, float]]:
    key = (
        str(settings.database),
        ticker,
        local_day,
        early_window,
        late_window,
        early_minutes,
        entry_cutoff,
        baseline_sessions,
        min_baseline_sessions,
        percentile,
    )
    if key in _PULLBACK_BASELINES:
        return _PULLBACK_BASELINES[key]
    early_values: list[float] = []
    late_values: list[float] = []
    complete_sessions = 0
    candidate = local_day - timedelta(days=1)
    for _ in range(baseline_sessions * 4 + 60):
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
            include_interpolated=False,
        )
        expected = (session_close - session_open) // 60
        continuous = len(rows) == expected and all(
            int(row["ts"]) == session_open + index * 60
            for index, row in enumerate(rows)
        )
        if continuous:
            early_stop = min(early_minutes, len(rows) - entry_cutoff)
            for index in range(early_window, max(early_window, early_stop)):
                early_values.append(
                    abs(
                        float(rows[index]["close"])
                        / float(rows[index - early_window]["close"])
                        - 1.0
                    )
                    * 100.0
                )
            late_start = max(early_minutes, late_window)
            late_stop = max(late_start, len(rows) - entry_cutoff)
            for index in range(late_start, late_stop):
                late_values.append(
                    abs(
                        float(rows[index]["close"])
                        / float(rows[index - late_window]["close"])
                        - 1.0
                    )
                    * 100.0
                )
            complete_sessions += 1
            if complete_sessions == baseline_sessions:
                break
        candidate -= timedelta(days=1)
    if (
        complete_sessions < min_baseline_sessions
        or not early_values
        or not late_values
    ):
        _PULLBACK_BASELINES[key] = None
        return None
    result = (
        _percentile(early_values, percentile),
        _percentile(late_values, percentile),
    )
    _PULLBACK_BASELINES[key] = result
    return result


def _signal_pullback(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[dict[str, Any]]:
    session = inputs["session"]
    sentiment = inputs["opening_sentiment"]
    if session is None or sentiment is None:
        return None
    early_minutes = int(parameters["early_minutes"])
    early_window = int(parameters["early_window_minutes"])
    late_window = int(parameters["late_window_minutes"])
    late_market_strength_ratio = float(parameters["late_market_strength_ratio"])
    max_threshold_ratio = float(parameters["max_threshold_ratio"])
    entry_cutoff = int(parameters["entry_cutoff_minutes"])
    baseline_sessions = int(parameters["baseline_sessions"])
    min_baseline_sessions = int(parameters["min_baseline_sessions"])
    percentile = float(parameters["percentile"])
    min_extreme_distance = float(parameters["min_extreme_distance_pct"])
    minute = int(session["minute"])
    direction = int(sentiment["direction"])
    regime = "early" if minute < early_minutes else "late"
    window = early_window if regime == "early" else late_window
    current = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts=? AND interpolated=0",
        (ticker, ts),
    ).fetchone()
    previous = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts=? AND interpolated=0",
        (ticker, ts - window * 60),
    ).fetchone()
    if current is None:
        return None
    price = float(current["close"])
    movement = (
        (price / float(previous["close"]) - 1.0) * 100.0
        if previous is not None and float(previous["close"]) > 0.0
        else None
    )
    local_day, session_open, _ = _session_window(settings, ts)
    extremes = connection.execute(
        """
        SELECT MAX(high) AS high,MIN(low) AS low
        FROM bars
        WHERE ticker=? AND ts>=? AND ts<=? AND interpolated=0
        """,
        (ticker, session_open, ts),
    ).fetchone()
    running_high = float(extremes["high"]) if extremes["high"] is not None else price
    running_low = float(extremes["low"]) if extremes["low"] is not None else price
    distance = (
        (running_high / price - 1.0) * 100.0
        if direction == 1 and price > 0.0
        else (price / running_low - 1.0) * 100.0
        if direction == -1 and running_low > 0.0
        else 0.0
    )
    baseline = _prior_pullback_baseline(
        connection,
        settings,
        ticker,
        local_day,
        early_window,
        late_window,
        early_minutes,
        entry_cutoff,
        baseline_sessions,
        min_baseline_sessions,
        percentile,
    )
    threshold = (
        baseline[0 if regime == "early" else 1] if baseline is not None else None
    )
    opening_market_return = require_float(
        sentiment.get("market_return_pct"),
        "opening_sentiment.market_return_pct",
        nullable=True,
        error=EvaluationError,
    )
    current_market_return = require_float(
        sentiment.get("current_market_return_pct"),
        "opening_sentiment.current_market_return_pct",
        nullable=True,
        error=EvaluationError,
    )
    market_strength_ratio = (
        abs(current_market_return) / abs(opening_market_return)
        if opening_market_return not in (None, 0.0)
        and current_market_return is not None
        else None
    )
    market_strength_valid = bool(
        regime == "early"
        or (
            market_strength_ratio is not None
            and market_strength_ratio >= late_market_strength_ratio
        )
    )
    threshold_ratio = (
        abs(movement) / threshold
        if movement is not None and threshold is not None and threshold > 0.0
        else None
    )
    setup_candidate = bool(
        direction
        and movement is not None
        and threshold is not None
        and movement * direction <= -threshold
        and threshold_ratio is not None
        and threshold_ratio <= max_threshold_ratio
        and distance >= min_extreme_distance
        and market_strength_valid
    )
    prior_setup = False
    if setup_candidate:
        rows = _session_bars(
            connection,
            ticker,
            session_open,
            ts,
            ts - 60,
            include_interpolated=False,
        )
        row_by_ts = {int(row["ts"]): row for row in rows}
        running_high: Optional[float] = None
        running_low: Optional[float] = None
        for row in rows:
            row_ts = int(row["ts"])
            row_high = float(row["high"])
            row_low = float(row["low"])
            running_high = (
                row_high if running_high is None else max(running_high, row_high)
            )
            running_low = row_low if running_low is None else min(running_low, row_low)
            candidate_minute = (row_ts - session_open) // 60
            in_regime = (
                int(sentiment["minutes"]) < candidate_minute < early_minutes
                if regime == "early"
                else candidate_minute >= early_minutes
            )
            if not in_regime:
                continue
            anchor = row_by_ts.get(row_ts - window * 60)
            if anchor is None or float(anchor["close"]) <= 0.0:
                continue
            candidate_price = float(row["close"])
            candidate_move = (
                candidate_price / float(anchor["close"]) - 1.0
            ) * 100.0
            candidate_distance = (
                (running_high / candidate_price - 1.0) * 100.0
                if direction == 1 and candidate_price > 0.0
                else (candidate_price / running_low - 1.0) * 100.0
                if direction == -1 and running_low > 0.0
                else 0.0
            )
            if (
                candidate_move * direction <= -threshold
                and candidate_distance >= min_extreme_distance
            ):
                prior_setup = True
                break
    trigger = bool(
        direction
        and minute > int(sentiment["minutes"])
        and float(session["to_close"]) > entry_cutoff
        and bool(sentiment["pattern_valid"])
        and setup_candidate
        and not prior_setup
    )
    return {
        "trigger": trigger,
        "direction": direction,
        "regime": regime,
        "window_minutes": window,
        "move_pct": movement,
        "threshold_pct": threshold,
        "threshold_ratio": threshold_ratio,
        "prior_setup": prior_setup,
        "distance_from_extreme_pct": distance,
        "market_strength_ratio": market_strength_ratio,
        "market_strength_valid": market_strength_valid,
        "pattern_valid": bool(sentiment["pattern_valid"]),
        "price": price,
        "ts": ts,
    }


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
    "metadata": SignalSpec(
        _signal_metadata, ("bar_metadata",), _normalize_metadata
    ),
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
    "opening_sentiment": SignalSpec(
        _signal_opening_sentiment,
        ("bars", "session"),
        _normalize_opening_sentiment,
    ),
    "pullback": SignalSpec(
        _signal_pullback,
        ("bars", "session", "opening_sentiment"),
        _normalize_pullback,
    ),
    "shape_v1": SignalSpec(_signal_shape_v1, ("bars",), _normalize_shape),
}


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
        if risk <= 0 or hit_stop or hit_target or float(session["to_close"]) <= flat_minutes:
            return False, True, direction
        return False, False, 0

    if any(_algo_output(row["output"])[0] for row in context.session_outputs):
        return False, False, 0
    if float(session["to_close"]) < entry_cutoff or rvol <= min_rvol:
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
    take_profit = float(context.parameters["take_profit_pct"])
    stop_loss = float(context.parameters["stop_loss_pct"])
    flat_minutes = float(context.parameters["flat_minutes"])
    pattern_exit = bool(context.parameters["pattern_exit"])
    if session is None or price is None:
        return False, False, 0

    if context.open_entries:
        entry = context.open_entries[0]
        direction = int(entry["direction"])
        elapsed = (int(session["ts"]) - int(entry["ts"])) / 60.0
        entry_minute = (int(entry["ts"]) - int(session["open_ts"])) / 60.0
        hold_minutes = early_hold if entry_minute < early_minutes else late_hold
        pnl_pct = direction * (float(price) / float(entry["price"]) - 1.0) * 100.0
        pattern_broke = (
            pattern_exit
            and (sentiment is None or not bool(sentiment["pattern_valid"]))
        )
        if (
            (take_profit > 0.0 and pnl_pct >= take_profit)
            or (stop_loss > 0.0 and pnl_pct <= -stop_loss)
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


def _normalize_range_breakout(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    names = {
        "direction",
        "target_r",
        "min_rvol",
        "entry_cutoff_minutes",
        "flat_minutes",
    }
    _parameter_keys(parameters, names)
    direction = parameters.get("direction")
    if direction not in ("both", "long", "short"):
        raise ConfigError("direction must be both, long, or short")
    return {
        "direction": direction,
        "target_r": _number(parameters.get("target_r"), "target_r"),
        "min_rvol": _number(parameters.get("min_rvol"), "min_rvol"),
        "entry_cutoff_minutes": _number(
            parameters.get("entry_cutoff_minutes"), "entry_cutoff_minutes"
        ),
        "flat_minutes": _number(parameters.get("flat_minutes"), "flat_minutes"),
    }


def _normalize_sentiment_pullback(
    parameters: Mapping[str, Any], tickers: tuple[str, ...]
) -> Mapping[str, Any]:
    names = {
        "early_minutes",
        "early_hold_minutes",
        "late_hold_minutes",
        "take_profit_pct",
        "stop_loss_pct",
        "pattern_exit",
        "flat_minutes",
        "capital_fraction",
    }
    _parameter_keys(parameters, names)
    capital_fraction = _number(
        parameters.get("capital_fraction"), "capital_fraction"
    )
    if capital_fraction <= 0.0 or capital_fraction > 0.5:
        raise ConfigError("capital_fraction must be > 0 and <= 0.5")
    return {
        "early_minutes": require_int(
            parameters.get("early_minutes"), "early_minutes", error=ConfigError
        ),
        "early_hold_minutes": require_int(
            parameters.get("early_hold_minutes"),
            "early_hold_minutes",
            error=ConfigError,
        ),
        "late_hold_minutes": require_int(
            parameters.get("late_hold_minutes"),
            "late_hold_minutes",
            error=ConfigError,
        ),
        "take_profit_pct": _number(
            parameters.get("take_profit_pct"), "take_profit_pct"
        ),
        "stop_loss_pct": _number(
            parameters.get("stop_loss_pct"), "stop_loss_pct"
        ),
        "pattern_exit": _boolean(parameters.get("pattern_exit"), "pattern_exit"),
        "flat_minutes": _number(parameters.get("flat_minutes"), "flat_minutes"),
        "capital_fraction": capital_fraction,
    }


def _normalize_algo_window(
    parameters: Mapping[str, Any],
    tickers: tuple[str, ...],
    float_names: tuple[str, ...],
) -> dict[str, Any]:
    _parameter_keys(parameters, {"minute_min", "minute_max", *float_names})
    result: dict[str, Any] = {
        "minute_min": require_int(
            parameters.get("minute_min"),
            "minute_min",
            minimum=0,
            error=ConfigError,
        ),
        "minute_max": require_int(
            parameters.get("minute_max"), "minute_max", error=ConfigError
        ),
    }
    if result["minute_min"] >= result["minute_max"]:
        raise ConfigError("minute_min must be less than minute_max")
    result.update(
        (name, _number(parameters.get(name), name)) for name in float_names
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
}


def _algo_output(value: Any) -> tuple[bool, bool, int]:
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise EvaluationError("algo output must be [is_entry,is_close_all,direction]")
    is_entry, is_close = value[:2]
    if not isinstance(is_entry, bool) or not isinstance(is_close, bool) or (is_entry and is_close):
        raise EvaluationError("algo actions must be exclusive booleans")
    direction = value[2] if len(value) == 3 else (1 if is_entry or is_close else 0)
    if isinstance(direction, bool) or not isinstance(direction, int) or direction not in (-1, 0, 1):
        raise EvaluationError("algo direction must be -1, 0, or 1")
    if (is_entry or is_close) and direction == 0:
        raise EvaluationError("an algo action must include direction")
    if not (is_entry or is_close) and direction != 0:
        raise EvaluationError("a quiet algo output must use direction 0")
    return is_entry, is_close, direction


def _prior_output(
    connection: sqlite3.Connection, ticker: str, ts: int, kind: str
) -> Any:
    row = connection.execute(
        """
        SELECT output FROM outputs
        WHERE ticker=? AND kind=? AND ts<?
        ORDER BY ts DESC LIMIT 1
        """,
        (ticker, kind, ts),
    ).fetchone()
    return json.loads(row["output"]) if row else None


def _output_state(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    algo: str,
    ts: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _, session_open, _ = _session_window(settings, ts)
    seed_key = (str(settings.database), ticker, algo, session_open)
    if seed_key not in _OUTPUT_SESSION_SEEDS:
        last_close = connection.execute(
            """
            SELECT ts FROM outputs
            WHERE ticker=? AND kind=? AND ts<?
              AND json_extract(output,'$[1]')=1
            ORDER BY ts DESC LIMIT 1
            """,
            (ticker, algo, session_open),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT o.ts, o.output, b.close
            FROM outputs o
            JOIN bars b ON b.ticker=o.ticker AND b.ts=o.ts
            WHERE o.ticker=? AND o.kind=?
              AND o.ts>? AND o.ts<?
              AND json_extract(o.output,'$[0]')=1
            ORDER BY o.ts
            """,
            (
                ticker,
                algo,
                int(last_close["ts"]) if last_close else -1,
                session_open,
            ),
        ).fetchall()
        _OUTPUT_SESSION_SEEDS[seed_key] = tuple(
            (
                int(row["ts"]),
                float(row["close"]),
                _algo_output(json.loads(row["output"]))[2],
            )
            for row in rows
        )
    rows = connection.execute(
        """
        SELECT o.ts, o.output, b.close
        FROM outputs o
        JOIN bars b ON b.ticker=o.ticker AND b.ts=o.ts
        WHERE o.ticker=? AND o.kind=?
          AND o.ts>=? AND o.ts<?
          AND (json_extract(o.output,'$[0]')=1 OR json_extract(o.output,'$[1]')=1)
        ORDER BY o.ts
        """,
        (ticker, algo, session_open, ts),
    ).fetchall()
    prior: list[dict[str, Any]] = []
    open_entries = [
        {"ts": entry_ts, "price": price, "direction": direction}
        for entry_ts, price, direction in _OUTPUT_SESSION_SEEDS[seed_key]
    ]
    for row in rows:
        output = json.loads(row["output"])
        is_entry, is_close, direction = _algo_output(output)
        prior.append({"ts": int(row["ts"]), "output": output})
        if is_close:
            open_entries = []
        elif is_entry:
            open_entries.append(
                {
                    "ts": int(row["ts"]),
                    "price": float(row["close"]),
                    "direction": direction,
                }
            )
    return open_entries, prior


@dataclass
class _EvaluationState:
    ticker: str
    session_open: int
    previous: dict[str, Any]
    open_entries: dict[str, list[dict[str, Any]]]
    session_outputs: dict[str, list[dict[str, Any]]]
    last_ts: Optional[int] = None

    def accepts(self, settings: Settings, ticker: str, ts: int) -> bool:
        _, session_open, _ = _session_window(settings, ts)
        return (
            ticker == self.ticker
            and session_open == self.session_open
            and (self.last_ts is None or ts == self.last_ts + 60)
        )

    def advance(self, result: Mapping[str, Any]) -> None:
        self.previous.update(result["signals"])
        close = float(result["_bar_close"])
        ts = int(result["ts"])
        for name, output in result["algos"].items():
            normalized = _algo_output(output)
            self.previous[name] = list(normalized)
            is_entry, is_close, direction = normalized
            if not (is_entry or is_close):
                continue
            self.session_outputs[name].append(
                {"ts": ts, "output": list(normalized)}
            )
            if is_close:
                self.open_entries[name] = []
            else:
                self.open_entries[name].append(
                    {"ts": ts, "price": close, "direction": direction}
                )
        self.last_ts = ts


def _seed_evaluation_state(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    ts: int,
) -> _EvaluationState:
    previous = {
        str(row["kind"]): json.loads(row["output"])
        for row in connection.execute(
            """
            SELECT kind,output FROM (
                SELECT kind,output,
                       ROW_NUMBER() OVER (PARTITION BY kind ORDER BY ts DESC) AS position
                FROM outputs
                WHERE ticker=? AND ts<?
            )
            WHERE position=1
            """,
            (ticker, ts),
        ).fetchall()
    }
    open_entries: dict[str, list[dict[str, Any]]] = {}
    session_outputs: dict[str, list[dict[str, Any]]] = {}
    for name in settings.enabled_algos():
        positions, outputs = _output_state(connection, settings, ticker, name, ts)
        open_entries[name] = positions
        session_outputs[name] = outputs
    _, session_open, _ = _session_window(settings, ts)
    return _EvaluationState(
        ticker=ticker,
        session_open=session_open,
        previous=previous,
        open_entries=open_entries,
        session_outputs=session_outputs,
    )


def _context_bars(
    connection: sqlite3.Connection,
    ticker: str,
    evaluation_ts: int,
    start_ts: int,
    through_ts: Optional[int],
) -> tuple[Mapping[str, Any], ...]:
    through = evaluation_ts if through_ts is None else min(
        evaluation_ts, through_ts
    )
    if start_ts > through:
        return ()
    rows = connection.execute(
        """
        SELECT ts,open,high,low,close,volume,interpolated
        FROM bars
        WHERE ticker=? AND ts>=? AND ts<=? AND interpolated=0
        ORDER BY ts
        """,
        (ticker, start_ts, through),
    ).fetchall()
    return tuple(rows)


def run_core(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    ts: int,
    algos: Optional[Sequence[str]] = None,
    state: Optional[_EvaluationState] = None,
) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    if ticker not in settings.tickers:
        raise EvaluationError("ticker is not in config: %s" % ticker)
    bar = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts=?", (ticker, ts)
    ).fetchone()
    if bar is None:
        raise EvaluationError("timestamp is not a stored bar: %s %s" % (ticker, ts))
    if state is not None and not state.accepts(settings, ticker, ts):
        raise EvaluationError("evaluation state does not precede %s %s" % (ticker, ts))

    signal_values: dict[str, Any] = {}
    serialized: dict[str, str] = {}
    for name in settings.signal_order:
        node = settings.signals[name]
        inputs = {
            target: None
            if target in ("bars", "bar_metadata", "events")
            else signal_values[target]
            for target in node["inputs"]
        }
        try:
            value = SIGNAL_FUNCTIONS[node["function"]].function(
                connection, ticker, ts, node["params"], inputs, settings
            )
            serialized[name] = _json(value)
        except Exception as exc:
            raise EvaluationError("signal %s failed: %s" % (name, exc)) from exc
        signal_values[name] = value

    selected = settings.enabled_algos() if algos is None else tuple(algos)
    required_algos: set[str] = set()
    for name in selected:
        if name not in settings.algos:
            raise EvaluationError("unknown algo: %s" % name)
        required_algos.update(settings.algo_requirements[name])

    algo_values: dict[str, tuple[bool, bool, int]] = {}
    algo_open_entries: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for name in settings.algo_order:
        if name not in required_algos:
            continue
        node = settings.algos[name]
        inputs: dict[str, Any] = {}
        previous: dict[str, Any] = {
            "_self": state.previous.get(name)
            if state is not None
            else _prior_output(connection, ticker, ts, name)
        }
        for target in node["inputs"]:
            if target in settings.signals:
                inputs[target] = signal_values[target]
            else:
                inputs[target] = list(algo_values[target])
            previous[target] = (
                state.previous.get(target)
                if state is not None
                else _prior_output(connection, ticker, ts, target)
            )
        if state is None:
            open_entries, session_outputs = _output_state(
                connection, settings, ticker, name, ts
            )
        else:
            open_entries = state.open_entries[name]
            session_outputs = state.session_outputs[name]
        position = tuple(open_entries)
        algo_open_entries[name] = position
        try:
            output = ALGO_FUNCTIONS[node["function"]].function(
                AlgoContext(
                    ticker=ticker,
                    ts=ts,
                    parameters=node["params"],
                    inputs=inputs,
                    previous=previous,
                    open_entries=position,
                    session_outputs=tuple(session_outputs),
                    read_bars=lambda start_ts, through_ts=None: _context_bars(
                        connection,
                        ticker,
                        ts,
                        start_ts,
                        through_ts,
                    ),
                )
            )
        except Exception as exc:
            raise EvaluationError("algo %s failed: %s" % (name, exc)) from exc
        try:
            result = _algo_output(output)
        except EvaluationError as exc:
            raise EvaluationError("algo %s returned invalid output: %s" % (name, exc)) from exc
        if result[1]:
            result = (
                (False, True, int(position[0]["direction"]))
                if position
                else (False, False, 0)
            )
        algo_values[name] = result
        serialized[name] = _json(result)

    selected_values = {name: algo_values[name] for name in selected}
    selected_positions = {name: algo_open_entries[name] for name in selected}
    return {
        "ticker": ticker,
        "ts": ts,
        "signals": signal_values,
        "algos": selected_values,
        "_open_entries": selected_positions,
        "_serialized": {
            name: serialized[name]
            for name in (*signal_values, *selected_values)
        },
        "_bar_close": float(bar["close"]),
    }


def core(
    ticker: str,
    ts: int,
    algos: Optional[Sequence[str]] = None,
    *,
    config_path: Path = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Run one read-only deterministic evaluation."""
    config_path = config_path.resolve()
    current = load_settings(config_path)
    connection = _connect(current.database, read_only=True)
    try:
        result = run_core(connection, current, ticker, ts, algos)
        return {key: result[key] for key in ("ticker", "ts", "signals", "algos")}
    finally:
        connection.close()


def _log(
    connection: sqlite3.Connection,
    level: str,
    message: str,
    now: Optional[int] = None,
    once: bool = False,
) -> None:
    if once and connection.execute(
        "SELECT 1 FROM logs WHERE service='algo' AND level=? AND message=? LIMIT 1",
        (level, message),
    ).fetchone():
        return
    connection.execute(
        "INSERT INTO logs(ts,service,level,message) VALUES (?,'algo',?,?)",
        (now if now is not None else int(time.time()), level, message),
    )


def _sync_live_definitions(
    connection: sqlite3.Connection, settings: Settings, now: int
) -> dict[str, Any]:
    """Apply current config definitions immediately."""
    try:
        document = json.loads(settings.content)
    except json.JSONDecodeError as exc:
        raise ConfigError("config content is invalid JSON") from exc
    if not isinstance(document, Mapping):
        raise ConfigError("config must be a JSON object")

    configured_signals = {
        str(name): _json(definition)
        for name, definition in _document_mapping(document, "signals").items()
    }
    stored_signals = {
        str(row["name"]): str(row["definition"])
        for row in connection.execute("SELECT name,definition FROM signals")
    }
    signals_changed = configured_signals != stored_signals
    for name in sorted(set(stored_signals) - set(configured_signals)):
        connection.execute("DELETE FROM signals WHERE name=?", (name,))
    for name, definition in sorted(configured_signals.items()):
        if name not in stored_signals:
            connection.execute(
                "INSERT INTO signals(name,definition,updated_at) VALUES (?,?,?)",
                (name, definition, now),
            )
        elif stored_signals[name] != definition:
            connection.execute(
                "UPDATE signals SET definition=?,updated_at=? WHERE name=?",
                (definition, now, name),
            )

    configured_algos = _algo_snapshots(document)
    stored_algos = {
        str(row["name"]): (str(row["definition"]), str(row["dependencies"]))
        for row in connection.execute(
            "SELECT name,definition,dependencies FROM algos"
        )
    }
    changed_algos: list[str] = []
    for name in sorted(set(stored_algos) - set(configured_algos)):
        connection.execute("DELETE FROM algos WHERE name=?", (name,))
        changed_algos.append(name)
    for name, (definition, dependencies) in sorted(configured_algos.items()):
        previous = stored_algos.get(name)
        if previous is None:
            connection.execute(
                """
                INSERT INTO algos(name,definition,dependencies,active_from)
                VALUES (?,?,?,?)
                """,
                (name, definition, dependencies, now),
            )
            changed_algos.append(name)
        elif previous != (definition, dependencies):
            connection.execute(
                """
                UPDATE algos
                SET definition=?,dependencies=?,active_from=?
                WHERE name=?
                """,
                (definition, dependencies, now, name),
            )
            changed_algos.append(name)

    if signals_changed or changed_algos:
        connection.execute("DELETE FROM outputs")
    if changed_algos:
        placeholders = ",".join("?" for _ in changed_algos)
        connection.execute(
            f"DELETE FROM trades WHERE algo IN ({placeholders})", changed_algos
        )
    return {
        "signals_changed": signals_changed,
        "changed_algos": changed_algos,
    }


def _pending(
    connection: sqlite3.Connection,
    settings: Settings,
    limit: int = CYCLE_PAIR_LIMIT,
    stop_event: Optional[threading.Event] = None,
) -> list[tuple[str, int]]:
    kinds = settings.output_kinds()
    if not kinds or limit < 1:
        return []
    marker = kinds[-1]
    pending: list[tuple[str, int]] = []
    # Respect config priority so the primary chart ticker is not starved by a
    # large historical backfill for alphabetically earlier symbols.
    for ticker in settings.tickers:
        if stop_event is not None and stop_event.is_set():
            break
        latest = connection.execute(
            "SELECT MAX(ts) FROM bars WHERE ticker=?", (ticker,)
        ).fetchone()[0]
        if latest is None:
            continue
        remaining = limit - len(pending)
        rows = connection.execute(
            """
            SELECT b.ts FROM bars b
            WHERE b.ticker=? AND b.ts>=? AND NOT EXISTS (
                SELECT 1 FROM outputs o
                WHERE o.ticker=b.ticker AND o.ts=b.ts
                  AND o.kind=?
            )
            ORDER BY b.ts LIMIT ?
            """,
            (
                ticker,
                int(latest) - settings.evaluation_days * 86_400,
                marker,
                remaining,
            ),
        ).fetchall()
        pending.extend((ticker, int(row["ts"])) for row in rows)
        if len(pending) == limit:
            break
    return pending


def _warm_primary_shape(
    connection: sqlite3.Connection,
    settings: Settings,
) -> tuple[str, int]:
    """Fill the primary ticker's current shape before deep history catches up."""
    shape_entry = next(
        (
            (name, node)
            for name, node in settings.signals.items()
            if node["function"] == "shape_v1"
        ),
        None,
    )
    if shape_entry is None or not settings.tickers:
        return "", 0

    kind, node = shape_entry
    ticker = settings.tickers[0]
    latest = connection.execute(
        "SELECT MAX(ts) FROM bars WHERE ticker=?", (ticker,)
    ).fetchone()[0]
    if latest is None:
        return ticker, 0

    _local_day, session_open, session_close = _session_window(settings, int(latest))
    if int(latest) >= session_close:
        return ticker, 0
    if int(latest) < session_open:
        timestamps = [int(latest)]
    else:
        timestamps = [
            int(row["ts"])
            for row in connection.execute(
                """
                SELECT ts FROM bars
                WHERE ticker=? AND ts>=? AND ts<=?
                ORDER BY ts
                """,
                (ticker, session_open, int(latest)),
            )
        ]
    if not timestamps:
        return ticker, 0

    stored = {
        int(row["ts"])
        for row in connection.execute(
            """
            SELECT ts FROM outputs
            WHERE ticker=? AND kind=? AND ts>=? AND ts<=?
            """,
            (ticker, kind, timestamps[0], timestamps[-1]),
        )
    }
    targets = [ts for ts in timestamps if ts not in stored]
    if not targets:
        return ticker, 0

    computed_at = int(time.time())
    rows = []
    for ts in targets:
        value = _signal_shape_v1(
            connection,
            ticker,
            ts,
            node["params"],
            {"bars": None},
            settings,
        )
        rows.append((ticker, ts, kind, _json(value), computed_at))
    connection.executemany(
        """
        INSERT INTO outputs(ticker,ts,kind,output,computed_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(ticker,ts,kind) DO UPDATE SET
          output=excluded.output,computed_at=excluded.computed_at
        """,
        rows,
    )
    return ticker, len(rows)


def _warm_primary_relative_momentum(
    connection: sqlite3.Connection,
    settings: Settings,
) -> tuple[str, int]:
    """Fill the primary ticker's latest session before deep history catches up."""
    momentum_entry = next(
        (
            (name, node)
            for name, node in settings.signals.items()
            if node["function"] == "relative_momentum"
        ),
        None,
    )
    if momentum_entry is None or not settings.tickers:
        return "", 0

    kind, node = momentum_entry
    ticker = settings.tickers[0]
    latest = connection.execute(
        "SELECT MAX(ts) FROM bars WHERE ticker=?", (ticker,)
    ).fetchone()[0]
    if latest is None:
        return ticker, 0
    _local_day, session_open, session_close = _session_window(settings, int(latest))
    timestamps = [
        int(row["ts"])
        for row in connection.execute(
            """
            SELECT ts FROM bars
            WHERE ticker=? AND ts>=? AND ts<?
            ORDER BY ts
            """,
            (ticker, session_open, session_close),
        )
    ]
    if not timestamps:
        return ticker, 0

    stored = {
        int(row["ts"]): row["output"]
        for row in connection.execute(
            """
            SELECT ts,output FROM outputs
            WHERE ticker=? AND kind=? AND ts>=? AND ts<?
            """,
            (ticker, kind, session_open, session_close),
        )
    }
    targets = [
        ts
        for ts in timestamps
        if ts not in stored or not _relative_momentum_has_score(stored[ts])
    ]
    if not targets:
        return ticker, 0

    computed_at = int(time.time())
    rows = []
    for ts in targets:
        session = _signal_session(connection, ticker, ts, {}, {}, settings)
        value = _signal_relative_momentum(
            connection,
            ticker,
            ts,
            node["params"],
            {"bars": None, "session": session},
            settings,
        )
        rows.append((ticker, ts, kind, _json(value), computed_at))
    connection.executemany(
        """
        INSERT INTO outputs(ticker,ts,kind,output,computed_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(ticker,ts,kind) DO UPDATE SET
          output=excluded.output,computed_at=excluded.computed_at
        """,
        rows,
    )
    return ticker, len(rows)


def _relative_momentum_has_score(output: str) -> bool:
    try:
        value = json.loads(output)
    except (TypeError, ValueError):
        return False
    if not isinstance(value, Mapping):
        return False
    score = value.get("persistence_score")
    return (
        not isinstance(score, bool)
        and isinstance(score, (int, float))
        and math.isfinite(score)
    )


def _repair_relative_momentum_gaps(
    connection: sqlite3.Connection,
    settings: Settings,
    limit: int = 5_000,
) -> int:
    """Recompute stored regular-session gaps with the continuous score."""
    momentum_entry = next(
        (
            (name, node)
            for name, node in settings.signals.items()
            if node["function"] == "relative_momentum"
        ),
        None,
    )
    if momentum_entry is None or limit < 1:
        return 0

    kind, node = momentum_entry
    computed_at = int(time.time())
    repaired_rows = []
    evaluated = 0
    database_key = str(settings.database)
    for ticker in settings.tickers:
        latest = connection.execute(
            "SELECT MAX(ts) FROM bars WHERE ticker=?", (ticker,)
        ).fetchone()[0]
        if latest is None:
            continue
        candidates = connection.execute(
            """
            SELECT ts,output FROM outputs
            WHERE ticker=? AND kind=? AND ts>=?
              AND (
                output='null' OR NOT json_valid(output) OR
                COALESCE(json_type(output, '$.persistence_score'), '')
                  NOT IN ('integer', 'real')
              )
            ORDER BY ts DESC
            """,
            (
                ticker,
                kind,
                int(latest) - settings.evaluation_days * 86_400,
            ),
        ).fetchall()
        for row in candidates:
            ts = int(row["ts"])
            attempt = (database_key, ticker, ts)
            if attempt in _RELATIVE_MOMENTUM_REPAIR_ATTEMPTS:
                continue
            _RELATIVE_MOMENTUM_REPAIR_ATTEMPTS.add(attempt)
            session = _signal_session(connection, ticker, ts, {}, {}, settings)
            if session is None:
                continue
            value = _signal_relative_momentum(
                connection,
                ticker,
                ts,
                node["params"],
                {"bars": None, "session": session},
                settings,
            )
            evaluated += 1
            serialized = _json(value)
            if _relative_momentum_has_score(serialized):
                repaired_rows.append(
                    (ticker, ts, kind, serialized, computed_at)
                )
            if evaluated >= limit:
                break
        if evaluated >= limit:
            break

    if repaired_rows:
        connection.executemany(
            """
            INSERT INTO outputs(ticker,ts,kind,output,computed_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(ticker,ts,kind) DO UPDATE SET
              output=excluded.output,computed_at=excluded.computed_at
            """,
            repaired_rows,
        )
    return len(repaired_rows)


def _write_result(
    connection: sqlite3.Connection,
    settings: Settings,
    result: Mapping[str, Any],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    computed_at = int(time.time())
    rows = []
    for name, value in result["signals"].items():
        rows.append(
            (
                result["ticker"],
                result["ts"],
                name,
                result["_serialized"][name],
                computed_at,
            )
        )
    for name, output in result["algos"].items():
        rows.append(
            (
                result["ticker"],
                result["ts"],
                name,
                result["_serialized"][name],
                computed_at,
            )
        )

    stats = {"pairs": 1, "outputs": len(rows), "entries": 0, "exits": 0}
    alerts: list[dict[str, Any]] = []
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            """
            INSERT INTO outputs(ticker,ts,kind,output,computed_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(ticker,ts,kind) DO UPDATE SET
              output=excluded.output,computed_at=excluded.computed_at
            """,
            rows,
        )
        for name, output in result["algos"].items():
            is_entry, is_close, direction = _algo_output(output)
            action = "exit_all" if is_close else "entry" if is_entry else None
            if action is None:
                continue
            if action == "exit_all":
                open_entries = result["_open_entries"][name]
                if not open_entries:
                    raise EvaluationError("algo %s closed without an open entry" % name)
                if direction != int(open_entries[0]["direction"]):
                    raise EvaluationError("algo %s closed in the wrong direction" % name)
            existing = connection.execute(
                "SELECT action,direction FROM trades WHERE ticker=? AND algo=? AND ts=?",
                (result["ticker"], name, result["ts"]),
            ).fetchall()
            if existing:
                if (
                    len(existing) == 1
                    and existing[0]["action"] == action
                    and int(existing[0]["direction"]) == direction
                ):
                    continue
                raise EvaluationError(
                    "stored trade conflicts with algo output: %s %s %s"
                    % (result["ticker"], name, result["ts"])
                )
            connection.execute(
                "INSERT INTO trades(ticker,algo,ts,action,direction) VALUES (?,?,?,?,?)",
                (result["ticker"], name, result["ts"], action, direction),
            )
            price = connection.execute(
                "SELECT close FROM bars WHERE ticker=? AND ts=?",
                (result["ticker"], result["ts"]),
            ).fetchone()
            alerts.append(
                {
                    "ticker": result["ticker"],
                    "algo": name,
                    "ts": result["ts"],
                    "action": action,
                    "direction": direction,
                    "price": None if price is None else price["close"],
                }
            )
            stats["exits" if action == "exit_all" else "entries"] += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return stats, alerts


def _live_trade_alerts(
    connection: sqlite3.Connection,
    settings: Settings,
    records: Sequence[Mapping[str, Any]],
    resume_after: int,
    now: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Keep only fresh latest-bar trades witnessed by the live service."""
    current = int(time.time()) if now is None else now
    latest_by_session: dict[tuple[str, int, int], Optional[int]] = {}
    eligible = []
    for record in records:
        ticker = str(record["ticker"])
        event_ts = int(record["ts"])
        if event_ts <= resume_after:
            continue
        _day, session_open, session_close = _session_window(settings, event_ts)
        if not (session_open <= event_ts < session_close):
            continue
        session_key = (ticker, session_open, session_close)
        if session_key not in latest_by_session:
            latest_by_session[session_key] = connection.execute(
                "SELECT MAX(ts) FROM bars WHERE ticker=? AND ts>=? AND ts<?",
                session_key,
            ).fetchone()[0]
        if latest_by_session[session_key] != event_ts:
            continue
        if not (session_open <= current < session_close + ALERT_CLOSE_GRACE_SECONDS):
            continue
        eligible.append(dict(record))
    return eligible


def _report_alert_failure(config_path: Path, reason: str) -> None:
    message = "alerting failed: %s" % " ".join(reason.split())
    logging.error(message)
    _best_effort_log(config_path, "error", message)


def _report_broker_result(config_path: Path, level: str, message: str) -> None:
    getattr(logging, level)(message)
    _best_effort_log(config_path, level, message)


def cycle(
    config_path: Path,
    stop_event: Optional[threading.Event] = None,
    alert_resume_after: Optional[int] = None,
) -> tuple[dict[str, int], Settings]:
    _RVOL_BASELINES.clear()
    _PULLBACK_BASELINES.clear()
    _OUTPUT_SESSION_SEEDS.clear()
    _SESSION_SUMMARIES.clear()
    _ATR_SESSION_VALUES.clear()
    _RELATIVE_MOMENTUM_SESSION_FEATURES.clear()
    _RELATIVE_MOMENTUM_BASELINES.clear()
    clear_shape_cache()
    config_path = config_path.resolve()
    current = load_settings(config_path)
    _init_database(current.database)
    connection = _connect(current.database)
    try:
        now = int(time.time())
        settings = current
        definition_sync = _sync_live_definitions(connection, settings, now)
        if definition_sync["signals_changed"] or definition_sync["changed_algos"]:
            _log(
                connection,
                "info",
                "applied live definitions signals_changed=%s algos=%s"
                % (
                    str(definition_sync["signals_changed"]).lower(),
                    ",".join(definition_sync["changed_algos"]) or "none",
                ),
                now,
            )
        connection.commit()

        shape_ticker, shape_rows = _warm_primary_shape(connection, settings)
        warm_ticker, warm_rows = _warm_primary_relative_momentum(
            connection, settings
        )
        repaired_rows = _repair_relative_momentum_gaps(connection, settings)
        if shape_rows:
            message = "priority shape warmup ticker=%s rows=%d" % (
                shape_ticker,
                shape_rows,
            )
            _log(connection, "info", message)
            logging.info(message)
        if warm_rows:
            message = "priority momentum warmup ticker=%s rows=%d" % (
                warm_ticker,
                warm_rows,
            )
            _log(connection, "info", message)
            logging.info(message)
        if repaired_rows:
            message = "continuous momentum repair rows=%d" % repaired_rows
            _log(connection, "info", message)
            logging.info(message)
        if shape_rows or warm_rows or repaired_rows:
            connection.commit()

        stats = {"pairs": 0, "outputs": 0, "entries": 0, "exits": 0}
        states: dict[str, _EvaluationState] = {}
        for ticker, ts in _pending(connection, settings, stop_event=stop_event):
            if stop_event is not None and stop_event.is_set():
                break
            state = states.get(ticker)
            if state is None or not state.accepts(settings, ticker, ts):
                state = _seed_evaluation_state(
                    connection, settings, ticker, ts
                )
                states[ticker] = state
            result = run_core(
                connection, settings, ticker, ts, state=state
            )
            part, trade_alerts = _write_result(connection, settings, result)
            state.advance(result)
            for key in stats:
                stats[key] += part[key]
            if alert_resume_after is not None and trade_alerts:
                live_records = _live_trade_alerts(
                    connection,
                    settings,
                    trade_alerts,
                    alert_resume_after,
                )
                send_trade_alerts(
                    live_records,
                    on_failure=lambda reason: _report_alert_failure(
                        config_path, reason
                    ),
                )
                if settings.broker.enabled:
                    send_broker_orders(
                        live_records,
                        config_path=config_path,
                        account_env=settings.broker.account_env,
                        quantity=settings.broker.quantity,
                        execution_tickers=settings.broker.execution_tickers,
                        on_result=lambda level, message: _report_broker_result(
                            config_path, level, message
                        ),
                    )
            if stats["pairs"] % CYCLE_PROGRESS_INTERVAL == 0:
                progress = "cycle progress pairs=%d outputs=%d entries=%d exits=%d" % (
                    stats["pairs"], stats["outputs"], stats["entries"], stats["exits"]
                )
                _log(connection, "info", progress)
                connection.commit()
                logging.info(progress)
        label = (
            "cycle stopped"
            if stop_event is not None and stop_event.is_set()
            else "cycle batch complete"
        )
        message = "%s pairs=%d outputs=%d entries=%d exits=%d" % (
            label,
            stats["pairs"], stats["outputs"], stats["entries"], stats["exits"]
        )
        if stats["pairs"]:
            _log(connection, "info", message)
            connection.commit()
            logging.info(message)
        else:
            logging.debug(message)
        return stats, settings
    finally:
        connection.close()


def _best_effort_log(
    config_path: Path,
    level: str,
    message: str,
    database: Optional[Path] = None,
) -> None:
    try:
        database_path = database
        if database_path is None:
            database_path = load_settings(config_path).database
        _init_database(database_path)
        connection = _connect(database_path)
        try:
            _log(connection, level, message)
            connection.commit()
        finally:
            connection.close()
    except Exception:
        logging.exception("could not write algo log")


def status(settings: Settings) -> str:
    connection = _connect(settings.database, read_only=True)
    try:
        lines = ["ticker  bars      outputs   latest"]
        for ticker in settings.tickers:
            row = connection.execute(
                "SELECT COUNT(*) AS count,MAX(ts) AS latest FROM bars WHERE ticker=?",
                (ticker,),
            ).fetchone()
            outputs = connection.execute(
                "SELECT COUNT(*) FROM outputs WHERE ticker=?",
                (ticker,),
            ).fetchone()[0]
            latest = datetime.fromtimestamp(row["latest"], UTC).isoformat() if row["latest"] else "-"
            lines.append("%-7s %-9d %-9d %s" % (ticker, row["count"], outputs, latest))
        trades = connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    finally:
        connection.close()
    lines.extend(
        (
            "enabled signals %d" % len(settings.enabled_signals()),
            "enabled algos %d" % len(settings.enabled_algos()),
            "trades %d" % trades,
            "database %s" % settings.database,
        )
    )
    return "\n".join(lines)


class Service:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.settings = load_settings(self.config_path)
        self.stop_event = threading.Event()
        self.started_at = int(time.time())
        self.alert_resume_after = self.started_at

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": "algo",
            "status": "running",
            "ts": int(time.time()),
            "started_at": self.started_at,
            "pid": os.getpid(),
        }

    def stop(self, signum=None, frame=None) -> None:
        self.stop_event.set()

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        _init_database(self.settings.database)
        health_server = ThreadingHTTPServer(
            ("127.0.0.1", self.settings.api_port), _HealthHandler
        )
        health_server.service = self
        health_thread = threading.Thread(
            target=health_server.serve_forever,
            name="algo-health-api",
            daemon=True,
        )
        health_thread.start()
        delay = 30
        try:
            logging.info("service started api=127.0.0.1:%d", self.settings.api_port)
            _best_effort_log(self.config_path, "info", "service started")
            while not self.stop_event.is_set():
                try:
                    observed = load_settings(self.config_path)
                    if observed.content != self.settings.content:
                        self.alert_resume_after = int(time.time())
                    stats, settings = cycle(
                        self.config_path,
                        self.stop_event,
                        alert_resume_after=self.alert_resume_after,
                    )
                    self.settings = settings
                    delay = (
                        0
                        if stats["pairs"] == CYCLE_PAIR_LIMIT
                        else settings.poll_seconds
                    )
                except ConfigError as exc:
                    logging.exception("configuration reload failed")
                    _best_effort_log(
                        self.config_path,
                        "error",
                        "configuration reload failed: %s" % exc,
                        database=self.settings.database,
                    )
                    raise
                except Exception as exc:
                    delay = 30
                    logging.exception("cycle failed")
                    _best_effort_log(self.config_path, "error", "cycle failed: %s" % exc)
                self.stop_event.wait(delay)
            _best_effort_log(self.config_path, "info", "service stopped")
        finally:
            health_server.shutdown()
            health_server.server_close()
            health_thread.join(timeout=2)


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "AlgoHealth/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/health":
            self.send_error(404)
            return
        body = json.dumps(
            self.server.service.health(), separators=(",", ":")
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def _timestamp(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigError("timestamp must be epoch seconds or ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ConfigError("ISO timestamp must include a timezone")
        return int(parsed.astimezone(UTC).timestamp())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("once")
    commands.add_parser("status")
    commands.add_parser("validate")
    evaluate = commands.add_parser("core")
    evaluate.add_argument("ticker")
    evaluate.add_argument("timestamp")
    evaluate.add_argument("--algo", action="append", dest="algos")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config_path = arguments.config.expanduser().resolve()
    try:
        if arguments.command == "serve":
            Service(config_path).run()
        elif arguments.command == "once":
            stats, settings = cycle(config_path)
            print(
                "processed pairs=%d outputs=%d entries=%d exits=%d"
                % (stats["pairs"], stats["outputs"], stats["entries"], stats["exits"])
            )
            print(status(settings))
        elif arguments.command == "status":
            print(status(load_settings(config_path)))
        elif arguments.command == "validate":
            settings = load_settings(config_path)
            print(
                "valid signals=%d algos=%d"
                % (len(settings.signals), len(settings.algos))
            )
        elif arguments.command == "core":
            print(
                json.dumps(
                    core(
                        arguments.ticker,
                        _timestamp(arguments.timestamp),
                        arguments.algos,
                        config_path=config_path,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        return 0
    except (ConfigError, EvaluationError, sqlite3.Error) as exc:
        logging.error("%s", exc)
        if arguments.command == "once":
            _best_effort_log(config_path, "error", "once failed: %s" % exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
