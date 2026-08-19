"""Robinhood connection, OAuth, and market-data payload handling."""

import asyncio
import fcntl
import json
import logging
import os
import webbrowser
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

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

from common.validation import is_symbol


UTC = timezone.utc


class ConfigError(ValueError):
    pass


class RelayError(RuntimeError):
    pass


class RelayAuthRequired(RelayError):
    pass


@dataclass(frozen=True)
class ProviderSettings:
    bounds: str
    url: str
    oauth_store: Path
    oauth_callback_port: int
    timeout_seconds: int
    max_symbols_per_call: int


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


def _bar_row(ticker: str, raw: object) -> BarRow:
    if not is_symbol(ticker):
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
        if not is_symbol(symbol):
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
        self.port = port
        self.server: Optional[HTTPServer] = None

    def _server(self) -> HTTPServer:
        if self.server is None:
            self.server = HTTPServer(
                ("127.0.0.1", self.port), _OAuthCallbackHandler
            )
            self.server.timeout = 300
            self.server.callback_path = None
        return self.server

    def close(self) -> None:
        if self.server is not None:
            self.server.server_close()
            self.server = None

    async def redirect(self, authorization_url: str) -> None:
        self._server()
        print("Opening Robinhood authorization in your browser.")
        print(authorization_url)
        webbrowser.open(authorization_url)

    async def callback(self) -> AuthorizationCodeResult:
        server = self._server()
        try:
            await asyncio.to_thread(server.handle_request)
        finally:
            self.close()
        callback_path = server.callback_path
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
    MAX_RANGE = timedelta(days=1) - timedelta(seconds=1)
    REQUEST_GRACE_SECONDS = 5
    CLOSE_TIMEOUT_SECONDS = 5

    def __init__(self, settings: ProviderSettings):
        self.settings = settings
        self.storage = FileTokenStorage(settings.oauth_store)
        self._session: Optional[ClientSession] = None

    async def _acquire_session_lock(self):
        lock_path = self.settings.oauth_store.with_suffix(
            self.settings.oauth_store.suffix + ".session.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        deadline = asyncio.get_running_loop().time() + self.settings.timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError:
                if asyncio.get_running_loop().time() >= deadline:
                    handle.close()
                    raise RelayError(
                        "Robinhood session lock timed out after %d seconds"
                        % self.settings.timeout_seconds
                    )
                await asyncio.sleep(0.25)

    def _oauth(self, interactive: bool):
        callbacks = (
            BrowserOAuthCallbacks(self.settings.oauth_callback_port)
            if interactive
            else NonInteractiveOAuthCallbacks()
        )
        callback_url = (
            "http://127.0.0.1:%d/callback" % self.settings.oauth_callback_port
        )
        return (
            OAuthClientProvider(
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
            ),
            callbacks,
        )

    async def _open_session(
        self, stack: AsyncExitStack, interactive: bool
    ) -> ClientSession:
        oauth, callbacks = self._oauth(interactive)
        if isinstance(callbacks, BrowserOAuthCallbacks):
            stack.callback(callbacks.close)
        timeout = httpx2.Timeout(float(self.settings.timeout_seconds))
        http_client = await stack.enter_async_context(
            httpx2.AsyncClient(
                auth=oauth,
                follow_redirects=True,
                timeout=timeout,
            )
        )
        read, write = await stack.enter_async_context(
            streamable_http_client(
                self.settings.url,
                http_client=http_client,
                terminate_on_close=False,
            )
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    @staticmethod
    def _relay_error(error: BaseException, action: str) -> RelayError:
        if isinstance(error, RelayAuthRequired) or _contains_exception(
            error, RelayAuthRequired
        ):
            return RelayAuthRequired(
                "Robinhood OAuth approval required; run: make auth"
            )
        return RelayError("Robinhood %s failed: %s" % (action, error))

    @asynccontextmanager
    async def session(self, interactive: bool = False):
        if self._session is not None:
            raise RelayError("Robinhood session is already open")
        stack = AsyncExitStack()
        lock_handle = await self._acquire_session_lock()
        open_timeout = self.settings.timeout_seconds + self.REQUEST_GRACE_SECONDS
        try:
            try:
                async with asyncio.timeout(open_timeout):
                    self._session = await self._open_session(stack, interactive)
            except TimeoutError as exc:
                raise RelayError(
                    "Robinhood session did not open within %d seconds"
                    % open_timeout
                ) from exc
            except Exception as exc:
                raise self._relay_error(exc, "connection") from exc
            yield self
        finally:
            self._session = None
            try:
                try:
                    async with asyncio.timeout(self.CLOSE_TIMEOUT_SECONDS):
                        await stack.aclose()
                except TimeoutError:
                    logging.error(
                        "Robinhood session did not close within %d seconds",
                        self.CLOSE_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    logging.error(
                        "could not close Robinhood connection cleanly: %s",
                        exc,
                    )
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    async def _request(self, action: str, name: str = "", arguments=None):
        session = self._session
        if session is None:
            raise RelayError("Robinhood connection is not open")
        timeout = self.settings.timeout_seconds + self.REQUEST_GRACE_SECONDS
        try:
            request = (
                session.list_tools()
                if action == "list"
                else session.call_tool(
                    name,
                    arguments or {},
                    read_timeout_seconds=float(self.settings.timeout_seconds),
                )
            )
            return await asyncio.wait_for(request, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RelayError(
                "Robinhood %s timed out after %d seconds" % (action, timeout)
            ) from exc
        except Exception as exc:
            raise self._relay_error(exc, action) from exc

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

    async def call_tool(self, name: str, arguments: dict, action: str) -> dict:
        """Call one Robinhood tool through the active shared session."""
        result = await self._request(action, name, arguments)
        return self._payload(result)

    async def list_tools(self) -> List[str]:
        result = await self._request("list")
        return [tool.name for tool in result.tools]

    async def authorize(self) -> List[str]:
        async with self.session(interactive=True) as session:
            tools = await session.list_tools()
        if self.TOOL not in tools:
            raise RelayError("Robinhood connection does not expose %s" % self.TOOL)
        return tools

    async def fetch_bars(
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
            payload = await self.call_tool(
                self.TOOL,
                {
                    "symbols": list(batch),
                    "start_time": start_iso,
                    "end_time": end_iso,
                    "interval": "minute",
                    "bounds": self.settings.bounds,
                },
                "historicals request",
            )
            rows.extend(_bar_rows(payload, batch, start, end))
        return rows
