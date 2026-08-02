"""HTTP and HTML tests for the console results view."""

from __future__ import annotations

from datetime import date, datetime, timezone
import http.client
import json
from pathlib import Path

import pandas as pd

from trader.console.config import ConsoleConfig
from trader.console.results import build_results_payload, render_results_html
from trader.console.server import ConsoleServer, run_server


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            json.dump(record, handle)
            handle.write("\n")


def _seed_config(config_dir: Path) -> None:
    config_dir.mkdir()
    (config_dir / "trader.yaml").write_text(
        "primary_symbol: SNDK\n",
        encoding="utf-8",
    )


def _seed_session(data_root: Path, session_id: str) -> Path:
    session_dir = data_root / "sessions" / session_id
    _write_jsonl(
        session_dir / "telemetry.jsonl",
        [
            {
                "ev": "session_start",
                "ts": "2026-07-02T13:25:00Z",
                "session": session_id,
                "mode": "backtest",
                "config_sha256": "fixture-config",
                "package_version": "0.5.0",
                "symbols": ["SNDK"],
                "roster": [
                    {"id": "orb5", "status": "emitting"},
                    {"id": "probe_only", "status": "probe"},
                ],
            },
            {
                "ev": "tick",
                "ts": "2026-07-02T13:31:00Z",
                "session": session_id,
                "bar_ts": "2026-07-02T13:30:00Z",
            },
            {
                "ev": "session_end",
                "ts": "2026-07-02T20:01:00Z",
                "session": session_id,
                "bars_processed": 1,
                "real_trades": 0,
                "shadow_trades": 0,
                "final_equity": 100000.0,
            },
        ],
    )
    return session_dir


def _seed_candles(data_root: Path, trading_day: date) -> list[dict]:
    frame = pd.DataFrame(
        {
            "o": [100.0, 101.0],
            "h": [101.0, 102.0],
            "l": [99.0, 100.0],
            "c": [100.5, 101.5],
            "v": [1000.0, 1001.0],
        },
        index=pd.DatetimeIndex(
            [
                datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc),
                datetime(2026, 7, 2, 13, 31, tzinfo=timezone.utc),
            ],
            name="t",
        ),
    )
    bars_dir = data_root / "bars" / "1m" / "SNDK"
    bars_dir.mkdir(parents=True)
    frame.to_parquet(bars_dir / f"{trading_day.isoformat()}.parquet")
    return [
        {
            "ts": "2026-07-02T13:30:00Z",
            "o": 100.0,
            "h": 101.0,
            "l": 99.0,
            "c": 100.5,
            "v": 1000.0,
        },
        {
            "ts": "2026-07-02T13:31:00Z",
            "o": 101.0,
            "h": 102.0,
            "l": 100.0,
            "c": 101.5,
            "v": 1001.0,
        },
    ]


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


def _get_html(server: ConsoleServer, path: str) -> tuple[int, str, dict[str, str]]:
    connection = http.client.HTTPConnection(
        server.server_address[0], server.server_address[1], timeout=1.0
    )
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        return response.status, body, dict(response.getheaders())
    finally:
        connection.close()


def _assert_results_html_is_self_contained(body: str) -> None:
    for required_markup in (
        'href="/"',
        'href="/results" aria-current="page"',
        'id="session-label"',
        'id="algo-filter"',
        'class="algo-pill"',
        'id="days-list"',
        'class="day-card"',
        'id="day-chart"',
        'id="day-trades-table"',
        'id="trade-detail"',
        'id="data-thin-warnings"',
        'id="exec-summary"',
        'id="per-algo-metrics"',
        'id="shadow-caveat"',
        'id="empty-state"',
        'class="price-basis"',
        'price-basis-unrecorded',
        "/api/results",
        "/api/results/day",
        "new URLSearchParams(window.location.search)",
    ):
        assert required_markup in body

    assert '<link rel="stylesheet"' not in body
    assert "<script src=" not in body


def test_render_results_html_is_self_contained() -> None:
    body = render_results_html()

    _assert_results_html_is_self_contained(body)


def test_results_api_returns_built_payload_for_explicit_session(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    config_dir = tmp_path / "config"
    _seed_config(config_dir)
    session_id = "backtest-20260702-093000"
    session_dir = _seed_session(data_root, session_id)
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=data_root,
        config_dir=config_dir,
    )

    with run_server(config) as server:
        status, body, headers = _get_json(server, f"/api/results?session={session_id}")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body == build_results_payload(session_dir)


def test_results_api_returns_404_when_no_sessions_exist(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    config_dir = tmp_path / "config"
    _seed_config(config_dir)
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=data_root,
        config_dir=config_dir,
    )

    with run_server(config) as server:
        status, body, headers = _get_json(server, "/api/results")

    assert status == 404
    assert headers["Content-Type"] == "application/json"
    assert body == {"error": f"no sessions found under {data_root / 'sessions'}"}


def test_results_day_api_returns_seeded_candle_data(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    config_dir = tmp_path / "config"
    _seed_config(config_dir)
    session_id = "backtest-20260702-093000"
    _seed_session(data_root, session_id)
    expected_candles = _seed_candles(data_root, date(2026, 7, 2))
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=data_root,
        config_dir=config_dir,
    )

    with run_server(config) as server:
        status, body, _headers = _get_json(
            server,
            f"/api/results/day?session={session_id}&day=2026-07-02",
        )

    assert status == 200
    assert body == {
        "day": "2026-07-02",
        "symbol": "SNDK",
        "candles": expected_candles,
    }


def test_results_day_api_returns_400_for_malformed_day(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    config_dir = tmp_path / "config"
    _seed_config(config_dir)
    session_id = "backtest-20260702-093000"
    _seed_session(data_root, session_id)
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=data_root,
        config_dir=config_dir,
    )

    with run_server(config) as server:
        status, body, _headers = _get_json(
            server,
            f"/api/results/day?session={session_id}&day=not-a-day",
        )

    assert status == 400
    assert body == {"error": "unparseable day: 'not-a-day'"}


def test_results_route_serves_self_contained_html(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _seed_config(config_dir)
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path / "data",
        config_dir=config_dir,
    )

    with run_server(config) as server:
        status, body, headers = _get_html(server, "/results")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    _assert_results_html_is_self_contained(body)
