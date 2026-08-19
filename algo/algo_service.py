#!/usr/bin/env python3
"""Small deterministic algo core and SQLite polling service."""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config import (
    configured_tickers,
    market_schedule,
    read_config,
    resolve_config_path,
)
from common.service import (
    request_operation as request_service_operation,
    write_json,
)
from common.validation import normalize_symbol, require_int

from algo_types import (
    AlgoContext,
    CYCLE_PAIR_LIMIT,
    BrokerSettings,
    ConfigError,
    EvaluationError,
    Settings,
    UTC,
    _json,
    _session_window,
)
from broker import send_broker_orders
from notify import send_trade_alerts
from shape_signal import clear_shape_cache
from signals import (
    SIGNAL_FUNCTIONS,
    clear_signal_caches,
    _signal_relative_momentum,
    _signal_session,
    _signal_shape_v1,
)
from storage import (
    _bar_close,
    _bar_closes_for_timestamps,
    _connect,
    _context_bars,
    _incomplete_algo_kinds,
    _init_database,
    _live_trade_alerts,
    _log,
    _output_state,
    _outputs_for_timestamps,
    _pending,
    _prior_output,
    _previous_outputs,
    _recalculation_pairs,
    _relative_momentum_has_score,
    _relative_momentum_repair_candidates,
    _relative_momentum_warm_targets,
    _replace_empty_calculation,
    _shape_warm_targets,
    _sync_live_definitions,
    _targeted_recalculation_pairs,
    _upsert_output_rows,
    _warm_primary,
    _write_result,
    clear_output_state_cache,
    recent_logs,
    status,
)
from strategies import ALGO_FUNCTIONS, _algo_output


DEFAULT_CONFIG = ROOT.parent / "config" / "config.json"
CYCLE_PROGRESS_INTERVAL = 1_000
PARALLEL_PAIR_THRESHOLD = 200
PARALLEL_WRITE_BATCH_SIZE = 50
_RELATIVE_MOMENTUM_REPAIR_ATTEMPTS: set[tuple[str, str, int]] = set()
_PARALLEL_WRITE_LOCK: Any = None
_PARALLEL_STOP_EVENT: Any = None



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
        function = value.get("function", name)
        if "trades" in value:
            raise ConfigError("%s.%s.trades is not supported" % (key, name))
        if not isinstance(function, str) or not function:
            raise ConfigError("%s.%s.function must be a name" % (key, name))
        if not isinstance(params, dict) or not isinstance(inputs, list):
            raise ConfigError(
                "%s.%s params must be an object and inputs must be a list"
                % (key, name)
            )
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
            symbol = normalize_symbol(
                value,
                error=ConfigError,
                message="broker.execution_tickers.%s.%s must be a ticker"
                % (ticker, direction),
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
            if layer == "signal" and target in ("bars", "events"):
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
        document = read_config(path, ConfigError)
    if not isinstance(document, dict):
        raise ConfigError("config must be a JSON object")

    normalized = configured_tickers(document, ConfigError)

    algo = document.get("algo", {})
    if not isinstance(algo, dict):
        raise ConfigError("algo must be an object")
    schedule = market_schedule(document, ConfigError)
    database = resolve_config_path(
        document, "database", path, "../data/trader.sqlite3", ConfigError
    )
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
            params = spec.normalize(node["params"], normalized)
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
            params = spec.normalize(node["params"], normalized)
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
        tickers=normalized,
        evaluation_days=require_int(
            algo.get("evaluation_days", 60), "algo.evaluation_days", error=ConfigError
        ),
        poll_seconds=require_int(
            algo.get("poll_seconds", 30), "algo.poll_seconds", error=ConfigError
        ),
        api_port=require_int(
            algo.get("api_port", 8791), "algo.api_port", 1024, error=ConfigError
        ),
        schedule=schedule,
        signals=signals,
        algos=algos,
        signal_order=signal_order,
        algo_order=algo_order,
        algo_requirements=algo_requirements,
        broker=_broker_settings(document, normalized),
        content=_json(document),
    )
    return settings



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
        self.previous.update(result.get("_state_inputs", result["signals"]))
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
    previous = _previous_outputs(connection, ticker, ts)
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



