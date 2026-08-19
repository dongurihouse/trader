"""Named database access for the algo service."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from algo_types import (
    ALERT_CLOSE_GRACE_SECONDS,
    CYCLE_PAIR_LIMIT,
    ConfigError,
    EvaluationError,
    Settings,
    UTC,
    _json,
    _session_window,
)
from strategies import _algo_output

ROOT = Path(__file__).resolve().parent
SCHEMA = ROOT.parent / "config" / "schema.sql"
_INITIALIZED_DATABASES: set[Path] = set()
_DATABASE_INIT_LOCK = threading.Lock()
_OUTPUT_SESSION_SEEDS: dict[
    tuple[str, str, str, int], tuple[tuple[int, float, int], ...]
] = {}



def clear_output_state_cache() -> None:
    _OUTPUT_SESSION_SEEDS.clear()


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
                    "ALTER TABLE trades ADD COLUMN direction INTEGER NOT NULL "
                    "DEFAULT 1 CHECK(direction IN (-1, 1))"
                )
            removed_history = _remove_definition_history(connection)
            migrated_outputs = _migrate_outputs(connection)
            if removed_history:
                _log(
                    connection,
                    "info",
                    "removed database definition history; config and git "
                    "are authoritative",
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



def _bar_metadata_value(
    connection: sqlite3.Connection,
    ticker: str,
    ts: int,
    name: str,
    query_key: str,
) -> Optional[str]:
    row = connection.execute(
        """
        SELECT value FROM bar_metadata
        WHERE ticker=? AND ts=? AND name=? AND params=?
        """,
        (ticker, ts, name, query_key),
    ).fetchone()
    return str(row["value"]) if row else None



def _exact_bar(
    connection: sqlite3.Connection, ticker: str, ts: int
) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT ts,open,high,low,close,volume,interpolated
        FROM bars WHERE ticker=? AND ts=? AND interpolated=0
        """,
        (ticker, ts),
    ).fetchone()



def _latest_close(
    connection: sqlite3.Connection,
    ticker: str,
    ts: int,
    include_interpolated: bool,
) -> Optional[float]:
    quality = "" if include_interpolated else "AND interpolated=0"
    row = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts<=? %s "
        "ORDER BY ts DESC LIMIT 1" % quality,
        (ticker, ts),
    ).fetchone()
    return float(row["close"]) if row else None



def _running_extremes(
    connection: sqlite3.Connection,
    ticker: str,
    start_ts: int,
    through_ts: int,
) -> tuple[Optional[float], Optional[float]]:
    row = connection.execute(
        """
        SELECT MAX(high) AS high,MIN(low) AS low
        FROM bars
        WHERE ticker=? AND ts>=? AND ts<=? AND interpolated=0
        """,
        (ticker, start_ts, through_ts),
    ).fetchone()
    high = float(row["high"]) if row and row["high"] is not None else None
    low = float(row["low"]) if row and row["low"] is not None else None
    return high, low



def _previous_outputs(
    connection: sqlite3.Connection, ticker: str, ts: int
) -> dict[str, Any]:
    return {
        str(row["kind"]): json.loads(row["output"])
        for row in connection.execute(
            """
            SELECT kind,output FROM (
                SELECT kind,output,
                       ROW_NUMBER() OVER (
                           PARTITION BY kind ORDER BY ts DESC
                       ) AS position
                FROM outputs
                WHERE ticker=? AND ts<?
            )
            WHERE position=1
            """,
            (ticker, ts),
        ).fetchall()
    }



def _bar_close(
    connection: sqlite3.Connection, ticker: str, ts: int
) -> Optional[float]:
    row = connection.execute(
        "SELECT close FROM bars WHERE ticker=? AND ts=?", (ticker, ts)
    ).fetchone()
    return float(row["close"]) if row else None



