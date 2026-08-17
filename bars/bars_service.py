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
import time
import webbrowser
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from typing import Dict, Iterator, List, Optional, Sequence, Tuple
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
DEFAULT_CONFIG_PATH = ROOT.parent / "config" / "config.json"
SCHEMA_PATH = ROOT.parent / "config" / "schema.sql"
EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
TECHNICAL_TOOL = "get_equity_technical_indicators"
TECHNICAL_CHUNK_DAYS = 3
TECHNICAL_DEFAULTS = {
    "ema": {"period": 9},
    "sma": {"period": 9},
    "rsi": {"period": 14},
    "momentum": {"period": 12},
    "roc": {"period": 14},
    "cci": {"period": 14},
    "williams_r": {"period": 10},
    "atr": {"period": 14},
    "mfi": {"period": 14},
    "adx": {"period": 10},
    "donchian_channels": {"period": 20},
    "bollinger_bands": {"period": 20, "num_std": 2},
    "macd": {"fast_period": 12, "slow_period": 26, "signal_period": 9},
    "keltner_channels": {"period": 20, "multiplier": 2},
    "supertrend": {"period": 10, "multiplier": 3},
    "vwap": {},
    "obv": {},
    "pivot_points": {"method": "classic"},
}


class ConfigError(ValueError):
    pass


class RelayError(RuntimeError):
    pass


class RelayAuthRequired(RelayError):
    pass


@dataclass(frozen=True)
class TechnicalSpec:
    name: str
    params: str

    def provider_params(self) -> dict:
        return json.loads(self.params)


@dataclass(frozen=True)
class ProviderSettings:
    bounds: str
    url: str
    oauth_store: Path
    oauth_callback_port: int
    timeout_seconds: int
    max_symbols_per_call: int


@dataclass(frozen=True)
class Settings:
    tickers: Tuple[str, ...]
    live_start: clock_time
    regular_close: clock_time
    early_close: clock_time
    early_close_days: frozenset
    after_close_minutes: int
    poll_seconds: int
    api_port: int
    backfill_days: int
    sweep_days: int
    poll_catchup_days: int
    idle_seconds: int
    retry_seconds: int
    retry_max_seconds: int
    database: Path
    provider: ProviderSettings
    technical_specs: Tuple[TechnicalSpec, ...]


