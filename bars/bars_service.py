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
from typing import Dict, List, Optional, Sequence, Tuple
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
REPO_ROOT = ROOT.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.json"
SCHEMA_PATH = REPO_ROOT / "schema.sql"
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
    bounds: str
    live_start: time
    regular_close: time
    early_close: time
    early_close_days: frozenset
    after_close_minutes: int
    poll_seconds: int
    sweep_days: int
    idle_seconds: int
    retry_seconds: int
    retry_max_seconds: int
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


def _clock(value, name: str) -> time:
    if not isinstance(value, str):
        raise ConfigError("%s must be HH:MM" % name)
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("%s must be HH:MM" % name) from exc
    if parsed.second or parsed.microsecond:
        raise ConfigError("%s must be precise to the minute" % name)
    return parsed


def load_settings(path: Path) -> Settings:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError("config file does not exist: %s" % path) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError("invalid JSON in %s: %s" % (path, exc)) from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")

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

    early_closes_raw = raw.get("early_closes") or []
    if not isinstance(early_closes_raw, list):
        raise ConfigError("early_closes must be a JSON list")
    try:
        early_close_days = frozenset(
            date.fromisoformat(value) for value in early_closes_raw
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError("early_closes must contain YYYY-MM-DD strings") from exc

    database_value = raw.get("database", "data/trader.sqlite3")
    database = Path(database_value).expanduser()
    if not database.is_absolute():
        database = path.parent / database
    oauth_store_value = provider.get(
        "oauth_store", "bars/data/robinhood_oauth.json"
    )
    oauth_store = Path(oauth_store_value).expanduser()
    if not oauth_store.is_absolute():
        oauth_store = path.parent / oauth_store

    return Settings(
        tickers=tuple(tickers),
        bounds=bounds,
        live_start=_clock(live.get("start", "04:00"), "live_polling.start"),
        regular_close=_clock(
            live.get("regular_close", "16:00"),
            "live_polling.regular_close",
        ),
        early_close=_clock(
            live.get("early_close", "13:00"),
            "live_polling.early_close",
        ),
        early_close_days=early_close_days,
        after_close_minutes=_positive_int(
            live.get("after_close_minutes", 5),
            "live_polling.after_close_minutes",
            0,
        ),
        poll_seconds=_positive_int(
            live.get("poll_seconds", 60), "live_polling.poll_seconds", 30
        ),
        sweep_days=_positive_int(bars.get("sweep_days", 30), "bars.sweep_days"),
        idle_seconds=_positive_int(
            bars.get("idle_seconds", 300), "bars.idle_seconds", 30
        ),
        retry_seconds=_positive_int(
            bars.get("retry_seconds", 30), "bars.retry_seconds", 5
        ),
        retry_max_seconds=_positive_int(
            bars.get("retry_max_seconds", 900),
            "bars.retry_max_seconds",
            30,
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


def _epoch(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp())


def _query_epoch(value: str, end: bool = False) -> int:
    if len(value) == 10:
        day = date.fromisoformat(value)
        clock = time(23, 59, 59) if end else time(0, 0)
        return _epoch(datetime.combine(day, clock, tzinfo=UTC))
    return _epoch(_parse_iso(value))


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
                        "interval": "minute",
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
        try:
            schema = SCHEMA_PATH.read_text()
        except FileNotFoundError as exc:
            raise ConfigError("schema file does not exist: %s" % SCHEMA_PATH) from exc
        with self.connect() as connection:
            connection.executescript(schema)

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

    @staticmethod
    def _ordered(open_price: float, high: float, low: float, close: float) -> bool:
        return low <= open_price <= high and low <= close <= high

    def _upsert(self, rows: Sequence[tuple]) -> None:
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO bars (ticker, ts, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, ts) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume
                """,
                rows,
            )

    def store_payload(self, payload: dict) -> Dict[str, int]:
        stats = {
            "received": 0,
            "written": 0,
            "interpolated": 0,
            "unordered": 0,
            "invalid": 0,
        }
        results = payload["data"]["results"]
        rows: List[tuple] = []
        for result in results:
            ticker = result["symbol"]
            for bar in result.get("bars") or []:
                stats["received"] += 1
                if bar.get("interpolated"):
                    stats["interpolated"] += 1
                    continue
                try:
                    timestamp = _epoch(_parse_iso(str(bar["begins_at"])))
                    prices = [
                        self._number(bar, field) for _, field in self.PRICE_FIELDS
                    ]
                    volume_value = self._number(bar, "volume")
                except (RelayError, KeyError):
                    stats["invalid"] += 1
                    continue
                if volume_value < 0 or not volume_value.is_integer():
                    stats["invalid"] += 1
                    continue
                if not self._ordered(prices[0], prices[1], prices[2], prices[3]):
                    stats["unordered"] += 1
                    continue
                rows.append(
                    (
                        ticker,
                        timestamp,
                        prices[0],
                        prices[1],
                        prices[2],
                        prices[3],
                        int(volume_value),
                    )
                )
        self._upsert(rows)
        stats["written"] = len(rows)
        return stats

    def latest_by_ticker(self, tickers: Sequence[str]) -> Dict[str, Optional[int]]:
        latest = {ticker: None for ticker in tickers}
        placeholders = ",".join("?" for _ in tickers)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT ticker, MAX(ts) AS ts FROM bars "
                "WHERE ticker IN (%s) GROUP BY ticker" % placeholders,
                tuple(tickers),
            ).fetchall()
        for row in rows:
            latest[row["ticker"]] = row["ts"]
        return latest

    def append_log(self, level: str, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO logs(ts, service, level, message) VALUES (?, ?, ?, ?)",
                (_epoch(datetime.now(UTC)), "bars", level, message),
            )

    def sweep_complete(self, day: date) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM logs WHERE service='bars' AND level='info' "
                "AND message LIKE ? LIMIT 1",
                ("sweep complete day=%s %%" % day.isoformat(),),
            ).fetchone()
        return row is not None

    def summary(self, tickers: Sequence[str]) -> List[sqlite3.Row]:
        placeholders = ",".join("?" for _ in tickers)
        query = """
            SELECT ticker, COUNT(*) AS bars, MIN(ts) AS earliest, MAX(ts) AS latest
            FROM bars
            WHERE ticker IN (%s)
            GROUP BY ticker
            ORDER BY ticker
        """ % placeholders
        with self.connect() as connection:
            return connection.execute(query, tuple(tickers)).fetchall()

    def migrate_legacy(self, source: Path) -> Dict[str, int]:
        if source.resolve() == self.path.resolve():
            raise ConfigError("legacy and destination databases must differ")
        if not source.is_file():
            raise ConfigError("legacy database does not exist: %s" % source)
        stats = {"read": 0, "written": 0, "rejected": 0}
        rows: List[tuple] = []
        legacy = sqlite3.connect(str(source))
        legacy.row_factory = sqlite3.Row
        try:
            cursor = legacy.execute(
                "SELECT symbol, begins_at, open, high, low, close, volume FROM bar"
            )
            for row in cursor:
                stats["read"] += 1
                values = tuple(float(row[name]) for name in ("open", "high", "low", "close"))
                volume = float(row["volume"])
                if (
                    not all(math.isfinite(value) for value in values)
                    or not math.isfinite(volume)
                    or volume < 0
                    or not volume.is_integer()
                    or not self._ordered(*values)
                ):
                    stats["rejected"] += 1
                    continue
                rows.append(
                    (
                        row["symbol"],
                        _epoch(_parse_iso(row["begins_at"])),
                        *values,
                        int(volume),
                    )
                )
        except sqlite3.Error as exc:
            raise ConfigError("cannot read legacy database %s: %s" % (source, exc)) from exc
        finally:
            legacy.close()
        self._upsert(rows)
        stats["written"] = len(rows)
        self.append_log(
            "info",
            "migration complete source=%s read=%d written=%d rejected=%d"
            % (source, stats["read"], stats["written"], stats["rejected"]),
        )
        return stats

    def query(
        self,
        ticker: str,
        start: Optional[str],
        end: Optional[str],
        limit: Optional[int],
    ) -> List[sqlite3.Row]:
        clauses = ["ticker=?"]
        arguments: List[object] = [ticker]
        if start:
            clauses.append("ts>=?")
            arguments.append(_query_epoch(start))
        if end:
            clauses.append("ts<=?")
            arguments.append(_query_epoch(end, end=True))
        query = (
            "SELECT ticker, ts, open, high, low, close, volume "
            "FROM bars WHERE %s ORDER BY ts" % " AND ".join(clauses)
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

    @staticmethod
    def _empty_stats() -> Dict[str, int]:
        return {
            "received": 0,
            "written": 0,
            "interpolated": 0,
            "unordered": 0,
            "invalid": 0,
        }

    @staticmethod
    def _add_stats(total: Dict[str, int], part: Dict[str, int]) -> None:
        for key in total:
            total[key] += part[key]

    def fetch_range(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        allow_missing: bool = False,
    ) -> Dict[str, int]:
        start_iso = _iso_utc(start)
        end_iso = _iso_utc(end)
        logging.info(
            "fetching %s from %s through %s",
            ",".join(symbols),
            start_iso,
            end_iso,
        )
        payload = self.provider.fetch(
            symbols, start_iso, end_iso, allow_missing=allow_missing
        )
        return self.store.store_payload(payload)

    def poll(self, now: Optional[datetime] = None) -> Dict[str, int]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        floor = current - timedelta(days=self.settings.sweep_days)
        groups: Dict[int, List[str]] = {}
        for ticker, latest in self.store.latest_by_ticker(
            self.settings.tickers
        ).items():
            start_ts = latest if latest is not None else _epoch(floor)
            if start_ts < _epoch(floor) or start_ts > _epoch(current):
                start_ts = _epoch(floor)
            groups.setdefault(start_ts, []).append(ticker)

        total = self._empty_stats()
        for start_ts, tickers in sorted(groups.items()):
            start = datetime.fromtimestamp(start_ts, UTC)
            self._add_stats(
                total,
                self.fetch_range(tickers, start, current, allow_missing=True),
            )
        self._record("poll complete", total)
        return total

    def sweep(self, now: Optional[datetime] = None) -> Dict[str, int]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        start = current - timedelta(days=self.settings.sweep_days)
        stats = self.fetch_range(self.settings.tickers, start, current)
        local_day = current.astimezone(EASTERN).date().isoformat()
        self._record("sweep complete day=%s" % local_day, stats)
        return stats

    def _record(self, prefix: str, stats: Dict[str, int]) -> None:
        message = self._summary(prefix, stats)
        self.store.append_log("info", message)
        logging.info(message)
        rejected = stats["interpolated"] + stats["unordered"] + stats["invalid"]
        if rejected:
            warning = "%s rejected=%d" % (prefix, rejected)
            self.store.append_log("warn", warning)
            logging.warning(warning)

    @staticmethod
    def _summary(prefix: str, stats: Dict[str, int]) -> str:
        return (
            "%s received=%d written=%d rejected_interpolated=%d "
            "rejected_unordered=%d rejected_invalid=%d"
            % (
                prefix,
                stats["received"],
                stats["written"],
                stats["interpolated"],
                stats["unordered"],
                stats["invalid"],
            )
        )

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


class Service:
    def __init__(self, collector: Collector):
        self.collector = collector
        self.settings = collector.settings
        self.stop_event = threading.Event()

    def stop(self, signum=None, frame=None) -> None:
        logging.info("stopping service")
        self.stop_event.set()

    def _wait(self, seconds: int) -> None:
        self.stop_event.wait(seconds)

    def _close(self, day: date) -> time:
        if day in self.settings.early_close_days:
            return self.settings.early_close
        return self.settings.regular_close

    def _cycle(self) -> int:
        now = datetime.now(EASTERN)
        day = now.date()
        self.collector.store.append_log("info", "heartbeat")
        if day.weekday() >= 5:
            return self.settings.idle_seconds

        start = datetime.combine(day, self.settings.live_start, tzinfo=EASTERN)
        close = datetime.combine(day, self._close(day), tzinfo=EASTERN)
        final_at = close + timedelta(minutes=self.settings.after_close_minutes)
        if start <= now < final_at:
            self.collector.poll(now)
            return self.settings.poll_seconds
        if now >= final_at and not self.collector.store.sweep_complete(day):
            self.collector.sweep(now)
        return self.settings.idle_seconds

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        logging.info("service started with tickers=%s", ",".join(self.settings.tickers))
        self.collector.store.append_log("info", "service started")
        delay = self.settings.retry_seconds
        while not self.stop_event.is_set():
            try:
                wait_seconds = self._cycle()
                delay = self.settings.retry_seconds
                self._wait(wait_seconds)
            except Exception as exc:
                message = "cycle failed: %s" % exc
                logging.exception("%s; retrying in %d seconds", message, delay)
                try:
                    self.collector.store.append_log("error", message)
                except sqlite3.Error:
                    logging.exception("could not write failure to logs table")
                self._wait(delay)
                delay = min(delay * 2, self.settings.retry_max_seconds)
        self.collector.store.append_log("info", "service stopped")
        logging.info(
            "service stopped",
        )


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
    subparsers.add_parser("once", help="run one live poll from the last stored bars")
    subparsers.add_parser("sweep", help="fetch the configured trailing window")
    subparsers.add_parser("status", help="show local bar coverage")

    migrate = subparsers.add_parser("migrate", help="import the legacy bar table")
    migrate.add_argument("source", type=Path)

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
        elif arguments.command == "once":
            collector.poll()
            print(collector.status_text())
        elif arguments.command == "sweep":
            collector.sweep()
            print(collector.status_text())
        elif arguments.command == "status":
            print(collector.status_text())
        elif arguments.command == "migrate":
            stats = store.migrate_legacy(arguments.source.expanduser().resolve())
            print(
                "migrated read=%d written=%d rejected=%d"
                % (stats["read"], stats["written"], stats["rejected"])
            )
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
                output = io.StringIO()
                fields = (
                    list(objects[0].keys())
                    if objects
                    else [
                        "ticker",
                        "ts",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                    ]
                )
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                writer.writerows(objects)
                sys.stdout.write(output.getvalue())
        return 0
    except (ConfigError, RelayError, ValueError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
