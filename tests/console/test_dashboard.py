"""HTTP acceptance test for the self-contained console dashboard."""

from __future__ import annotations

import http.client
from pathlib import Path

import pytest

from trader.console.config import ConsoleConfig, load_console_config
from trader.console.dashboard import SHADOW_CAVEAT_TEXT
from trader.console.server import ConsoleServer, run_server


@pytest.fixture
def console_config(tmp_path: Path) -> ConsoleConfig:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "console.yaml").write_text(
        "host: 127.0.0.1\nport: 0\n", encoding="utf-8"
    )
    (config_dir / "trader.yaml").write_text("data_root: data\n", encoding="utf-8")

    session_dir = tmp_path / "data" / "sessions" / "fixture-session-001"
    session_dir.mkdir(parents=True)
    (session_dir / "telemetry.jsonl").write_text("", encoding="utf-8")

    return load_console_config(config_dir, base_dir=tmp_path)


@pytest.fixture
def running_server(console_config: ConsoleConfig) -> ConsoleServer:
    with run_server(console_config) as server:
        serving_thread = server.serve_thread
        yield server

    assert serving_thread.is_alive() is False


def test_root_serves_self_contained_dashboard(running_server: ConsoleServer) -> None:
    connection = http.client.HTTPConnection(
        running_server.server_address[0],
        running_server.server_address[1],
        timeout=1.0,
    )
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        headers = dict(response.getheaders())
    finally:
        connection.close()

    assert response.status == 200
    assert headers["Content-Type"].startswith("text/html")
    for required_markup in (
        'id="mode-badge"',
        'id="leaderboard"',
        'id="leaderboard-body"',
        "n_real",
        "n_shadow",
        'id="shadow-caveat"',
        SHADOW_CAVEAT_TEXT,
        'id="cum-r-sparkline"',
        "<canvas",
        'id="positions"',
        'id="intents"',
        'id="errors"',
    ):
        assert required_markup in body

    assert '<link rel="stylesheet"' not in body
    assert "<script src=" not in body