def _empty_evaluation_state(
    settings: Settings, ticker: str, ts: int
) -> _EvaluationState:
    """Start a recalculation without inheriting the rows it will replace."""
    _, session_open, _ = _session_window(settings, ts)
    return _EvaluationState(
        ticker=ticker,
        session_open=session_open,
        previous={},
        open_entries={name: [] for name in settings.enabled_algos()},
        session_outputs={name: [] for name in settings.enabled_algos()},
    )


def _evaluate_algo_node(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    ts: int,
    name: str,
    inputs: Mapping[str, Any],
    previous: Mapping[str, Any],
    state: Optional[_EvaluationState],
) -> tuple[tuple[bool, bool, int], tuple[Mapping[str, Any], ...]]:
    node = settings.algos[name]
    if state is None:
        open_entries, session_outputs = _output_state(
            connection, settings, ticker, name, ts
        )
    else:
        open_entries = state.open_entries[name]
        session_outputs = state.session_outputs[name]
    position = tuple(open_entries)
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
        raise EvaluationError(
            "algo %s returned invalid output: %s" % (name, exc)
        ) from exc
    if result[1]:
        result = (
            (False, True, int(position[0]["direction"]))
            if position
            else (False, False, 0)
        )
    return result, position



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
    bar_close = _bar_close(connection, ticker, ts)
    if bar_close is None:
        raise EvaluationError("timestamp is not a stored bar: %s %s" % (ticker, ts))
    if state is not None and not state.accepts(settings, ticker, ts):
        raise EvaluationError("evaluation state does not precede %s %s" % (ticker, ts))

    signal_values: dict[str, Any] = {}
    serialized: dict[str, str] = {}
    for name in settings.signal_order:
        node = settings.signals[name]
        inputs = {
            target: None if target in ("bars", "events") else signal_values[target]
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
        result, position = _evaluate_algo_node(
            connection,
            settings,
            ticker,
            ts,
            name,
            inputs,
            previous,
            state,
        )
        algo_open_entries[name] = position
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
        "_bar_close": bar_close,
    }


def _targeted_input_names(
    settings: Settings, algos: Sequence[str]
) -> tuple[str, ...]:
    selected = set(algos)
    names: list[str] = []
    for name in settings.algo_order:
        if name not in selected:
            continue
        for target in settings.algos[name]["inputs"]:
            if target not in selected and target not in names:
                names.append(target)
    return tuple(names)


def _seed_targeted_evaluation_state(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    ts: int,
    algos: Sequence[str],
) -> _EvaluationState:
    """Seed a resumed target stream, but keep a fresh target window empty."""
    marker = next((name for name in settings.algo_order if name in algos), None)
    if marker is None or _prior_output(connection, ticker, ts, marker) is None:
        return _empty_evaluation_state(settings, ticker, ts)
    return _seed_evaluation_state(connection, settings, ticker, ts)


def run_targeted_core(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    ts: int,
    algos: Sequence[str],
    state: _EvaluationState,
    stored_inputs: Mapping[str, Any],
    bar_close: float,
) -> dict[str, Any]:
    """Evaluate selected algos from persisted, unchanged upstream outputs."""
    ticker = ticker.strip().upper()
    if ticker not in settings.tickers:
        raise EvaluationError("ticker is not in config: %s" % ticker)
    if not state.accepts(settings, ticker, ts):
        raise EvaluationError("evaluation state does not precede %s %s" % (ticker, ts))

    requested = set(algos)
    selected = tuple(name for name in settings.algo_order if name in requested)
    unknown = requested - set(selected)
    if unknown:
        raise EvaluationError("unknown algo: %s" % sorted(unknown)[0])
    required_inputs = _targeted_input_names(settings, selected)
    missing = [name for name in required_inputs if name not in stored_inputs]
    if missing:
        raise EvaluationError(
            "persisted input %s is missing for %s %s"
            % (missing[0], ticker, ts)
        )

    selected_set = set(selected)
    algo_values: dict[str, tuple[bool, bool, int]] = {}
    algo_open_entries: dict[str, tuple[Mapping[str, Any], ...]] = {}
    serialized: dict[str, str] = {}
    for name in settings.algo_order:
        if name not in selected_set:
            continue
        node = settings.algos[name]
        inputs: dict[str, Any] = {}
        previous: dict[str, Any] = {"_self": state.previous.get(name)}
        for target in node["inputs"]:
            inputs[target] = (
                list(algo_values[target])
                if target in selected_set
                else stored_inputs[target]
            )
            previous[target] = state.previous.get(target)
        result, position = _evaluate_algo_node(
            connection,
            settings,
            ticker,
            ts,
            name,
            inputs,
            previous,
            state,
        )
        algo_open_entries[name] = position
        algo_values[name] = result
        serialized[name] = _json(result)

    return {
        "ticker": ticker,
        "ts": ts,
        "signals": {},
        "algos": algo_values,
        "_open_entries": algo_open_entries,
        "_serialized": serialized,
        "_state_inputs": dict(stored_inputs),
        "_bar_close": bar_close,
    }


