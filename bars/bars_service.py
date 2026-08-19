#!/usr/bin/python3
"""Always-on Robinhood minute-bar collector with a small SQLite query CLI."""

import argparse
import asyncio
import csv
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config import (
    MarketSchedule,
    configured_tickers,
    market_schedule,
    read_config,
    resolve_config_path,
)
from common.service import (
    request_operation as request_service_operation,
    write_json,
)
from common.validation import require_int

from bar_provider import (
    UTC,
    ConfigError,
    ProviderSettings,
    RelayError,
    RobinhoodClient,
    _epoch,
    _iso_utc,
)
from bar_store import BAR_COLUMNS, BarStore, JobRef, JobState


DEFAULT_CONFIG_PATH = ROOT.parent / "config" / "config.json"
EASTERN = ZoneInfo("America/New_York")
LIVE_INT_FIELDS = (
    ("poll_seconds", 60, 30),
)
BARS_INT_FIELDS = (
    ("api_port", 8789, 1024),
    ("sweep_days", 30, 1),
    ("poll_catchup_days", 7, 1),
    ("idle_seconds", 300, 30),
    ("retry_seconds", 30, 5),
    ("retry_max_seconds", 900, 30),
)
PROVIDER_INT_FIELDS = (
    ("oauth_callback_port", 8765, 1024),
    ("timeout_seconds", 300, 30),
    ("max_symbols_per_call", 3, 1),
)


@dataclass(frozen=True)
class Settings:
    tickers: Tuple[str, ...]
    schedule: MarketSchedule
    poll_seconds: int
    api_port: int
    history_start: date
    sweep_days: int
    poll_catchup_days: int
    idle_seconds: int
    retry_seconds: int
    retry_max_seconds: int
    database: Path
    provider: ProviderSettings


def _positive_int_fields(
    raw: dict,
    section: str,
    fields: Sequence[Tuple[str, int, int]],
) -> Dict[str, int]:
    return {
        name: require_int(
            raw.get(name, default),
            "%s.%s" % (section, name),
            minimum,
            error=ConfigError,
        )
        for name, default, minimum in fields
    }


