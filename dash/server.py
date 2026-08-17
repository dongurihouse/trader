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

    def overview(self) -> dict[str, Any]:
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
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
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
        if span not in {"1D", "5D", "1M", "3M", "120D", "ALL"}:
            raise DashboardError("Range must be 1D, 5D, 1M, 3M, 120D, or ALL")

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
            trades = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT ticker, algo, ts, action
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
                    "open_prices": [],
                    "last_price": float(row["close"]),
                },
            )
            price = float(row["close"])
            group["last_price"] = price
            is_entry, is_close = self._output_pair(self._json_value(row["output"]))
            if is_close:
                for entry_price in group["open_prices"]:
                    result = ((price / entry_price) - 1.0) * 100.0
                    group["realized_pct"] += result
                    group["closed_units"] += 1
                    if result > 0:
                        group["wins"] += 1
                group["open_prices"] = []
            elif is_entry:
                group["entries"] += 1
                group["open_prices"].append(price)

        performance_rows = []
        for group in groups.values():
            unrealized = sum(
                ((group["last_price"] / entry_price) - 1.0) * 100.0
                for entry_price in group["open_prices"]
            )
            closed = group["closed_units"]
            performance_rows.append(
                {
                    "algo": group["algo"],
                    "version": group["version"],
                    "entries": group["entries"],
                    "closed_units": closed,
                    "open_units": len(group["open_prices"]),
                    "realized_pct": round(group["realized_pct"], 4),
                    "total_pct": round(group["realized_pct"] + unrealized, 4),
                    "win_rate": round((group["wins"] / closed) * 100.0, 1) if closed else None,
                }
            )
        performance_rows.sort(key=lambda item: (item["algo"], str(item["version"])))
        return {"ticker": ticker, "rows": performance_rows}

    def health(self) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("SELECT 1 FROM bars LIMIT 1").fetchone()
        return {
            "ok": True,
            "database": "read-only",
            "services": {"bars": self._bars_health(self.config())},
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
                WHERE service != 'bars'
            )
            WHERE position = 1
            ORDER BY service
            """
        ).fetchall()
        now = int(datetime.now(tz=UTC).timestamp())
        services = []
        for row in rows:
            age = max(0, now - int(row["ts"]))
            if row["service"] == "algo":
                cadence = config.get("algo", {}).get("poll_seconds", 30)
            elif row["service"] == "events":
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
        services.append(self._bars_health(config))
        services.sort(key=lambda service: service["service"])
        return services

    @staticmethod
    def _bars_health(config: dict[str, Any]) -> dict[str, Any]:
        now = int(datetime.now(tz=UTC).timestamp())
        try:
            port = int(config.get("bars", {}).get("api_port", 8789))
            with urlopen("http://127.0.0.1:%d/health" % port, timeout=0.5) as response:
                payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise ValueError("invalid Bars health response")
            if payload.get("ok") is not True or payload.get("service") != "bars":
                raise ValueError("invalid Bars health response")
            timestamp = int(payload.get("ts", now))
            return {
                "service": "bars",
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
                "service": "bars",
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
    def _output_pair(value: Any) -> tuple[bool, bool]:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return bool(value[0]), bool(value[1])
        if isinstance(value, dict):
            return (
                bool(value.get("is_entry", value.get("entry", False))),
                bool(value.get("is_close_all", value.get("close_all", False))),
            )
        return False, False

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
                payload = self.data.overview()
            elif path == "/api/bars":
                payload = self.data.bars(self._param(query, "ticker"), self._param(query, "range", "1D"))
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