def _evaluate_pair(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    ts: int,
    state: _EvaluationState,
    target_algos: Sequence[str],
    stored_inputs: Optional[Mapping[str, Any]] = None,
    bar_close: Optional[float] = None,
) -> dict[str, Any]:
    if not target_algos:
        return run_core(connection, settings, ticker, ts, state=state)
    if stored_inputs is None:
        stored_inputs = _outputs_for_timestamps(
            connection,
            ticker,
            (ts,),
            _targeted_input_names(settings, target_algos),
        ).get(ts, {})
    if bar_close is None:
        bar_close = _bar_close(connection, ticker, ts)
    if bar_close is None:
        raise EvaluationError(
            "timestamp is not a stored bar: %s %s" % (ticker, ts)
        )
    return run_targeted_core(
        connection,
        settings,
        ticker,
        ts,
        target_algos,
        state,
        stored_inputs,
        bar_close,
    )



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


def _parallel_worker_init(write_lock: Any, stop_event: Any) -> None:
    global _PARALLEL_WRITE_LOCK, _PARALLEL_STOP_EVENT
    _PARALLEL_WRITE_LOCK = write_lock
    _PARALLEL_STOP_EVENT = stop_event


def _parallel_ticker_batch(
    settings: Settings,
    ticker: str,
    timestamps: Sequence[int],
    target_algos: Sequence[str] = (),
) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    """Evaluate one ticker chronologically and serialize each database write."""
    clear_signal_caches()
    clear_output_state_cache()
    clear_shape_cache()
    stats = {"pairs": 0, "outputs": 0, "entries": 0, "exits": 0}
    alerts: list[dict[str, Any]] = []
    state: Optional[_EvaluationState] = None
    pending_results: list[Mapping[str, Any]] = []
    connection = _connect(settings.database)
    targeted = bool(target_algos)
    stored_by_ts = (
        _outputs_for_timestamps(
            connection,
            ticker,
            timestamps,
            _targeted_input_names(settings, target_algos),
        )
        if targeted
        else {}
    )
    closes_by_ts = (
        _bar_closes_for_timestamps(connection, ticker, timestamps)
        if targeted
        else {}
    )

    def flush_results() -> None:
        if not pending_results:
            return
        batch_stats = {"pairs": 0, "outputs": 0, "entries": 0, "exits": 0}
        batch_alerts: list[dict[str, Any]] = []
        if _PARALLEL_WRITE_LOCK is None:
            raise RuntimeError("parallel database write lock is unavailable")
        with _PARALLEL_WRITE_LOCK:
            try:
                for pending in pending_results:
                    part, trade_alerts = _write_result(
                        connection,
                        settings,
                        pending,
                        commit=False,
                    )
                    _add_stats(batch_stats, part)
                    batch_alerts.extend(trade_alerts)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        _add_stats(stats, batch_stats)
        alerts.extend(batch_alerts)
        pending_results.clear()

    try:
        for ts in timestamps:
            if _PARALLEL_STOP_EVENT is not None and _PARALLEL_STOP_EVENT.is_set():
                break
            if state is None or not state.accepts(settings, ticker, ts):
                flush_results()
                state = (
                    _seed_targeted_evaluation_state(
                        connection, settings, ticker, ts, target_algos
                    )
                    if targeted
                    else _seed_evaluation_state(connection, settings, ticker, ts)
                )
            result = _evaluate_pair(
                connection,
                settings,
                ticker,
                ts,
                state,
                target_algos,
                stored_by_ts.get(ts) if targeted else None,
                closes_by_ts.get(ts) if targeted else None,
            )
            state.advance(result)
            pending_results.append(result)
            if len(pending_results) == PARALLEL_WRITE_BATCH_SIZE:
                flush_results()
        flush_results()
        return ticker, stats, alerts
    finally:
        connection.close()



