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

from shape_signal import clear_shape_cache, shape_v1


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT.parent / "config" / "config.json"
SCHEMA = ROOT.parent / "config" / "schema.sql"
UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
BAR_FIELDS = {"open", "high", "low", "close", "volume"}
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


class ConfigError(ValueError):
    pass


class EvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    version: str
    database: Path
    tickers: tuple[str, ...]
    evaluation_days: int
    poll_seconds: int
    api_port: int
    signals: Mapping[str, Mapping[str, Any]]
    algos: Mapping[str, Mapping[str, Any]]
    document: Mapping[str, Any]
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


def _positive_int(value: Any, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError("%s must be an integer >= %d" % (name, minimum))
    return value


def _nodes(document: Mapping[str, Any], key: str) -> dict[str, Mapping[str, Any]]:
    raw = document.get(key, {})
    if not isinstance(raw, dict):
        raise ConfigError("%s must be an object keyed by node name" % key)
    nodes: dict[str, Mapping[str, Any]] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or not isinstance(value, dict):
            raise ConfigError("every %s entry must be a named object" % key)
        function = value.get("function", name)
        params = value.get("params", {})
        inputs = value.get("inputs", [])
        trades = value.get("trades", False) if key == "algos" else False
        if not isinstance(function, str) or not function:
            raise ConfigError("%s.%s.function must be a name" % (key, name))
        if not isinstance(params, dict) or not isinstance(inputs, list):
            raise ConfigError("%s.%s params must be an object and inputs must be a list" % (key, name))
        if not all(isinstance(target, str) and target for target in inputs):
            raise ConfigError("%s.%s inputs must contain node names" % (key, name))
        if not isinstance(trades, bool):
            raise ConfigError("%s.%s.trades must be boolean" % (key, name))
        nodes[name] = {
            "function": function,
            "params": params,
            "inputs": inputs,
            "trades": trades,
        }
    return nodes


def _check_dependencies(settings: Settings) -> None:
    visiting: set[tuple[str, str]] = set()
    complete: set[tuple[str, str]] = set()

    def visit(layer: str, name: str) -> None:
        key = (layer, name)
        if key in complete:
            return
        if key in visiting:
            raise ConfigError("dependency cycle at %s" % name)
        nodes = settings.signals if layer == "signal" else settings.algos
        node = nodes[name]
        visiting.add(key)
        for target in node["inputs"]:
            if layer == "signal" and target in ("bars", "bar_metadata", "events"):
                continue
            if target in settings.signals:
                visit("signal", target)
            elif layer == "algo" and target in settings.algos:
                visit("algo", target)
            else:
                raise ConfigError("%s references unknown node %s" % (name, target))
        visiting.remove(key)
        complete.add(key)

    for name in settings.enabled_signals():
        visit("signal", name)
    for name in settings.enabled_algos():
        visit("algo", name)


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

    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, (str, int)) or not str(version):
        raise ConfigError("version must be a non-empty string or integer")
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
    database_value = document.get("database", "../data/trader.sqlite3")
    if not isinstance(database_value, str) or not database_value:
        raise ConfigError("database must be a path string")
    database = Path(database_value).expanduser()
    if not database.is_absolute():
        database = (path.parent / database).resolve()
    if database_override is not None:
        database = database_override

    settings = Settings(
        version=str(version),
        database=database,
        tickers=tuple(normalized),
        evaluation_days=_positive_int(algo.get("evaluation_days", 60), "algo.evaluation_days"),
        poll_seconds=_positive_int(algo.get("poll_seconds", 30), "algo.poll_seconds"),
        api_port=_positive_int(algo.get("api_port", 8791), "algo.api_port", 1024),
        signals=_nodes(document, "signals"),
        algos=_nodes(document, "algos"),
        document=document,
        content=_json(document),
    )
    if set(settings.signals) & set(settings.algos):
        raise ConfigError("signal and algo names must be unique")
    unknown_signals = {
        node["function"]
        for node in settings.signals.values()
        if node["inputs"] != ["bar_metadata"]
    } - set(SIGNAL_FUNCTIONS)
    unknown_algos = {
        node["function"] for node in settings.algos.values()
    } - set(ALGO_FUNCTIONS)
    if unknown_signals or unknown_algos:
        raise ConfigError(
            "unknown functions: %s" % ", ".join(sorted(unknown_signals | unknown_algos))
        )
    _check_dependencies(settings)
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