def _positive_int(value, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError("%s must be an integer >= %d" % (name, minimum))
    return value


def _clock(value, name: str) -> clock_time:
    if not isinstance(value, str):
        raise ConfigError("%s must be HH:MM" % name)
    try:
        parsed = clock_time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("%s must be HH:MM" % name) from exc
    if parsed.second or parsed.microsecond:
        raise ConfigError("%s must be precise to the minute" % name)
    return parsed


def _technical_specs(raw: dict) -> Tuple[TechnicalSpec, ...]:
    signals = raw.get("signals") or {}
    if not isinstance(signals, dict):
        raise ConfigError("signals must be a JSON object")
    specs: List[TechnicalSpec] = []
    seen = set()
    for signal_name, node in signals.items():
        if not isinstance(node, dict) or node.get("inputs") != ["bar_metadata"]:
            continue
        configured = node.get("params")
        if not isinstance(configured, dict):
            raise ConfigError("signals.%s.params must be a JSON object" % signal_name)
        name = configured.get("name")
        if name not in TECHNICAL_DEFAULTS:
            raise ConfigError(
                "signals.%s.params.name is not a supported provider indicator"
                % signal_name
            )
        supplied = {key: value for key, value in configured.items() if key != "name"}
        unknown = set(supplied) - set(TECHNICAL_DEFAULTS[name])
        if unknown:
            raise ConfigError(
                "signals.%s has unsupported parameters: %s"
                % (signal_name, ", ".join(sorted(unknown)))
            )
        parameters = dict(TECHNICAL_DEFAULTS[name])
        parameters.update(supplied)
        for key, value in parameters.items():
            if key.endswith("period") or key == "period":
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ConfigError(
                        "signals.%s.params.%s must be a positive integer"
                        % (signal_name, key)
                    )
            elif key in ("num_std", "multiplier"):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or value <= 0
                ):
                    raise ConfigError(
                        "signals.%s.params.%s must be a positive number"
                        % (signal_name, key)
                    )
            elif key == "method" and value != "classic":
                raise ConfigError(
                    "signals.%s.params.method must be 'classic'" % signal_name
                )
        if name == "macd" and parameters["fast_period"] >= parameters["slow_period"]:
            raise ConfigError(
                "signals.%s fast_period must be less than slow_period" % signal_name
            )
        encoded = json.dumps(parameters, separators=(",", ":"), sort_keys=True)
        key = (name, encoded)
        if key not in seen:
            seen.add(key)
            specs.append(TechnicalSpec(name=name, params=encoded))
    return tuple(specs)


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

    database_value = raw.get("database", "../data/trader.sqlite3")
    database = Path(database_value).expanduser()
    if not database.is_absolute():
        database = (path.parent / database).resolve()
    oauth_store_value = provider.get(
        "oauth_store", "../data/robinhood_oauth.json"
    )
    oauth_store = Path(oauth_store_value).expanduser()
    if not oauth_store.is_absolute():
        oauth_store = (path.parent / oauth_store).resolve()

    return Settings(
        tickers=tuple(tickers),
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
        api_port=_positive_int(bars.get("api_port", 8789), "bars.api_port", 1024),
        backfill_days=_positive_int(
            bars.get("backfill_days", 120), "bars.backfill_days"
        ),
        sweep_days=_positive_int(bars.get("sweep_days", 30), "bars.sweep_days"),
        poll_catchup_days=_positive_int(
            bars.get("poll_catchup_days", 7), "bars.poll_catchup_days"
        ),
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
        provider=ProviderSettings(
            bounds=bounds,
            url=str(
                provider.get("url", "https://agent.robinhood.com/mcp/trading")
            ),
            oauth_store=oauth_store,
            oauth_callback_port=_positive_int(
                provider.get("oauth_callback_port", 8765),
                "bars.provider.oauth_callback_port",
                1024,
            ),
            timeout_seconds=_positive_int(
                provider.get("timeout_seconds", 300),
                "bars.provider.timeout_seconds",
                30,
            ),
            max_symbols_per_call=_positive_int(
                provider.get("max_symbols_per_call", 3),
                "bars.provider.max_symbols_per_call",
            ),
        ),
        technical_specs=_technical_specs(raw),
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
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            clock = clock_time(23, 59, 59) if end else clock_time(0, 0)
            return _epoch(datetime.combine(day, clock, tzinfo=EASTERN))
        return _epoch(_parse_iso(value))
    except (TypeError, ValueError) as exc:
        raise ConfigError("invalid timestamp: %s" % value) from exc


def _collection_window(
    settings: Settings, day: date
) -> Tuple[datetime, datetime]:
    close = (
        settings.early_close
        if day in settings.early_close_days
        else settings.regular_close
    )
    start = datetime.combine(day, settings.live_start, tzinfo=EASTERN)
    end = datetime.combine(day, close, tzinfo=EASTERN) + timedelta(
        minutes=settings.after_close_minutes
    )
    return start.astimezone(UTC), end.astimezone(UTC)


def _collection_chunks(
    settings: Settings, start: datetime, end: datetime
) -> Iterator[Tuple[datetime, datetime]]:
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    day = start.astimezone(EASTERN).date()
    final_day = end.astimezone(EASTERN).date()
    while day <= final_day:
        if day.weekday() < 5:
            window_start, window_end = _collection_window(settings, day)
            chunk_start = max(start, window_start)
            chunk_end = min(end, window_end)
            if chunk_start <= chunk_end:
                yield chunk_start, chunk_end
        day += timedelta(days=1)


def _fixed_chunks(
    start: datetime, end: datetime, days: int
) -> Iterator[Tuple[datetime, datetime]]:
    cursor = start.astimezone(UTC)
    end = end.astimezone(UTC)
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=days))
        yield cursor, chunk_end
        cursor = chunk_end


@dataclass(frozen=True)
class BarRow:
    ticker: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    interpolated: bool


@dataclass(frozen=True)
class MetadataPoint:
    ts: int
    value: str


