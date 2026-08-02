from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

import pandas as pd

from trader.provider import relay
from trader.provider.cli import (
    handle_fetch,
    handle_ingest,
    handle_validate,
    register,
)
from trader.provider.store import read_1m_day, write_1m_day


def test_register_adds_provider_commands_with_handlers_and_defaults() -> None:
    parser = argparse.ArgumentParser(prog="trader")
    subparsers = parser.add_subparsers(dest="command")

    register(subparsers)

    fetch = parser.parse_args(
        ["fetch", "--symbols", "SNDK,SNXX", "--day", "2026-07-01"]
    )
    assert fetch.func is handle_fetch
    assert fetch.data_root == "data"
    assert fetch.calendar_path == "config/calendar.yaml"
    assert fetch.claude_bin == "claude"
    assert fetch.tool == "mcp__robinhood-trading__get_equity_historicals"
    assert fetch.model == "claude-haiku-4-5-20251001"
    assert fetch.tag is None
    assert fetch.interval == "minute"
    assert fetch.bounds == "regular"
    assert fetch.min_intraday_bars == 100

    custom_fetch = parser.parse_args(
        [
            "fetch",
            "--symbols",
            "SNDK",
            "--day",
            "2026-07-01",
            "--min-intraday-bars",
            "42",
        ]
    )
    assert custom_fetch.min_intraday_bars == 42

    ingest = parser.parse_args(["ingest"])
    assert ingest.func is handle_ingest
    assert ingest.data_root == "data"
    assert ingest.raw_root == "data/raw/robinhood"
    assert ingest.etf_leverage_factor == 2.0
    assert ingest.etf_tolerance == 0.25
    assert ingest.bad_tick_neighbor_fraction == 0.05
    assert ingest.max_bad_run_bars == 3
    assert ingest.quarantine_abort_fraction == 0.05

    validate = parser.parse_args(
        [
            "validate",
            "--symbols",
            "SNDK,SNXX",
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-02",
        ]
    )
    assert validate.func is handle_validate
    assert validate.data_root == "data"
    assert validate.etf_leverage_factor == 2.0
    assert validate.etf_tolerance == 0.25
    assert validate.bad_tick_neighbor_fraction == 0.05
    assert validate.max_bad_run_bars == 3
    assert validate.quarantine_abort_fraction == 0.05
    assert validate.underlying_symbol == "SNDK"


def _bar(
    timestamp: str,
    *,
    open_price: float,
    close_price: float,
    high_price: float | None = None,
    low_price: float | None = None,
) -> dict:
    high = max(open_price, close_price) if high_price is None else high_price
    low = min(open_price, close_price) if low_price is None else low_price
    return {
        "begins_at": timestamp,
        "open_price": str(open_price),
        "close_price": str(close_price),
        "high_price": str(high),
        "low_price": str(low),
        "volume": 1000,
        "session": "reg",
    }


def _raw_payload(symbol: str = "SNDK") -> dict:
    return {
        "data": {
            "results": [
                {
                    "symbol": symbol,
                    "interval": "minute",
                    "bounds": "regular",
                    "bars": [
                        _bar(
                            "2026-07-01T13:30:00Z",
                            open_price=100,
                            close_price=100,
                            high_price=101,
                            low_price=99,
                        ),
                        _bar(
                            "2026-07-01T13:31:00Z",
                            open_price=100,
                            close_price=100.5,
                            high_price=101,
                            low_price=99.5,
                        ),
                        _bar(
                            "2026-07-01T13:32:00Z",
                            open_price=100.5,
                            close_price=101,
                            high_price=102,
                            low_price=100,
                        ),
                    ],
                }
            ]
        }
    }


def test_ingest_handler_uses_existing_ingest_pipeline(tmp_path, capsys) -> None:
    raw_root = tmp_path / "raw"
    raw_path = raw_root / "SNDK" / "fixture.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(json.dumps(_raw_payload()))
    data_root = tmp_path / "data"
    args = argparse.Namespace(
        raw_root=str(raw_root),
        data_root=str(data_root),
        etf_leverage_factor=2.0,
        etf_tolerance=0.25,
        bad_tick_neighbor_fraction=0.05,
        max_bad_run_bars=3,
        quarantine_abort_fraction=0.05,
    )

    assert handle_ingest(args) == 0

    stored = read_1m_day(data_root, "SNDK", date(2026, 7, 1))
    assert stored is not None
    assert len(stored) == 3
    output = capsys.readouterr().out
    assert "SNDK: bars=3 days=1 min_date=2026-07-01 max_date=2026-07-01" in output
    assert "quarantined=0" in output
    assert "etf_warnings=0" in output


