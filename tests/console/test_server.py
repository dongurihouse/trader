"""Tests for console configuration, telemetry tailing, and HTTP/SSE serving."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import re
import socket
import time

import pytest

from trader.console.config import ConsoleConfig, load_console_config
from trader.console.server import ConsoleServer, run_server
from trader.console.sessions import list_sessions, resolve_session_dir
from trader.console.telemetry_feed import read_new_lines


SAMPLE_TELEMETRY = (
    Path(__file__).resolve().parents[1] / "fixtures" / "telemetry.sample.jsonl"
)


class _SSEReader:
    """Bounded SSE reader that retains partial socket data between reads."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = b""
        self.headers: bytes | None = None

    def read_frames(self, deadline: float, expected_count: int) -> list[dict]:
        records: list[dict] = []

        while time.monotonic() < deadline and len(records) < expected_count:
            records.extend(self._consume_complete_frames())
            if len(records) >= expected_count:
                break

            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue

            if not chunk:
                break
            self.buffer += chunk

        records.extend(self._consume_complete_frames())
        return records

    def _consume_complete_frames(self) -> list[dict]:
        if self.headers is None:
            if b"\r\n\r\n" not in self.buffer:
                return []
            self.headers, self.buffer = self.buffer.split(b"\r\n\r\n", 1)

        records: list[dict] = []
        while b"\n\n" in self.buffer:
            frame, self.buffer = self.buffer.split(b"\n\n", 1)
            if frame.startswith(b"data: "):
                records.append(json.loads(frame.removeprefix(b"data: ")))
        return records


@pytest.fixture
def console_config(tmp_path: Path) -> tuple[ConsoleConfig, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "console.yaml").write_text(
        "host: 127.0.0.1\nport: 0\n", encoding="utf-8"
    )
    (config_dir / "trader.yaml").write_text("data_root: data\n", encoding="utf-8")

    session_dir = tmp_path / "data" / "sessions" / "fixture-session-001"
    session_dir.mkdir(parents=True)
    telemetry_path = session_dir / "telemetry.jsonl"
    telemetry_path.write_bytes(SAMPLE_TELEMETRY.read_bytes())

    return load_console_config(config_dir, base_dir=tmp_path), telemetry_path


@pytest.fixture
def running_server(
    console_config: tuple[ConsoleConfig, Path],
) -> tuple[ConsoleServer, Path]:
    config, telemetry_path = console_config

    with run_server(config) as server:
        serving_thread = server.serve_thread
        yield server, telemetry_path

    assert serving_thread.is_alive() is False


def _get_json(server: ConsoleServer, path: str) -> tuple[int, dict, dict[str, str]]:
    connection = http.client.HTTPConnection(
        server.server_address[0], server.server_address[1], timeout=1.0
    )
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = json.loads(response.read())
        return response.status, body, dict(response.getheaders())
    finally:
        connection.close()


def _get_status(server: ConsoleServer, path: str) -> int:
    connection = http.client.HTTPConnection(
        server.server_address[0], server.server_address[1], timeout=1.0
    )
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status
    finally:
        connection.close()