def _bar_row(ticker: str, raw: object) -> BarRow:
    if not isinstance(ticker, str) or not SYMBOL_RE.fullmatch(ticker):
        raise RelayError("Robinhood result has an invalid symbol")
    if not isinstance(raw, dict):
        raise RelayError("Robinhood returned a malformed bar")
    interpolated = raw.get("interpolated", False)
    if not isinstance(interpolated, bool):
        raise RelayError("Robinhood bar interpolated flag is not boolean")
    try:
        return BarRow(
            ticker=ticker,
            ts=_epoch(_parse_iso(raw["begins_at"])),
            open=float(raw["open_price"]),
            high=float(raw["high_price"]),
            low=float(raw["low_price"]),
            close=float(raw["close_price"]),
            volume=int(raw["volume"]),
            interpolated=interpolated,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise RelayError("Robinhood returned a malformed bar") from exc


def _bar_rows(
    payload: object,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
) -> List[BarRow]:
    data = payload.get("data") if isinstance(payload, dict) else None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise RelayError("Robinhood payload is missing data.results")

    wanted = set(symbols)
    got = set()
    rows: List[BarRow] = []
    for result in results:
        if not isinstance(result, dict):
            raise RelayError("Robinhood result block is not an object")
        symbol = result.get("symbol")
        if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
            raise RelayError("Robinhood result has an invalid symbol")
        bars = result.get("bars")
        if not isinstance(bars, list):
            raise RelayError("Robinhood result bars is not a list")
        got.add(symbol)
        for raw in bars:
            row = _bar_row(symbol, raw)
            timestamp = datetime.fromtimestamp(row.ts, UTC)
            if timestamp < start or timestamp > end:
                raise RelayError(
                    "%s bar %s is outside [%s, %s]"
                    % (
                        symbol,
                        _iso_utc(timestamp),
                        _iso_utc(start),
                        _iso_utc(end),
                    )
                )
            rows.append(row)

    unexpected = got - wanted
    if unexpected:
        raise RelayError(
            "Robinhood returned unexpected tickers: %s"
            % ", ".join(sorted(unexpected))
        )
    missing = wanted - got
    if missing:
        raise RelayError(
            "Robinhood omitted requested tickers: %s"
            % ", ".join(sorted(missing))
        )
    return rows


def _metadata_points(
    payload: object,
    ticker: str,
    spec: TechnicalSpec,
    start: datetime,
    end: datetime,
) -> List[MetadataPoint]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("symbol") != ticker:
        raise RelayError("Robinhood indicator payload has the wrong symbol")
    if data.get("interval") != "minute":
        raise RelayError("Robinhood indicator payload has the wrong interval")
    indicators = data.get("indicators")
    if not isinstance(indicators, list) or len(indicators) != 1:
        raise RelayError("Robinhood indicator payload must contain one indicator")
    indicator = indicators[0]
    if not isinstance(indicator, dict) or indicator.get("type") != spec.name:
        raise RelayError("Robinhood indicator payload has the wrong type")
    series = indicator.get("series")
    if not isinstance(series, list):
        raise RelayError("Robinhood indicator payload is missing its series")

    points: List[MetadataPoint] = []
    for raw in series:
        if not isinstance(raw, dict):
            raise RelayError("Robinhood returned a malformed indicator point")
        try:
            timestamp = _parse_iso(raw["begins_at"])
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise RelayError("Robinhood returned a malformed indicator point") from exc
        if timestamp < start or timestamp > end:
            raise RelayError(
                "%s %s point %s is outside [%s, %s]"
                % (
                    ticker,
                    spec.name,
                    _iso_utc(timestamp),
                    _iso_utc(start),
                    _iso_utc(end),
                )
            )
        values = {}
        for name, value in raw.items():
            if name == "begins_at" or value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise RelayError("Robinhood returned a non-numeric indicator value")
            values[name] = value
        if values:
            points.append(
                MetadataPoint(
                    ts=_epoch(timestamp),
                    value=json.dumps(
                        values,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
            )
    return points


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


@dataclass
class _RobinhoodSessionState:
    requests: Queue
    ready: threading.Event
    error: Optional[BaseException] = None
    thread: Optional[threading.Thread] = None


class RobinhoodClient:
    TOOL = "get_equity_historicals"
    MAX_RANGE = timedelta(days=1) - timedelta(seconds=1)
    REQUEST_GRACE_SECONDS = 5
    CLOSE_TIMEOUT_SECONDS = 5

    def __init__(self, settings: ProviderSettings):
        self.settings = settings
        self.storage = FileTokenStorage(settings.oauth_store)
        self._state: Optional[_RobinhoodSessionState] = None

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
            server_url=self.settings.url,
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

    async def _session_worker(
        self, state: _RobinhoodSessionState, interactive: bool
    ) -> None:
        oauth = self._oauth(interactive)
        timeout = httpx2.Timeout(float(self.settings.timeout_seconds))
        async with httpx2.AsyncClient(
            auth=oauth,
            follow_redirects=True,
            timeout=timeout,
        ) as http_client:
            async with streamable_http_client(
                self.settings.url,
                http_client=http_client,
                terminate_on_close=False,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    state.ready.set()
                    while True:
                        request = await asyncio.to_thread(state.requests.get)
                        if request is None:
                            return
                        action, name, arguments, future = request
                        try:
                            if action == "list":
                                result = await session.list_tools()
                            else:
                                result = await session.call_tool(
                                    name,
                                    arguments,
                                    read_timeout_seconds=float(
                                        self.settings.timeout_seconds
                                    ),
                                )
                        except Exception as exc:
                            if not future.cancelled():
                                future.set_exception(exc)
                        else:
                            if not future.cancelled():
                                future.set_result(result)

    def _worker_main(
        self, state: _RobinhoodSessionState, interactive: bool
    ) -> None:
        try:
            asyncio.run(self._session_worker(state, interactive))
        except Exception as exc:
            state.error = exc
            state.ready.set()

    @staticmethod
    def _relay_error(error: BaseException, action: str) -> RelayError:
        if isinstance(error, RelayAuthRequired) or _contains_exception(
            error, RelayAuthRequired
        ):
            return RelayAuthRequired(
                "Robinhood OAuth approval required; run: make auth"
            )
        return RelayError("Robinhood %s failed: %s" % (action, error))

    def _close_state(self, state: _RobinhoodSessionState) -> bool:
        state.requests.put(None)
        if state.thread is not None:
            state.thread.join(timeout=self.CLOSE_TIMEOUT_SECONDS)
        alive = state.thread is not None and state.thread.is_alive()
        if self._state is state:
            self._state = None
        return alive

    @contextmanager
    def session(self, interactive: bool = False):
        if self._state is not None:
            raise RelayError("Robinhood session is already open")
        state = _RobinhoodSessionState(Queue(), threading.Event())
        state.thread = threading.Thread(
            target=self._worker_main,
            args=(state, interactive),
            name="robinhood-mcp-session",
            daemon=True,
        )
        self._state = state
        state.thread.start()
        open_timeout = (
            self.settings.timeout_seconds + self.REQUEST_GRACE_SECONDS
        )
        if not state.ready.wait(timeout=open_timeout):
            self._close_state(state)
            raise RelayError(
                "Robinhood session did not open within %d seconds"
                % open_timeout
            )
        if state.error is not None:
            error = self._relay_error(state.error, "connection")
            self._close_state(state)
            raise error
        try:
            yield RobinhoodSession(self)
        finally:
            close_timed_out = self._close_state(state)
            if close_timed_out:
                logging.error(
                    "Robinhood session did not close within %d seconds",
                    self.CLOSE_TIMEOUT_SECONDS,
                )
            if state.error is not None:
                logging.error(
                    "could not close Robinhood connection cleanly: %s",
                    state.error,
                )

    def _request(self, action: str, name: str = "", arguments=None):
        state = self._state
        if state is None:
            raise RelayError("Robinhood connection is not open")
        if state.error is not None:
            raise self._relay_error(state.error, action)
        future = Future()
        state.requests.put((action, name, arguments or {}, future))
        try:
            return future.result(
                timeout=(
                    self.settings.timeout_seconds + self.REQUEST_GRACE_SECONDS
                )
            )
        except FutureTimeoutError as exc:
            future.cancel()
            raise RelayError(
                "Robinhood %s timed out after %d seconds"
                % (
                    action,
                    self.settings.timeout_seconds + self.REQUEST_GRACE_SECONDS,
                )
            ) from exc
        except Exception as exc:
            raise self._relay_error(exc, action) from exc

    def authorize(self) -> List[str]:
        with self.session(interactive=True) as session:
            tools = session.list_tools()
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

    def _fetch_bars(
        self,
        symbols: Sequence[str],
        start_iso: str,
        end_iso: str,
    ) -> List[BarRow]:
        start = _parse_iso(start_iso)
        end = _parse_iso(end_iso)
        if end - start > self.MAX_RANGE:
            raise RelayError("minute request exceeds the one-day provider limit")
        rows: List[BarRow] = []
        size = self.settings.max_symbols_per_call
        for offset in range(0, len(symbols), size):
            batch = symbols[offset : offset + size]
            result = self._request(
                "historicals request",
                self.TOOL,
                {
                    "symbols": list(batch),
                    "start_time": start_iso,
                    "end_time": end_iso,
                    "interval": "minute",
                    "bounds": self.settings.bounds,
                },
            )
            payload = self._payload(result)
            rows.extend(_bar_rows(payload, batch, start, end))
        return rows

    def _fetch_technical(
        self,
        ticker: str,
        spec: TechnicalSpec,
        start_iso: str,
        end_iso: str,
    ) -> List[MetadataPoint]:
        start = _parse_iso(start_iso)
        end = _parse_iso(end_iso)
        arguments = {
            "symbol": ticker,
            "type": spec.name,
            "interval": "minute",
            "start_time": start_iso,
            "end_time": end_iso,
            "bounds": self.settings.bounds,
            "output": "series",
        }
        arguments.update(spec.provider_params())
        result = self._request(
            "technical indicator request",
            TECHNICAL_TOOL,
            arguments,
        )
        return _metadata_points(self._payload(result), ticker, spec, start, end)


class RobinhoodSession:
    def __init__(self, client: RobinhoodClient):
        self.client = client

    def list_tools(self) -> List[str]:
        result = self.client._request("list")
        return [tool.name for tool in result.tools]

    def fetch_bars(
        self,
        symbols: Sequence[str],
        start_iso: str,
        end_iso: str,
    ) -> List[BarRow]:
        return self.client._fetch_bars(symbols, start_iso, end_iso)

    def fetch_technical(
        self,
        ticker: str,
        spec: TechnicalSpec,
        start_iso: str,
        end_iso: str,
    ) -> List[MetadataPoint]:
        return self.client._fetch_technical(ticker, spec, start_iso, end_iso)


@dataclass(frozen=True)
class JobState:
    kind: str
    scope: str
    target: str
    window_start: int
    window_end: int
    progress_ts: Optional[int]


class BarStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            schema = SCHEMA_PATH.read_text()
        except FileNotFoundError as exc:
            raise ConfigError("schema file does not exist: %s" % SCHEMA_PATH) from exc
        self.connection = sqlite3.connect(str(self.path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(schema)

    @contextmanager
    def transaction(self):
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _upsert(connection, rows: Sequence[tuple]) -> None:
        connection.executemany(
            """
            INSERT INTO bars (
                ticker, ts, open, high, low, close, volume,
                interpolated, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, ts) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                interpolated=excluded.interpolated,
                fetched_at=excluded.fetched_at
            """,
            rows,
        )

    def store_bars(
        self,
        bars: Sequence[BarRow],
        job: Optional[Tuple[str, Sequence[str], str, int]] = None,
    ) -> Dict[str, int]:
        stats = {
            "received": len(bars),
            "written": 0,
            "interpolated": sum(int(bar.interpolated) for bar in bars),
        }
        fetched_at = _epoch(datetime.now(UTC))
        rows = [
            (
                bar.ticker,
                bar.ts,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                int(bar.interpolated),
                fetched_at,
            )
            for bar in bars
        ]
        with self.transaction() as connection:
            self._upsert(connection, rows)
            if job is not None:
                kind, scopes, target, progress_ts = job
                connection.executemany(
                    """
                    UPDATE bar_jobs
                    SET progress_ts = CASE
                        WHEN progress_ts IS NULL OR progress_ts < ? THEN ?
                        ELSE progress_ts
                    END
                    WHERE kind=? AND scope=? AND target=? AND completed_at IS NULL
                    """,
                    [
                        (progress_ts, progress_ts, kind, scope, target)
                        for scope in scopes
                    ],
                )
        stats["written"] = len(rows)
        return stats

    def store_metadata(
        self,
        points: Sequence[MetadataPoint],
        ticker: str,
        spec: TechnicalSpec,
        start: datetime,
        end: datetime,
    ) -> Dict[str, int]:
        allowed = {
            row["ts"]
            for row in self.connection.execute(
                "SELECT ts FROM bars WHERE ticker=? AND ts BETWEEN ? AND ?",
                (ticker, _epoch(start), _epoch(end)),
            ).fetchall()
        }
        fetched_at = _epoch(datetime.now(UTC))
        rows = [
            (ticker, point.ts, spec.name, spec.params, point.value, fetched_at)
            for point in points
            if point.ts in allowed
        ]
        with self.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO bar_metadata (
                    ticker, ts, name, params, value, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, ts, name, params) DO UPDATE SET
                    value=excluded.value,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return {"received": len(points), "written": len(rows)}

    def latest_metadata(
        self, ticker: str, spec: TechnicalSpec, refreshed_after: int
    ) -> Optional[int]:
        row = self.connection.execute(
            """
            SELECT MAX(ts) AS ts FROM bar_metadata
            WHERE ticker=? AND name=? AND params=? AND fetched_at>=?
            """,
            (ticker, spec.name, spec.params, refreshed_after),
        ).fetchone()
        return row["ts"]

    def ensure_jobs(
        self,
        kind: str,
        scopes: Sequence[str],
        target: str,
        window_start: int,
        window_end: int,
        force: bool = False,
    ) -> None:
        started_at = _epoch(datetime.now(UTC))
        with self.transaction() as connection:
            if force:
                connection.executemany(
                    """
                    INSERT INTO bar_jobs (
                        kind, scope, target, window_start, window_end, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(kind, scope, target) DO UPDATE SET
                        window_start=excluded.window_start,
                        window_end=excluded.window_end,
                        progress_ts=NULL,
                        started_at=excluded.started_at,
                        completed_at=NULL
                    """,
                    [
                        (kind, scope, target, window_start, window_end, started_at)
                        for scope in scopes
                    ],
                )
            else:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO bar_jobs (
                        kind, scope, target, window_start, window_end, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (kind, scope, target, window_start, window_end, started_at)
                        for scope in scopes
                    ],
                )

    def incomplete_jobs(
        self, kind: str, scopes: Sequence[str], target: str
    ) -> List[JobState]:
        placeholders = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            """
            SELECT kind, scope, target, window_start, window_end, progress_ts
            FROM bar_jobs
            WHERE kind=? AND target=? AND completed_at IS NULL
              AND scope IN (%s)
            ORDER BY scope
            """ % placeholders,
            (kind, target, *scopes),
        ).fetchall()
        return [JobState(**dict(row)) for row in rows]

    def complete_jobs(
        self, kind: str, scopes: Sequence[str], target: str
    ) -> None:
        completed_at = _epoch(datetime.now(UTC))
        with self.transaction() as connection:
            connection.executemany(
                """
                UPDATE bar_jobs
                SET progress_ts=window_end, completed_at=?
                WHERE kind=? AND scope=? AND target=? AND completed_at IS NULL
                """,
                [(completed_at, kind, scope, target) for scope in scopes],
            )

    def latest_by_ticker(self, tickers: Sequence[str]) -> Dict[str, Optional[int]]:
        latest = {ticker: None for ticker in tickers}
        placeholders = ",".join("?" for _ in tickers)
        rows = self.connection.execute(
            "SELECT ticker, MAX(ts) AS ts FROM bars "
            "WHERE ticker IN (%s) GROUP BY ticker" % placeholders,
            tuple(tickers),
        ).fetchall()
        for row in rows:
            latest[row["ticker"]] = row["ts"]
        return latest

    def append_log(self, level: str, message: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO logs(ts, service, level, message) VALUES (?, ?, ?, ?)",
                (_epoch(datetime.now(UTC)), "bars", level, message),
            )

    def sweep_complete(self, day: date) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM bar_jobs
            WHERE kind='sweep' AND scope='all' AND target=?
              AND completed_at IS NOT NULL
            LIMIT 1
            """,
            (day.isoformat(),),
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
        return self.connection.execute(query, tuple(tickers)).fetchall()

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
            "SELECT ticker, ts, open, high, low, close, volume, interpolated "
            "FROM bars WHERE %s ORDER BY ts" % " AND ".join(clauses)
        )
        if limit:
            query += " LIMIT ?"
            arguments.append(limit)
        return self.connection.execute(query, tuple(arguments)).fetchall()


class Collector:
    def __init__(self, settings: Settings, store: BarStore, provider=None):
        self.settings = settings
        self.store = store
        self.provider = provider or RobinhoodClient(settings.provider)

    @staticmethod
    def _empty_stats() -> Dict[str, int]:
        return {
            "received": 0,
            "written": 0,
            "interpolated": 0,
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
        job: Optional[Tuple[str, str, Sequence[str]]] = None,
    ) -> Dict[str, int]:
        total = self._empty_stats()
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        with self.provider.session() as session:
            for chunk_start, chunk_end in _collection_chunks(
                self.settings, start, end
            ):
                start_iso = _iso_utc(chunk_start)
                end_iso = _iso_utc(chunk_end)
                logging.info(
                    "fetching %s from %s through %s",
                    ",".join(symbols),
                    start_iso,
                    end_iso,
                )
                bars = session.fetch_bars(symbols, start_iso, end_iso)
                progress = None
                if job is not None:
                    progress = (
                        job[0],
                        job[2],
                        job[1],
                        _epoch(chunk_end),
                    )
                self._add_stats(
                    total,
                    self.store.store_bars(bars, job=progress),
                )
        return total

    def fetch_metadata_range(
        self, start: datetime, end: datetime, refreshed_after: int
    ) -> Dict[str, int]:
        total = {"requests": 0, "received": 0, "written": 0}
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        if not self.settings.technical_specs:
            return total
        with self.provider.session() as session:
            for spec in self.settings.technical_specs:
                for ticker in self.settings.tickers:
                    latest = self.store.latest_metadata(
                        ticker, spec, refreshed_after
                    )
                    cursor = start
                    if latest is not None:
                        cursor = max(
                            cursor,
                            datetime.fromtimestamp(latest + 60, UTC),
                        )
                    chunks = (
                        _collection_chunks(self.settings, cursor, end)
                        if spec.name == "pivot_points"
                        else _fixed_chunks(cursor, end, TECHNICAL_CHUNK_DAYS)
                    )
                    for chunk_start, chunk_end in chunks:
                        logging.info(
                            "fetching %s %s from %s through %s",
                            ticker,
                            spec.name,
                            _iso_utc(chunk_start),
                            _iso_utc(chunk_end),
                        )
                        points = session.fetch_technical(
                            ticker,
                            spec,
                            _iso_utc(chunk_start),
                            _iso_utc(chunk_end),
                        )
                        stats = self.store.store_metadata(
                            points,
                            ticker,
                            spec,
                            chunk_start,
                            chunk_end,
                        )
                        total["requests"] += 1
                        total["received"] += stats["received"]
                        total["written"] += stats["written"]
        return total

    def _run_jobs(
        self, kind: str, target: str, jobs: Sequence[JobState]
    ) -> Dict[str, int]:
        total = self._empty_stats()
        groups: Dict[Tuple[int, int, Optional[int]], List[str]] = {}
        for job in jobs:
            key = (job.window_start, job.window_end, job.progress_ts)
            groups.setdefault(key, []).append(job.scope)
        for (window_start, window_end, progress_ts), scopes in groups.items():
            start_ts = window_start if progress_ts is None else progress_ts + 1
            if start_ts <= window_end:
                self._add_stats(
                    total,
                    self.fetch_range(
                        scopes,
                        datetime.fromtimestamp(start_ts, UTC),
                        datetime.fromtimestamp(window_end, UTC),
                        job=(kind, target, scopes),
                    ),
                )
            self.store.complete_jobs(kind, scopes, target)
        return total

    def poll(self, now: Optional[datetime] = None) -> Dict[str, int]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self.backfill_pending(current)
        floor = current - timedelta(days=self.settings.sweep_days)
        groups: Dict[int, List[str]] = {}
        for ticker, latest in self.store.latest_by_ticker(
            self.settings.tickers
        ).items():
            start_ts = latest if latest is not None else _epoch(floor)
            if start_ts > _epoch(current):
                start_ts = _epoch(floor)
            groups.setdefault(start_ts, []).append(ticker)

        total = self._empty_stats()
        for start_ts, tickers in sorted(groups.items()):
            start = datetime.fromtimestamp(start_ts, UTC)
            end = min(
                current,
                start + timedelta(days=self.settings.poll_catchup_days),
            )
            self._add_stats(
                total,
                self.fetch_range(tickers, start, end),
            )
        self._record("poll complete", total)
        return total

    def sweep(
        self,
        now: Optional[datetime] = None,
        scheduled_day: Optional[date] = None,
    ) -> Dict[str, int]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        metadata_refreshed_after = _epoch(
            datetime.combine(
                datetime.now(UTC).date(), clock_time(0), tzinfo=UTC
            )
        )
        self.backfill_pending(current)
        start = current - timedelta(days=self.settings.sweep_days)
        scheduled = scheduled_day is not None
        if not scheduled:
            stats = self.fetch_range(self.settings.tickers, start, current)
            metadata_start = start
            metadata_end = current
        else:
            target = scheduled_day.isoformat()
            self.store.ensure_jobs(
                "sweep",
                ("all",),
                target,
                _epoch(start),
                _epoch(current),
            )
            jobs = self.store.incomplete_jobs("sweep", ("all",), target)
            if not jobs:
                return self._empty_stats()
            sweep_job = jobs[0]
            selected = tuple(self.settings.tickers)
            resume = (
                sweep_job.window_start
                if sweep_job.progress_ts is None
                else sweep_job.progress_ts + 1
            )
            if resume <= sweep_job.window_end:
                stats = self.fetch_range(
                    selected,
                    datetime.fromtimestamp(resume, UTC),
                    datetime.fromtimestamp(sweep_job.window_end, UTC),
                    job=("sweep", target, ("all",)),
                )
            else:
                stats = self._empty_stats()
            metadata_start = datetime.fromtimestamp(sweep_job.window_start, UTC)
            metadata_end = datetime.fromtimestamp(sweep_job.window_end, UTC)
        metadata_stats = self.fetch_metadata_range(
            metadata_start,
            metadata_end,
            metadata_refreshed_after,
        )
        if scheduled:
            self.store.complete_jobs("sweep", ("all",), target)
        prefix = "sweep complete"
        if scheduled_day is not None:
            prefix += " day=%s" % scheduled_day.isoformat()
        self._record(prefix, stats)
        self._record_metadata(metadata_stats)
        return stats

    def backfill(
        self,
        now: Optional[datetime] = None,
        tickers: Optional[Sequence[str]] = None,
        force: bool = False,
    ) -> Dict[str, int]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        selected = tuple(tickers or self.settings.tickers)
        start = current - timedelta(days=self.settings.backfill_days)
        target = str(self.settings.backfill_days)
        self.store.ensure_jobs(
            "backfill",
            selected,
            target,
            _epoch(start),
            _epoch(current),
            force=force,
        )
        jobs = self.store.incomplete_jobs("backfill", selected, target)
        stats = self._run_jobs("backfill", target, jobs)
        self._record(
            "backfill run complete tickers=%s days=%d"
            % (",".join(selected), self.settings.backfill_days),
            stats,
        )
        return stats

    def backfill_pending(
        self, now: Optional[datetime] = None
    ) -> Optional[Dict[str, int]]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        target = str(self.settings.backfill_days)
        self.store.ensure_jobs(
            "backfill",
            self.settings.tickers,
            target,
            _epoch(current - timedelta(days=self.settings.backfill_days)),
            _epoch(current),
        )
        jobs = self.store.incomplete_jobs(
            "backfill", self.settings.tickers, target
        )
        if not jobs:
            return None
        stats = self._run_jobs("backfill", target, jobs)
        self._record(
            "backfill run complete tickers=%s days=%d"
            % (
                ",".join(job.scope for job in jobs),
                self.settings.backfill_days,
            ),
            stats,
        )
        return stats

    def _record(self, prefix: str, stats: Dict[str, int]) -> None:
        message = (
            "%s received=%d written=%d interpolated=%d"
            % (
                prefix,
                stats["received"],
                stats["written"],
                stats["interpolated"],
            )
        )
        self.store.append_log("info", message)
        logging.info(message)

    def _record_metadata(self, stats: Dict[str, int]) -> None:
        message = (
            "metadata sweep complete requests=%d received=%d written=%d"
            % (stats["requests"], stats["received"], stats["written"])
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


class Service:
    def __init__(self, collector: Collector):
        self.collector = collector
        self.settings = collector.settings
        self.stop_event = threading.Event()
        self.started_at = _epoch(datetime.now(UTC))

    def health(self) -> dict:
        return {
            "ok": True,
            "service": "bars",
            "status": "running",
            "ts": _epoch(datetime.now(UTC)),
            "started_at": self.started_at,
            "pid": os.getpid(),
        }

    def stop(self, signum=None, frame=None) -> None:
        logging.info("stopping service")
        self.stop_event.set()

    def _wait(self, seconds: int) -> None:
        self.stop_event.wait(seconds)

    def _cycle(self) -> int:
        now = datetime.now(UTC)
        day = now.astimezone(EASTERN).date()
        if day.weekday() >= 5:
            return self.settings.idle_seconds

        start, final_at = _collection_window(self.settings, day)
        if start <= now < final_at:
            self.collector.poll(now)
            return self.settings.poll_seconds
        if now >= final_at and not self.collector.store.sweep_complete(day):
            self.collector.sweep(now, scheduled_day=day)
        return self.settings.idle_seconds

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
        payload = self.server.service.health()
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    subparsers.add_parser("once", help="run one live poll from the last stored bars")
    subparsers.add_parser("backfill", help="fetch the configured initial window")
    subparsers.add_parser("sweep", help="fetch the configured trailing window")
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
    store = None
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
        elif arguments.command == "backfill":
            collector.backfill(force=True)
            print(collector.status_text())
        elif arguments.command == "sweep":
            collector.sweep()
            print(collector.status_text())
        elif arguments.command == "status":
            print(collector.status_text())
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
                        "interpolated",
                    ]
                )
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                writer.writerows(objects)
                sys.stdout.write(output.getvalue())
        return 0
    except (ConfigError, RelayError, sqlite3.Error, OSError) as exc:
        logging.error("%s", exc)
        if store is not None:
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