def _stored_frame(*, bad_high: bool = False, leveraged: bool = False) -> pd.DataFrame:
    if leveraged:
        opens = [50.0, 50.0, 50.25]
        closes = [50.0, 50.25, 50.5]
        highs = [50.5, 50.75, 51.0]
        lows = [49.5, 49.75, 50.0]
    else:
        opens = [100.0, 180.0 if bad_high else 101.0, 102.0]
        closes = [100.0, 180.0 if bad_high else 101.0, 102.0]
        highs = [101.0, 180.5 if bad_high else 102.0, 103.0]
        lows = [99.0, 179.5 if bad_high else 100.5, 101.5]
    return pd.DataFrame(
        {
            "o": opens,
            "h": highs,
            "l": lows,
            "c": closes,
            "v": [1000.0, 1100.0, 1200.0],
        },
        index=pd.DatetimeIndex(
            [
                "2026-07-01T13:30:00Z",
                "2026-07-01T13:31:00Z",
                "2026-07-01T13:32:00Z",
            ],
            name="t",
        ),
    )


def _validate_args(
    data_root: Path,
    *,
    quarantine_abort_fraction: float = 0.05,
) -> argparse.Namespace:
    return argparse.Namespace(
        data_root=str(data_root),
        symbols="SNDK,SNXX",
        start="2026-07-01",
        end="2026-07-01",
        etf_leverage_factor=2.0,
        etf_tolerance=0.25,
        bad_tick_neighbor_fraction=0.05,
        max_bad_run_bars=3,
        quarantine_abort_fraction=quarantine_abort_fraction,
        underlying_symbol="SNDK",
    )


def test_validate_handler_returns_zero_for_clean_stored_day(tmp_path, capsys) -> None:
    day = date(2026, 7, 1)
    write_1m_day(tmp_path, "SNDK", day, _stored_frame())
    matching_etf = _stored_frame(leveraged=True).copy()
    matching_etf.loc[matching_etf.index[-1], "c"] = 52.0
    write_1m_day(tmp_path, "SNXX", day, matching_etf)

    assert handle_validate(_validate_args(tmp_path)) == 0
    assert capsys.readouterr().out == ""


def test_validate_handler_reports_findings_without_writing_store(
    tmp_path, capsys
) -> None:
    day = date(2026, 7, 1)
    write_1m_day(tmp_path, "SNDK", day, _stored_frame(bad_high=True))
    write_1m_day(tmp_path, "SNXX", day, _stored_frame(leveraged=True))
    stored_paths = sorted((tmp_path / "bars" / "1m").rglob("*.parquet"))
    original_bytes = {path: path.read_bytes() for path in stored_paths}

    assert handle_validate(
        _validate_args(tmp_path, quarantine_abort_fraction=0.5)
    ) == 1

    output = capsys.readouterr().out
    assert "2026-07-01 SNDK bad_tick" in output
    assert "field=high value=180.5" in output
    assert "2026-07-01 SNXX leverage_warning" in output
    assert "expected_range=(1.5, 2.5) actual_ratio=0.5" in output
    assert {path: path.read_bytes() for path in stored_paths} == original_bytes


def test_validate_handler_reports_bad_ticks_at_day_edges(tmp_path, capsys) -> None:
    day = date(2026, 7, 1)
    frame = pd.DataFrame(
        {
            "o": [180.0, 100.0, 100.2, 100.1, 100.3, 40.0],
            "h": [180.5, 100.5, 100.7, 100.6, 100.8, 40.5],
            "l": [179.5, 99.5, 99.7, 99.6, 99.8, 39.5],
            "c": [180.0, 100.0, 100.2, 100.1, 100.3, 40.0],
            "v": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
        },
        index=pd.DatetimeIndex(
            [
                "2026-07-01T13:30:00Z",
                "2026-07-01T13:31:00Z",
                "2026-07-01T13:32:00Z",
                "2026-07-01T13:33:00Z",
                "2026-07-01T13:34:00Z",
                "2026-07-01T13:35:00Z",
            ],
            name="t",
        ),
    )
    write_1m_day(tmp_path, "SNDK", day, frame)

    assert handle_validate(
        _validate_args(tmp_path, quarantine_abort_fraction=0.5)
    ) == 1

    assert capsys.readouterr().out.splitlines() == [
        "2026-07-01 SNDK bad_tick "
        "timestamp=2026-07-01T13:30:00+00:00 field=high value=180.5",
        "2026-07-01 SNDK bad_tick "
        "timestamp=2026-07-01T13:35:00+00:00 field=low value=39.5",
    ]