def _open_sse(server: ConsoleServer, session_id: str) -> tuple[socket.socket, _SSEReader]:
    sock = socket.create_connection(server.server_address, timeout=1.0)
    sock.settimeout(0.1)
    request = (
        f"GET /events?session={session_id} HTTP/1.1\r\n"
        "Host: x\r\n"
        "Connection: keep-alive\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    return sock, _SSEReader(sock)


def test_load_console_config_resolves_relative_data_root(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "console.yaml").write_text(
        "host: 127.0.0.1\nport: 8777\n", encoding="utf-8"
    )
    (config_dir / "trader.yaml").write_text(
        "data_root: relative-data\n", encoding="utf-8"
    )

    config = load_console_config(config_dir, base_dir=tmp_path)

    assert config == ConsoleConfig(
        host="127.0.0.1",
        port=8777,
        data_root=(tmp_path / "relative-data").resolve(),
        config_dir=config_dir.resolve(),
    )
    assert config.data_root.is_absolute()
    assert config.config_dir.is_absolute()


@pytest.mark.parametrize(
    ("console_yaml", "trader_yaml", "missing_key"),
    [
        ("port: 8777\n", "data_root: data\n", "host"),
        ("host: 127.0.0.1\n", "data_root: data\n", "port"),
        ("host: 127.0.0.1\nport: 8777\n", "timezone: UTC\n", "data_root"),
    ],
)
def test_load_console_config_names_missing_required_key(
    tmp_path: Path, console_yaml: str, trader_yaml: str, missing_key: str
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "console.yaml").write_text(console_yaml, encoding="utf-8")
    (config_dir / "trader.yaml").write_text(trader_yaml, encoding="utf-8")

    with pytest.raises(ValueError, match=missing_key):
        load_console_config(config_dir, base_dir=tmp_path)


def test_sessions_are_sorted_and_resolve_named_or_newest(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    for session_id in (
        "paper-20260702-093000",
        "paper-20260701-093000",
        "paper-20260703-093000",
    ):
        (sessions_dir / session_id).mkdir()
    (sessions_dir / "not-a-directory").write_text("ignored", encoding="utf-8")

    assert list_sessions(tmp_path) == [
        "paper-20260701-093000",
        "paper-20260702-093000",
        "paper-20260703-093000",
    ]
    assert resolve_session_dir(tmp_path, "paper-20260702-093000") == (
        sessions_dir / "paper-20260702-093000"
    )
    assert resolve_session_dir(tmp_path, None) == sessions_dir / "paper-20260703-093000"


def test_sessions_sort_by_timestamp_across_modes_and_resolve_newest(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    for session_id in (
        "paper-20260731-093000",
        "backtest-20260801-090000",
        "cross-ticker-20260731-100000",
        "fixture-session-001",
    ):
        (sessions_dir / session_id).mkdir()

    assert list_sessions(tmp_path) == [
        "fixture-session-001",
        "paper-20260731-093000",
        "cross-ticker-20260731-100000",
        "backtest-20260801-090000",
    ]
    assert resolve_session_dir(tmp_path, None) == (
        sessions_dir / "backtest-20260801-090000"
    )


def test_resolve_session_dir_names_missing_session(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does-not-exist"):
        resolve_session_dir(tmp_path, "does-not-exist")

    with pytest.raises(FileNotFoundError, match="no sessions"):
        resolve_session_dir(tmp_path, None)


@pytest.mark.parametrize(
    "session_id",
    ["../outside", "nested/session", r"nested\session", ".", ".."],
)
def test_resolve_session_dir_rejects_non_simple_session_ids(
    tmp_path: Path, session_id: str
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (tmp_path / "outside").mkdir()
    (sessions_dir / "nested" / "session").mkdir(parents=True)
    (sessions_dir / r"nested\session").mkdir()

    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_session_dir(tmp_path, session_id)


def test_resolve_session_dir_rejects_absolute_session_id(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_session_dir(tmp_path, str(outside))


def test_resolve_session_dir_rejects_symlink_outside_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (sessions_dir / "linked-session").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_session_dir(tmp_path, "linked-session")


def test_resolve_session_dir_rejects_newest_symlink_outside_sessions(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (sessions_dir / "paper-20260801-090000").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_session_dir(tmp_path, None)


def test_resolve_session_dir_accepts_simple_session_id(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "fixture-session-001"
    session_dir.mkdir(parents=True)

    assert resolve_session_dir(tmp_path, "fixture-session-001") == session_dir


def test_read_new_lines_preserves_partial_line_offset(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.jsonl"
    first_line = b'{"ev": "session_start"}\n'
    partial_line = b'{"ev": "tick"}'
    path.write_bytes(first_line + partial_line)

    offset, records = read_new_lines(path, 0)

    assert offset == len(first_line)
    assert records == [{"ev": "session_start"}]

    with path.open("ab") as stream:
        stream.write(b"\n")

    new_offset, new_records = read_new_lines(path, offset)

    assert new_offset == len(first_line) + len(partial_line) + 1
    assert new_records == [{"ev": "tick"}]


def test_read_new_lines_treats_missing_file_as_empty(tmp_path: Path) -> None:
    assert read_new_lines(tmp_path / "missing.jsonl", 17) == (17, [])


def test_read_new_lines_skips_malformed_complete_lines(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.jsonl"
    contents = (
        b'{"ev": "session_start"}\n'
        b"not json at all\n"
        b'{"ev": "tick"}\n'
    )
    path.write_bytes(contents)

    offset, records = read_new_lines(path, 0)

    assert offset == len(contents)
    assert records == [{"ev": "session_start"}, {"ev": "tick"}]
    assert read_new_lines(path, offset) == (offset, [])


@pytest.mark.parametrize(
    ("host", "expected_family"),
    [
        ("localhost", socket.AF_INET),
        ("127.0.0.1", socket.AF_INET),
        ("::1", socket.AF_INET6),
    ],
)
def test_console_server_accepts_loopback_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    expected_family: socket.AddressFamily,
) -> None:
    monkeypatch.setattr(ConsoleServer, "server_bind", lambda _server: None)
    monkeypatch.setattr(ConsoleServer, "server_activate", lambda _server: None)
    config = ConsoleConfig(host=host, port=0, data_root=tmp_path, config_dir=tmp_path)

    server = ConsoleServer(config)
    try:
        assert server.socket.family == expected_family
    finally:
        server.server_close()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "console.local"])
def test_console_server_rejects_non_loopback_hosts_before_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    def fail_if_binding_attempted(_server: ConsoleServer) -> None:
        raise AssertionError("invalid host reached socket binding")

    monkeypatch.setattr(ConsoleServer, "server_bind", fail_if_binding_attempted)
    config = ConsoleConfig(host=host, port=0, data_root=tmp_path, config_dir=tmp_path)

    with pytest.raises(ValueError, match=re.escape(host)):
        ConsoleServer(config)


def test_console_server_names_occupied_port(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = ConsoleConfig(
            host="127.0.0.1",
            port=port,
            data_root=tmp_path,
            config_dir=tmp_path,
        )

        with pytest.raises(RuntimeError) as exc_info:
            ConsoleServer(config)

    assert str(exc_info.value) == f"console server: port {port} is already in use"


def test_server_binds_ephemeral_localhost_and_lists_sessions(
    running_server: tuple[ConsoleServer, Path],
) -> None:
    server, _ = running_server

    assert server.server_address[0] == "127.0.0.1"
    assert server.server_address[1] != 0

    status, body, headers = _get_json(server, "/sessions")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body == {"sessions": ["fixture-session-001"]}


def test_events_stream_fixture_then_incremental_record_on_same_socket(
    running_server: tuple[ConsoleServer, Path],
) -> None:
    server, telemetry_path = running_server
    expected_records = [
        json.loads(line) for line in SAMPLE_TELEMETRY.read_text(encoding="utf-8").splitlines()
    ]
    sock, reader = _open_sse(server, "fixture-session-001")

    try:
        initial_records = reader.read_frames(
            time.monotonic() + 2.0,
            expected_count=len(expected_records),
        )

        assert reader.headers is not None
        assert reader.headers.startswith(b"HTTP/1.0 200")
        assert b"Content-Type: text/event-stream" in reader.headers
        assert b"Content-Length:" not in reader.headers
        assert initial_records == expected_records
        assert initial_records[0]["ev"] == "session_start"
        assert initial_records[-1]["ev"] == "session_end"

        appended_record = {
            "ev": "tick",
            "ts": "2026-07-01T20:01:00Z",
            "session": "fixture-session-001",
            "bar_ts": "2026-07-01T20:00:00Z",
        }
        with telemetry_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(appended_record) + "\n")

        incremental_records = reader.read_frames(
            time.monotonic() + 2.0, expected_count=1
        )

        assert incremental_records == [appended_record]
    finally:
        sock.close()


def test_events_returns_404_for_missing_session(
    running_server: tuple[ConsoleServer, Path],
) -> None:
    server, _ = running_server

    status, body, headers = _get_json(server, "/events?session=does-not-exist")

    assert status == 404
    assert headers["Content-Type"] == "application/json"
    assert "does-not-exist" in body["error"]


def test_events_returns_404_for_url_encoded_session_separator(
    running_server: tuple[ConsoleServer, Path],
) -> None:
    server, _ = running_server
    (server.config.data_root / "outside").mkdir()

    status = _get_status(server, "/events?session=..%2foutside")

    assert status == 404


def test_unknown_route_returns_404(running_server: tuple[ConsoleServer, Path]) -> None:
    server, _ = running_server

    status, body, _ = _get_json(server, "/unknown")

    assert status == 404
    assert "error" in body