def _relative_momentum_repair_candidates(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    kind: str,
) -> Sequence[sqlite3.Row]:
    latest = connection.execute(
        "SELECT MAX(ts) FROM bars WHERE ticker=?", (ticker,)
    ).fetchone()[0]
    if latest is None:
        return ()
    return connection.execute(
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
    ticker_rows: list[tuple[str, list[int]]] = []
    for ticker in settings.tickers:
        if stop_event is not None and stop_event.is_set():
            break
        latest = connection.execute(
            "SELECT MAX(ts) FROM bars WHERE ticker=?", (ticker,)
        ).fetchone()[0]
        if latest is None:
            continue
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
                limit,
            ),
        ).fetchall()
        ticker_rows.append((ticker, [int(row["ts"]) for row in rows]))
    return _round_robin_pairs(ticker_rows, limit)


def _round_robin_pairs(
    ticker_rows: Sequence[tuple[str, Sequence[int]]], limit: int
) -> list[tuple[str, int]]:
    """Share a bounded batch fairly while preserving each ticker's order."""
    pairs: list[tuple[str, int]] = []
    offsets = [0] * len(ticker_rows)
    while len(pairs) < limit:
        added = False
        for index, (ticker, timestamps) in enumerate(ticker_rows):
            offset = offsets[index]
            if offset >= len(timestamps):
                continue
            pairs.append((ticker, int(timestamps[offset])))
            offsets[index] += 1
            added = True
            if len(pairs) == limit:
                break
        if not added:
            break
    return pairs


def _recalculation_pairs(
    connection: sqlite3.Connection,
    settings: Settings,
    limit: int = CYCLE_PAIR_LIMIT,
    stop_event: Optional[threading.Event] = None,
) -> list[tuple[str, int]]:
    """Return a fresh bounded evaluation window without reading live outputs."""
    if not settings.output_kinds() or limit < 1:
        return []
    ticker_rows: list[tuple[str, list[int]]] = []
    for ticker in settings.tickers:
        if stop_event is not None and stop_event.is_set():
            break
        latest = connection.execute(
            "SELECT MAX(ts) FROM bars WHERE ticker=?", (ticker,)
        ).fetchone()[0]
        if latest is None:
            continue
        rows = connection.execute(
            """
            SELECT ts FROM bars
            WHERE ticker=? AND ts>=?
            ORDER BY ts LIMIT ?
            """,
            (
                ticker,
                int(latest) - settings.evaluation_days * 86_400,
                limit,
            ),
        ).fetchall()
        ticker_rows.append((ticker, [int(row["ts"]) for row in rows]))
    return _round_robin_pairs(ticker_rows, limit)


def _upsert_output_rows(
    connection: sqlite3.Connection,
    rows: Sequence[tuple[str, int, str, str, int]],
) -> None:
    connection.executemany(
        """
        INSERT INTO outputs(ticker,ts,kind,output,computed_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(ticker,ts,kind) DO UPDATE SET
          output=excluded.output,computed_at=excluded.computed_at
        """,
        rows,
    )


def _warm_primary(
    connection: sqlite3.Connection,
    settings: Settings,
    function_name: str,
    targets: Callable[
        [sqlite3.Connection, Settings, str, int, str], Sequence[int]
    ],
    compute: Callable[[str, int, Mapping[str, Any]], Any],
) -> tuple[str, int]:
    signal_entry = next(
        (
            (name, node)
            for name, node in settings.signals.items()
            if node["function"] == function_name
        ),
        None,
    )
    if signal_entry is None or not settings.tickers:
        return "", 0

    kind, node = signal_entry
    ticker = settings.tickers[0]
    latest = connection.execute(
        "SELECT MAX(ts) FROM bars WHERE ticker=?", (ticker,)
    ).fetchone()[0]
    if latest is None:
        return ticker, 0
    target_timestamps = targets(
        connection, settings, ticker, int(latest), kind
    )
    if not target_timestamps:
        return ticker, 0

    computed_at = int(time.time())
    rows = [
        (
            ticker,
            ts,
            kind,
            _json(compute(ticker, ts, node)),
            computed_at,
        )
        for ts in target_timestamps
    ]
    _upsert_output_rows(connection, rows)
    return ticker, len(rows)


