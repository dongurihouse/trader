#!/usr/bin/env python3
"""Read-only web dashboard for the shared trader database."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
STATIC_DIR = Path(__file__).resolve().parent / "static"
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,24}$")
SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CHART_HISTORY_START = int(datetime(2026, 7, 6, tzinfo=EASTERN).timestamp())


class DashboardError(Exception):
    """An error that is safe to return to the browser."""


class DashboardData:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()

    def config(self) -> dict[str, Any]:
        try:
            content = self.config_path.read_text(encoding="utf-8")
            value = json.loads(content)
        except (OSError, json.JSONDecodeError) as exc:
            raise DashboardError(f"Cannot read config: {exc}") from exc
        if not isinstance(value, dict):
            raise DashboardError("The config root must be a JSON object")
        return value

    def database_path(self) -> Path:
        raw = self.config().get("database")
        if not isinstance(raw, str) or not raw:
            raise DashboardError("Config has no database path")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.config_path.parent / path
        path = path.resolve()
        if not path.is_file():
            raise DashboardError(f"Database does not exist: {path}")
        return path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        path = self.database_path()
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2.5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 2500")
            yield connection
        except sqlite3.Error as exc:
            raise DashboardError(f"Cannot read database: {exc}") from exc
        finally:
            if "connection" in locals():
                connection.close()

    def overview(self, compact: bool = False) -> dict[str, Any]:
        config = self.config()
        configured_tickers = [str(item) for item in config.get("tickers", [])]
        with self.connection() as connection:
            database_tickers = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT ticker FROM bars ORDER BY ticker"
                )
            ]
            tickers = list(dict.fromkeys(configured_tickers + database_tickers))
            quotes = [self._quote(connection, ticker) for ticker in tickers]
            if compact:
                services = []
                problems = []
                history = []
                nodes = []
                stored_versions = []
                counts = {}
            else:
                services = self._services(connection, config)
                problems = self._logs(
                    connection,
                    "WHERE level IN ('warn', 'error')",
                    limit=24,
                )
                history = self._logs(
                    connection,
                    "WHERE level = 'info' AND lower(message) NOT LIKE 'heartbeat%'",
                    limit=36,
                )
                nodes = self._nodes(connection, config)
                stored_versions = [
                    {
                        "version": row["version"],
                        "first_seen": row["first_seen"],
                    }
                    for row in connection.execute(
                        "SELECT version, first_seen FROM configs ORDER BY first_seen DESC"
                    )
                ]
                counts = {
                    table: connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for table in ("bars", "events", "trades", "outputs")
                }

        return {
            "generated_at": int(datetime.now(tz=UTC).timestamp()),
            "market": self._market_state(config),
            "config": {
                "version": config.get("version"),
                "tickers": configured_tickers,
                "evaluation_days": config.get("algo", {}).get("evaluation_days"),
                "poll_seconds": config.get("algo", {}).get("poll_seconds"),
                "early_closes": config.get("early_closes", []),
                "signals": config.get("signals", {}),
                "algos": config.get("algos", {}),
            },
            "counts": counts,
            "quotes": quotes,
            "services": services,
            "problems": problems,
            "history": history,
            "nodes": nodes,
            "stored_versions": stored_versions,
        }

    def bars(self, ticker: str, span: str) -> dict[str, Any]:
        ticker = self._valid_symbol(ticker)
        span = span.upper()
        if span not in {"HISTORY", "1D", "5D", "1M", "3M", "120D", "ALL"}:
            raise DashboardError(
                "Range must be HISTORY, 1D, 5D, 1M, 3M, 120D, or ALL"
            )
        config = self.config()

        with self.connection() as connection:
            latest = connection.execute(
                "SELECT MAX(ts) FROM bars WHERE ticker = ?", (ticker,)
            ).fetchone()[0]
            if latest is None:
                return {
                    "ticker": ticker,
                    "range": span,
                    "bars": [],
                    "trades": [],
                    "events": [],
                    "shape_forecast": self._shape_forecast(
                        connection, config, ticker, 0, 0
                    ),
                    "algo_overlays": self._algo_overlays([], config),
                    "source_count": 0,
                    "interpolated_count": 0,
                }

            start = self._range_start(connection, ticker, int(latest), span)
            rows = connection.execute(
                """
                SELECT ts, open, high, low, close, volume, interpolated, fetched_at
                FROM bars
                WHERE ticker = ? AND ts >= ?
                ORDER BY ts
                """,
                (ticker, start),
            ).fetchall()
            # Every range returns the literal stored minute history. The chart's
            # client-side viewport handles display density, zoom, and panning.
            chart_bars = self._compact_bars(rows, limit=max(1, len(rows)))
            trade_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(trades)")
            }
            direction_column = (
                "direction" if "direction" in trade_columns else "1 AS direction"
            )
            trades = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT ticker, algo, ts, action, {direction_column}
                    FROM trades
                    WHERE ticker = ? AND ts >= ?
                    ORDER BY ts
                    """,
                    (ticker, start),
                )
            ]
            events = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT event, event_ts, window_start, window_end, direction, strength
                    FROM events
                    WHERE ticker = ? AND window_end >= ? AND window_start <= ?
                    ORDER BY event_ts
                    """,
                    (ticker, start, latest),
                )
            ]
            shape_forecast = self._shape_forecast(
                connection, config, ticker, start, int(latest)
            )

        return {
            "ticker": ticker,
            "range": span,
            "start": int(rows[0]["ts"]) if rows else None,
            "end": int(rows[-1]["ts"]) if rows else None,
            "source_count": len(rows),
            "interpolated_count": sum(int(row["interpolated"]) for row in rows),
            "bars": chart_bars,
            "trades": trades,
            "events": events,
            "shape_forecast": shape_forecast,
            "algo_overlays": self._algo_overlays(rows, config),
        }

    def logs(
        self,
        service: str | None,
        level: str | None,
        limit: str | int,
    ) -> dict[str, Any]:
        service = service or None
        level = level or None
        if service and not SERVICE_PATTERN.fullmatch(service):
            raise DashboardError("Invalid service filter")
        if level and level not in {"info", "warn", "error"}:
            raise DashboardError("Level must be info, warn, or error")
        try:
            row_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise DashboardError("Log limit must be a number") from exc
        if not 1 <= row_limit <= 1000:
            raise DashboardError("Log limit must be between 1 and 1000")

        row_clauses: list[str] = []
        row_parameters: list[Any] = []
        if service:
            row_clauses.append("service = ?")
            row_parameters.append(service)
        if level:
            row_clauses.append("level = ?")
            row_parameters.append(level)
        row_where = f"WHERE {' AND '.join(row_clauses)}" if row_clauses else ""

        count_where = "WHERE service = ?" if service else ""
        count_parameters = (service,) if service else ()
        with self.connection() as connection:
            services = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT service FROM logs ORDER BY service"
                )
            ]
            counts = {"info": 0, "warn": 0, "error": 0}
            for row in connection.execute(
                f"SELECT level, COUNT(*) AS rows FROM logs {count_where} GROUP BY level",
                count_parameters,
            ):
                counts[row["level"]] = row["rows"]
            rows = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT ts, service, level, message
                    FROM logs
                    {row_where}
                    ORDER BY ts DESC, rowid DESC
                    LIMIT ?
                    """,
                    (*row_parameters, row_limit),
                )
            ]

        return {
            "generated_at": int(datetime.now(tz=UTC).timestamp()),
            "service": service,
            "level": level,
            "limit": row_limit,
            "services": services,
            "counts": {**counts, "total": sum(counts.values())},
            "rows": rows,
            "has_more": len(rows) == row_limit,
        }

    def node_detail(self, ticker: str, kind: str, version: str | None) -> dict[str, Any]:
        ticker = self._valid_symbol(ticker)
        if not kind or len(kind) > 120:
            raise DashboardError("A signal or algo name is required")

        with self.connection() as connection:
            if version is None:
                row = connection.execute(
                    """
                    SELECT config
                    FROM outputs
                    WHERE ticker = ? AND kind = ?
                    ORDER BY ts DESC, computed_at DESC
                    LIMIT 1
                    """,
                    (ticker, kind),
                ).fetchone()
                version = str(row["config"]) if row else str(self.config().get("version", ""))

            definition, node_type = self._node_definition(connection, kind, version)
            values = []
            for row in connection.execute(
                """
                SELECT ts, output, computed_at
                FROM outputs
                WHERE ticker = ? AND kind = ? AND config = ?
                ORDER BY ts DESC
                LIMIT 240
                """,
                (ticker, kind, version),
            ).fetchall():
                values.append(
                    {
                        "ts": row["ts"],
                        "output": self._json_value(row["output"]),
                        "computed_at": row["computed_at"],
                    }
                )
            values.reverse()

        return {
            "ticker": ticker,
            "kind": kind,
            "node_type": node_type,
            "version": version,
            "definition": definition,
            "values": values,
        }

    def performance(self, ticker: str) -> dict[str, Any]:
        ticker = self._valid_symbol(ticker)
        config = self.config()
        algo_names = set(self._mapping(config.get("algos")).keys())

        with self.connection() as connection:
            for row in connection.execute("SELECT content FROM configs"):
                stored = self._json_value(row["content"])
                if isinstance(stored, dict):
                    algo_names.update(self._mapping(stored.get("algos")).keys())

            if not algo_names:
                return {"ticker": ticker, "rows": []}

            placeholders = ",".join("?" for _ in algo_names)
            query = f"""
                SELECT o.kind, o.config, o.ts, o.output, b.close
                FROM outputs o
                JOIN bars b ON b.ticker = o.ticker AND b.ts = o.ts
                WHERE o.ticker = ? AND o.kind IN ({placeholders})
                ORDER BY o.kind, o.config, o.ts
            """
            rows = connection.execute(query, (ticker, *sorted(algo_names))).fetchall()

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["kind"], row["config"])
            group = groups.setdefault(
                key,
                {
                    "algo": row["kind"],
                    "version": row["config"],
                    "entries": 0,
                    "closed_units": 0,
                    "wins": 0,
                    "realized_pct": 0.0,
                    "open_units": [],
                    "last_price": float(row["close"]),
                },
            )
            price = float(row["close"])
            group["last_price"] = price
            is_entry, is_close, direction = self._output_action(
                self._json_value(row["output"])
            )
            if is_close:
                for entry_price, entry_direction in group["open_units"]:
                    result = ((price / entry_price) - 1.0) * 100.0 * entry_direction
                    group["realized_pct"] += result
                    group["closed_units"] += 1
                    if result > 0:
                        group["wins"] += 1
                group["open_units"] = []
            elif is_entry:
                group["entries"] += 1
                group["open_units"].append((price, direction))

        performance_rows = []
        for group in groups.values():
            unrealized = sum(
                ((group["last_price"] / entry_price) - 1.0) * 100.0 * direction
                for entry_price, direction in group["open_units"]
            )
            closed = group["closed_units"]
            performance_rows.append(
                {
                    "algo": group["algo"],
                    "version": group["version"],
                    "entries": group["entries"],
                    "closed_units": closed,
                    "open_units": len(group["open_units"]),
                    "realized_pct": round(group["realized_pct"], 4),
                    "total_pct": round(group["realized_pct"] + unrealized, 4),
                    "win_rate": round((group["wins"] / closed) * 100.0, 1) if closed else None,
                }
            )
        performance_rows.sort(key=lambda item: (item["algo"], str(item["version"])))
        return {"ticker": ticker, "rows": performance_rows}

    @staticmethod
    def _median(values: list[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    @classmethod
    def _outcome_stats(
        cls,
        closed_trades: list[dict[str, Any]],
        open_positions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        returns = [float(item["return_pct"]) for item in closed_trades]
        unrealized = [
            float(item["return_pct"])
            for item in open_positions
            if item.get("return_pct") is not None
        ]
        holds = [float(item["hold_minutes"]) for item in closed_trades]
        wins = sum(value > 0 for value in returns)
        losses = sum(value < 0 for value in returns)
        flats = len(returns) - wins - losses
        gross_profit = sum(value for value in returns if value > 0)
        gross_loss = sum(value for value in returns if value < 0)
        realized = sum(returns)

        exit_returns: dict[int, float] = {}
        for item in closed_trades:
            timestamp = int(item["exit_ts"])
            exit_returns[timestamp] = exit_returns.get(timestamp, 0.0) + float(
                item["return_pct"]
            )
        cumulative = 0.0
        peak = 0.0
        maximum_drawdown = 0.0
        for timestamp in sorted(exit_returns):
            cumulative += exit_returns[timestamp]
            peak = max(peak, cumulative)
            maximum_drawdown = min(maximum_drawdown, cumulative - peak)

        return {
            "closed_units": len(returns),
            "open_units": len(open_positions),
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "win_rate": round((wins / len(returns)) * 100.0, 1) if returns else None,
            "realized_return_pct": round(realized, 4),
            "unrealized_return_pct": round(sum(unrealized), 4),
            "total_return_pct": round(realized + sum(unrealized), 4),
            "average_return_pct": round(realized / len(returns), 4) if returns else None,
            "median_return_pct": round(cls._median(returns), 4) if returns else None,
            "best_return_pct": round(max(returns), 4) if returns else None,
            "worst_return_pct": round(min(returns), 4) if returns else None,
            "gross_profit_pct": round(gross_profit, 4),
            "gross_loss_pct": round(gross_loss, 4),
            "profit_factor": (
                round(gross_profit / abs(gross_loss), 3) if gross_loss < 0 else None
            ),
            "profit_factor_unbounded": bool(gross_profit > 0 and gross_loss == 0),
            "max_drawdown_pct": round(maximum_drawdown, 4),
            "average_hold_minutes": round(sum(holds) / len(holds), 1) if holds else None,
            "median_hold_minutes": round(cls._median(holds), 1) if holds else None,
        }

    def algorithms(self) -> dict[str, Any]:
        config = self.config()
        current_version = str(config.get("version", ""))
        current_definitions = self._mapping(config.get("algos"))
        definitions: dict[str, dict[str, Any]] = {
            name: {
                "definition": definition if isinstance(definition, dict) else {},
                "version": current_version,
                "configured": True,
            }
            for name, definition in current_definitions.items()
        }

        with self.connection() as connection:
            for row in connection.execute(
                "SELECT version, content FROM configs ORDER BY first_seen DESC"
            ):
                stored = self._json_value(row["content"])
                if not isinstance(stored, dict):
                    continue
                for name, definition in self._mapping(stored.get("algos")).items():
                    definitions.setdefault(
                        name,
                        {
                            "definition": definition if isinstance(definition, dict) else {},
                            "version": str(row["version"]),
                            "configured": False,
                        },
                    )

            trade_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(trades)")
            }
            direction_column = (
                "t.direction" if "direction" in trade_columns else "1 AS direction"
            )
            trade_rows = connection.execute(
                f"""
                SELECT t.ticker, t.algo, t.ts, t.action, {direction_column}, b.close
                FROM trades t
                LEFT JOIN bars b ON b.ticker = t.ticker AND b.ts = t.ts
                ORDER BY t.ts, t.ticker, t.algo,
                         CASE WHEN t.action = 'entry' THEN 0 ELSE 1 END
                """
            ).fetchall()
            latest_prices = {
                row["ticker"]: {"ts": int(row["ts"]), "price": float(row["close"])}
                for row in connection.execute(
                    """
                    SELECT b.ticker, b.ts, b.close
                    FROM bars b
                    JOIN (
                        SELECT ticker, MAX(ts) AS ts FROM bars GROUP BY ticker
                    ) latest ON latest.ticker = b.ticker AND latest.ts = b.ts
                    """
                )
            }

        algo_data: dict[str, dict[str, Any]] = {}

        def group_for(name: str) -> dict[str, Any]:
            return algo_data.setdefault(
                name,
                {
                    "entries": 0,
                    "exit_actions": 0,
                    "orphan_exits": 0,
                    "unpriced_units": 0,
                    "first_action": None,
                    "last_action": None,
                    "sessions": set(),
                    "closed": [],
                    "ticker_counts": {},
                },
            )

        open_units: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in trade_rows:
            algo = str(row["algo"])
            ticker = str(row["ticker"])
            timestamp = int(row["ts"])
            action = str(row["action"])
            group = group_for(algo)
            ticker_counts = group["ticker_counts"].setdefault(
                ticker,
                {"entries": 0, "exit_actions": 0, "first_action": None, "last_action": None},
            )
            group["first_action"] = timestamp if group["first_action"] is None else min(group["first_action"], timestamp)
            group["last_action"] = timestamp if group["last_action"] is None else max(group["last_action"], timestamp)
            ticker_counts["first_action"] = timestamp if ticker_counts["first_action"] is None else min(ticker_counts["first_action"], timestamp)
            ticker_counts["last_action"] = timestamp if ticker_counts["last_action"] is None else max(ticker_counts["last_action"], timestamp)
            group["sessions"].add(datetime.fromtimestamp(timestamp, tz=EASTERN).date().isoformat())
            key = (algo, ticker)
            units = open_units.setdefault(key, [])
            price = float(row["close"]) if row["close"] is not None else None

            if action == "entry":
                group["entries"] += 1
                ticker_counts["entries"] += 1
                units.append(
                    {
                        "ticker": ticker,
                        "entry_ts": timestamp,
                        "entry_price": price,
                        "direction": int(row["direction"]),
                    }
                )
                continue

            group["exit_actions"] += 1
            ticker_counts["exit_actions"] += 1
            if not units:
                group["orphan_exits"] += 1
                continue
            for entry in units:
                if entry["entry_price"] is None or price is None:
                    group["unpriced_units"] += 1
                    continue
                result = (
                    ((price / float(entry["entry_price"])) - 1.0)
                    * 100.0
                    * int(entry["direction"])
                )
                group["closed"].append(
                    {
                        **entry,
                        "exit_ts": timestamp,
                        "exit_price": price,
                        "return_pct": round(result, 4),
                        "hold_minutes": round(max(0, timestamp - int(entry["entry_ts"])) / 60.0, 1),
                    }
                )
            open_units[key] = []

        for name in definitions:
            group_for(name)

        algorithms = []
        for name in sorted(algo_data):
            group = algo_data[name]
            definition_meta = definitions.get(
                name,
                {"definition": {}, "version": None, "configured": False},
            )
            definition = definition_meta["definition"]
            positions = []
            for (algo, ticker), units in open_units.items():
                if algo != name:
                    continue
                latest = latest_prices.get(ticker)
                for entry in units:
                    marked_return = None
                    if entry["entry_price"] is not None and latest:
                        marked_return = (
                            ((latest["price"] / float(entry["entry_price"])) - 1.0)
                            * 100.0
                            * int(entry["direction"])
                        )
                    positions.append(
                        {
                            **entry,
                            "mark_ts": latest["ts"] if latest else None,
                            "mark_price": latest["price"] if latest else None,
                            "return_pct": round(marked_return, 4) if marked_return is not None else None,
                        }
                    )

            ticker_rows = []
            tickers = sorted(
                set(group["ticker_counts"])
                | {item["ticker"] for item in group["closed"]}
                | {item["ticker"] for item in positions}
            )
            for ticker in tickers:
                counts = group["ticker_counts"].get(
                    ticker,
                    {"entries": 0, "exit_actions": 0, "first_action": None, "last_action": None},
                )
                ticker_stats = self._outcome_stats(
                    [item for item in group["closed"] if item["ticker"] == ticker],
                    [item for item in positions if item["ticker"] == ticker],
                )
                ticker_rows.append({"ticker": ticker, **counts, **ticker_stats})

            stats = self._outcome_stats(group["closed"], positions)
            stats.update(
                {
                    "entries": group["entries"],
                    "exit_actions": group["exit_actions"],
                    "orphan_exits": group["orphan_exits"],
                    "unpriced_units": group["unpriced_units"],
                    "session_count": len(group["sessions"]),
                    "ticker_count": len(tickers),
                    "first_action": group["first_action"],
                    "last_action": group["last_action"],
                }
            )
            algorithms.append(
                {
                    "name": name,
                    "function": definition.get("function", name),
                    "inputs": definition.get("inputs", []),
                    "params": definition.get("params", {}),
                    "trades_enabled": definition.get("trades") is True,
                    "configured": definition_meta["configured"],
                    "version": definition_meta["version"],
                    "stats": stats,
                    "tickers": ticker_rows,
                    "recent_trades": sorted(
                        group["closed"], key=lambda item: item["exit_ts"], reverse=True
                    )[:12],
                    "open_positions": sorted(
                        positions, key=lambda item: item["entry_ts"], reverse=True
                    ),
                }
            )

        return {
            "generated_at": int(datetime.now(tz=UTC).timestamp()),
            "market": self._market_state(config),
            "version": current_version,
            "return_basis": (
                "Gross percentage points summed per entry unit; no sizing, fees, or slippage. "
                "Trades are grouped by algorithm name because trade rows do not store config versions."
            ),
            "algorithms": algorithms,
        }

    def health(self) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("SELECT 1 FROM bars LIMIT 1").fetchone()
        return {
            "ok": True,
            "database": "read-only",
            "services": {
                "bars": self._service_health(self.config(), "bars", 8789),
                "algo": self._service_health(self.config(), "algo", 8791),
            },
        }

    def _quote(self, connection: sqlite3.Connection, ticker: str) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT ts, open, high, low, close, volume, fetched_at
            FROM bars WHERE ticker = ? ORDER BY ts DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if row is None:
            return {"ticker": ticker, "available": False}

        ts = int(row["ts"])
        local_day = datetime.fromtimestamp(ts, tz=EASTERN).date()
        start = int(datetime.combine(local_day, time.min, tzinfo=EASTERN).timestamp())
        previous = connection.execute(
            "SELECT close FROM bars WHERE ticker = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (ticker, start),
        ).fetchone()
        session = connection.execute(
            """
            SELECT MIN(low) AS low, MAX(high) AS high, SUM(volume) AS volume,
                   (SELECT open FROM bars WHERE ticker = ? AND ts >= ? ORDER BY ts LIMIT 1) AS open
            FROM bars WHERE ticker = ? AND ts >= ?
            """,
            (ticker, start, ticker, start),
        ).fetchone()
        baseline = float(previous["close"]) if previous else float(session["open"] or row["open"])
        close = float(row["close"])
        change = close - baseline
        change_pct = (change / baseline * 100.0) if baseline else 0.0
        return {
            "ticker": ticker,
            "available": True,
            "ts": ts,
            "price": close,
            "change": change,
            "change_pct": change_pct,
            "open": session["open"],
            "high": session["high"],
            "low": session["low"],
            "volume": session["volume"],
            "fetched_at": row["fetched_at"],
        }

    def _services(self, connection: sqlite3.Connection, config: dict[str, Any]) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT service, ts, level, message
            FROM (
                SELECT service, ts, level, message,
                       ROW_NUMBER() OVER (
                           PARTITION BY service ORDER BY ts DESC
                       ) AS position
                FROM logs
                WHERE service NOT IN ('bars', 'algo')
            )
            WHERE position = 1
            ORDER BY service
            """
        ).fetchall()
        now = int(datetime.now(tz=UTC).timestamp())
        services = []
        for row in rows:
            age = max(0, now - int(row["ts"]))
            if row["service"] == "events":
                cadence = 86_400
            else:
                cadence = 60
            cadence = max(1, int(cadence))
            if age <= max(90, cadence * 2.5):
                state = "active"
            elif age <= max(300, cadence * 6):
                state = "quiet"
            else:
                state = "stale"
            services.append(
                {
                    "service": row["service"],
                    "ts": row["ts"],
                    "level": row["level"],
                    "message": row["message"],
                    "age_seconds": age,
                    "state": state,
                }
            )
        services.extend(
            (
                self._service_health(config, "bars", 8789),
                self._service_health(config, "algo", 8791),
            )
        )
        services.sort(key=lambda service: service["service"])
        return services

    @staticmethod
    def _service_health(
        config: dict[str, Any], service: str, default_port: int
    ) -> dict[str, Any]:
        now = int(datetime.now(tz=UTC).timestamp())
        try:
            port = int(config.get(service, {}).get("api_port", default_port))
            with urlopen("http://127.0.0.1:%d/health" % port, timeout=0.5) as response:
                payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise ValueError("invalid health response")
            if payload.get("ok") is not True or payload.get("service") != service:
                raise ValueError("invalid health response")
            timestamp = int(payload.get("ts", now))
            return {
                "service": service,
                "ts": timestamp,
                "level": "info",
                "message": "health API: %s" % payload.get("status", "running"),
                "age_seconds": max(0, now - timestamp),
                "state": "active",
                "started_at": payload.get("started_at"),
                "pid": payload.get("pid"),
            }
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return {
                "service": service,
                "ts": now,
                "level": "error",
                "message": "health API unavailable",
                "age_seconds": 0,
                "state": "stale",
            }

    def _logs(self, connection: sqlite3.Connection, where: str, limit: int) -> list[dict[str, Any]]:
        query = f"SELECT ts, service, level, message FROM logs {where} ORDER BY ts DESC, rowid DESC LIMIT ?"
        return [dict(row) for row in connection.execute(query, (limit,))]

    def _nodes(self, connection: sqlite3.Connection, config: dict[str, Any]) -> list[dict[str, Any]]:
        nodes: dict[str, dict[str, Any]] = {}
        for node_type, key in (("signal", "signals"), ("algo", "algos")):
            for name, definition in self._mapping(config.get(key)).items():
                nodes[name] = {
                    "name": name,
                    "node_type": node_type,
                    "definition": definition,
                    "enabled": not isinstance(definition, dict) or definition.get("enabled", True),
                    "latest_by_ticker": {},
                }

        for row in connection.execute(
            """
            SELECT ticker, kind, config, ts, output
            FROM (
                SELECT ticker, kind, config, ts, output,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker, kind ORDER BY ts DESC, computed_at DESC
                       ) AS position
                FROM outputs
            )
            WHERE position = 1
            ORDER BY kind, ticker
            """
        ):
            node = nodes.setdefault(
                row["kind"],
                {
                    "name": row["kind"],
                    "node_type": "output",
                    "definition": {},
                    "enabled": True,
                    "latest_by_ticker": {},
                },
            )
            node.setdefault("latest_by_ticker", {})[row["ticker"]] = {
                "ts": row["ts"],
                "version": row["config"],
                "output": self._json_value(row["output"]),
            }
        return sorted(nodes.values(), key=lambda item: (item["node_type"], item["name"]))

    def _node_definition(
        self, connection: sqlite3.Connection, kind: str, version: str
    ) -> tuple[Any, str]:
        current = self.config()
        candidates: list[dict[str, Any]] = []
        if str(current.get("version", "")) == str(version):
            candidates.append(current)
        row = connection.execute(
            "SELECT content FROM configs WHERE version = ?", (version,)
        ).fetchone()
        if row:
            stored = self._json_value(row["content"])
            if isinstance(stored, dict):
                candidates.append(stored)
        if not candidates:
            candidates.append(current)

        for candidate in candidates:
            signals = self._mapping(candidate.get("signals"))
            if kind in signals:
                return signals[kind], "signal"
            algos = self._mapping(candidate.get("algos"))
            if kind in algos:
                return algos[kind], "algo"
        return {}, "output"

    def _range_start(
        self, connection: sqlite3.Connection, ticker: str, latest: int, span: str
    ) -> int:
        if span == "HISTORY":
            return CHART_HISTORY_START
        if span == "ALL":
            return 0
        if span in {"1M", "3M", "120D"}:
            days = {"1M": 30, "3M": 90, "120D": 120}[span]
            return latest - (days * 86_400)
        session_count = 1 if span == "1D" else 5
        row = connection.execute(
            """
            SELECT MIN(first_ts)
            FROM (
                SELECT date(ts, 'unixepoch') AS session, MIN(ts) AS first_ts
                FROM bars
                WHERE ticker = ? AND ts <= ?
                GROUP BY session
                ORDER BY session DESC
                LIMIT ?
            )
            """,
            (ticker, latest, session_count),
        ).fetchone()
        return int(row[0] or latest)

    def _algo_overlays(
        self, rows: list[sqlite3.Row], config: dict[str, Any]
    ) -> list[dict[str, Any]]:
        signals = self._mapping(config.get("signals"))
        algos = self._mapping(config.get("algos"))
        polling = self._mapping(config.get("live_polling"))
        early_dates = {str(value) for value in config.get("early_closes", [])}
        try:
            regular_open = time.fromisoformat(str(polling.get("regular_open", "09:30")))
            regular_close = time.fromisoformat(str(polling.get("regular_close", "16:00")))
            early_close = time.fromisoformat(str(polling.get("early_close", "13:00")))
        except ValueError:
            regular_open, regular_close, early_close = time(9, 30), time(16), time(13)

        overlays = []
        for algo_name, definition in algos.items():
            if not isinstance(definition, dict) or not definition.get("trades", False):
                continue
            if definition.get("function", algo_name) != "range_breakout":
                continue
            inputs = definition.get("inputs", [])
            if not isinstance(inputs, list):
                continue
            range_definition = next(
                (
                    signals.get(name)
                    for name in inputs
                    if isinstance(signals.get(name), dict)
                    and signals[name].get("function", name) == "opening_range"
                ),
                None,
            )
            params = range_definition.get("params", {}) if range_definition else {}
            minutes = params.get("minutes") if isinstance(params, dict) else None
            if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
                continue

            sessions: dict[str, list[sqlite3.Row]] = {}
            session_bounds: dict[str, tuple[int, int]] = {}
            for row in rows:
                timestamp = int(row["ts"])
                local = datetime.fromtimestamp(timestamp, tz=EASTERN)
                session = local.date().isoformat()
                close_time = early_close if session in early_dates else regular_close
                session_open = datetime.combine(local.date(), regular_open, tzinfo=EASTERN)
                session_close = datetime.combine(local.date(), close_time, tzinfo=EASTERN)
                start = int(session_open.timestamp())
                end = int(session_close.timestamp())
                if start <= timestamp < end and not int(row["interpolated"]):
                    sessions.setdefault(session, []).append(row)
                    session_bounds[session] = (start, end)

            ranges = []
            for session, session_rows in sessions.items():
                opening_rows = session_rows[:minutes]
                if len(opening_rows) < minutes:
                    continue
                start, end = session_bounds[session]
                ranges.append(
                    {
                        "date": session,
                        "start": start + minutes * 60,
                        "end": end,
                        "high": max(float(row["high"]) for row in opening_rows),
                        "low": min(float(row["low"]) for row in opening_rows),
                    }
                )
            overlays.append(
                {
                    "algo": algo_name,
                    "kind": "opening_range",
                    "minutes": minutes,
                    "ranges": ranges,
                }
            )
        return overlays

    def _shape_forecast(
        self,
        connection: sqlite3.Connection,
        config: dict[str, Any],
        ticker: str,
        start: int,
        end: int,
    ) -> dict[str, Any] | None:
        shape_entry = next(
            (
                (name, definition)
                for name, definition in self._mapping(config.get("signals")).items()
                if isinstance(definition, dict)
                and definition.get("function", name) == "shape_v1"
            ),
            None,
        )
        if shape_entry is None:
            return None

        kind, definition = shape_entry
        params = self._mapping(definition.get("params"))
        stride_minutes = params.get("stride_minutes", 5)
        if (
            isinstance(stride_minutes, bool)
            or not isinstance(stride_minutes, int)
            or stride_minutes < 1
        ):
            stride_minutes = 5

        snapshots = []
        for row in connection.execute(
            """
            SELECT ts, output
            FROM outputs
            WHERE ticker = ? AND kind = ? AND config = ?
              AND ts >= ? AND ts <= ? AND output != 'null'
            ORDER BY ts
            """,
            (ticker, kind, str(config.get("version", "")), start, end),
        ):
            output = self._json_value(row["output"])
            if not isinstance(output, dict):
                continue
            top_shapes = output.get("top_shapes")
            if not isinstance(top_shapes, list):
                continue
            valid_shapes = []
            for item in top_shapes[:3]:
                if not isinstance(item, dict) or not isinstance(item.get("shape"), str):
                    continue
                probability = item.get("probability")
                if (
                    isinstance(probability, bool)
                    or not isinstance(probability, (int, float))
                    or not math.isfinite(probability)
                ):
                    continue
                valid_shapes.append(
                    {"shape": item["shape"], "probability": float(probability)}
                )
            if valid_shapes:
                snapshots.append({"ts": int(row["ts"]), "top_shapes": valid_shapes})

        return {
            "kind": kind,
            "stride_minutes": stride_minutes,
            "snapshots": snapshots,
        }

    def _compact_bars(self, rows: list[sqlite3.Row], limit: int = 1400) -> list[dict[str, Any]]:
        if not rows:
            return []
        bucket_size = max(1, math.ceil(len(rows) / limit))
        result: list[dict[str, Any]] = []
        session_rows: list[sqlite3.Row] = []
        session: str | None = None

        def flush() -> None:
            for index in range(0, len(session_rows), bucket_size):
                bucket = session_rows[index : index + bucket_size]
                result.append(
                    {
                        "ts": bucket[0]["ts"],
                        "open": bucket[0]["open"],
                        "high": max(item["high"] for item in bucket),
                        "low": min(item["low"] for item in bucket),
                        "close": bucket[-1]["close"],
                        "volume": sum(item["volume"] for item in bucket),
                        "interpolated": int(any(item["interpolated"] for item in bucket)),
                        "interpolated_count": sum(int(item["interpolated"]) for item in bucket),
                        "fetched_at": max(item["fetched_at"] for item in bucket),
                        "samples": len(bucket),
                    }
                )

        for row in rows:
            row_session = datetime.fromtimestamp(row["ts"], tz=EASTERN).date().isoformat()
            if session is not None and row_session != session:
                flush()
                session_rows = []
            session = row_session
            session_rows.append(row)
        flush()
        return result

    def _market_state(self, config: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(tz=EASTERN)
        polling = config.get("live_polling", {})
        early_dates = {str(value) for value in config.get("early_closes", [])}
        close_key = "early_close" if now.date().isoformat() in early_dates else "regular_close"
        try:
            start_time = time.fromisoformat(str(polling.get("start", "04:00")))
            close_time = time.fromisoformat(str(polling.get(close_key, "16:00")))
        except ValueError:
            start_time, close_time = time(4), time(16)
        after_close = int(polling.get("after_close_minutes", 5))
        start = datetime.combine(now.date(), start_time, tzinfo=EASTERN)
        end = datetime.combine(now.date(), close_time, tzinfo=EASTERN) + timedelta(minutes=after_close)
        weekday = now.weekday() < 5
        live = weekday and start <= now <= end
        return {
            "state": "live" if live else "closed",
            "label": "Live polling" if live else "Outside live window",
            "eastern_time": int(now.timestamp()),
        }

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {
                str(item.get("name")): item
                for item in value
                if isinstance(item, dict) and item.get("name")
            }
        return {}

    @staticmethod
    def _json_value(value: str) -> Any:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value

    @staticmethod
    def _output_action(value: Any) -> tuple[bool, bool, int]:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            direction = value[2] if len(value) >= 3 else (1 if value[0] else 0)
            return bool(value[0]), bool(value[1]), int(direction)
        if isinstance(value, dict):
            return (
                bool(value.get("is_entry", value.get("entry", False))),
                bool(value.get("is_close_all", value.get("close_all", False))),
                int(
                    value.get(
                        "direction",
                        1 if value.get("is_entry", value.get("entry", False)) else 0,
                    )
                ),
            )
        return False, False, 0

    @staticmethod
    def _valid_symbol(value: str) -> str:
        if not SYMBOL_PATTERN.fullmatch(value):
            raise DashboardError("Invalid ticker")
        return value.upper()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TraderDashboard/1.0"

    @property
    def data(self) -> DashboardData:
        return self.server.data  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._api(parsed.path, parse_qs(parsed.query))
            return
        self._static(parsed.path)

    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            if path == "/api/overview":
                payload = self.data.overview(
                    self._param(query, "compact", "0") == "1"
                )
            elif path == "/api/bars":
                payload = self.data.bars(
                    self._param(query, "ticker"),
                    self._param(query, "range", "HISTORY"),
                )
            elif path == "/api/logs":
                payload = self.data.logs(
                    self._param(query, "service", None),
                    self._param(query, "level", None),
                    self._param(query, "limit", "250"),
                )
            elif path == "/api/detail":
                payload = self.data.node_detail(
                    self._param(query, "ticker"),
                    self._param(query, "kind"),
                    self._param(query, "version", None),
                )
            elif path == "/api/performance":
                payload = self.data.performance(self._param(query, "ticker"))
            elif path == "/api/algorithms":
                payload = self.data.algorithms()
            elif path == "/api/health":
                payload = self.data.health()
            else:
                self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(payload)
        except DashboardError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.log_error("Unhandled dashboard request failure: %s", exc)
            self._json({"error": "The dashboard could not read this view"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _static(self, path: str) -> None:
        route_files = {
            "": "index.html",
            "/": "index.html",
            "/logs": "logs.html",
            "/logs/": "logs.html",
            "/algos": "algos.html",
            "/algos/": "algos.html",
        }
        relative = route_files.get(path, path.lstrip("/"))
        candidate = (STATIC_DIR / relative).resolve()
        if STATIC_DIR not in candidate.parents and candidate != STATIC_DIR:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            candidate = STATIC_DIR / "index.html"
        content = candidate.read_bytes()
        content_type, _ = mimetypes.guess_type(candidate.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        self.end_headers()
        self.wfile.write(content)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(content)

    @staticmethod
    def _param(query: dict[str, list[str]], key: str, default: Any = "") -> Any:
        values = query.get(key)
        return values[0] if values else default

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], data: DashboardData) -> None:
        self.data = data
        super().__init__(address, DashboardHandler)


def parse_args() -> argparse.Namespace:
    default_config = Path(__file__).resolve().parent.parent / "config" / "config.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = DashboardData(args.config)
    data.health()
    server = DashboardServer((args.host, args.port), data)
    print(f"Trader dashboard: http://{args.host}:{args.port}")
    print(f"Config: {data.config_path}")
    print(f"Database: {data.database_path()} (read-only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