def _warm_primary_shape(
    connection: sqlite3.Connection,
    settings: Settings,
) -> tuple[str, int]:
    """Fill the primary ticker's current shape before deep history catches up."""
    return _warm_primary(
        connection,
        settings,
        "shape_v1",
        _shape_warm_targets,
        lambda ticker, ts, node: _signal_shape_v1(
            connection, ticker, ts, node["params"], {"bars": None}, settings
        ),
    )



def _warm_primary_relative_momentum(
    connection: sqlite3.Connection,
    settings: Settings,
) -> tuple[str, int]:
    """Fill the primary ticker's latest session before deep history catches up."""
    return _warm_primary(
        connection,
        settings,
        "relative_momentum",
        _relative_momentum_warm_targets,
        lambda ticker, ts, node: _signal_relative_momentum(
            connection,
            ticker,
            ts,
            node["params"],
            {
                "bars": None,
                "session": _signal_session(
                    connection, ticker, ts, {}, {}, settings
                ),
            },
            settings,
        ),
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
        candidates = _relative_momentum_repair_candidates(
            connection, settings, ticker, kind
        )
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
        _upsert_output_rows(connection, repaired_rows)
    return len(repaired_rows)



def _report_alert_failure(config_path: Path, reason: str) -> None:
    message = "alerting failed: %s" % " ".join(reason.split())
    logging.error(message)
    _best_effort_log(config_path, "error", message)



def _report_broker_result(config_path: Path, level: str, message: str) -> None:
    getattr(logging, level)(message)
    _best_effort_log(config_path, level, message)



@dataclass(frozen=True)
class _CyclePlan:
    mode: str
    replace_algos: tuple[str, ...] = ()
    replacement_pending: bool = False

    def target_algos(self, settings: Settings) -> tuple[str, ...]:
        if self.mode != "targeted":
            return ()
        selected = set(self.replace_algos)
        return tuple(name for name in settings.algo_order if name in selected)


def _prepare_cycle(
    connection: sqlite3.Connection,
    settings: Settings,
    force_recalculate: bool,
    stop_event: Optional[threading.Event] = None,
) -> _CyclePlan:
    now = int(time.time())
    definition_sync = _sync_live_definitions(connection, settings, now)
    changed_algos = tuple(definition_sync["changed_algos"])
    signals_changed = bool(definition_sync["signals_changed"])
    if signals_changed or changed_algos:
        _log(
            connection,
            "info",
            "applied live definitions signals_changed=%s algos=%s"
            % (
                str(signals_changed).lower(),
                ",".join(changed_algos) or "none",
            ),
            now,
        )

    if force_recalculate:
        _log(
            connection,
            "info",
            "explicit algorithm recalculation requested",
            now,
        )
    if force_recalculate or signals_changed:
        replace_algos = tuple(
            dict.fromkeys((*settings.enabled_algos(), *changed_algos))
        )
        return _CyclePlan("full", replace_algos, True)
    if changed_algos and settings.signal_order:
        return _CyclePlan("targeted", changed_algos, True)
    if changed_algos:
        replace_algos = tuple(
            dict.fromkeys((*settings.enabled_algos(), *changed_algos))
        )
        return _CyclePlan("full", replace_algos, True)

    incomplete_algos = _incomplete_algo_kinds(
        connection, settings, stop_event=stop_event
    )
    if incomplete_algos:
        return _CyclePlan("targeted", incomplete_algos, False)

    connection.commit()
    shape_ticker, shape_rows = _warm_primary_shape(connection, settings)
    momentum_ticker, momentum_rows = _warm_primary_relative_momentum(
        connection, settings
    )
    repaired_rows = _repair_relative_momentum_gaps(connection, settings)
    maintenance = (
        (
            shape_rows,
            "priority shape warmup ticker=%s rows=%d" % (shape_ticker, shape_rows),
        ),
        (
            momentum_rows,
            "priority momentum warmup ticker=%s rows=%d"
            % (momentum_ticker, momentum_rows),
        ),
        (repaired_rows, "continuous momentum repair rows=%d" % repaired_rows),
    )
    for row_count, message in maintenance:
        if row_count:
            _log(connection, "info", message)
            logging.info(message)
    if any(row_count for row_count, _message in maintenance):
        connection.commit()
    return _CyclePlan("pending")



def _dispatch_trade_alerts(
    connection: sqlite3.Connection,
    settings: Settings,
    trade_alerts: Sequence[Mapping[str, Any]],
    alert_resume_after: Optional[int],
    config_path: Path,
) -> None:
    if alert_resume_after is None or not trade_alerts:
        return
    live_records = _live_trade_alerts(
        connection,
        settings,
        trade_alerts,
        alert_resume_after,
    )
    send_trade_alerts(
        live_records,
        on_failure=lambda reason: _report_alert_failure(config_path, reason),
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


def _add_stats(total: dict[str, int], part: Mapping[str, int]) -> None:
    for key in total:
        total[key] += int(part[key])


def _should_parallelize(
    pairs: Sequence[tuple[str, int]], replacement_pending: bool
) -> bool:
    candidates = pairs[1:] if replacement_pending else pairs
    return (
        len(candidates) >= PARALLEL_PAIR_THRESHOLD
        and len({ticker for ticker, _ts in candidates}) > 1
    )


def _evaluate_parallel_pairs(
    connection: sqlite3.Connection,
    settings: Settings,
    pairs: Sequence[tuple[str, int]],
    replace_algos: Sequence[str],
    replacement_pending: bool,
    stop_event: Optional[threading.Event],
    target_algos: Sequence[str] = (),
) -> tuple[dict[str, int], list[dict[str, Any]], bool]:
    stats = {"pairs": 0, "outputs": 0, "entries": 0, "exits": 0}
    trade_alerts: list[dict[str, Any]] = []
    remaining = list(pairs)

    # Keep cache replacement atomic with the first new result before workers
    # begin writing independent ticker streams.
    if replacement_pending:
        if stop_event is not None and stop_event.is_set():
            return stats, trade_alerts, replacement_pending
        ticker, ts = remaining.pop(0)
        state = _empty_evaluation_state(settings, ticker, ts)
        result = _evaluate_pair(
            connection, settings, ticker, ts, state, target_algos
        )
        part, first_alerts = _write_result(
            connection,
            settings,
            result,
            replace_algos=replace_algos,
            replace_all_outputs=not target_algos,
        )
        _add_stats(stats, part)
        trade_alerts.extend(first_alerts)
        replacement_pending = False

    ticker_batches: dict[str, list[int]] = {
        ticker: [] for ticker in settings.tickers
    }
    for ticker, ts in remaining:
        ticker_batches[ticker].append(ts)
    ticker_batches = {
        ticker: timestamps
        for ticker, timestamps in ticker_batches.items()
        if timestamps
    }
    if not ticker_batches:
        return stats, trade_alerts, replacement_pending

    context = multiprocessing.get_context("spawn")
    write_lock = context.Lock()
    worker_stop_event = context.Event()
    monitor_done = threading.Event()
    monitor_thread: Optional[threading.Thread] = None
    if stop_event is not None:

        def mirror_stop() -> None:
            while not monitor_done.wait(0.1):
                if stop_event.is_set():
                    worker_stop_event.set()
                    return

        monitor_thread = threading.Thread(
            target=mirror_stop,
            name="algo-parallel-stop",
            daemon=True,
        )
        monitor_thread.start()

    max_workers = min(len(ticker_batches), os.cpu_count() or 1)
    try:
        with ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=context,
            initializer=_parallel_worker_init,
            initargs=(write_lock, worker_stop_event),
        ) as executor:
            futures = {
                executor.submit(
                    _parallel_ticker_batch,
                    settings,
                    ticker,
                    tuple(timestamps),
                    tuple(target_algos),
                ): ticker
                for ticker, timestamps in ticker_batches.items()
            }
            for future in as_completed(futures):
                try:
                    ticker, part, alerts = future.result()
                except Exception:
                    worker_stop_event.set()
                    raise
                _add_stats(stats, part)
                trade_alerts.extend(alerts)
                logging.info(
                    "parallel ticker batch complete ticker=%s pairs=%d "
                    "outputs=%d entries=%d exits=%d",
                    ticker,
                    part["pairs"],
                    part["outputs"],
                    part["entries"],
                    part["exits"],
                )
    finally:
        monitor_done.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=1)
    return stats, trade_alerts, replacement_pending



