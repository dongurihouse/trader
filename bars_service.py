#!/usr/bin/python3
"""Always-on Robinhood minute-bar collector with a small SQLite query CLI."""

import argparse
import csv
import io
import json
import logging
import math
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time as system_time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


class ConfigError(ValueError):
    pass


class RelayError(RuntimeError):
    pass


class RelayAuthRequired(RelayError):
    pass


@dataclass(frozen=True)
class Settings:
    tickers: Tuple[str, ...]
    interval: str
    bounds: str
    poll_seconds: int
    idle_seconds: int
    retry_seconds: int
    retry_max_seconds: int
    final_delay_minutes: int
    edge_empty_sessions: int
    recovery_days: int
    database: Path
    codex_bin: str
    model: str
    reasoning_effort: str
    timeout_seconds: int
    max_symbols_per_call: int


def _positive_int(value, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError("%s must be an integer >= %d" % (name, minimum))
    return value


def load_settings(path: Path) -> Settings:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError("config file does not exist: %s" % path) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError("invalid JSON in %s: %s" % (path, exc)) from exc

    tickers_raw = raw.get("tickers")
    if not isinstance(tickers_raw, list) or not tickers_raw:
        raise ConfigError("tickers must be a non-empty JSON list")
    tickers: List[str] = []
    for value in tickers_raw:
        if not isinstance(value, str):
            raise ConfigError("every ticker must be a string")
        ticker = value.strip().upper()
        if not SYMBOL_RE.fullmatch(ticker):
            raise ConfigError("invalid ticker: %r" % value)
        if ticker not in tickers:
            tickers.append(ticker)

    interval = raw.get("interval", "minute")
    if interval != "minute":
        raise ConfigError(
            "this minimal collector supports interval='minute' only; got %r"
            % interval
        )
    bounds = raw.get("bounds", "regular")
    if bounds not in ("regular", "extended"):
        raise ConfigError("bounds must be 'regular' or 'extended'")

    service = raw.get("service") or {}
    backfill = raw.get("backfill") or {}
    provider = raw.get("provider") or {}
    database_value = raw.get("database", "data/bars.sqlite3")
    database = Path(database_value).expanduser()
    if not database.is_absolute():
        database = ROOT / database

    return Settings(
        tickers=tuple(tickers),
        interval=interval,
        bounds=bounds,
        poll_seconds=_positive_int(
            service.get("poll_seconds", 300), "service.poll_seconds", 30
        ),
        idle_seconds=_positive_int(
            service.get("idle_seconds", 300), "service.idle_seconds", 30
        ),
        retry_seconds=_positive_int(
            service.get("retry_seconds", 30), "service.retry_seconds", 5
        ),
        retry_max_seconds=_positive_int(
            service.get("retry_max_seconds", 900),
            "service.retry_max_seconds",
            30,
        ),
        final_delay_minutes=_positive_int(
            service.get("final_delay_minutes", 5),
            "service.final_delay_minutes",
            1,
        ),
        edge_empty_sessions=_positive_int(
            backfill.get("empty_sessions_to_stop", 10),
            "backfill.empty_sessions_to_stop",
            2,
        ),
        recovery_days=_positive_int(
            backfill.get("recovery_days", 60),
            "backfill.recovery_days",
            1,
        ),
        database=database,
        codex_bin=str(provider.get("codex_bin", "/Users/xup/.local/bin/codex")),
        model=str(provider.get("model", "gpt-5.4-mini")),
        reasoning_effort=str(provider.get("reasoning_effort", "low")),
        timeout_seconds=_positive_int(
            provider.get("timeout_seconds", 300),
            "provider.timeout_seconds",
            30,
        ),
        max_symbols_per_call=_positive_int(
            provider.get("max_symbols_per_call", 3),
            "provider.max_symbols_per_call",
            1,
        ),
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RelayError("timestamp has no timezone: %s" % value)
    return parsed.astimezone(UTC)


def session_window(day: date, bounds: str) -> Tuple[str, str]:
    start_clock = time(9, 30) if bounds == "regular" else time(4, 0)
    start = datetime.combine(day, start_clock, tzinfo=EASTERN)
    end = datetime.combine(day, time(16, 0), tzinfo=EASTERN)
    return _iso_utc(start), _iso_utc(end)


def previous_weekday(day: date) -> date:
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def next_weekday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def weekdays(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            yield cursor
        cursor += timedelta(days=1)


class RobinhoodRelay:
    SERVER = "robinhood-trading"
    URL = "https://agent.robinhood.com/mcp/trading"
    TOOL = "get_equity_historicals"
    AUTH_MARKERS = (
        "auth",
        "unavailable",
        "not available",
        "no such tool",
        "not connected",
    )

    def __init__(self, settings: Settings):
        self.settings = settings

    def _command(
        self, symbols: Sequence[str], start_iso: str, end_iso: str
    ) -> List[str]:
        arguments = json.dumps(
            {
                "symbols": list(symbols),
                "start_time": start_iso,
                "end_time": end_iso,
                "interval": self.settings.interval,
                "bounds": self.settings.bounds,
            },
            separators=(",", ":"),
        )
        prompt = (
            "Use tool search to locate the MCP tool named %s on server %s, "
            "then call it exactly once with exactly these arguments:\n%s\n"
            "Do not change any argument. Make no other tool calls. After the "
            "tool returns, reply with exactly DONE and no other text."
            % (self.TOOL, self.SERVER, arguments)
        )
        return [
            self.settings.codex_bin,
            "exec",
            "--ignore-user-config",
            "--json",
            "-s",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--disable",
            "shell_tool",
            "-m",
            self.settings.model,
            "-c",
            'model_reasoning_effort="%s"' % self.settings.reasoning_effort,
            "-c",
            "tools.web_search=false",
            "-c",
            'mcp_servers.%s.url="%s"' % (self.SERVER, self.URL),
            "-c",
            "mcp_servers.%s.enabled_tools=%s"
            % (self.SERVER, json.dumps([self.TOOL])),
            "-c",
            'mcp_servers.%s.default_tools_approval_mode="approve"'
            % self.SERVER,
            prompt,
        ]

    @staticmethod
    def _events(stdout: str) -> List[dict]:
        events: List[dict] = []
        for line in (stdout or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    @staticmethod
    def _calls(events: Sequence[dict], event_type: str) -> List[dict]:
        calls: List[dict] = []
        for event in events:
            item = event.get("item")
            if (
                event.get("type") == event_type
                and isinstance(item, dict)
                and item.get("type") == "mcp_tool_call"
            ):
                calls.append(item)
        return calls

    @staticmethod
    def _content_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
            )
        return ""

    def _failure(self, message: str, detail: str):
        lowered = detail.lower()
        if any(marker in lowered for marker in self.AUTH_MARKERS):
            raise RelayAuthRequired(
                "%s. Run: codex mcp login robinhood-trading" % message
            )
        raise RelayError(message)

    def _extract(self, events: Sequence[dict], process) -> dict:
        completed = self._calls(events, "item.completed")
        attempted = completed + self._calls(events, "item.started")
        stray = {
            "%s/%s" % (call.get("server"), call.get("tool"))
            for call in attempted
            if (call.get("server"), call.get("tool"))
            != (self.SERVER, self.TOOL)
        }
        if stray:
            raise RelayError(
                "read-only tool allowlist failed; reached: %s"
                % ", ".join(sorted(stray))
            )
        if len(completed) != 1:
            details = []
            for event in events:
                if event.get("type") in ("error", "turn.failed"):
                    details.append(json.dumps(event))
                item = event.get("item") or {}
                if (
                    event.get("type") == "item.completed"
                    and item.get("type") == "agent_message"
                ):
                    details.append(str(item.get("text", "")))
            details.append(process.stderr or "")
            detail = "\n".join(part for part in details if part)
            self._failure(
                "expected one %s/%s call; got %d (exit %s): %s"
                % (
                    self.SERVER,
                    self.TOOL,
                    len(completed),
                    process.returncode,
                    detail[:500],
                ),
                detail,
            )

        call = completed[0]
        error = call.get("error")
        if call.get("status") != "completed" or error:
            detail = json.dumps(error) if error else str(call.get("status"))
            self._failure("Robinhood tool call failed: %s" % detail, detail)

        result = call.get("result")
        if not isinstance(result, dict):
            raise RelayError("Robinhood tool call returned no result object")
        text = self._content_text(result.get("content"))
        if text.strip():
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RelayError(
                    "Robinhood result is invalid JSON at character %d of %d"
                    % (exc.pos, len(text))
                ) from exc
            if isinstance(payload, dict):
                return payload
            raise RelayError("Robinhood result is not a JSON object")
        structured = result.get("structured_content")
        if isinstance(structured, dict):
            return structured
        raise RelayError("Robinhood result contains no JSON payload")

    def _run_once(
        self, symbols: Sequence[str], start_iso: str, end_iso: str
    ) -> dict:
        command = self._command(symbols, start_iso, end_iso)
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.settings.timeout_seconds,
                stdin=subprocess.DEVNULL,
                cwd=str(ROOT),
            )
        except FileNotFoundError as exc:
            raise RelayError("Codex CLI not found: %s" % self.settings.codex_bin) from exc
        except subprocess.TimeoutExpired as exc:
            raise RelayError(
                "Robinhood relay timed out after %d seconds"
                % self.settings.timeout_seconds
            ) from exc
        return self._extract(self._events(process.stdout), process)

    def fetch(
        self,
        symbols: Sequence[str],
        start_iso: str,
        end_iso: str,
        allow_missing: bool = False,
    ) -> dict:
        results: List[dict] = []
        chunk_size = self.settings.max_symbols_per_call
        for offset in range(0, len(symbols), chunk_size):
            chunk = symbols[offset : offset + chunk_size]
            payload = self._run_once(chunk, start_iso, end_iso)
            chunk_results = self._validate(
                payload, chunk, start_iso, end_iso, allow_missing
            )
            results.extend(chunk_results)
        return {"data": {"results": results}}

    def _validate(
        self,
        payload: dict,
        symbols: Sequence[str],
        start_iso: str,
        end_iso: str,
        allow_missing: bool,
    ) -> List[dict]:
        data = payload.get("data") if isinstance(payload, dict) else None
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise RelayError("Robinhood payload is missing data.results")

        wanted = set(symbols)
        got = {
            result.get("symbol")
            for result in results
            if isinstance(result, dict)
        }
        unexpected = got - wanted
        if unexpected:
            raise RelayError(
                "Robinhood returned unexpected tickers: %s"
                % ", ".join(sorted(unexpected))
            )
        missing = wanted - got
        if missing and not allow_missing:
            raise RelayError(
                "Robinhood omitted requested tickers: %s"
                % ", ".join(sorted(missing))
            )

        start = _parse_iso(start_iso)
        end = _parse_iso(end_iso)
        for result in results:
            if not isinstance(result, dict):
                raise RelayError("Robinhood result block is not an object")
            bars = result.get("bars") or []
            if not isinstance(bars, list):
                raise RelayError("Robinhood result bars is not a list")
            for bar in bars:
                if not isinstance(bar, dict) or "begins_at" not in bar:
                    raise RelayError("Robinhood returned a malformed bar")
                timestamp = _parse_iso(str(bar["begins_at"]))
                if timestamp < start or timestamp > end:
                    raise RelayError(
                        "%s bar %s is outside [%s, %s]"
                        % (result.get("symbol"), bar["begins_at"], start_iso, end_iso)
                    )
        return results


SCHEMA = """
CREATE TABLE IF NOT EXISTS bar (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    begins_at TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    session TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, begins_at)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS bar_time_idx
ON bar(interval, begins_at);

CREATE TABLE IF NOT EXISTS fetch (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    session_date TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    real_bars INTEGER NOT NULL,
    interpolated_bars INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, session_date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
) WITHOUT ROWID;
"""


class BarStore:
    PRICE_FIELDS = (
        ("open", "open_price"),
        ("high", "high_price"),
        ("low", "low_price"),
        ("close", "close_price"),
    )

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _number(bar: dict, field: str) -> float:
        try:
            value = float(bar[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise RelayError("invalid bar field %s: %r" % (field, bar.get(field))) from exc
        if not math.isfinite(value):
            raise RelayError("non-finite bar field %s" % field)
        return value

    def store_payload(
        self,
        payload: dict,
        requested: Sequence[str],
        interval: str,
        session_day: date,
    ) -> Dict[str, dict]:
        fetched_at = _iso_utc(datetime.now(UTC))
        stats = {
            symbol: {"real_bars": 0, "interpolated_bars": 0}
            for symbol in requested
        }
        results = payload["data"]["results"]
        rows: List[tuple] = []
        for result in results:
            symbol = result["symbol"]
            for bar in result.get("bars") or []:
                if bar.get("interpolated"):
                    stats[symbol]["interpolated_bars"] += 1
                    continue
                timestamp = _iso_utc(_parse_iso(str(bar["begins_at"])))
                prices = [self._number(bar, field) for _, field in self.PRICE_FIELDS]
                volume = self._number(bar, "volume")
                if volume < 0:
                    raise RelayError("negative volume for %s at %s" % (symbol, timestamp))
                rows.append(
                    (
                        symbol,
                        interval,
                        timestamp,
                        prices[0],
                        prices[1],
                        prices[2],
                        prices[3],
                        volume,
                        bar.get("session"),
                        fetched_at,
                    )
                )
                stats[symbol]["real_bars"] += 1

        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO bar (
                    symbol, interval, begins_at, open, high, low, close,
                    volume, session, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, begins_at) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    session=excluded.session,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
            connection.executemany(
                """
                INSERT INTO fetch (
                    symbol, interval, session_date, attempted_at,
                    real_bars, interpolated_bars
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, session_date) DO UPDATE SET
                    attempted_at=excluded.attempted_at,
                    real_bars=excluded.real_bars,
                    interpolated_bars=excluded.interpolated_bars
                """,
                [
                    (
                        symbol,
                        interval,
                        session_day.isoformat(),
                        fetched_at,
                        values["real_bars"],
                        values["interpolated_bars"],
                    )
                    for symbol, values in stats.items()
                ],
            )
        return stats

    def get_state(self, key: str):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM state WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def set_state(self, key: str, value) -> None:
        now = _iso_utc(datetime.now(UTC))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, separators=(",", ":")), now),
            )

    def latest_fetch_day(self, symbol: str, interval: str) -> Optional[date]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(session_date) AS day FROM fetch "
                "WHERE symbol=? AND interval=?",
                (symbol, interval),
            ).fetchone()
        return date.fromisoformat(row["day"]) if row and row["day"] else None

    def has_fetch(self, symbol: str, interval: str, day: date) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM fetch WHERE symbol=? AND interval=? "
                "AND session_date=?",
                (symbol, interval, day.isoformat()),
            ).fetchone()
        return row is not None

    def summary(self, tickers: Sequence[str], interval: str) -> List[sqlite3.Row]:
        placeholders = ",".join("?" for _ in tickers)
        query = """
            SELECT symbol, COUNT(*) AS bars, MIN(begins_at) AS earliest,
                   MAX(begins_at) AS latest
            FROM bar
            WHERE interval=? AND symbol IN (%s)
            GROUP BY symbol
            ORDER BY symbol
        """ % placeholders
        with self.connect() as connection:
            rows = connection.execute(query, (interval,) + tuple(tickers)).fetchall()
        return rows

    def query(
        self,
        symbol: str,
        interval: str,
        start: Optional[str],
        end: Optional[str],
        limit: Optional[int],
    ) -> List[sqlite3.Row]:
        clauses = ["symbol=?", "interval=?"]
        arguments: List[object] = [symbol, interval]
        if start:
            clauses.append("begins_at>=?")
            arguments.append(start + "T00:00:00Z" if len(start) == 10 else start)
        if end:
            clauses.append("begins_at<=?")
            arguments.append(end + "T23:59:59Z" if len(end) == 10 else end)
        query = (
            "SELECT symbol, interval, begins_at, open, high, low, close, "
            "volume, session FROM bar WHERE %s ORDER BY begins_at"
            % " AND ".join(clauses)
        )
        if limit:
            query += " LIMIT ?"
            arguments.append(limit)
        with self.connect() as connection:
            return connection.execute(query, tuple(arguments)).fetchall()


