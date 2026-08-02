"""Threaded HTTP/SSE server for local console telemetry."""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

from trader.console import workbench_algos, workbench_execution, workbench_provider
from trader.console.config import ConsoleConfig
from trader.console.dashboard import render_dashboard_html
from trader.console.sessions import list_sessions, resolve_session_dir
from trader.console.telemetry_feed import read_new_lines


POLL_INTERVAL_SECONDS = 0.05


class ConsolePortInUseError(RuntimeError):
    """Raised when the configured console port already has a listener."""


class ConsoleServer(ThreadingHTTPServer):
    """Thread-per-request server carrying console configuration and stop state."""

    daemon_threads = True

    def __init__(self, config: ConsoleConfig) -> None:
        host_address = _require_loopback_host(config.host)
        if isinstance(host_address, ipaddress.IPv6Address):
            self.address_family = socket.AF_INET6

        self.config = config
        self.stop_event = threading.Event()
        self.serve_thread: threading.Thread | None = None
        try:
            super().__init__((config.host, config.port), _ConsoleRequestHandler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            raise ConsolePortInUseError(
                f"console server: port {config.port} is already in use"
            ) from None


def _require_loopback_host(
    host: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if host == "localhost":
        return None

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"console host {host!r} is not a loopback address") from None

    if not address.is_loopback:
        raise ValueError(f"console host {host!r} is not a loopback address")
    return address


class _ConsoleRequestHandler(BaseHTTPRequestHandler):
    """Serve bounded JSON endpoints and an incrementally tailed SSE endpoint."""

    server: ConsoleServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/sessions":
            self._send_json(200, {"sessions": list_sessions(self.server.config.data_root)})
            return

        if parsed.path == "/events":
            session_id = parse_qs(parsed.query).get("session", [None])[0]
            try:
                session_dir = resolve_session_dir(
                    self.server.config.data_root, session_id
                )
            except FileNotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
                return

            self._stream_events(session_dir / "telemetry.jsonl")
            return

        if parsed.path == "/api/workbench/provider":
            query_params = {
                key: values[0]
                for key, values in parse_qs(parsed.query).items()
            }
            status, payload = workbench_provider.handle_query(
                query_params,
                data_root=self.server.config.data_root,
                config_dir=self.server.config.config_dir,
            )
            self._send_json(status, payload)
            return

        if parsed.path == "/api/workbench/algos":
            query_params = {
                key: values[0]
                for key, values in parse_qs(parsed.query).items()
            }
            status, payload = workbench_algos.handle_query(
                query_params,
                data_root=self.server.config.data_root,
                config_dir=self.server.config.config_dir,
            )
            self._send_json(status, payload)
            return

        if parsed.path == "/api/workbench/execution":
            query_params = {
                key: values[0]
                for key, values in parse_qs(parsed.query).items()
            }
            status, payload = workbench_execution.handle_query(
                query_params,
                data_root=self.server.config.data_root,
                config_dir=self.server.config.config_dir,
            )
            self._send_json(status, payload)
            return

        if parsed.path == "/workbench/provider":
            self._send_html(200, workbench_provider.render_provider_workbench_html())
            return

        if parsed.path == "/workbench/algos":
            self._send_html(
                200,
                workbench_algos.render_algos_workbench_html(
                    config_dir=self.server.config.config_dir
                ),
            )
            return

        if parsed.path == "/workbench/execution":
            self._send_html(
                200,
                workbench_execution.render_execution_workbench_html(
                    config_dir=self.server.config.config_dir
                ),
            )
            return

        if parsed.path == "/":
            self._send_html(200, render_dashboard_html())
            return

        self._send_json(404, {"error": f"route {parsed.path!r} not found"})

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _send_html(self, status: int, document: str) -> None:
        body = document.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _stream_events(self, telemetry_path) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        offset = 0
        while True:
            new_offset, records = read_new_lines(telemetry_path, offset)
            try:
                for record in records:
                    frame = f"data: {json.dumps(record)}\n\n".encode("utf-8")
                    self.wfile.write(frame)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

            offset = new_offset
            if self.server.stop_event.is_set():
                return
            time.sleep(POLL_INTERVAL_SECONDS)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def run_server(config: ConsoleConfig) -> AbstractContextManager[ConsoleServer]:
    """Run a console server on a daemon thread and shut it down deterministically."""
    server = ConsoleServer(config)
    serving_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": POLL_INTERVAL_SECONDS},
        daemon=True,
        name="trader-console-server",
    )
    server.serve_thread = serving_thread
    serving_thread.start()

    try:
        yield server
    finally:
        server.stop_event.set()
        server.shutdown()
        server.server_close()
        serving_thread.join(timeout=2)
