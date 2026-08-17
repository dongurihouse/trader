#!/usr/bin/env python3
"""Small deterministic algo core and SQLite polling service."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT.parent / "config" / "config.json"
SCHEMA = ROOT.parent / "config" / "schema.sql"
UTC = timezone.utc
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
BAR_FIELDS = {"open", "high", "low", "close", "volume"}


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
    signals: Mapping[str, Mapping[str, Any]]
    algos: Mapping[str, Mapping[str, Any]]
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


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError("%s must be a positive integer" % name)
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
            if layer == "signal" and target in ("bars", "events"):
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
        signals=_nodes(document, "signals"),
        algos=_nodes(document, "algos"),
        content=_json(document),
    )
    if set(settings.signals) & set(settings.algos):
        raise ConfigError("signal and algo names must be unique")
    unknown_signals = {
        node["function"] for node in settings.signals.values()
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


def _signal_sma(connection, ticker, ts, parameters, inputs) -> Optional[float]:
    if list(inputs) != ["bars"]:
        raise EvaluationError("sma inputs must be ['bars']")
    field, period, include = _bar_parameters(parameters)
    rows = _bars(connection, ticker, ts, period, include)
    if len(rows) < period:
        return None
    return sum(float(row[field]) for row in rows) / period


SIGNAL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sma": _signal_sma,
}


def _number(value: Any, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EvaluationError("%s must be a finite number or null" % name)
    return float(value)


def _algo_crossover(connection, ticker, ts, name, version, parameters, inputs, previous, open_entries):
    if len(inputs) != 2:
        raise EvaluationError("crossover requires [fast, slow] inputs")
    fast_name, slow_name = inputs
    fast = _number(inputs[fast_name], "fast")
    slow = _number(inputs[slow_name], "slow")
    old_fast = _number(previous.get(fast_name), "previous fast")
    old_slow = _number(previous.get(slow_name), "previous slow")
    if None in (fast, slow, old_fast, old_slow):
        return False, False
    return old_fast <= old_slow and fast > slow, old_fast >= old_slow and fast < slow


ALGO_FUNCTIONS: dict[str, Callable[..., tuple[bool, bool]]] = {
    "crossover": _algo_crossover,
}


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


def _open_entries(
    connection: sqlite3.Connection, ticker: str, algo: str, ts: int
) -> list[dict[str, Any]]:
    last_exit = connection.execute(
        "SELECT MAX(ts) FROM trades WHERE ticker=? AND algo=? AND action='exit_all' AND ts<?",
        (ticker, algo, ts),
    ).fetchone()[0]
    rows = connection.execute(
        """
        SELECT tr.ts, b.close FROM trades tr
        JOIN bars b ON b.ticker=tr.ticker AND b.ts=tr.ts
        WHERE tr.ticker=? AND tr.algo=? AND tr.action='entry'
          AND tr.ts>? AND tr.ts<? ORDER BY tr.ts
        """,
        (ticker, algo, last_exit if last_exit is not None else -1, ts),
    ).fetchall()
    return [{"ts": int(row["ts"]), "price": float(row["close"])} for row in rows]


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
    algo_values: dict[str, tuple[bool, bool]] = {}
    visiting: set[str] = set()

    def evaluate_signal(name: str) -> Any:
        if name in values:
            return values[name]
        if name in visiting:
            raise EvaluationError("dependency cycle at %s" % name)
        node = settings.signals[name]
        visiting.add(name)
        inputs = {
            target: None if target in ("bars", "events") else evaluate_signal(target)
            for target in node["inputs"]
        }
        try:
            value = SIGNAL_FUNCTIONS[node["function"]](
                connection, ticker, ts, node["params"], inputs
            )
            _json(value)
        except Exception as exc:
            raise EvaluationError("signal %s failed: %s" % (name, exc)) from exc
        visiting.remove(name)
        values[name] = value
        signal_values[name] = value
        return value

    def evaluate_algo(name: str) -> tuple[bool, bool]:
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
                pair = evaluate_algo(target)
                inputs[target] = [pair[0], pair[1]]
            previous[target] = _prior_output(connection, ticker, ts, target, settings.version)
        try:
            pair = ALGO_FUNCTIONS[node["function"]](
                connection,
                ticker,
                ts,
                name,
                settings.version,
                node["params"],
                inputs,
                previous,
                _open_entries(connection, ticker, name, ts),
            )
        except Exception as exc:
            raise EvaluationError("algo %s failed: %s" % (name, exc)) from exc
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(isinstance(value, bool) for value in pair)
            or all(pair)
        ):
            raise EvaluationError("algo %s returned an invalid pair" % name)
        visiting.remove(name)
        result = (pair[0], pair[1])
        algo_values[name] = result
        values[name] = [result[0], result[1]]
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
    for name, pair in result["algos"].items():
        rows.append((result["ticker"], result["ts"], name, settings.version, _json(pair), computed_at))

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
            for name, pair in result["algos"].items():
                if not settings.algos[name]["trades"]:
                    continue
                action = "exit_all" if pair[1] else "entry" if pair[0] else None
                if action is None:
                    continue
                if action == "exit_all" and not _open_entries(
                    connection, result["ticker"], name, result["ts"]
                ):
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM trades WHERE ticker=? AND algo=? AND ts=?",
                    (result["ticker"], name, result["ts"]),
                ).fetchone()
                if exists:
                    continue
                connection.execute(
                    "INSERT INTO trades(ticker,algo,ts,action) VALUES (?,?,?,?)",
                    (result["ticker"], name, result["ts"], action),
                )
                stats["exits" if action == "exit_all" else "entries"] += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return stats


def cycle(config_path: Path) -> tuple[dict[str, int], Settings]:
    config_path = config_path.resolve()
    current = load_settings(config_path)
    _init_database(current.database)
    connection = _connect(current.database)
    try:
        now = int(time.time())
        _log(connection, "info", "heartbeat", now)
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
        _log(connection, "info", message)
        connection.commit()
        logging.info(message)
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
        self.stop_event = threading.Event()

    def stop(self, signum=None, frame=None) -> None:
        self.stop_event.set()

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        delay = 30
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
