"""Tests for the algos console workbench."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest

from trader.console.config import ConsoleConfig
from trader.console.server import ConsoleServer, run_server
from trader.console import workbench_algos
from trader.provider.store import write_1d, write_1m_day


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
PEER_SYMBOLS = ("MU", "SOXX", "SPY", "QQQ")


def _minute_frame(
    day: date,
    closes: list[float],
    *,
    first_open: float | None = None,
    high_offset: float = 0.2,
    low_offset: float = 0.2,
    volume: float = 100.0,
) -> pd.DataFrame:
    start = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    opens = [first_open if first_open is not None else closes[0], *closes[:-1]]
    return pd.DataFrame(
        {
            "o": opens,
            "h": [
                max(open_px, close) + high_offset
                for open_px, close in zip(opens, closes)
            ],
            "l": [
                min(open_px, close) - low_offset
                for open_px, close in zip(opens, closes)
            ],
            "c": closes,
            "v": [volume] * len(closes),
        },
        index=pd.DatetimeIndex(
            [start + timedelta(minutes=offset) for offset in range(len(closes))],
            name="t",
        ),
        dtype="float64",
    )


def _daily_frame(values: dict[date, float]) -> pd.DataFrame:
    rows = [
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        for day in values
    ]
    closes = list(values.values())
    return pd.DataFrame(
        {
            "o": closes,
            "h": [close + 1.0 for close in closes],
            "l": [close - 1.0 for close in closes],
            "c": closes,
            "v": [1_000.0] * len(closes),
        },
        index=pd.DatetimeIndex(rows, name="t"),
        dtype="float64",
    )


def _seed_translation_dailies(data_root: Path, prior_day: date) -> None:
    write_1d(data_root, "SNDK", _daily_frame({prior_day: 98.0}))
    write_1d(data_root, "SNXX", _daily_frame({prior_day: 49.0}))
    write_1d(data_root, "SNDQ", _daily_frame({prior_day: 51.0}))


def _seed_prior_context(
    data_root: Path,
    *,
    current_day: date,
    current: pd.DataFrame,
    prior: pd.DataFrame | None = None,
    peer_bars: pd.DataFrame | None = None,
) -> None:
    prior_day = current_day - timedelta(days=1)
    if prior is None:
        prior = _minute_frame(
            prior_day,
            [98.0] * 60,
            first_open=98.0,
            high_offset=2.0,
            low_offset=2.0,
            volume=100.0,
        )
    write_1m_day(data_root, "SNDK", prior_day, prior)
    write_1m_day(data_root, "SNDK", current_day, current)
    _seed_translation_dailies(data_root, prior_day)

    if peer_bars is not None:
        for symbol in PEER_SYMBOLS:
            write_1m_day(data_root, symbol, current_day, peer_bars)


def _seed_orb5_breakout_day(data_root: Path) -> date:
    current_day = date(2026, 7, 2)
    current = _minute_frame(
        current_day,
        [105.0, 105.1, 105.2, 105.3, 105.4, 106.0],
        first_open=105.0,
        volume=200.0,
    )
    peer_bars = _minute_frame(
        current_day,
        [50.1, 50.2, 50.3, 50.4, 50.5, 50.6],
        first_open=50.0,
        high_offset=0.1,
        low_offset=0.1,
        volume=100.0,
    )
    _seed_prior_context(
        data_root,
        current_day=current_day,
        current=current,
        peer_bars=peer_bars,
    )
    return current_day


def _seed_no_candidate_day(data_root: Path) -> date:
    current_day = date(2026, 7, 2)
    current = _minute_frame(
        current_day,
        [101.0] * 390,
        first_open=101.0,
        volume=100.0,
    )
    _seed_prior_context(data_root, current_day=current_day, current=current)
    return current_day


def _handle(params: dict[str, str], data_root: Path) -> tuple[int, dict]:
    return workbench_algos.handle_query(
        params,
        data_root=data_root,
        config_dir=CONFIG_DIR,
    )


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


def test_handle_query_runs_real_roster_algo_and_returns_rule_trace_and_bracket(
    tmp_path: Path,
) -> None:
    current_day = _seed_orb5_breakout_day(tmp_path)

    status, payload = _handle(
        {"algo_id": "orb5", "day": current_day.isoformat()},
        tmp_path,
    )

    assert status == 200
    assert payload["outcome"] == "ok"
    assert payload["operation"] == "run_algo"
    assert payload["params"] == {"algo_id": "orb5", "day": "2026-07-02"}
    assert payload["message"] is None
    assert payload["data"]["algo_id"] == "orb5"
    assert payload["data"]["status"] == "emitting"
    assert payload["data"]["day"] == "2026-07-02"
    assert len(payload["data"]["candidates"]) == 1

    candidate = payload["data"]["candidates"][0]
    assert candidate["ts"] == "2026-07-02T13:36:00Z"
    assert candidate["side"] == "long"
    assert candidate["action"] == "open"
    assert candidate["bracket"] == {
        "instrument": "SNXX",
        "entry": "market_next_open",
        "stop": 55.8,
        "target": 59.4,
    }
    assert candidate["confidence"] == 0.65
    assert "orb5 long candidate fired" in candidate["reason"]
    assert candidate["rule_trace"] == {
        "setup_id": "orb5",
        "rules_version": "v1.6-trader",
        "rules_fired": ["gate-in-play", "dir-gap-go-long", "conf-tape-agree"],
        "direction_votes": ["dir-gap-go-long"],
        "gates_pass": True,
        "vetoed": None,
        "uncalibrated": True,
    }


def test_handle_query_returns_ok_with_empty_candidates_when_algo_never_fires(
    tmp_path: Path,
) -> None:
    current_day = _seed_no_candidate_day(tmp_path)

    status, payload = _handle(
        {"algo_id": "orb5", "day": current_day.isoformat()},
        tmp_path,
    )

    assert status == 200
    assert payload["outcome"] == "ok"
    assert payload["message"] is None
    assert payload["data"] == {
        "algo_id": "orb5",
        "status": "emitting",
        "day": "2026-07-02",
        "candidates": [],
    }


def test_handle_query_returns_day_skipped_when_previous_session_has_no_data(
    tmp_path: Path,
) -> None:
    current_day = date(2026, 7, 2)
    current = _minute_frame(current_day, [105.0] * 6, first_open=105.0)
    write_1m_day(tmp_path, "SNDK", current_day, current)
    _seed_translation_dailies(tmp_path, current_day - timedelta(days=1))

    status, payload = _handle(
        {"algo_id": "orb5", "day": current_day.isoformat()},
        tmp_path,
    )

    assert status == 200
    assert payload["outcome"] == "day_skipped"
    assert payload["operation"] == "run_algo"
    assert payload["data"] is None
    assert "2026-07-02 has no previous trading session with data" in payload["message"]
    assert "reason: no_prev_session" in payload["message"]


def test_unknown_algo_returns_400_without_importing_trader_algos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import_module = workbench_algos.importlib.import_module

    def guarded_import(name: str, package: str | None = None) -> ModuleType:
        if name.startswith("trader.algos"):
            raise AssertionError("unknown algo must not import trader.algos")
        return real_import_module(name, package)

    monkeypatch.setattr(workbench_algos.importlib, "import_module", guarded_import)

    status, payload = _handle(
        {"algo_id": "not_rostered", "day": "2026-07-02"},
        tmp_path,
    )

    assert status == 400
    assert payload["outcome"] == "error"
    assert payload["operation"] == "run_algo"
    assert payload["data"] is None
    assert "unknown algo_id" in payload["message"]
    assert "not_rostered" in payload["message"]
    assert "orb5" in payload["message"]


def test_missing_required_param_returns_400_with_param_name(tmp_path: Path) -> None:
    status, payload = _handle({"algo_id": "orb5"}, tmp_path)

    assert status == 400
    assert payload["outcome"] == "error"
    assert payload["operation"] == "run_algo"
    assert payload["data"] is None
    assert "day" in payload["message"]


def test_render_algos_workbench_html_is_self_contained() -> None:
    body = workbench_algos.render_algos_workbench_html(config_dir=CONFIG_DIR)

    for required_markup in (
        'href="/"',
        'href="/workbench/algos" aria-current="page"',
        'id="algo-id"',
        "orb5 (emitting)",
        'id="day"',
        'id="submit"',
        'id="result-table"',
        'id="result-raw"',
        'id="result-outcome"',
        "new URLSearchParams",
        "/api/workbench/algos?",
    ):
        assert required_markup in body

    assert '<link rel="stylesheet"' not in body
    assert "<script src=" not in body


def test_algos_api_route_serves_handle_query_end_to_end(tmp_path: Path) -> None:
    current_day = _seed_orb5_breakout_day(tmp_path)
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path,
        config_dir=CONFIG_DIR,
    )

    with run_server(config) as server:
        status, body, headers = _get_json(
            server,
            f"/api/workbench/algos?algo_id=orb5&day={current_day.isoformat()}",
        )

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body["outcome"] == "ok"
    assert body["operation"] == "run_algo"
    assert body["data"]["candidates"][0]["rule_trace"]["setup_id"] == "orb5"


def test_algos_workbench_route_serves_self_contained_html(tmp_path: Path) -> None:
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path,
        config_dir=CONFIG_DIR,
    )

    with run_server(config) as server:
        status, body, headers = _get_html(server, "/workbench/algos")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert 'href="/workbench/algos" aria-current="page"' in body
    assert 'id="algo-id"' in body
    assert "orb5 (emitting)" in body
    assert '<link rel="stylesheet"' not in body
    assert "<script src=" not in body
