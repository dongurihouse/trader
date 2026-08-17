#!/usr/bin/python3
"""Always-on Robinhood minute-bar collector with a small SQLite query CLI."""

import argparse
import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import threading
import time as system_time
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

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
    provider_url: str
    oauth_store: Path
    oauth_callback_port: int
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
    oauth_store_value = provider.get("oauth_store", "data/robinhood_oauth.json")
    oauth_store = Path(oauth_store_value).expanduser()
    if not oauth_store.is_absolute():
        oauth_store = ROOT / oauth_store

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
        provider_url=str(
            provider.get("url", "https://agent.robinhood.com/mcp/trading")
        ),
        oauth_store=oauth_store,
        oauth_callback_port=_positive_int(
            provider.get("oauth_callback_port", 8765),
            "provider.oauth_callback_port",
            1024,
        ),
        timeout_seconds=_positive_int(
            provider.get("timeout_seconds", 300),
            "provider.timeout_seconds",
            30,
        ),
        max_symbols_per_call=_positive_int(
            provider.get("max_symbols_per_call", 3),
            "provider.max_symbols_per_call",
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


class FileTokenStorage(TokenStorage):
    """Persist the MCP OAuth client registration and rotating tokens."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise ConfigError("invalid OAuth store %s: %s" % (self.path, exc)) from exc
        if not isinstance(value, dict):
            raise ConfigError("OAuth store is not a JSON object: %s" % self.path)
        return value

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(
            str(temporary),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(str(temporary), str(self.path))
        os.chmod(self.path, 0o600)

    async def get_tokens(self) -> Optional[OAuthToken]:
        value = self._read().get("tokens")
        return OAuthToken.model_validate(value) if isinstance(value, dict) else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        value = self._read()
        value["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(value)

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        value = self._read().get("client_info")
        return (
            OAuthClientInformationFull.model_validate(value)
            if isinstance(value, dict)
            else None
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        value = self._read()
        value["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(value)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.callback_path = self.path
        body = b"Robinhood authorization complete. You can close this window.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        return


class BrowserOAuthCallbacks:
    def __init__(self, port: int):
        self.server = HTTPServer(("127.0.0.1", port), _OAuthCallbackHandler)
        self.server.timeout = 300
        self.server.callback_path = None

    async def redirect(self, authorization_url: str) -> None:
        print("Opening Robinhood authorization in your browser.")
        print(authorization_url)
        webbrowser.open(authorization_url)

    async def callback(self) -> AuthorizationCodeResult:
        try:
            await asyncio.to_thread(self.server.handle_request)
        finally:
            self.server.server_close()
        callback_path = self.server.callback_path
        if not callback_path:
            raise RelayAuthRequired("Robinhood authorization timed out; run: make auth")
        parameters = parse_qs(urlparse(callback_path).query)
        if "error" in parameters:
            raise RelayAuthRequired(
                "Robinhood authorization failed: %s"
                % parameters.get("error_description", parameters["error"])[0]
            )
        if "code" not in parameters:
            raise RelayAuthRequired(
                "Robinhood callback did not include an authorization code"
            )
        return AuthorizationCodeResult(
            code=parameters["code"][0],
            state=parameters.get("state", [None])[0],
            iss=parameters.get("iss", [None])[0],
        )


class NonInteractiveOAuthCallbacks:
    async def redirect(self, authorization_url: str) -> None:
        raise RelayAuthRequired("Robinhood OAuth approval required; run: make auth")

    async def callback(self) -> AuthorizationCodeResult:
        raise RelayAuthRequired("Robinhood OAuth approval required; run: make auth")


def _contains_exception(error: BaseException, wanted_type) -> bool:
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, wanted_type):
            return True
        pending.extend(getattr(current, "exceptions", ()) or ())
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


class RobinhoodClient:
    TOOL = "get_equity_historicals"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = FileTokenStorage(settings.oauth_store)

    def _oauth(self, interactive: bool) -> OAuthClientProvider:
        callbacks = (
            BrowserOAuthCallbacks(self.settings.oauth_callback_port)
            if interactive
            else NonInteractiveOAuthCallbacks()
        )
        callback_url = (
            "http://127.0.0.1:%d/callback" % self.settings.oauth_callback_port
        )
        return OAuthClientProvider(
            server_url=self.settings.provider_url,
            client_metadata=OAuthClientMetadata(
                client_name="bars",
                redirect_uris=[AnyUrl(callback_url)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="internal",
                token_endpoint_auth_method="none",
                application_type="native",
            ),
            storage=self.storage,
            redirect_handler=callbacks.redirect,
            callback_handler=callbacks.callback,
        )

    async def _session_call(self, name: str, arguments: dict, interactive: bool):
        oauth = self._oauth(interactive)
        timeout = httpx2.Timeout(float(self.settings.timeout_seconds))
        async with httpx2.AsyncClient(
            auth=oauth,
            follow_redirects=True,
            timeout=timeout,
        ) as http_client:
            async with streamable_http_client(
                self.settings.provider_url,
                http_client=http_client,
                terminate_on_close=False,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if name == "tools/list":
                        return await session.list_tools()
                    return await session.call_tool(
                        name,
                        arguments,
                        read_timeout_seconds=float(self.settings.timeout_seconds),
                    )

    @staticmethod
    def _run(awaitable, action: str):
        try:
            return asyncio.run(awaitable)
        except RelayAuthRequired:
            raise
        except Exception as exc:
            if _contains_exception(exc, RelayAuthRequired):
                raise RelayAuthRequired(
                    "Robinhood OAuth approval required; run: make auth"
                ) from exc
            raise RelayError("Robinhood %s failed: %s" % (action, exc)) from exc

    def authorize(self) -> List[str]:
        result = self._run(
            self._session_call("tools/list", {}, interactive=True),
            "authorization",
        )
        tools = [tool.name for tool in result.tools]
        if self.TOOL not in tools:
            raise RelayError("Robinhood connection does not expose %s" % self.TOOL)
        return tools

    @staticmethod
    def _payload(result) -> dict:
        if getattr(result, "is_error", False):
            detail = "\n".join(
                str(getattr(block, "text", "")) for block in result.content
            )
            raise RelayError("Robinhood tool returned an error: %s" % detail)
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        text = "\n".join(
            str(getattr(block, "text", ""))
            for block in getattr(result, "content", [])
            if getattr(block, "text", None) is not None
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RelayError(
                "Robinhood result is invalid JSON at character %d of %d"
                % (exc.pos, len(text))
            ) from exc
        if not isinstance(payload, dict):
            raise RelayError("Robinhood result is not a JSON object")
        return payload

    def fetch(
        self,
        symbols: Sequence[str],
        start_iso: str,
        end_iso: str,
        allow_missing: bool = False,
    ) -> dict:
        combined = {"data": {"results": []}}
        size = self.settings.max_symbols_per_call
        for offset in range(0, len(symbols), size):
            batch = symbols[offset : offset + size]
            result = self._run(
                self._session_call(
                    self.TOOL,
                    {
                        "symbols": list(batch),
                        "start_time": start_iso,
                        "end_time": end_iso,
                        "interval": self.settings.interval,
                        "bounds": self.settings.bounds,
                    },
                    interactive=False,
                ),
                "historicals request",
            )
            payload = self._payload(result)
            self._validate(payload, batch, start_iso, end_iso, allow_missing)
            combined["data"]["results"].extend(payload["data"]["results"])
        return combined

    @staticmethod
    def _validate(
        payload: dict,
        symbols: Sequence[str],
        start_iso: str,
        end_iso: str,
        allow_missing: bool,
    ) -> None:
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
            raise RelayError(
                "invalid bar field %s: %r" % (field, bar.get(field))
            ) from exc
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
                    raise RelayError(
                        "negative volume for %s at %s" % (symbol, timestamp)
                    )
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
        self.provider = RobinhoodClient(settings)

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
        payload = self.provider.fetch(
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
                logging.exception(
                    "collector cycle failed; retrying in %d seconds", delay
                )
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

    subparsers.add_parser("auth", help="authorize the direct Robinhood connection")
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

        if arguments.command == "auth":
            tools = collector.provider.authorize()
            print("Robinhood authorization complete.")
            print("Verified tool: %s" % collector.provider.TOOL)
            print("Available tools: %d" % len(tools))
        elif arguments.command == "serve":
            Service(collector).run()
        elif arguments.command == "backfill":
            collector.backfill()
            collector.catch_up()
            print(collector.status_text())
        elif arguments.command == "once":
            day = arguments.day or datetime.now(EASTERN).date()
            collector.fetch_day(
                day,
                allow_missing=(day == datetime.now(EASTERN).date()),
            )
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