def _init_database(path: Path) -> None:
    try:
        schema = SCHEMA.read_text()
    except FileNotFoundError as exc:
        raise ConfigError("schema file does not exist: %s" % SCHEMA) from exc
    connection = _connect(path)
    try:
        connection.executescript(schema)
        trade_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(trades)")
        }
        if "direction" not in trade_columns:
            connection.execute(
                "ALTER TABLE trades ADD COLUMN direction INTEGER NOT NULL DEFAULT 1 "
                "CHECK(direction IN (-1, 1))"
            )
        connection.commit()
    finally:
        connection.close()


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


def _bar_parameters(parameters: Mapping[str, Any]) -> tuple[str, int, bool]:
    field = parameters.get("field")
    period = parameters.get("period")
    include = parameters.get("include_interpolated")
    if field not in BAR_FIELDS:
        raise EvaluationError("field must name a stored bar field")
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise EvaluationError("period must be a positive integer")
    if not isinstance(include, bool):
        raise EvaluationError("include_interpolated must be boolean")
    return field, period, include


def _signal_sma(
    connection, ticker, ts, parameters, inputs, settings=None
) -> Optional[float]:
    if list(inputs) != ["bars"]:
        raise EvaluationError("sma inputs must be ['bars']")
    field, period, include = _bar_parameters(parameters)
    rows = _bars(connection, ticker, ts, period, include)
    if len(rows) < period:
        return None
    return sum(float(row[field]) for row in rows) / period


def _signal_metadata(
    connection, ticker, ts, parameters, inputs, settings=None
) -> Any:
    if list(inputs) != ["bar_metadata"]:
        raise EvaluationError("provider metadata input must be ['bar_metadata']")
    name = parameters.get("name")
    if name not in METADATA_DEFAULTS:
        raise EvaluationError("name must be a supported provider indicator")
    supplied = {key: value for key, value in parameters.items() if key != "name"}
    unknown = set(supplied) - set(METADATA_DEFAULTS[name])
    if unknown:
        raise EvaluationError(
            "unsupported provider parameters: %s" % ", ".join(sorted(unknown))
        )
    normalized = dict(METADATA_DEFAULTS[name])
    normalized.update(supplied)
    row = connection.execute(
        """
        SELECT value FROM bar_metadata
        WHERE ticker=? AND ts=? AND name=? AND params=?
        """,
        (ticker, ts, name, _json(normalized)),
    ).fetchone()
    return json.loads(row["value"]) if row else None


def _configured_time(settings: Settings, key: str, default: str) -> clock_time:
    polling = settings.document.get("live_polling", {})
    if not isinstance(polling, dict):
        raise EvaluationError("live_polling must be an object")
    try:
        return clock_time.fromisoformat(str(polling.get(key, default)))
    except ValueError as exc:
        raise EvaluationError("live_polling.%s must be HH:MM" % key) from exc


def _session_window(settings: Settings, ts: int) -> tuple[date, int, int]:
    local_day = datetime.fromtimestamp(ts, tz=EASTERN).date()
    early_closes = settings.document.get("early_closes", [])
    if not isinstance(early_closes, list):
        raise EvaluationError("early_closes must be a list")
    close_key = "early_close" if local_day.isoformat() in {
        str(value) for value in early_closes
    } else "regular_close"
    session_open = datetime.combine(
        local_day, _configured_time(settings, "regular_open", "09:30"), tzinfo=EASTERN
    )
    session_close = datetime.combine(
        local_day,
        _configured_time(
            settings, close_key, "13:00" if close_key == "early_close" else "16:00"
        ),
        tzinfo=EASTERN,
    )
    return local_day, int(session_open.timestamp()), int(session_close.timestamp())