def load_settings(path: Path) -> Settings:
    raw = read_config(path, ConfigError)
    tickers = configured_tickers(raw, ConfigError)
    schedule = market_schedule(raw, ConfigError)

    live = raw.get("live_polling") or {}
    bars = raw.get("bars") or {}
    for value, name in (
        (live, "live_polling"),
        (bars, "bars"),
    ):
        if not isinstance(value, dict):
            raise ConfigError("%s must be a JSON object" % name)
    provider = bars.get("provider") or {}
    if not isinstance(provider, dict):
        raise ConfigError("bars.provider must be a JSON object")

    bounds = provider.get("bounds", "extended")
    if bounds not in ("regular", "extended"):
        raise ConfigError("bars.provider.bounds must be 'regular' or 'extended'")

    history_start_raw = bars.get("history_start")
    try:
        history_start = date.fromisoformat(history_start_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("bars.history_start must be YYYY-MM-DD") from exc

    database = resolve_config_path(
        raw, "database", path, "../data/trader.sqlite3", ConfigError
    )
    oauth_store_value = provider.get(
        "oauth_store", "../data/robinhood_oauth.json"
    )
    oauth_store = Path(oauth_store_value).expanduser()
    if not oauth_store.is_absolute():
        oauth_store = (path.parent / oauth_store).resolve()

    live_ints = _positive_int_fields(live, "live_polling", LIVE_INT_FIELDS)
    bars_ints = _positive_int_fields(bars, "bars", BARS_INT_FIELDS)
    provider_ints = _positive_int_fields(
        provider, "bars.provider", PROVIDER_INT_FIELDS
    )

    return Settings(
        tickers=tickers,
        schedule=schedule,
        poll_seconds=live_ints["poll_seconds"],
        api_port=bars_ints["api_port"],
        history_start=history_start,
        sweep_days=bars_ints["sweep_days"],
        poll_catchup_days=bars_ints["poll_catchup_days"],
        idle_seconds=bars_ints["idle_seconds"],
        retry_seconds=bars_ints["retry_seconds"],
        retry_max_seconds=bars_ints["retry_max_seconds"],
        database=database,
        provider=ProviderSettings(
            bounds=bounds,
            url=str(
                provider.get("url", "https://agent.robinhood.com/mcp/trading")
            ),
            oauth_store=oauth_store,
            oauth_callback_port=provider_ints["oauth_callback_port"],
            timeout_seconds=provider_ints["timeout_seconds"],
            max_symbols_per_call=provider_ints["max_symbols_per_call"],
        ),
    )


def _collection_chunks(
    settings: Settings, start: datetime, end: datetime
) -> Iterator[Tuple[datetime, datetime]]:
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    day = start.astimezone(EASTERN).date()
    final_day = end.astimezone(EASTERN).date()
    while day <= final_day:
        if day.weekday() < 5:
            window_start, window_end = settings.schedule.collection_window(day)
            chunk_start = max(start, window_start)
            chunk_end = min(end, window_end)
            if chunk_start <= chunk_end:
                yield chunk_start, chunk_end
        day += timedelta(days=1)


class Collector:
    def __init__(self, settings: Settings, store: BarStore, provider=None):
        self.settings = settings
        self.store = store
        self.provider = provider or RobinhoodClient(settings.provider)

    async def _fetch_range(
        self,
        session,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        job: Optional[JobRef] = None,
    ) -> Dict[str, int]:
        total = Counter(received=0, interpolated=0)
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        for chunk_start, chunk_end in _collection_chunks(self.settings, start, end):
            start_iso = _iso_utc(chunk_start)
            end_iso = _iso_utc(chunk_end)
            logging.info(
                "fetching %s from %s through %s",
                ",".join(symbols),
                start_iso,
                end_iso,
            )
            bars = await session.fetch_bars(symbols, start_iso, end_iso)
            total.update(
                self.store.store_bars(
                    bars,
                    job=job,
                    progress_ts=_epoch(chunk_end) if job is not None else None,
                )
            )
        return total

    async def fetch_range(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        job: Optional[JobRef] = None,
    ) -> Dict[str, int]:
        async with self.provider.session() as session:
            return await self._fetch_range(session, symbols, start, end, job)

    async def _run_jobs(
        self,
        kind: str,
        target: str,
        jobs: Sequence[JobState],
        complete: bool = True,
    ) -> Dict[str, int]:
        total = Counter(received=0, interpolated=0)
        groups: Dict[Tuple[int, int, Optional[int]], List[str]] = {}
        for job in jobs:
            key = (job.window_start, job.window_end, job.progress_ts)
            groups.setdefault(key, []).append(job.scope)

        pending = []
        for (window_start, window_end, progress_ts), scopes in groups.items():
            start_ts = window_start if progress_ts is None else progress_ts + 1
            if start_ts <= window_end:
                pending.append((start_ts, window_end, scopes))
            elif complete:
                self.store.complete_jobs(kind, scopes, target)

        if not pending:
            return total

        async with self.provider.session() as session:
            for start_ts, window_end, scopes in pending:
                total.update(
                    await self._fetch_range(
                        session,
                        scopes,
                        datetime.fromtimestamp(start_ts, UTC),
                        datetime.fromtimestamp(window_end, UTC),
                        job=JobRef(
                            kind=kind,
                            target=target,
                            scopes=tuple(scopes),
                        ),
                    )
                )
                if complete:
                    self.store.complete_jobs(kind, scopes, target)
        return total

    async def poll(self, now: Optional[datetime] = None) -> Dict[str, int]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        await self.backfill(current)
        floor = current - timedelta(days=self.settings.sweep_days)
        groups: Dict[int, List[str]] = {}
        for ticker, latest in self.store.latest_by_ticker(
            self.settings.tickers
        ).items():
            start_ts = latest if latest is not None else _epoch(floor)
            if start_ts > _epoch(current):
                start_ts = _epoch(floor)
            groups.setdefault(start_ts, []).append(ticker)

        total = Counter(received=0, interpolated=0)
        async with self.provider.session() as session:
            for start_ts, tickers in sorted(groups.items()):
                start = datetime.fromtimestamp(start_ts, UTC)
                end = min(
                    current,
                    start + timedelta(days=self.settings.poll_catchup_days),
                )
                total.update(await self._fetch_range(session, tickers, start, end))
        self._record("poll complete", total)
        return total

    async def sweep(
        self,
        now: Optional[datetime] = None,
        scheduled_day: Optional[date] = None,
    ) -> Dict[str, int]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        await self.backfill(current)
        start = current - timedelta(days=self.settings.sweep_days)
        scheduled = scheduled_day is not None
        if not scheduled:
            stats = await self.fetch_range(self.settings.tickers, start, current)
        else:
            target = scheduled_day.isoformat()
            selected = tuple(self.settings.tickers)
            self.store.ensure_jobs(
                "sweep",
                selected,
                target,
                _epoch(start),
                _epoch(current),
            )
            jobs = self.store.incomplete_jobs("sweep", selected, target)
            if not jobs:
                return Counter(received=0, interpolated=0)
            stats = await self._run_jobs(
                "sweep",
                target,
                jobs,
            )
        prefix = "sweep complete"
        if scheduled_day is not None:
            prefix += " day=%s" % scheduled_day.isoformat()
        self._record(prefix, stats)
        return stats

    async def backfill(
        self,
        now: Optional[datetime] = None,
        force: bool = False,
    ) -> Optional[Dict[str, int]]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        selected = self.settings.tickers
        start = datetime.combine(
            self.settings.history_start,
            self.settings.schedule.live_start,
            tzinfo=EASTERN,
        ).astimezone(UTC)
        target = self.settings.history_start.isoformat()
        self.store.ensure_jobs(
            "backfill",
            selected,
            target,
            _epoch(start),
            _epoch(current),
            force=force,
        )
        jobs = self.store.incomplete_jobs("backfill", selected, target)
        if not jobs:
            return None
        stats = await self._run_jobs("backfill", target, jobs)
        self._record(
            "backfill run complete tickers=%s history_start=%s"
            % (
                ",".join(job.scope for job in jobs),
                self.settings.history_start.isoformat(),
            ),
            stats,
        )
        return stats

    def _record(self, prefix: str, stats: Dict[str, int]) -> None:
        message = " ".join(
            [prefix] + ["%s=%d" % item for item in stats.items()]
        )
        self.store.append_log("info", message)
        logging.info(message)

    def status_text(self) -> str:
        rows = self.store.summary(self.settings.tickers)
        by_ticker = {row["ticker"]: row for row in rows}
        lines = ["ticker  bars      earliest              latest"]
        for ticker in self.settings.tickers:
            row = by_ticker.get(ticker)
            if row:
                earliest = _iso_utc(datetime.fromtimestamp(row["earliest"], UTC))
                latest = _iso_utc(datetime.fromtimestamp(row["latest"], UTC))
                lines.append(
                    "%-7s %-9d %-21s %s"
                    % (ticker, row["bars"], earliest, latest)
                )
            else:
                lines.append("%-7s %-9d %-21s %s" % (ticker, 0, "-", "-"))
        lines.append("database %s" % self.store.path)
        return "\n".join(lines)

    def logs_text(self, limit: int = 50) -> str:
        rows = self.store.recent_logs(limit)
        lines = ["utc                  service  level  message"]
        for row in rows:
            timestamp = datetime.fromtimestamp(int(row["ts"]), UTC).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            lines.append(
                "%-20s %-8s %-6s %s"
                % (timestamp, row["service"], row["level"], row["message"])
            )
        return "\n".join(lines)


def request_operation(settings: Settings, operation: str) -> dict:
    """Submit collection work to the one running bars writer."""
    return request_service_operation(
        settings.api_port,
        operation,
        ("poll", "backfill", "sweep"),
        "bars",
        "make install",
        ConfigError,
    )


class Service:
    def __init__(self, collector: Collector):
        self.collector = collector
        self.settings = collector.settings
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.operation_lock = threading.Lock()
        self.requested_operation: Optional[str] = None
        self.operation_status: dict = {"name": None, "status": "idle"}
        self.started_at = _epoch(datetime.now(UTC))

    def health(self) -> dict:
        with self.operation_lock:
            operation = dict(self.operation_status)
        return {
            "ok": True,
            "service": "bars",
            "status": "running",
            "ts": _epoch(datetime.now(UTC)),
            "started_at": self.started_at,
            "pid": os.getpid(),
            "operation": operation,
        }

    def request_operation(self, name: str) -> bool:
        if name not in ("poll", "backfill", "sweep"):
            return False
        with self.operation_lock:
            if self.operation_status["status"] in ("queued", "running"):
                return False
            self.requested_operation = name
            self.operation_status = {
                "name": name,
                "status": "queued",
                "requested_at": _epoch(datetime.now(UTC)),
            }
        self.wake_event.set()
        return True

    def _take_operation(self) -> Optional[str]:
        with self.operation_lock:
            name = self.requested_operation
            if name is None:
                return None
            self.requested_operation = None
            self.operation_status["status"] = "running"
            self.operation_status["started_at"] = _epoch(datetime.now(UTC))
            return name

    def _finish_operation(self, error: Optional[Exception] = None) -> None:
        with self.operation_lock:
            if error is None:
                self.operation_status["status"] = "complete"
                self.operation_status["completed_at"] = _epoch(datetime.now(UTC))
                self.operation_status.pop("error", None)
            else:
                self.operation_status["status"] = "failed"
                self.operation_status["failed_at"] = _epoch(datetime.now(UTC))
                self.operation_status["error"] = str(error)

    def stop(self, signum=None, frame=None) -> None:
        logging.info("stopping service")
        self.stop_event.set()
        self.wake_event.set()

    async def _cycle(self) -> int:
        now = datetime.now(UTC)
        day = now.astimezone(EASTERN).date()
        if day.weekday() >= 5:
            return self.settings.idle_seconds

        start, final_at = self.settings.schedule.collection_window(day)
        if start <= now < final_at:
            await self.collector.poll(now)
            return self.settings.poll_seconds
        if now >= final_at and not self.collector.store.sweep_complete(
            day, self.settings.tickers
        ):
            await self.collector.sweep(now, scheduled_day=day)
        return self.settings.idle_seconds

    async def _run_operation(self, name: str) -> None:
        if name == "poll":
            await self.collector.poll()
        elif name == "backfill":
            await self.collector.backfill(force=True)
        elif name == "sweep":
            await self.collector.sweep()
        else:
            raise ConfigError("unknown bars operation: %s" % name)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        health_server = ThreadingHTTPServer(
            ("127.0.0.1", self.settings.api_port), _HealthHandler
        )
        health_server.service = self
        health_thread = threading.Thread(
            target=health_server.serve_forever,
            name="bars-health-api",
            daemon=True,
        )
        health_thread.start()
        try:
            logging.info(
                "service started with tickers=%s api=127.0.0.1:%d",
                ",".join(self.settings.tickers),
                self.settings.api_port,
            )
            self.collector.store.append_log("info", "service started")
            delay = self.settings.retry_seconds
            while not self.stop_event.is_set():
                requested = self._take_operation()
                try:
                    if requested is None:
                        wait_seconds = asyncio.run(self._cycle())
                    else:
                        asyncio.run(self._run_operation(requested))
                        self._finish_operation()
                        wait_seconds = 0
                    delay = self.settings.retry_seconds
                    self.wake_event.wait(wait_seconds)
                    self.wake_event.clear()
                except Exception as exc:
                    if requested is not None:
                        self._finish_operation(exc)
                    message = "cycle failed: %s" % exc
                    logging.exception("%s; retrying in %d seconds", message, delay)
                    try:
                        self.collector.store.append_log("error", message)
                    except sqlite3.Error:
                        logging.exception("could not write failure to logs table")
                    self.wake_event.wait(delay)
                    self.wake_event.clear()
                    delay = min(delay * 2, self.settings.retry_max_seconds)
            self.collector.store.append_log("info", "service stopped")
            logging.info("service stopped")
        finally:
            health_server.shutdown()
            health_server.server_close()
            health_thread.join(timeout=2)


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "BarsHealth/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/health":
            self.send_error(404)
            return
        write_json(self, self.server.service.health())

    def do_POST(self) -> None:  # noqa: N802
        operation = urlparse(self.path).path.lstrip("/")
        if operation not in ("poll", "backfill", "sweep"):
            self.send_error(404)
            return
        accepted = self.server.service.request_operation(operation)
        payload = {
            "ok": accepted,
            "operation": operation,
            "status": "queued" if accepted else "already_running",
        }
        write_json(self, payload, 202 if accepted else 409)

    def log_message(self, format: str, *args) -> None:
        return


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth", help="authorize the direct Robinhood connection")
    subparsers.add_parser("serve", help="run the always-on collector")
    subparsers.add_parser("once", help="request one live poll from the bars service")
    subparsers.add_parser("backfill", help="request the configured initial backfill")
    subparsers.add_parser("sweep", help="request the configured trailing sweep")
    subparsers.add_parser("status", help="show local bar coverage")
    subparsers.add_parser("logs", help="show recent service logs")

    query = subparsers.add_parser("query", help="read stored bars")
    query.add_argument("symbol")
    query.add_argument("--start")
    query.add_argument("--end")
    query.add_argument("--limit", type=int)
    query.add_argument("--format", choices=("csv", "json"), default="csv")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    _configure_logging(arguments.verbose)
    store = None
    try:
        settings = load_settings(arguments.config.resolve())
        submitted_operation = {
            "once": "poll",
            "backfill": "backfill",
            "sweep": "sweep",
        }.get(arguments.command)
        if submitted_operation is not None:
            print(
                json.dumps(
                    request_operation(settings, submitted_operation),
                    sort_keys=True,
                )
            )
            return 0

        store = BarStore(
            settings.database,
            read_only=arguments.command in ("status", "logs", "query"),
        )
        collector = Collector(settings, store)

        if arguments.command == "auth":
            tools = asyncio.run(collector.provider.authorize())
            print("Robinhood authorization complete.")
            print("Verified tool: %s" % collector.provider.TOOL)
            print("Available tools: %d" % len(tools))
        elif arguments.command == "serve":
            Service(collector).run()
        elif arguments.command == "status":
            print(collector.status_text())
        elif arguments.command == "logs":
            print(collector.logs_text())
        elif arguments.command == "query":
            ticker = arguments.symbol.strip().upper()
            if ticker not in settings.tickers:
                raise ConfigError("ticker is not in config: %s" % ticker)
            rows = store.query(
                ticker,
                arguments.start,
                arguments.end,
                arguments.limit,
            )
            objects = [dict(row) for row in rows]
            if arguments.format == "json":
                print(json.dumps(objects, indent=2))
            else:
                writer = csv.DictWriter(sys.stdout, fieldnames=BAR_COLUMNS)
                writer.writeheader()
                writer.writerows(objects)
        return 0
    except (ConfigError, RelayError, sqlite3.Error, OSError) as exc:
        logging.error("%s", exc)
        if store is not None and not store.read_only:
            try:
                store.append_log("error", "%s failed: %s" % (arguments.command, exc))
            except sqlite3.Error:
                logging.exception("could not write failure to logs table")
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