def _evaluate_cycle(
    connection: sqlite3.Connection,
    settings: Settings,
    config_path: Path,
    plan: _CyclePlan,
    stop_event: Optional[threading.Event],
    alert_resume_after: Optional[int],
) -> dict[str, int]:
    stats = {"pairs": 0, "outputs": 0, "entries": 0, "exits": 0}
    states: dict[str, _EvaluationState] = {}
    target_algos = plan.target_algos(settings)
    if plan.mode == "full":
        pairs = _recalculation_pairs(connection, settings, stop_event=stop_event)
    elif plan.mode == "targeted" and plan.replacement_pending:
        pairs = (
            _recalculation_pairs(connection, settings, stop_event=stop_event)
            if target_algos
            else []
        )
    elif plan.mode == "targeted":
        pairs = _targeted_recalculation_pairs(
            connection,
            settings,
            target_algos,
            stop_event=stop_event,
        )
    else:
        pairs = _pending(connection, settings, stop_event=stop_event)
    replacement_pending = plan.replacement_pending
    parallel = _should_parallelize(pairs, replacement_pending)
    if parallel:
        stats, trade_alerts, replacement_pending = _evaluate_parallel_pairs(
            connection,
            settings,
            pairs,
            plan.replace_algos,
            replacement_pending,
            stop_event,
            target_algos,
        )
        _dispatch_trade_alerts(
            connection,
            settings,
            trade_alerts,
            alert_resume_after,
            config_path,
        )
    else:
        for ticker, ts in pairs:
            if stop_event is not None and stop_event.is_set():
                break
            state = states.get(ticker)
            if state is None or not state.accepts(settings, ticker, ts):
                state = (
                    _empty_evaluation_state(settings, ticker, ts)
                    if replacement_pending
                    else (
                        _seed_targeted_evaluation_state(
                            connection, settings, ticker, ts, target_algos
                        )
                        if target_algos
                        else _seed_evaluation_state(
                            connection, settings, ticker, ts
                        )
                    )
                )
                states[ticker] = state
            result = _evaluate_pair(
                connection, settings, ticker, ts, state, target_algos
            )
            part, trade_alerts = _write_result(
                connection,
                settings,
                result,
                replace_algos=(
                    plan.replace_algos if replacement_pending else None
                ),
                replace_all_outputs=not target_algos,
            )
            replacement_pending = False
            state.advance(result)
            _add_stats(stats, part)
            _dispatch_trade_alerts(
                connection,
                settings,
                trade_alerts,
                alert_resume_after,
                config_path,
            )
            if stats["pairs"] % CYCLE_PROGRESS_INTERVAL == 0:
                progress = (
                    "cycle progress pairs=%d outputs=%d entries=%d exits=%d"
                    % (
                        stats["pairs"],
                        stats["outputs"],
                        stats["entries"],
                        stats["exits"],
                    )
                )
                _log(connection, "info", progress)
                connection.commit()
                logging.info(progress)

    if replacement_pending and not pairs:
        _replace_empty_calculation(
            connection,
            plan.replace_algos,
            replace_all_outputs=plan.mode == "full",
        )
    label = (
        "cycle stopped"
        if stop_event is not None and stop_event.is_set()
        else "cycle batch complete"
    )
    message = "%s pairs=%d outputs=%d entries=%d exits=%d" % (
        label,
        stats["pairs"],
        stats["outputs"],
        stats["entries"],
        stats["exits"],
    )
    if stats["pairs"]:
        _log(connection, "info", message)
        connection.commit()
        logging.info(message)
    else:
        logging.debug(message)
    return stats