def _signal_session(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[dict[str, Any]]:
    if parameters or inputs:
        raise EvaluationError("session takes no inputs or params")
    local_day, session_open, session_close = _session_window(settings, ts)
    if ts < session_open or ts >= session_close:
        return None
    return {
        "date": local_day.isoformat(),
        "minute": int((ts - session_open) // 60),
        "to_close": (session_close - ts) / 60.0,
    }


def _whole_number(parameters: Mapping[str, Any], name: str, minimum: int = 1) -> int:
    value = parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvaluationError("%s must be an integer >= %d" % (name, minimum))
    return value


def _signal_opening_range(
    connection, ticker, ts, parameters, inputs, settings
) -> Optional[dict[str, float]]:
    if list(inputs) != ["bars", "session"]:
        raise EvaluationError("opening_range inputs must be ['bars', 'session']")
    minutes = _whole_number(parameters, "minutes")
    session = inputs["session"]
    if session is None or session.get("minute", -1) < minutes:
        return None
    _, session_open, session_close = _session_window(settings, ts)
    rows = connection.execute(
        """
        SELECT high, low FROM bars
        WHERE ticker=? AND ts>=? AND ts<? AND ts<=? AND interpolated=0
        ORDER BY ts LIMIT ?
        """,
        (ticker, session_open, session_close, ts, minutes),
    ).fetchall()
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
        rows = connection.execute(
            """
            SELECT volume FROM bars
            WHERE ticker=? AND ts>=? AND ts<? AND interpolated=0
            ORDER BY ts LIMIT ?
            """,
            (ticker, session_open, session_close, cap_bars),
        ).fetchall()
        if len(rows) == cap_bars:
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
    if list(inputs) != ["bars", "session"]:
        raise EvaluationError("rvol_open inputs must be ['bars', 'session']")
    cap_bars = _whole_number(parameters, "cap_bars")
    baseline_sessions = _whole_number(parameters, "baseline_sessions")
    session = inputs["session"]
    if session is None:
        return None
    local_day, session_open, session_close = _session_window(settings, ts)
    rows = connection.execute(
        """
        SELECT volume FROM bars
        WHERE ticker=? AND ts>=? AND ts<? AND ts<=? AND interpolated=0
        ORDER BY ts LIMIT ?
        """,
        (ticker, session_open, session_close, ts, cap_bars),
    ).fetchall()
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


def _signal_last_close(connection, ticker, ts, parameters, inputs, settings) -> Optional[float]:
    if list(inputs) != ["bars"]:
        raise EvaluationError("last_close inputs must be ['bars']")
    include = parameters.get("include_interpolated")
    if not isinstance(include, bool):
        raise EvaluationError("include_interpolated must be boolean")
    quality = "" if include else "AND interpolated=0"
    row = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts<=? %s ORDER BY ts DESC LIMIT 1"
        % quality,
        (ticker, ts),
    ).fetchone()
    return float(row["close"]) if row else None


def _signal_shape_v1(connection, ticker, ts, parameters, inputs, settings) -> Any:
    return shape_v1(
        connection,
        ticker,
        ts,
        parameters,
        inputs,
        settings,
        _session_window,
    )


SIGNAL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sma": _signal_sma,
    "session": _signal_session,
    "opening_range": _signal_opening_range,
    "rvol_open": _signal_rvol_open,
    "last_close": _signal_last_close,
    "shape_v1": _signal_shape_v1,
}


