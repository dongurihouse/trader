"""Tests for the provider console workbench."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import http.client
import json
from pathlib import Path

import pandas as pd
import pytest

from trader.console.config import ConsoleConfig
from trader.console.server import ConsoleServer, run_server
from trader.console import workbench_provider
from trader.provider.store import write_1d, write_1m_day


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _bar_frame(timestamps: list[datetime], *, base: float = 100.0) -> pd.DataFrame:
    values = [base + position for position in range(len(timestamps))]
    return pd.DataFrame(
        {
            "o": values,
            "h": [value + 1.0 for value in values],
            "l": [value - 1.0 for value in values],
            "c": [value + 0.5 for value in values],
            "v": [100.0 + position for position in range(len(timestamps))],
        },
        index=pd.DatetimeIndex(timestamps, name="t"),
        dtype="float64",
    )


def _session_frame(day: date, count: int, *, base: float) -> pd.DataFrame:
    open_utc = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    return _bar_frame(
        [open_utc + timedelta(minutes=position) for position in range(count)],
        base=base,
    )


def _daily_frame(days: list[date], *, base: float = 90.0) -> pd.DataFrame:
    return _bar_frame(
        [
            datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            for day in days
        ],
        base=base,
    )


def _seed_history(data_root: Path) -> pd.DataFrame:
    prior_day = date(2026, 7, 1)
    current_day = date(2026, 7, 2)
    prior = _session_frame(prior_day, 60, base=90.0)
    current = _session_frame(current_day, 10, base=101.0)
    write_1m_day(data_root, "SNDK", prior_day, prior)
    write_1m_day(data_root, "SNDK", current_day, current)
    write_1d(
        data_root,
        "SNDK",
        _daily_frame([date(2026, 6, 30), prior_day], base=80.0),
    )
    return current


def _handle(params: dict[str, str], data_root: Path) -> tuple[int, dict]:
    return workbench_provider.handle_query(
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


def test_handle_query_returns_successful_1m_bars(tmp_path: Path) -> None:
    current = _seed_history(tmp_path)

    status, payload = _handle(
        {
            "operation": "bars_1m",
            "symbol": "SNDK",
            "asof": "2026-07-02T13:33:00Z",
            "lookback_minutes": "2",
        },
        tmp_path,
    )

    assert status == 200
    assert payload["outcome"] == "ok"
    assert payload["operation"] == "bars_1m"
    assert payload["params"] == {
        "operation": "bars_1m",
        "symbol": "SNDK",
        "asof": "2026-07-02T13:33:00Z",
        "lookback_minutes": 2,
    }
    assert payload["message"] is None
    assert payload["data"] == [
        {
            "ts": "2026-07-02T13:31:00Z",
            "o": 102.0,
            "h": 103.0,
            "l": 101.0,
            "c": 102.5,
            "v": 101.0,
        },
        {
            "ts": "2026-07-02T13:32:00Z",
            "o": 103.0,
            "h": 104.0,
            "l": 102.0,
            "c": 103.5,
            "v": 102.0,
        },
    ]
    assert current.index[-1].isoformat().startswith("2026-07-02T13:39:00")


def test_handle_query_returns_successful_daily_bars(tmp_path: Path) -> None:
    _seed_history(tmp_path)

    status, payload = _handle(
        {
            "operation": "bars_1d",
            "symbol": "SNDK",
            "asof": "2026-07-02",
            "lookback_days": "1",
        },
        tmp_path,
    )

    assert status == 200
    assert payload["outcome"] == "ok"
    assert payload["params"]["lookback_days"] == 1
    assert payload["data"] == [
        {
            "ts": "2026-07-01",
            "o": 81.0,
            "h": 82.0,
            "l": 80.0,
            "c": 81.5,
            "v": 101.0,
        }
    ]


def test_handle_query_labels_lookahead_refusals(tmp_path: Path) -> None:
    _seed_history(tmp_path)

    status, payload = _handle(
        {
            "operation": "bars_1m",
            "symbol": "SNDK",
            "asof": "2026-07-02T13:42:00Z",
        },
        tmp_path,
    )

    assert status == 200
    assert payload["outcome"] == "lookahead_refused"
    assert payload["operation"] == "bars_1m"
    assert payload["data"] is None
    assert "minute data is known only through" in payload["message"]


def test_handle_query_returns_signal_value_with_registry_metadata(
    tmp_path: Path,
) -> None:
    _seed_history(tmp_path)

    status, payload = _handle(
        {
            "operation": "signal",
            "name": "price",
            "asof": "2026-07-02T13:35:00Z",
        },
        tmp_path,
    )

    assert status == 200
    assert payload["outcome"] == "ok"
    assert payload["data"] == {
        "name": "price",
        "value": pytest.approx(105.5),
        "description": "SNDK price at this bar's close",
        "units": "USD",
    }


def test_unknown_signal_returns_400_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trader.provider.market as market_module

    def fail_provider_construction(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unknown signal must not construct ProviderMarketData")

    monkeypatch.setattr(
        market_module, "ProviderMarketData", fail_provider_construction
    )

    status, payload = _handle(
        {
            "operation": "signal",
            "name": "not_registered",
            "asof": "2026-07-02T13:35:00Z",
        },
        tmp_path,
    )

    assert status == 400
    assert payload["outcome"] == "error"
    assert payload["operation"] == "signal"
    assert payload["data"] is None
    assert "unknown signal" in payload["message"]
    assert "not_registered" in payload["message"]


def test_event_without_data_returns_ok_null_data(tmp_path: Path) -> None:
    status, payload = _handle(
        {
            "operation": "event",
            "kind": "implied_move_pct",
            "asof": "2026-08-01T16:00:00Z",
        },
        tmp_path,
    )

    assert status == 200
    assert payload["outcome"] == "ok"
    assert payload["operation"] == "event"
    assert payload["data"] is None
    assert payload["message"] is None


def test_calendar_operations_return_serialized_values(tmp_path: Path) -> None:
    cases = [
        (
            {"calendar_op": "is_session", "day": "2026-07-06"},
            True,
        ),
        (
            {"calendar_op": "prev_session", "day": "2026-07-06"},
            "2026-07-02",
        ),
        (
            {"calendar_op": "session_close", "day": "2026-07-06"},
            "2026-07-06T20:00:00Z",
        ),
    ]

    for params, expected_data in cases:
        status, payload = _handle(
            {"operation": "calendar", **params},
            tmp_path,
        )

        assert status == 200
        assert payload["outcome"] == "ok"
        assert payload["data"] == expected_data
        assert payload["message"] is None


def test_calendar_session_close_value_error_is_structured(tmp_path: Path) -> None:
    status, payload = _handle(
        {
            "operation": "calendar",
            "calendar_op": "session_close",
            "day": "2026-07-04",
        },
        tmp_path,
    )

    assert status == 200
    assert payload["outcome"] == "error"
    assert payload["operation"] == "calendar"
    assert payload["data"] is None
    assert "not a market session" in payload["message"]


def test_missing_required_param_returns_400_with_param_name(tmp_path: Path) -> None:
    status, payload = _handle(
        {"operation": "bars_1m", "asof": "2026-07-02T13:35:00Z"},
        tmp_path,
    )

    assert status == 400
    assert payload["outcome"] == "error"
    assert payload["operation"] == "bars_1m"
    assert payload["data"] is None
    assert "symbol" in payload["message"]


def test_unparseable_asof_returns_400_with_operation(tmp_path: Path) -> None:
    status, payload = _handle(
        {"operation": "signal", "name": "price", "asof": "not-a-timestamp"},
        tmp_path,
    )

    assert status == 400
    assert payload["outcome"] == "error"
    assert payload["operation"] == "signal"
    assert payload["params"] == {
        "operation": "signal",
        "name": "price",
    }
    assert "asof" in payload["message"]


def test_render_provider_workbench_html_is_self_contained() -> None:
    body = workbench_provider.render_provider_workbench_html()

    for required_markup in (
        'href="/"',
        'href="/workbench/provider" aria-current="page"',
        'id="operation"',
        'id="submit"',
        'id="result-table"',
        'id="result-raw"',
        'id="result-outcome"',
        "new URLSearchParams",
        "/api/workbench/provider?",
    ):
        assert required_markup in body

    assert '<link rel="stylesheet"' not in body
    assert "<script src=" not in body

    style_start = body.index("<style>")
    style_end = body.index("</style>", style_start)
    style = body[style_start:style_end]
    assert ".field-group[hidden]" in style
    hidden_rule_start = style.index(".field-group[hidden]")
    hidden_rule_end = style.index("}", hidden_rule_start)
    assert "display: none;" in style[hidden_rule_start:hidden_rule_end]


def test_provider_api_route_serves_handle_query_end_to_end(tmp_path: Path) -> None:
    _seed_history(tmp_path)
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path,
        config_dir=CONFIG_DIR,
    )

    with run_server(config) as server:
        status, body, headers = _get_json(
            server,
            "/api/workbench/provider?"
            "operation=bars_1m&symbol=SNDK&asof=2026-07-02T13%3A32%3A00Z"
            "&lookback_minutes=1",
        )

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body["outcome"] == "ok"
    assert body["operation"] == "bars_1m"
    assert body["data"][0]["ts"] == "2026-07-02T13:31:00Z"


def test_provider_workbench_route_serves_self_contained_html(
    tmp_path: Path,
) -> None:
    config = ConsoleConfig(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path,
        config_dir=CONFIG_DIR,
    )

    with run_server(config) as server:
        status, body, headers = _get_html(server, "/workbench/provider")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert 'href="/workbench/provider" aria-current="page"' in body
    assert 'id="operation"' in body
    assert '<link rel="stylesheet"' not in body
    assert "<script src=" not in body