def test_validate_handler_reports_bad_tick_validation_errors(
    tmp_path, capsys
) -> None:
    day = date(2026, 7, 1)
    frame = pd.DataFrame(
        {
            "o": [100.0, 180.0, 180.1, 180.2, 180.3, 100.2],
            "h": [100.5, 180.5, 180.6, 180.7, 180.8, 100.7],
            "l": [99.5, 179.5, 179.6, 179.7, 179.8, 99.7],
            "c": [100.0, 180.0, 180.1, 180.2, 180.3, 100.2],
            "v": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
        },
        index=pd.DatetimeIndex(
            [
                "2026-07-01T13:30:00Z",
                "2026-07-01T13:31:00Z",
                "2026-07-01T13:32:00Z",
                "2026-07-01T13:33:00Z",
                "2026-07-01T13:34:00Z",
                "2026-07-01T13:35:00Z",
            ],
            name="t",
        ),
    )
    write_1m_day(tmp_path, "SNDK", day, frame)

    assert handle_validate(_validate_args(tmp_path)) == 1

    assert capsys.readouterr().out.splitlines() == [
        "2026-07-01 SNDK validation_error "
        "reason=rule 4: candidate run exceeds max_bad_run_bars=3; "
        "span 2026-07-01 13:31:00+00:00 to "
        "2026-07-01 13:34:00+00:00 (4 bars)",
    ]


def test_fetch_handler_saves_validated_raw_payload_without_real_subprocess(
    tmp_path, monkeypatch, capsys
) -> None:
    payload = _raw_payload()
    calls: list[dict] = []

    def fake_run_relay(symbols, **kwargs):
        calls.append({"symbols": symbols, **kwargs})
        return payload

    monkeypatch.setattr(relay, "run_relay", fake_run_relay)
    args = argparse.Namespace(
        symbols="sndk",
        day="2026-07-01",
        data_root=str(tmp_path),
        calendar_path="config/calendar.yaml",
        claude_bin="test-claude",
        tool="mcp__test__bars",
        model="test-model",
        tag=None,
        interval="5minute",
        bounds="regular",
        min_intraday_bars=3,
    )

    assert handle_fetch(args) == 0

    path = (
        tmp_path
        / "raw"
        / "robinhood"
        / "SNDK"
        / "rh-relay-2026-07-01-2026-07-01.json"
    )
    assert json.loads(path.read_text()) == payload
    assert calls == [
        {
            "symbols": ["SNDK"],
            "start_iso": "2026-07-01T13:30:00Z",
            "end_iso": "2026-07-01T20:00:00Z",
            "interval": "5minute",
            "bounds": "regular",
            "tool": "mcp__test__bars",
            "claude_bin": "test-claude",
            "model": "test-model",
        }
    ]
    assert capsys.readouterr().out.strip() == str(path)


def test_fetch_handler_reports_relay_errors_to_stderr(
    tmp_path, monkeypatch, capsys
) -> None:
    def failing_relay(symbols, **kwargs):
        raise relay.RelayError("relay unavailable")

    monkeypatch.setattr(relay, "run_relay", failing_relay)
    args = argparse.Namespace(
        symbols="SNDK",
        day="2026-07-01",
        data_root=str(tmp_path),
        calendar_path="config/calendar.yaml",
        claude_bin="claude",
        tool="mcp__test__bars",
        model="test-model",
        tag="retry",
        interval="minute",
        bounds="regular",
        min_intraday_bars=100,
    )

    assert handle_fetch(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "relay unavailable" in captured.err
    assert not (tmp_path / "raw").exists()