def _number(value: Any, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EvaluationError("%s must be a finite number or null" % name)
    return float(value)


def _algo_crossover(
    connection, ticker, ts, name, version, parameters, inputs, previous,
    open_entries, session_outputs, settings,
):
    if len(inputs) != 2:
        raise EvaluationError("crossover requires [fast, slow] inputs")
    fast_name, slow_name = inputs
    fast = _number(inputs[fast_name], "fast")
    slow = _number(inputs[slow_name], "slow")
    old_fast = _number(previous.get(fast_name), "previous fast")
    old_slow = _number(previous.get(slow_name), "previous slow")
    if None in (fast, slow, old_fast, old_slow):
        return False, False, 0
    is_entry = old_fast <= old_slow and fast > slow
    is_close = old_fast >= old_slow and fast < slow
    direction = 1 if is_entry else (
        int(open_entries[0]["direction"]) if is_close and open_entries else 1 if is_close else 0
    )
    return is_entry, is_close, direction


def _parameter_number(
    parameters: Mapping[str, Any], name: str, *, minimum: float = 0.0
) -> float:
    value = parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError("%s must be a number" % name)
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise EvaluationError("%s must be finite and >= %g" % (name, minimum))
    return number


def _algo_range_breakout(
    connection, ticker, ts, name, version, parameters, inputs, previous,
    open_entries, session_outputs, settings,
):
    if len(inputs) != 4:
        raise EvaluationError(
            "range_breakout requires [session, opening_range, rvol_open, last_close] inputs"
        )
    session_name, range_name, rvol_name, price_name = inputs
    session = inputs[session_name]
    opening_range = inputs[range_name]
    rvol = _number(inputs[rvol_name], "rvol_open")
    price = _number(inputs[price_name], "last_close")
    direction_param = parameters.get("direction")
    if direction_param not in ("both", "long", "short"):
        raise EvaluationError("direction must be both, long, or short")
    target_r = _parameter_number(parameters, "target_r", minimum=0.0)
    min_rvol = _parameter_number(parameters, "min_rvol", minimum=0.0)
    entry_cutoff = _parameter_number(parameters, "entry_cutoff_minutes", minimum=0.0)
    flat_minutes = _parameter_number(parameters, "flat_minutes", minimum=0.0)
    if session is None or opening_range is None or rvol is None or price is None:
        return False, False, 0

    if open_entries:
        entry = open_entries[0]
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

    if any(_algo_output(row["output"])[0] for row in session_outputs):
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


ALGO_FUNCTIONS: dict[str, Callable[..., tuple[bool, bool, int]]] = {
    "crossover": _algo_crossover,
    "range_breakout": _algo_range_breakout,
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
    connection: sqlite3.Connection, ticker: str, ts: int, kind: str, version: str
) -> Any:
    row = connection.execute(
        """
        SELECT output FROM outputs
        WHERE ticker=? AND kind=? AND config=? AND ts<?
        ORDER BY ts DESC LIMIT 1
        """,
        (ticker, kind, version, ts),
    ).fetchone()
    return json.loads(row["output"]) if row else None


def _trade_open_entries(
    connection: sqlite3.Connection, ticker: str, algo: str, ts: int
) -> list[dict[str, Any]]:
    last_exit = connection.execute(
        "SELECT MAX(ts) FROM trades WHERE ticker=? AND algo=? AND action='exit_all' AND ts<?",
        (ticker, algo, ts),
    ).fetchone()[0]
    rows = connection.execute(
        """
        SELECT tr.ts, tr.direction, b.close FROM trades tr
        JOIN bars b ON b.ticker=tr.ticker AND b.ts=tr.ts
        WHERE tr.ticker=? AND tr.algo=? AND tr.action='entry'
          AND tr.ts>? AND tr.ts<? ORDER BY tr.ts
        """,
        (ticker, algo, last_exit if last_exit is not None else -1, ts),
    ).fetchall()
    return [
        {
            "ts": int(row["ts"]),
            "price": float(row["close"]),
            "direction": int(row["direction"]),
        }
        for row in rows
    ]


def _output_state(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    algo: str,
    ts: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _, session_open, _ = _session_window(settings, ts)
    rows = connection.execute(
        """
        SELECT o.ts, o.output, b.close
        FROM outputs o
        JOIN bars b ON b.ticker=o.ticker AND b.ts=o.ts
        WHERE o.ticker=? AND o.kind=? AND o.config=?
          AND o.ts>=? AND o.ts<?
          AND (substr(o.output,1,6)='[true,' OR substr(o.output,1,12)='[false,true,')
        ORDER BY o.ts
        """,
        (ticker, algo, settings.version, session_open, ts),
    ).fetchall()
    prior: list[dict[str, Any]] = []
    open_entries: list[dict[str, Any]] = []
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


def run_core(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    ts: int,
    algos: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    if ticker not in settings.tickers:
        raise EvaluationError("ticker is not in config: %s" % ticker)
    if connection.execute("SELECT 1 FROM bars WHERE ticker=? AND ts=?", (ticker, ts)).fetchone() is None:
        raise EvaluationError("timestamp is not a stored bar: %s %s" % (ticker, ts))

    values: dict[str, Any] = {}
    signal_values: dict[str, Any] = {}
    algo_values: dict[str, tuple[bool, bool, int]] = {}
    visiting: set[str] = set()

    def evaluate_signal(name: str) -> Any:
        if name in values:
            return values[name]
        if name in visiting:
            raise EvaluationError("dependency cycle at %s" % name)
        node = settings.signals[name]
        visiting.add(name)
        inputs = {
            target: None
            if target in ("bars", "bar_metadata", "events")
            else evaluate_signal(target)
            for target in node["inputs"]
        }
        try:
            function = (
                _signal_metadata
                if node["inputs"] == ["bar_metadata"]
                else SIGNAL_FUNCTIONS[node["function"]]
            )
            value = function(
                connection, ticker, ts, node["params"], inputs, settings
            )
            _json(value)
        except Exception as exc:
            raise EvaluationError("signal %s failed: %s" % (name, exc)) from exc
        visiting.remove(name)
        values[name] = value
        signal_values[name] = value
        return value

    def evaluate_algo(name: str) -> tuple[bool, bool, int]:
        if name in algo_values:
            return algo_values[name]
        if name in visiting:
            raise EvaluationError("dependency cycle at %s" % name)
        node = settings.algos[name]
        visiting.add(name)
        inputs: dict[str, Any] = {}
        previous: dict[str, Any] = {
            "_self": _prior_output(connection, ticker, ts, name, settings.version)
        }
        for target in node["inputs"]:
            if target in settings.signals:
                inputs[target] = evaluate_signal(target)
            else:
                output = evaluate_algo(target)
                inputs[target] = list(output)
            previous[target] = _prior_output(connection, ticker, ts, target, settings.version)
        open_entries, session_outputs = _output_state(
            connection, settings, ticker, name, ts
        )
        try:
            output = ALGO_FUNCTIONS[node["function"]](
                connection,
                ticker,
                ts,
                name,
                settings.version,
                node["params"],
                inputs,
                previous,
                open_entries,
                session_outputs,
                settings,
            )
        except Exception as exc:
            raise EvaluationError("algo %s failed: %s" % (name, exc)) from exc
        try:
            result = _algo_output(output)
        except EvaluationError as exc:
            raise EvaluationError("algo %s returned invalid output: %s" % (name, exc)) from exc
        visiting.remove(name)
        algo_values[name] = result
        values[name] = list(result)
        return result

    for name in settings.enabled_signals():
        evaluate_signal(name)
    selected = settings.enabled_algos() if algos is None else tuple(algos)
    for name in selected:
        if name not in settings.algos:
            raise EvaluationError("unknown algo: %s" % name)
        evaluate_algo(name)
    selected_values = {name: algo_values[name] for name in selected}
    return {"ticker": ticker, "ts": ts, "signals": signal_values, "algos": selected_values}


def _effective_settings(
    connection: sqlite3.Connection, current: Settings, config_path: Path
) -> tuple[Settings, bool]:
    row = connection.execute(
        "SELECT content FROM configs WHERE version=?", (current.version,)
    ).fetchone()
    if row is None:
        return current, False
    try:
        document = json.loads(row["content"])
    except json.JSONDecodeError as exc:
        raise ConfigError("stored config %s is invalid JSON" % current.version) from exc
    if _json(document) == current.content:
        return current, False
    stored = load_settings(config_path, document, current.database)
    if stored.version != current.version:
        raise ConfigError("stored config version does not match its row key")
    return stored, True


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
        settings, _ = _effective_settings(connection, current, config_path)
        return run_core(connection, settings, ticker, ts, algos)
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


def _pending(connection: sqlite3.Connection, settings: Settings) -> list[tuple[str, int]]:
    kinds = settings.output_kinds()
    if not kinds:
        return []
    tickers = ",".join("?" for _ in settings.tickers)
    names = ",".join("?" for _ in kinds)
    query = """
        WITH latest AS (
            SELECT ticker, MAX(ts) AS ts FROM bars
            WHERE ticker IN (%s) GROUP BY ticker
        )
        SELECT b.ticker, b.ts FROM bars b JOIN latest l ON l.ticker=b.ticker
        WHERE b.ts>=l.ts-? AND (
            SELECT COUNT(*) FROM outputs o
            WHERE o.ticker=b.ticker AND o.ts=b.ts AND o.config=?
              AND o.kind IN (%s)
        )<? ORDER BY b.ticker,b.ts
    """ % (tickers, names)
    arguments = (
        *settings.tickers,
        settings.evaluation_days * 86_400,
        settings.version,
        *kinds,
        len(kinds),
    )
    return [(row["ticker"], int(row["ts"])) for row in connection.execute(query, arguments)]


def _write_result(
    connection: sqlite3.Connection,
    settings: Settings,
    result: Mapping[str, Any],
) -> dict[str, int]:
    computed_at = int(time.time())
    rows = []
    for name, value in result["signals"].items():
        rows.append((result["ticker"], result["ts"], name, settings.version, _json(value), computed_at))
    for name, output in result["algos"].items():
        rows.append((result["ticker"], result["ts"], name, settings.version, _json(output), computed_at))

    stats = {"pairs": 1, "outputs": len(rows), "entries": 0, "exits": 0}
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            """
            INSERT INTO outputs(ticker,ts,kind,config,output,computed_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(ticker,ts,kind,config) DO UPDATE SET
              output=excluded.output,computed_at=excluded.computed_at
            """,
            rows,
        )
        latest = connection.execute(
            "SELECT MAX(ts) FROM bars WHERE ticker=?", (result["ticker"],)
        ).fetchone()[0]
        if latest == result["ts"]:
            for name, output in result["algos"].items():
                if not settings.algos[name]["trades"]:
                    continue
                is_entry, is_close, direction = _algo_output(output)
                action = "exit_all" if is_close else "entry" if is_entry else None
                if action is None:
                    continue
                if action == "exit_all":
                    live_entries = _trade_open_entries(
                        connection, result["ticker"], name, result["ts"]
                    )
                    if not live_entries:
                        continue
                    direction = int(live_entries[0]["direction"])
                exists = connection.execute(
                    "SELECT 1 FROM trades WHERE ticker=? AND algo=? AND ts=?",
                    (result["ticker"], name, result["ts"]),
                ).fetchone()
                if exists:
                    continue
                connection.execute(
                    "INSERT INTO trades(ticker,algo,ts,action,direction) VALUES (?,?,?,?,?)",
                    (result["ticker"], name, result["ts"], action, direction),
                )
                stats["exits" if action == "exit_all" else "entries"] += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return stats


def cycle(config_path: Path) -> tuple[dict[str, int], Settings]:
    _RVOL_BASELINES.clear()
    clear_shape_cache()
    config_path = config_path.resolve()
    current = load_settings(config_path)
    _init_database(current.database)
    connection = _connect(current.database)
    try:
        now = int(time.time())
        row = connection.execute(
            "SELECT 1 FROM configs WHERE version=?", (current.version,)
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO configs(version,first_seen,content) VALUES (?,?,?)",
                (current.version, now, current.content),
            )
            settings, mismatch = current, False
        else:
            settings, mismatch = _effective_settings(connection, current, config_path)
        if mismatch:
            _log(
                connection,
                "warn",
                "config version %s changed without a version bump; using stored content"
                % current.version,
                now,
                once=True,
            )
        connection.commit()

        stats = {"pairs": 0, "outputs": 0, "entries": 0, "exits": 0}
        for ticker, ts in _pending(connection, settings):
            part = _write_result(connection, settings, run_core(connection, settings, ticker, ts))
            for key in stats:
                stats[key] += part[key]
        message = "cycle complete pairs=%d outputs=%d entries=%d exits=%d" % (
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


def _best_effort_log(config_path: Path, level: str, message: str) -> None:
    try:
        settings = load_settings(config_path)
        _init_database(settings.database)
        connection = _connect(settings.database)
        try:
            _log(connection, level, message)
            connection.commit()
        finally:
            connection.close()
    except Exception:
        logging.exception("could not write algo log")


def status(settings: Settings) -> str:
    _init_database(settings.database)
    connection = _connect(settings.database)
    try:
        lines = ["ticker  bars      outputs   latest"]
        for ticker in settings.tickers:
            row = connection.execute(
                "SELECT COUNT(*) AS count,MAX(ts) AS latest FROM bars WHERE ticker=?",
                (ticker,),
            ).fetchone()
            outputs = connection.execute(
                "SELECT COUNT(*) FROM outputs WHERE ticker=? AND config=?",
                (ticker, settings.version),
            ).fetchone()[0]
            latest = datetime.fromtimestamp(row["latest"], UTC).isoformat() if row["latest"] else "-"
            lines.append("%-7s %-9d %-9d %s" % (ticker, row["count"], outputs, latest))
        trades = connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    finally:
        connection.close()
    lines.extend(
        (
            "version %s" % settings.version,
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
                    _, settings = cycle(self.config_path)
                    delay = settings.poll_seconds
                except Exception as exc:
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
                "valid version=%s signals=%d algos=%d"
                % (settings.version, len(settings.signals), len(settings.algos))
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
