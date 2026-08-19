#!/usr/bin/env python3
"""Read-only web dashboard for the shared trader database."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen
from zoneinfo import ZoneInfo

DASH_ROOT = Path(__file__).resolve().parent
REPO_ROOT = DASH_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config import market_schedule, read_config
from common.validation import normalize_symbol


EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
STATIC_DIR = DASH_ROOT / "static"
SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CHART_HISTORY_START = int(datetime(2026, 7, 6, tzinfo=EASTERN).timestamp())
ROBINHOOD_WORKER = DASH_ROOT / "robinhood_snapshot.py"
ROBINHOOD_TIMEOUT_SECONDS = 330


class DashboardError(Exception):
    """An error that is safe to return to the browser."""


class DashboardData:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()

    def config(self) -> dict[str, Any]:
        return dict(read_config(self.config_path, DashboardError))

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
            quotes = [self._quote(connection, ticker) for ticker in configured_tickers]

        return {
            "generated_at": int(datetime.now(tz=UTC).timestamp()),
            "market": self._market_state(config),
            "config": {
                "tickers": configured_tickers,
                "evaluation_days": config.get("algo", {}).get("evaluation_days"),
                "poll_seconds": config.get("algo", {}).get("poll_seconds"),
                "early_closes": config.get("early_closes", []),
                "signals": config.get("signals", {}),
                "algos": config.get("algos", {}),
            },
            "quotes": quotes,
        }

    def bars(
        self,
        ticker: str,
        span: str,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> dict[str, Any]:
        ticker = self._valid_symbol(ticker)
        span = span.upper()
        if span not in {"HISTORY", "1D", "5D", "1M", "3M", "120D", "ALL"}:
            raise DashboardError(
                "Range must be HISTORY, 1D, 5D, 1M, 3M, 120D, or ALL"
            )
        config = self.config()
        algo_display_names = self._algo_display_names(config)
        custom_window = window_start is not None or window_end is not None
        if custom_window:
            if window_start is None or window_end is None:
                raise DashboardError("A chart window requires start and end")
            try:
                requested_start = int(window_start)
                requested_end = int(window_end)
            except ValueError as exc:
                raise DashboardError("Chart window timestamps must be integers") from exc
            if requested_start < 0 or requested_end < requested_start:
                raise DashboardError("Invalid chart window")

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
                    "relative_momentum": self._relative_momentum(
                        connection, config, ticker, 0, 0
                    ),
                    "algo_overlays": self._algo_overlays([], config),
                    "source_count": 0,
                    "interpolated_count": 0,
                }

            if custom_window:
                start = requested_start
                end = min(requested_end, int(latest))
                response_range = "WINDOW"
            else:
                start = self._range_start(connection, ticker, int(latest), span)
                end = int(latest)
                response_range = span
            rows = connection.execute(
                """
                SELECT ts, open, high, low, close, volume, interpolated, fetched_at
                FROM bars
                WHERE ticker = ? AND ts >= ? AND ts <= ?
                ORDER BY ts
                """,
                (ticker, start, end),
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
                {
                    **dict(row),
                    "display_name": algo_display_names.get(str(row["algo"])),
                }
                for row in connection.execute(
                    f"""
                    SELECT ticker, algo, ts, action, {direction_column}
                    FROM trades
                    WHERE ticker = ? AND ts >= ? AND ts <= ?
                    ORDER BY ts
                    """,
                    (ticker, start, end),
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
                    (ticker, start, end),
                )
            ]
            shape_forecast = self._shape_forecast(
                connection, config, ticker, start, end
            )
            relative_momentum = self._relative_momentum(
                connection, config, ticker, start, end
            )

        return {
            "ticker": ticker,
            "range": response_range,
            "start": int(rows[0]["ts"]) if rows else None,
            "end": int(rows[-1]["ts"]) if rows else None,
            "source_count": len(rows),
            "interpolated_count": sum(int(row["interpolated"]) for row in rows),
            "bars": chart_bars,
            "trades": trades,
            "events": events,
            "shape_forecast": shape_forecast,
            "relative_momentum": relative_momentum,
            "algo_overlays": self._algo_overlays(rows, config),
        }

    def calendar(self, ticker: str, span: str) -> dict[str, Any]:
        ticker = self._valid_symbol(ticker)
        span = span.upper()
        if span not in {"HISTORY", "1D", "5D", "1M", "3M", "120D", "ALL"}:
            raise DashboardError(
                "Range must be HISTORY, 1D, 5D, 1M, 3M, 120D, or ALL"
            )

        with self.connection() as connection:
            latest = connection.execute(
                "SELECT MAX(ts) FROM bars WHERE ticker = ?", (ticker,)
            ).fetchone()[0]
            if latest is None:
                return {"ticker": ticker, "range": span, "sessions": []}
            start = self._range_start(connection, ticker, int(latest), span)
            timestamps = connection.execute(
                "SELECT ts FROM bars WHERE ticker = ? AND ts >= ? ORDER BY ts",
                (ticker, start),
            )

            sessions: list[dict[str, Any]] = []
            for row in timestamps:
                timestamp = int(row["ts"])
                date = datetime.fromtimestamp(timestamp, tz=EASTERN).date().isoformat()
                previous = sessions[-1] if sessions else None
                if previous is None or previous["date"] != date:
                    sessions.append(
                        {"date": date, "start": timestamp, "end": timestamp}
                    )
                else:
                    previous["end"] = timestamp

        return {
            "ticker": ticker,
            "range": span,
            "start": sessions[0]["start"] if sessions else None,
            "end": sessions[-1]["end"] if sessions else None,
            "sessions": list(reversed(sessions)),
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

    def node_detail(self, ticker: str, kind: str) -> dict[str, Any]:
        ticker = self._valid_symbol(ticker)
        if not kind or len(kind) > 120:
            raise DashboardError("A signal or algo name is required")

        with self.connection() as connection:
            definition, node_type = self._node_definition(kind)
            values = []
            for row in connection.execute(
                """
                SELECT ts, output, computed_at
                FROM outputs
                WHERE ticker = ? AND kind = ?
                ORDER BY ts DESC
                LIMIT 240
                """,
                (ticker, kind),
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
            "definition": definition,
            "values": values,
        }

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
        algo_display_names = self._algo_display_names(config)
        configured_tickers = {str(ticker) for ticker in config.get("tickers", [])}
        current_definitions = self._mapping(config.get("algos"))
        definitions: dict[str, dict[str, Any]] = {
            name: {
                "definition": definition if isinstance(definition, dict) else {},
                "configured": True,
                "active_from": None,
            }
            for name, definition in current_definitions.items()
        }
        with self.connection() as connection:
            for row in connection.execute(
                "SELECT name,definition,active_from FROM algos ORDER BY name"
            ):
                name = str(row["name"])
                definition = self._json_value(row["definition"])
                definitions[name] = {
                    "definition": definition if isinstance(definition, dict) else {},
                    "configured": name in current_definitions,
                    "active_from": row["active_from"],
                }
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
                {"definition": {}, "configured": False, "active_from": None},
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
                configured_tickers
                | set(group["ticker_counts"])
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
                    "display_name": algo_display_names.get(name),
                    "function": definition.get("function", name),
                    "inputs": definition.get("inputs", []),
                    "params": definition.get("params", {}),
                    "configured": definition_meta["configured"],
                    "active_from": definition_meta["active_from"],
                    "stats": stats,
                    "tickers": ticker_rows,
                    "trades": sorted(
                        group["closed"], key=lambda item: item["exit_ts"], reverse=True
                    ),
                    "open_positions": sorted(
                        positions, key=lambda item: item["entry_ts"], reverse=True
                    ),
                }
            )

        return {
            "generated_at": int(datetime.now(tz=UTC).timestamp()),
            "market": self._market_state(config),
            "return_basis": (
                "Gross percentage points summed per entry unit; no sizing, fees, or slippage."
            ),
            "algorithms": algorithms,
        }

    def trades(self) -> dict[str, Any]:
        """Return one combined trade book with daily and active summaries."""
        algorithm_payload = self.algorithms()
        broker_entries: dict[tuple[str, str, int], dict[str, Any]] = {}
        with self.connection() as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "broker_positions" in tables:
                for row in connection.execute(
                    """
                    SELECT ticker, algo, entry_ts, direction, symbol,
                           entry_order_id, exit_ts, exit_order_id
                    FROM broker_positions
                    ORDER BY entry_ts
                    """
                ):
                    broker_entries[
                        (str(row["ticker"]), str(row["algo"]), int(row["entry_ts"]))
                    ] = {
                        "execution_symbol": str(row["symbol"]),
                        "real_entry": True,
                        "real_close": row["exit_order_id"] is not None,
                        "broker_exit_ts": row["exit_ts"],
                    }

        closed: list[dict[str, Any]] = []
        active: list[dict[str, Any]] = []
        for algorithm in algorithm_payload["algorithms"]:
            algorithm_name = str(algorithm["name"])
            display_name = algorithm.get("display_name") or algorithm_name
            for trade in algorithm.get("trades", []):
                key = (str(trade["ticker"]), algorithm_name, int(trade["entry_ts"]))
                broker = broker_entries.get(key)
                closed.append(
                    {
                        **trade,
                        "algo": algorithm_name,
                        "display_name": display_name,
                        **self._broker_trade_state(broker, closed=True),
                    }
                )
            for position in algorithm.get("open_positions", []):
                key = (
                    str(position["ticker"]),
                    algorithm_name,
                    int(position["entry_ts"]),
                )
                broker = broker_entries.get(key)
                active.append(
                    {
                        **position,
                        "algo": algorithm_name,
                        "display_name": display_name,
                        **self._broker_trade_state(broker, closed=False),
                    }
                )

        closed.sort(key=lambda item: int(item["exit_ts"]), reverse=True)
        active.sort(key=lambda item: int(item["entry_ts"]), reverse=True)
        daily_trades: dict[str, list[dict[str, Any]]] = {}
        for trade in closed:
            market_date = datetime.fromtimestamp(
                int(trade["exit_ts"]), tz=EASTERN
            ).date().isoformat()
            daily_trades.setdefault(market_date, []).append(trade)
        daily = [
            {
                "date": market_date,
                "stats": self._outcome_stats(day_trades, []),
                "trades": day_trades,
            }
            for market_date, day_trades in sorted(daily_trades.items(), reverse=True)
        ]
        summary = self._outcome_stats(closed, active)
        summary.update(
            {
                "real_open_units": sum(
                    item["broker_state"] == "real_open" for item in active
                ),
                "paper_open_units": sum(
                    item["broker_state"] == "paper" for item in active
                ),
                "missing_real_closes": sum(
                    item["broker_state"] == "close_missing" for item in closed
                ),
                "trading_days": len(daily),
            }
        )
        return {
            "generated_at": int(datetime.now(tz=UTC).timestamp()),
            "market": algorithm_payload["market"],
            "return_basis": algorithm_payload["return_basis"],
            "summary": summary,
            "active": active,
            "daily": daily,
        }

    def robinhood(self) -> dict[str, Any]:
        """Fetch a fresh, sanitized snapshot from the configured Robinhood account."""
        config = self.config()
        broker = config.get("broker")
        if not isinstance(broker, dict):
            raise DashboardError("Broker config is unavailable")
        account_env = broker.get("account_env")
        if not isinstance(account_env, str) or not account_env.strip():
            raise DashboardError("Broker account config is unavailable")
        runtime_root = self.config_path.parent.parent
        account_number = os.environ.get(account_env, "").strip() or self._env_value(
            runtime_root / ".env", account_env
        )
        if not account_number:
            raise DashboardError("Robinhood account is not configured")
        python_path = Path(
            os.environ.get(
                "TRADER_ROBINHOOD_PYTHON",
                str(runtime_root / "bars" / ".venv" / "bin" / "python"),
            )
        )
        if not python_path.is_file():
            raise DashboardError("Robinhood runtime is unavailable")
        try:
            completed = subprocess.run(
                [str(python_path), str(ROBINHOOD_WORKER), str(self.config_path)],
                input=json.dumps({"account_number": account_number}),
                text=True,
                capture_output=True,
                timeout=ROBINHOOD_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DashboardError(
                "Robinhood did not respond before the timeout"
            ) from exc
        except OSError as exc:
            raise DashboardError("Robinhood runtime could not start") from exc
        if completed.returncode:
            detail = self._clean_error(completed.stderr or completed.stdout).replace(
                account_number, "configured account"
            )
            raise DashboardError(
                "Robinhood data is unavailable%s" % (f": {detail}" if detail else "")
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DashboardError("Robinhood returned an invalid response") from exc
        if not isinstance(payload, dict):
            raise DashboardError("Robinhood returned an invalid response")
        return payload

    def health(self) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("SELECT 1 FROM bars LIMIT 1").fetchone()
        config = self.config()
        return {
            "ok": True,
            "database": "read-only",
            "market": self._market_state(config),
            "services": {
                "bars": self._service_health(config, "bars", 8789),
                "algo": self._service_health(config, "algo", 8791),
            },
        }

    @staticmethod
    def _broker_trade_state(
        broker: dict[str, Any] | None, *, closed: bool
    ) -> dict[str, Any]:
        if broker is None:
            return {
                "execution_symbol": None,
                "real_entry": False,
                "real_close": False,
                "broker_state": "paper",
            }
        real_close = bool(broker["real_close"])
        if closed:
            state = "real_closed" if real_close else "close_missing"
        else:
            state = "closed_mismatch" if real_close else "real_open"
        return {**broker, "broker_state": state}

    @staticmethod
    def _env_value(path: Path, name: str) -> str:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() != name:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value.strip()
        return ""

    @staticmethod
    def _clean_error(value: str, limit: int = 240) -> str:
        return " ".join(str(value or "").split())[:limit]

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
        output_row = connection.execute(
            "SELECT MAX(computed_at) FROM outputs WHERE ticker = ? AND ts = ?",
            (ticker, ts),
        ).fetchone()
        chart_revision = max(
            int(row["fetched_at"]),
            int(output_row[0] or 0),
        )
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
            "chart_revision": chart_revision,
        }

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

    def _node_definition(self, kind: str) -> tuple[Any, str]:
        current = self.config()
        signals = self._mapping(current.get("signals"))
        if kind in signals:
            return signals[kind], "signal"
        algos = self._mapping(current.get("algos"))
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
        schedule = market_schedule(config, DashboardError)

        overlays = []
        for algo_name, definition in algos.items():
            if not isinstance(definition, dict):
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
                start, end = schedule.session_window(local.date())
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
            SELECT candidate.ts,
                   CASE WHEN json_valid(candidate.output)
                        THEN json_extract(candidate.output, '$.top_shapes')
                        ELSE NULL END AS top_shapes
            FROM bars AS chart_bar
            CROSS JOIN outputs AS candidate
              ON candidate.ticker = chart_bar.ticker
             AND candidate.ts = chart_bar.ts
            WHERE chart_bar.ticker = ?
              AND chart_bar.ts >= ? AND chart_bar.ts <= ?
              AND candidate.kind = ?
              AND candidate.output != 'null'
            ORDER BY candidate.ts
            """,
            (ticker, start, end, kind),
        ):
            top_shapes = self._json_value(row["top_shapes"])
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

    def _relative_momentum(
        self,
        connection: sqlite3.Connection,
        config: dict[str, Any],
        ticker: str,
        start: int,
        end: int,
    ) -> dict[str, Any] | None:
        momentum_entry = next(
            (
                (name, definition)
                for name, definition in self._mapping(config.get("signals")).items()
                if isinstance(definition, dict)
                and definition.get("function", name) == "relative_momentum"
            ),
            None,
        )
        if momentum_entry is None:
            return None

        kind, definition = momentum_entry
        params = self._mapping(definition.get("params"))
        strong_threshold = params.get("strong_percentile", 80.0)
        if (
            isinstance(strong_threshold, bool)
            or not isinstance(strong_threshold, (int, float))
            or not math.isfinite(strong_threshold)
        ):
            strong_threshold = 80.0
        strong_threshold = max(0.0, min(100.0, float(strong_threshold)))
        snapshots = []
        for row in connection.execute(
            """
            SELECT candidate.ts,
                   CASE WHEN json_valid(candidate.output)
                        THEN json_extract(candidate.output, '$.persistence_score')
                        ELSE NULL END AS persistence_score,
                   CASE WHEN json_valid(candidate.output)
                        THEN json_extract(candidate.output, '$.signed_persistence')
                        ELSE NULL END AS signed_persistence,
                   CASE WHEN json_valid(candidate.output)
                        THEN json_extract(candidate.output, '$.persistent')
                        ELSE NULL END AS persistent
            FROM bars AS chart_bar
            CROSS JOIN outputs AS candidate
              ON candidate.ticker = chart_bar.ticker
             AND candidate.ts = chart_bar.ts
            WHERE chart_bar.ticker = ?
              AND chart_bar.ts >= ? AND chart_bar.ts <= ?
              AND candidate.kind = ?
              AND candidate.output != 'null'
            ORDER BY candidate.ts
            """,
            (ticker, start, end, kind),
        ):
            persistence_score = row["persistence_score"]
            signed_persistence = row["signed_persistence"]
            if (
                isinstance(persistence_score, bool)
                or not isinstance(persistence_score, (int, float))
                or not math.isfinite(persistence_score)
                or isinstance(signed_persistence, bool)
                or not isinstance(signed_persistence, (int, float))
                or not math.isfinite(signed_persistence)
            ):
                continue
            snapshot = {
                "ts": int(row["ts"]),
                "value": round(
                    max(-100.0, min(100.0, float(signed_persistence) * 100.0)),
                    3,
                ),
                "persistent": bool(row["persistent"]),
            }
            snapshots.append(snapshot)

        return {
            "kind": kind,
            "strong_threshold": strong_threshold,
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
        start, end = market_schedule(config, DashboardError).collection_window(
            now.date()
        )
        live = now.weekday() < 5 and start <= now <= end
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

    @classmethod
    def _algo_display_names(cls, config: dict[str, Any]) -> dict[str, str]:
        return {
            str(name): value.strip()
            for name, value in cls._mapping(config.get("algo_display_names")).items()
            if isinstance(value, str) and value.strip()
        }

    @staticmethod
    def _json_value(value: str) -> Any:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value

    @staticmethod
    def _valid_symbol(value: str) -> str:
        return normalize_symbol(
            value,
            error=DashboardError,
            message="Invalid ticker",
        )


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
                payload = self.data.bars(
                    self._param(query, "ticker"),
                    self._param(query, "range", "HISTORY"),
                    self._param(query, "start", None),
                    self._param(query, "end", None),
                )
            elif path == "/api/calendar":
                payload = self.data.calendar(
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
                )
            elif path == "/api/algorithms":
                payload = self.data.algorithms()
            elif path == "/api/trades":
                payload = self.data.trades()
            elif path == "/api/robinhood":
                payload = self.data.robinhood()
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
            "/trades": "trades.html",
            "/trades/": "trades.html",
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
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
        compressed = accepts_gzip and len(content) >= 1024
        if compressed:
            content = gzip.compress(content, compresslevel=1)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
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