def _shape_warm_targets(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    latest: int,
    kind: str,
) -> Sequence[int]:
    _local_day, session_open, session_close = _session_window(settings, latest)
    if latest >= session_close:
        return ()
    if latest < session_open:
        timestamps = [latest]
    else:
        timestamps = [
            int(row["ts"])
            for row in connection.execute(
                """
                SELECT ts FROM bars
                WHERE ticker=? AND ts>=? AND ts<=?
                ORDER BY ts
                """,
                (ticker, session_open, latest),
            )
        ]
    if not timestamps:
        return ()

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
    return [ts for ts in timestamps if ts not in stored]


def _relative_momentum_warm_targets(
    connection: sqlite3.Connection,
    settings: Settings,
    ticker: str,
    latest: int,
    kind: str,
) -> Sequence[int]:
    _local_day, session_open, session_close = _session_window(settings, latest)
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
        return ()

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
    return [
        ts
        for ts in timestamps
        if ts not in stored or not _relative_momentum_has_score(stored[ts])
    ]


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


def _write_result(
    connection: sqlite3.Connection,
    settings: Settings,
    result: Mapping[str, Any],
    replace_algos: Optional[Sequence[str]] = None,
    *,
    commit: bool = True,
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

    trade_rows: list[tuple[str, str, int, str, int]] = []
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
        trade_rows.append(
            (result["ticker"], name, result["ts"], action, direction)
        )

    stats = {"pairs": 1, "outputs": len(rows), "entries": 0, "exits": 0}
    alerts: list[dict[str, Any]] = []
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        if replace_algos is not None:
            connection.execute("DELETE FROM outputs")
        _upsert_output_rows(connection, rows)
        replaced_names = set(replace_algos or ())
        if replace_algos is not None and replaced_names:
            placeholders = ",".join("?" for _ in replaced_names)
            connection.execute(
                f"DELETE FROM trades WHERE algo IN ({placeholders})",
                tuple(sorted(replaced_names)),
            )
            connection.executemany(
                "INSERT INTO trades(ticker,algo,ts,action,direction) "
                "VALUES (?,?,?,?,?)",
                [row for row in trade_rows if row[1] in replaced_names],
            )
        for ticker, name, ts, action, direction in trade_rows:
            if name not in replaced_names:
                existing = connection.execute(
                    "SELECT action,direction FROM trades "
                    "WHERE ticker=? AND algo=? AND ts=?",
                    (ticker, name, ts),
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
                        % (ticker, name, ts)
                    )
                else:
                    connection.execute(
                        "INSERT INTO trades(ticker,algo,ts,action,direction) "
                        "VALUES (?,?,?,?,?)",
                        (ticker, name, ts, action, direction),
                    )
            price = connection.execute(
                "SELECT close FROM bars WHERE ticker=? AND ts=?",
                (ticker, ts),
            ).fetchone()
            alerts.append(
                {
                    "ticker": ticker,
                    "algo": name,
                    "ts": ts,
                    "action": action,
                    "direction": direction,
                    "price": None if price is None else price["close"],
                }
            )
            stats["exits" if action == "exit_all" else "entries"] += 1
        if commit:
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    return stats, alerts


def _replace_empty_calculation(
    connection: sqlite3.Connection, replace_algos: Sequence[str]
) -> None:
    """Finish a recalculation when its source window contains no bars."""
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM outputs")
        if replace_algos:
            placeholders = ",".join("?" for _ in replace_algos)
            connection.execute(
                f"DELETE FROM trades WHERE algo IN ({placeholders})",
                tuple(replace_algos),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


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
            latest = (
                datetime.fromtimestamp(row["latest"], UTC).isoformat()
                if row["latest"]
                else "-"
            )
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


def recent_logs(settings: Settings, limit: int = 50) -> str:
    """Return recent algo logs through the service-owned read function."""
    connection = _connect(settings.database, read_only=True)
    try:
        rows = connection.execute(
            """
            SELECT ts,level,message FROM logs
            WHERE service='algo'
            ORDER BY rowid DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    lines = ["utc                  level  message"]
    for row in rows:
        timestamp = datetime.fromtimestamp(int(row["ts"]), UTC).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        lines.append("%-20s %-6s %s" % (timestamp, row["level"], row["message"]))
    return "\n".join(lines)
