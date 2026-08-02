"""Tests for the execution console workbench."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import http.client
import json
from pathlib import Path

import pandas as pd
import pytest

from trader.console.config import ConsoleConfig
from trader.console.server import ConsoleServer, run_server
from trader.console import workbench_execution
from trader.provider.store import write_1m_day


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _minute_frame(
    day: date,
    closes: list[float],
    *,
    first_open: float | None = None,
) -> pd.DataFrame:
    start = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    opens = [first_open if first_open is not None else closes[0], *closes[:-1]]
    return pd.DataFrame(
        {
            "o": opens,
            "h": [max(open_px, close) + 0.25 for open_px, close in zip(opens, closes)],
            "l": [min(open_px, close) - 0.25 for open_px, close in zip(opens, closes)],
            "c": closes,
            "v": [100.0 + offset for offset in range(len(closes))],
        },
        index=pd.DatetimeIndex(
            [start + timedelta(minutes=offset) for offset in range(len(closes))],
            name="t",
        ),
        dtype="float64",
    )


def _seed_reference_bar(data_root: Path) -> None:
    write_1m_day(
        data_root,
        "SNXX",
        date(2026, 7, 2),
        _minute_frame(date(2026, 7, 2), [100.0], first_open=100.0),
    )


def _base_params(**overrides: str) -> dict[str, str]:
    params = {
        "algo_id": "workbench-manual",
        "side": "long",
        "instrument": "SNXX",
        "ts": "2026-07-02T13:31:00Z",
        "stop": "95",
        "target": "110",
    }
    params.update(overrides)
    return params


def _handle(params: dict[str, str], data_root: Path) -> tuple[int, dict]:
    return workbench_execution.handle_query(
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


def test_handle_query_accepts_and_returns_real_sizing_arithmetic(
    tmp_path: Path,
) -> None:
    _seed_reference_bar(tmp_path)
    before_files = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    status, payload = _handle(_base_params(confidence="0.7"), tmp_path)

    assert status == 200
    assert payload["outcome"] == "ok"
    assert payload["operation"] == "check_and_size"
    assert payload["message"] is None
    assert payload["params"] == {
        "algo_id": "workbench-manual",
        "action": "open",
        "side": "long",
        "signal_symbol": "SNDK",
        "instrument": "SNXX",
        "ts": "2026-07-02T13:31:00Z",
        "entry": "market_next_open",
        "stop": 95.0,
        "target": 110.0,
        "confidence": 0.7,
        "reason": "manual workbench entry",
        "meta": {},
        "equity": 10000.0,
        "capital_fraction": 1.0,
        "day_slots": 2,
        "entries_today": 0,
        "positions": [],
        "muted_until": None,
        "realized_r_today": 0.0,
    }
    assert payload["data"] == {
        "decision": "accepted",
        "ticket": {
            "ticket_id": "workbench-manual-20260702T133100Z-0",
            "algo_id": "workbench-manual",
            "instrument": "SNXX",
            "side": "long",
            "shares": 50,
            "entry": "market_next_open",
            "stop": 95.0,
            "target": 110.0,
            "risk": {"slot": 1, "dollars": 250.0, "equity": 10000},
        },
        "rejection": None,
        "sizing_context": {
            "equity": 10000.0,
            "capital_fraction": 1.0,
            "day_slots": 2,
            "slot_capital": 5000.0,
        },
    }
    after_files = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert after_files == before_files


def test_handle_query_rejects_with_specific_risk_rail(tmp_path: Path) -> None:
    _seed_reference_bar(tmp_path)

    status, payload = _handle(_base_params(stop="110", target="105"), tmp_path)

    assert status == 200
    assert payload["outcome"] == "ok"
    assert payload["data"]["decision"] == "rejected"
    assert payload["data"]["ticket"] is None
    assert payload["data"]["rejection"]["rule"] == "degenerate_bracket"
    assert "instrument bracket has no tradeable stop/target spread" in (
        payload["data"]["rejection"]["detail"]
    )
    assert payload["data"]["sizing_context"] == {
        "equity": 10000.0,
        "capital_fraction": 1.0,
        "day_slots": 2,
        "slot_capital": 5000.0,
    }


def test_positions_param_can_trigger_one_position_rail(tmp_path: Path) -> None:
    _seed_reference_bar(tmp_path)

    status, payload = _handle(_base_params(positions="SNXX"), tmp_path)

    assert status == 200
    assert payload["outcome"] == "ok"
    assert payload["params"]["positions"] == ["SNXX"]
    assert payload["data"]["decision"] == "rejected"
    assert payload["data"]["rejection"] == {
        "rule": "one_position_at_a_time",
        "detail": "portfolio already has an open position",
    }


def test_handle_query_labels_lookahead_refusals(tmp_path: Path) -> None:
    _seed_reference_bar(tmp_path)

    status, payload = _handle(_base_params(ts="2026-07-02T13:32:00Z"), tmp_path)

    assert status == 200
    assert payload["outcome"] == "lookahead_refused"
    assert payload["operation"] == "check_and_size"
    assert payload["data"] is None
    assert "minute data is known only through" in payload["message"]


@pytest.mark.parametrize("missing", ["side", "instrument", "ts", "stop", "target"])
def test_missing_required_params_return_400_with_param_name(
    tmp_path: Path,
    missing: str,
) -> None:
    params = _base_params()
    params.pop(missing)

    status, payload = _handle(params, tmp_path)

    assert status == 400
    assert payload["outcome"] == "error"
    assert payload["operation"] == "check_and_size"
    assert payload["data"] is None
    assert missing in payload["message"]


def test_invalid_side_returns_400(tmp_path: Path) -> None:
    status, payload = _handle(_base_params(side="flat"), tmp_path)

    assert status == 400
    assert payload["outcome"] == "error"
    assert payload["data"] is None
    assert "side" in payload["message"]
    assert "long" in payload["message"]
    assert "short" in payload["message"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("ts", "not-a-timestamp"), ("stop", "not-a-number")],
)
def test_unparseable_required_params_return_400(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    status, payload = _handle(_base_params(**{field: value}), tmp_path)

    assert status == 400
    assert payload["outcome"] == "error"
    assert payload["data"] is None
    assert field in payload["message"]


def test_render_execution_workbench_html_is_self_contained() -> None:
    body = workbench_execution.render_execution_workbench_html()

    for required_markup in (
        'href="/"',
        'href="/workbench/execution" aria-current="page"',
        'id="side"',
        'id="instrument"',
        'id="stop"',
        'id="target"',
        'id="submit"',
        'id="result-outcome"',
        'id="result-accepted"',
        'id="result-rejected"',
        'id="result-raw"',
        "Entry: market_next_open",
        "new URLSearchParams",
        "/api/workbench/execution?",
    ):
        assert required_markup in body

    assert '<link rel="stylesheet"' not in body
    assert "<script src=" not in body


def test_execution_api_route_serves_handle_query_end_to_end(tmp_path: Path) -> None:
    _seed_reference_bar(tmp_path)
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path,
        config_dir=CONFIG_DIR,
    )

    with run_server(config) as server:
        status, body, headers = _get_json(
            server,
            "/api/workbench/execution?"
            "side=long&instrument=SNXX&ts=2026-07-02T13%3A31%3A00Z"
            "&stop=95&target=110",
        )

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body["outcome"] == "ok"
    assert body["operation"] == "check_and_size"
    assert body["data"]["decision"] == "accepted"
    assert body["data"]["ticket"]["shares"] == 50


def test_execution_workbench_route_serves_self_contained_html(tmp_path: Path) -> None:
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path,
        config_dir=CONFIG_DIR,
    )

    with run_server(config) as server:
        status, body, headers = _get_html(server, "/workbench/execution")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert 'href="/workbench/execution" aria-current="page"' in body
    assert 'id="side"' in body
    assert '<link rel="stylesheet"' not in body
    assert "<script src=" not in body