def cycle(
    config_path: Path,
    stop_event: Optional[threading.Event] = None,
    alert_resume_after: Optional[int] = None,
    force_recalculate: bool = False,
) -> tuple[dict[str, int], Settings]:
    clear_signal_caches()
    clear_output_state_cache()
    clear_shape_cache()
    config_path = config_path.resolve()
    settings = load_settings(config_path)
    _init_database(settings.database)
    connection = _connect(settings.database)
    try:
        plan = _prepare_cycle(
            connection,
            settings,
            force_recalculate,
            stop_event=stop_event,
        )
        stats = _evaluate_cycle(
            connection,
            settings,
            config_path,
            plan,
            stop_event,
            alert_resume_after,
        )
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



def request_operation(settings: Settings, operation: str) -> dict[str, Any]:
    """Submit work to the one running algo writer."""
    return request_service_operation(
        settings.api_port,
        operation,
        ("cycle", "recalculate"),
        "algo",
        "make algo-install",
        ConfigError,
    )



class Service:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.settings = load_settings(self.config_path)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.operation_lock = threading.Lock()
        self.recalculation_requested = False
        self.recalculation_status: dict[str, Any] = {
            "name": "recalculate",
            "status": "idle",
        }
        self.started_at = int(time.time())
        self.alert_resume_after = self.started_at

    def health(self) -> dict[str, Any]:
        with self.operation_lock:
            recalculation = dict(self.recalculation_status)
        return {
            "ok": True,
            "service": "algo",
            "status": "running",
            "ts": int(time.time()),
            "started_at": self.started_at,
            "pid": os.getpid(),
            "operation": recalculation,
        }

    def request_recalculation(self) -> bool:
        with self.operation_lock:
            if self.recalculation_status["status"] in ("queued", "running"):
                return False
            self.recalculation_requested = True
            self.recalculation_status = {
                "name": "recalculate",
                "status": "queued",
                "requested_at": int(time.time()),
            }
        self.wake_event.set()
        return True

    def request_cycle(self) -> None:
        self.alert_resume_after = int(time.time())
        self.wake_event.set()

    def _take_recalculation(self) -> bool:
        with self.operation_lock:
            if not self.recalculation_requested:
                return False
            self.recalculation_requested = False
            self.recalculation_status["status"] = "running"
            self.recalculation_status["started_at"] = int(time.time())
        self.alert_resume_after = int(time.time())
        return True

    def _finish_recalculation(self, error: Optional[Exception] = None) -> None:
        with self.operation_lock:
            if error is None:
                self.recalculation_status["status"] = "complete"
                self.recalculation_status["completed_at"] = int(time.time())
                self.recalculation_status.pop("error", None)
            else:
                self.recalculation_status["status"] = "failed"
                self.recalculation_status["failed_at"] = int(time.time())
                self.recalculation_status["error"] = str(error)

    def stop(self, signum=None, frame=None) -> None:
        self.stop_event.set()
        self.wake_event.set()

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
            recalculation_running = False
            while not self.stop_event.is_set():
                force_recalculate = self._take_recalculation()
                if force_recalculate:
                    recalculation_running = True
                try:
                    observed = load_settings(self.config_path)
                    if observed.content != self.settings.content:
                        self.alert_resume_after = int(time.time())
                    stats, settings = cycle(
                        self.config_path,
                        self.stop_event,
                        alert_resume_after=self.alert_resume_after,
                        force_recalculate=force_recalculate,
                    )
                    self.settings = settings
                    if (
                        recalculation_running
                        and stats["pairs"] < CYCLE_PAIR_LIMIT
                    ):
                        self._finish_recalculation()
                        recalculation_running = False
                    delay = (
                        0
                        if stats["pairs"] >= CYCLE_PAIR_LIMIT
                        else settings.poll_seconds
                    )
                except ConfigError as exc:
                    if recalculation_running:
                        self._finish_recalculation(exc)
                        recalculation_running = False
                    logging.exception("configuration reload failed")
                    _best_effort_log(
                        self.config_path,
                        "error",
                        "configuration reload failed: %s" % exc,
                        database=self.settings.database,
                    )
                    raise
                except Exception as exc:
                    if recalculation_running:
                        self._finish_recalculation(exc)
                        recalculation_running = False
                    delay = 30
                    logging.exception("cycle failed")
                    _best_effort_log(
                        self.config_path, "error", "cycle failed: %s" % exc
                    )
                self.wake_event.wait(delay)
                self.wake_event.clear()
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
        write_json(self, self.server.service.health())

    def do_POST(self) -> None:  # noqa: N802
        operation = urlparse(self.path).path.lstrip("/")
        if operation == "cycle":
            self.server.service.request_cycle()
            payload = {"ok": True, "operation": "cycle", "status": "queued"}
            status = 202
        elif operation == "recalculate":
            accepted = self.server.service.request_recalculation()
            payload = {
                "ok": accepted,
                "operation": "recalculate",
                "status": "queued" if accepted else "already_running",
            }
            status = 202 if accepted else 409
        else:
            self.send_error(404)
            return
        write_json(self, payload, status)

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
    commands.add_parser("logs")
    commands.add_parser("recalculate")
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
            print(
                json.dumps(
                    request_operation(load_settings(config_path), "cycle"),
                    sort_keys=True,
                )
            )
        elif arguments.command == "status":
            print(status(load_settings(config_path)))
        elif arguments.command == "logs":
            print(recent_logs(load_settings(config_path)))
        elif arguments.command == "recalculate":
            print(
                json.dumps(
                    request_operation(load_settings(config_path), "recalculate"),
                    sort_keys=True,
                )
            )
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
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