class Collector:
    def __init__(self, settings: Settings, store: BarStore):
        self.settings = settings
        self.store = store
        self.relay = RobinhoodRelay(settings)

    def fetch_day(
        self,
        day: date,
        symbols: Optional[Sequence[str]] = None,
        allow_missing: bool = False,
    ) -> Dict[str, dict]:
        selected = tuple(symbols or self.settings.tickers)
        start_iso, end_iso = session_window(day, self.settings.bounds)
        logging.info(
            "fetching %s for %s (%s)",
            ",".join(selected),
            day.isoformat(),
            self.settings.interval,
        )
        payload = self.relay.fetch(
            selected, start_iso, end_iso, allow_missing=allow_missing
        )
        stats = self.store.store_payload(
            payload, selected, self.settings.interval, day
        )
        logging.info(
            "stored %s",
            ", ".join(
                "%s=%d real/%d interpolated"
                % (
                    symbol,
                    values["real_bars"],
                    values["interpolated_bars"],
                )
                for symbol, values in sorted(stats.items())
            ),
        )
        return stats

    def _backfill_key(self, symbol: str) -> str:
        return "backfill:v1:%s:%s:%s" % (
            self.settings.interval,
            self.settings.bounds,
            symbol,
        )

    def _first_backfill_day(self, now: datetime) -> date:
        local = now.astimezone(EASTERN)
        day = local.date()
        start_clock = time(9, 30) if self.settings.bounds == "regular" else time(4, 0)
        if day.weekday() >= 5 or local.time() < start_clock:
            return previous_weekday(day)
        return day

    def backfill(self, now: Optional[datetime] = None) -> None:
        current = now or datetime.now(UTC)
        first_day = self._first_backfill_day(current)
        progress: Dict[str, dict] = {}
        for symbol in self.settings.tickers:
            key = self._backfill_key(symbol)
            value = self.store.get_state(key)
            if value and value.get("complete"):
                continue
            if not value:
                value = {
                    "cursor": first_day.isoformat(),
                    "empty_streak": 0,
                    "complete": False,
                }
            progress[symbol] = value

        if not progress:
            logging.info("startup backfill already complete")
            return

        logging.info(
            "startup backfill begins; stopping after %d empty weekdays per ticker",
            self.settings.edge_empty_sessions,
        )
        while progress:
            groups: Dict[date, List[str]] = {}
            for symbol, value in progress.items():
                cursor = date.fromisoformat(value["cursor"])
                groups.setdefault(cursor, []).append(symbol)

            for day in sorted(groups, reverse=True):
                symbols = groups[day]
                stats = self.fetch_day(day, symbols)
                for symbol in symbols:
                    value = progress[symbol]
                    real_bars = stats[symbol]["real_bars"]
                    value["empty_streak"] = (
                        value["empty_streak"] + 1 if real_bars == 0 else 0
                    )
                    if value["empty_streak"] >= self.settings.edge_empty_sessions:
                        value["complete"] = True
                        value["edge_checked_through"] = day.isoformat()
                        logging.info(
                            "%s reached the provider history edge at %s",
                            symbol,
                            day.isoformat(),
                        )
                    else:
                        value["cursor"] = previous_weekday(day).isoformat()
                    self.store.set_state(self._backfill_key(symbol), value)
                    if value["complete"]:
                        del progress[symbol]

    def catch_up(self, now: Optional[datetime] = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(EASTERN)
        today = current.date()
        close_with_delay = datetime.combine(
            today, time(16, 0), tzinfo=EASTERN
        ) + timedelta(minutes=self.settings.final_delay_minutes)
        last_closed = today if current >= close_with_delay else previous_weekday(today)
        earliest_allowed = last_closed - timedelta(days=self.settings.recovery_days)

        pending_by_day: Dict[date, List[str]] = {}
        for symbol in self.settings.tickers:
            latest = self.store.latest_fetch_day(symbol, self.settings.interval)
            start = next_weekday(latest) if latest else earliest_allowed
            if start < earliest_allowed:
                start = earliest_allowed
            for day in weekdays(start, last_closed):
                if not self.store.has_fetch(symbol, self.settings.interval, day):
                    pending_by_day.setdefault(day, []).append(symbol)

        for day in sorted(pending_by_day):
            self.fetch_day(day, pending_by_day[day])

    def status_text(self) -> str:
        rows = self.store.summary(self.settings.tickers, self.settings.interval)
        by_symbol = {row["symbol"]: row for row in rows}
        lines = ["symbol  bars      earliest              latest"]
        for symbol in self.settings.tickers:
            row = by_symbol.get(symbol)
            if row:
                lines.append(
                    "%-7s %-9d %-21s %s"
                    % (symbol, row["bars"], row["earliest"], row["latest"])
                )
            else:
                lines.append("%-7s %-9d %-21s %s" % (symbol, 0, "-", "-"))
        lines.append("database %s" % self.store.path)
        return "\n".join(lines)


class Service:
    def __init__(self, collector: Collector):
        self.collector = collector
        self.settings = collector.settings
        self.stop_event = threading.Event()
        self.bootstrapped = False

    def stop(self, signum=None, frame=None) -> None:
        logging.info("stopping service")
        self.stop_event.set()

    def _wait(self, seconds: int) -> None:
        self.stop_event.wait(seconds)

    def _cycle(self) -> int:
        now = datetime.now(EASTERN)
        day = now.date()
        if day.weekday() >= 5:
            return self.settings.idle_seconds

        start_clock = time(9, 30) if self.settings.bounds == "regular" else time(4, 0)
        start = datetime.combine(day, start_clock, tzinfo=EASTERN)
        close = datetime.combine(day, time(16, 0), tzinfo=EASTERN)
        final_at = close + timedelta(minutes=self.settings.final_delay_minutes)
        final_key = "final:v1:%s:%s:%s" % (
            self.settings.interval,
            self.settings.bounds,
            day.isoformat(),
        )

        if start <= now < final_at:
            self.collector.fetch_day(day, allow_missing=True)
            return self.settings.poll_seconds
        if now >= final_at and not self.collector.store.get_state(final_key):
            self.collector.fetch_day(day)
            self.collector.store.set_state(final_key, True)
        return self.settings.idle_seconds

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        logging.info("service started with tickers=%s", ",".join(self.settings.tickers))
        delay = self.settings.retry_seconds
        while not self.stop_event.is_set():
            try:
                if not self.bootstrapped:
                    self.collector.backfill()
                    self.collector.catch_up()
                    self.bootstrapped = True
                    logging.info("startup backfill and catch-up complete")
                wait_seconds = self._cycle()
                delay = self.settings.retry_seconds
                self._wait(wait_seconds)
            except Exception:
                logging.exception("collector cycle failed; retrying in %d seconds", delay)
                self._wait(delay)
                delay = min(delay * 2, self.settings.retry_max_seconds)
        logging.info("service stopped")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = system_time.gmtime


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serve", help="run the always-on collector")
    subparsers.add_parser("backfill", help="resume startup backfill and catch-up")
    once = subparsers.add_parser("once", help="fetch one day now")
    once.add_argument("--date", dest="day", type=date.fromisoformat)
    subparsers.add_parser("status", help="show local bar coverage")

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
    try:
        settings = load_settings(arguments.config.resolve())
        store = BarStore(settings.database)
        collector = Collector(settings, store)

        if arguments.command == "serve":
            Service(collector).run()
        elif arguments.command == "backfill":
            collector.backfill()
            collector.catch_up()
            print(collector.status_text())
        elif arguments.command == "once":
            day = arguments.day or datetime.now(EASTERN).date()
            collector.fetch_day(day, allow_missing=(day == datetime.now(EASTERN).date()))
            print(collector.status_text())
        elif arguments.command == "status":
            print(collector.status_text())
        elif arguments.command == "query":
            symbol = arguments.symbol.strip().upper()
            if symbol not in settings.tickers:
                raise ConfigError("ticker is not in config: %s" % symbol)
            rows = store.query(
                symbol,
                settings.interval,
                arguments.start,
                arguments.end,
                arguments.limit,
            )
            objects = [dict(row) for row in rows]
            if arguments.format == "json":
                print(json.dumps(objects, indent=2))
            else:
                output = io.StringIO()
                fields = (
                    list(objects[0].keys())
                    if objects
                    else [
                        "symbol",
                        "interval",
                        "begins_at",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "session",
                    ]
                )
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                writer.writerows(objects)
                sys.stdout.write(output.getvalue())
        return 0
    except (ConfigError, RelayError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
