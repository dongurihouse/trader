from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.provider.calendar import load_calendar
from trader.provider.relay import (
    RelayAuthRequired,
    RelayError,
    build_command,
    run_relay,
    save_raw,
    session_range_iso,
    validate_payload,
)


DEFAULT_TOOL = "mcp__robinhood-trading__get_equity_historicals"


@pytest.mark.parametrize(
    (
        "symbols",
        "start_iso",
        "end_iso",
        "interval",
        "bounds",
        "model",
        "tool",
        "claude_bin",
        "expected_symbols",
    ),
    [
        (
            ["sndk", " SNXX "],
            "2026-07-01T13:30:00Z",
            "2026-07-01T20:00:00Z",
            "minute",
            "regular",
            "claude-haiku-4-5-20251001",
            DEFAULT_TOOL,
            "claude",
            ["SNDK", "SNXX"],
        ),
        (
            ["spy"],
            "2026-11-27T14:30:00Z",
            "2026-11-27T18:00:00Z",
            "5minute",
            "extended",
            "test-model",
            "mcp__example__historicals",
            "/opt/bin/claude",
            ["SPY"],
        ),
    ],
)
def test_build_command_produces_exact_headless_relay_argv(
    symbols,
    start_iso,
    end_iso,
    interval,
    bounds,
    model,
    tool,
    claude_bin,
    expected_symbols,
) -> None:
    args_json = json.dumps(
        {
            "symbols": expected_symbols,
            "start_time": start_iso,
            "end_time": end_iso,
            "interval": interval,
            "bounds": bounds,
        }
    )
    prompt = (
        f"Call the tool {tool} exactly once, with exactly these arguments:\n"
        f"{args_json}\n"
        "Do not change any argument. Make no other tool calls. After the "
        "tool returns, reply with exactly DONE and output no other text."
    )

    assert build_command(
        symbols,
        start_iso,
        end_iso,
        interval,
        bounds,
        model,
        tool=tool,
        claude_bin=claude_bin,
    ) == [
        claude_bin,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        tool,
        "--max-turns",
        "2",
        "--model",
        model,
    ]


def test_session_range_iso_uses_provider_calendar_and_rejects_holiday() -> None:
    calendar = load_calendar(Path("config/calendar.yaml"))

    assert session_range_iso(calendar, date(2026, 7, 1)) == (
        "2026-07-01T13:30:00Z",
        "2026-07-01T20:00:00Z",
    )
    with pytest.raises(RelayError, match="2026-07-03"):
        session_range_iso(calendar, date(2026, 7, 3))


def _payload(*symbols: str) -> dict:
    return {
        "data": {
            "results": [
                {
                    "symbol": symbol,
                    "bars": [
                        {"begins_at": "2026-07-01T13:30:00Z"},
                        {"begins_at": "2026-07-01T20:00:00Z"},
                    ],
                }
                for symbol in symbols
            ]
        }
    }


def _bar_count_payload(
    symbol: str,
    *,
    count: int,
    start_iso: str = "2026-07-01T13:30:00Z",
    end_iso: str = "2026-07-01T20:00:00Z",
) -> dict:
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    if count == 1:
        timestamps = [start]
    else:
        timestamps = [
            start,
            *[start + timedelta(minutes=offset) for offset in range(1, count - 1)],
            end,
        ]
    return {
        "data": {
            "results": [
                {
                    "symbol": symbol,
                    "bars": [
                        {"begins_at": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")}
                        for timestamp in timestamps
                    ],
                }
            ]
        }
    }


def _transcript(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def test_run_relay_returns_matching_tool_result_payload() -> None:
    payload = _payload("SNDK")
    stdout = _transcript(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": DEFAULT_TOOL,
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": json.dumps(payload),
                    }
                ]
            },
        },
    )

    def fake_runner(command, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    assert run_relay(
        ["SNDK"],
        start_iso="2026-07-01T13:30:00Z",
        end_iso="2026-07-01T20:00:00Z",
        runner=fake_runner,
    ) == payload


def test_run_relay_reads_auto_saved_tool_result(tmp_path) -> None:
    payload = _payload("SNDK")
    saved = tmp_path / "relay-result.json"
    saved.write_text(json.dumps(payload))
    stdout = _transcript(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": DEFAULT_TOOL,
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Output has been saved to {saved}.",
                            }
                        ],
                    }
                ]
            },
        },
    )

    def fake_runner(command, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    assert run_relay(
        ["SNDK"],
        start_iso="2026-07-01T13:30:00Z",
        end_iso="2026-07-01T20:00:00Z",
        runner=fake_runner,
    ) == payload


def test_run_relay_rejects_transcript_without_matching_result() -> None:
    stdout = _transcript(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": DEFAULT_TOOL,
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "different-tool",
                        "content": "{}",
                    }
                ]
            },
        },
    )

    def fake_runner(command, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=1)

    with pytest.raises(RelayError, match="never produced a result"):
        run_relay(
            ["SNDK"],
            start_iso="2026-07-01T13:30:00Z",
            end_iso="2026-07-01T20:00:00Z",
            runner=fake_runner,
        )


def test_run_relay_classifies_auth_shaped_failure() -> None:
    stdout = _transcript(
        {
            "type": "result",
            "result": "Tool call failed: unauthorized",
        }
    )

    def fake_runner(command, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=1)

    with pytest.raises(RelayAuthRequired, match="re-authenticate"):
        run_relay(
            ["SNDK"],
            start_iso="2026-07-01T13:30:00Z",
            end_iso="2026-07-01T20:00:00Z",
            runner=fake_runner,
        )


def test_validate_payload_accepts_requested_symbols_with_in_range_bars() -> None:
    calendar = load_calendar(Path("config/calendar.yaml"))

    validate_payload(
        _payload("SNDK", "SNXX"),
        ["sndk", "snxx"],
        "2026-07-01T13:30:00Z",
        "2026-07-01T20:00:00Z",
        calendar=calendar,
        day=date(2026, 7, 1),
        min_intraday_bars=2,
    )


def test_validate_payload_accepts_regular_day_at_min_intraday_bars() -> None:
    calendar = load_calendar(Path("config/calendar.yaml"))

    validate_payload(
        _bar_count_payload("SNDK", count=120),
        ["SNDK"],
        "2026-07-01T13:30:00Z",
        "2026-07-01T20:00:00Z",
        calendar=calendar,
        day=date(2026, 7, 1),
        min_intraday_bars=100,
    )


def test_validate_payload_rejects_regular_day_below_min_intraday_bars() -> None:
    calendar = load_calendar(Path("config/calendar.yaml"))

    with pytest.raises(RelayError, match="SNDK.*99 bars.*minimum 100"):
        validate_payload(
            _bar_count_payload("SNDK", count=99),
            ["SNDK"],
            "2026-07-01T13:30:00Z",
            "2026-07-01T20:00:00Z",
            calendar=calendar,
            day=date(2026, 7, 1),
            min_intraday_bars=100,
        )


def test_validate_payload_rejects_any_requested_symbol_below_minimum() -> None:
    calendar = load_calendar(Path("config/calendar.yaml"))
    payload = _bar_count_payload("SNDK", count=100)
    payload["data"]["results"].append(
        _bar_count_payload("SNXX", count=99)["data"]["results"][0]
    )

    with pytest.raises(RelayError, match="SNXX.*99 bars.*minimum 100"):
        validate_payload(
            payload,
            ["SNDK", "SNXX"],
            "2026-07-01T13:30:00Z",
            "2026-07-01T20:00:00Z",
            calendar=calendar,
            day=date(2026, 7, 1),
            min_intraday_bars=100,
        )


def test_validate_payload_accepts_early_close_payload_above_scaled_minimum() -> None:
    calendar = load_calendar(Path("config/calendar.yaml"))

    validate_payload(
        _bar_count_payload(
            "SNDK",
            count=60,
            start_iso="2026-11-27T14:30:00Z",
            end_iso="2026-11-27T18:00:00Z",
        ),
        ["SNDK"],
        "2026-11-27T14:30:00Z",
        "2026-11-27T18:00:00Z",
        calendar=calendar,
        day=date(2026, 11, 27),
        min_intraday_bars=100,
    )


def test_validate_payload_rejects_early_close_payload_below_scaled_minimum() -> None:
    calendar = load_calendar(Path("config/calendar.yaml"))

    with pytest.raises(RelayError, match="SNDK.*53 bars.*minimum 54"):
        validate_payload(
            _bar_count_payload(
                "SNDK",
                count=53,
                start_iso="2026-11-27T14:30:00Z",
                end_iso="2026-11-27T18:00:00Z",
            ),
            ["SNDK"],
            "2026-11-27T14:30:00Z",
            "2026-11-27T18:00:00Z",
            calendar=calendar,
            day=date(2026, 11, 27),
            min_intraday_bars=100,
        )


@pytest.mark.parametrize(
    ("payload", "symbols", "message"),
    [
        ({"data": {"results": []}}, ["SNDK"], "no data.results"),
        (_payload("SNDK"), ["SNDK", "SNXX"], "SNXX"),
        (
            {
                "data": {
                    "results": [{"symbol": "SNDK", "bars": []}]
                }
            },
            ["SNDK"],
            "has no bars",
        ),
        (
            {
                "data": {
                    "results": [
                        {
                            "symbol": "SNDK",
                            "bars": [
                                {"begins_at": "2026-07-01T13:29:00Z"},
                                {"begins_at": "2026-07-01T20:00:00Z"},
                            ],
                        }
                    ]
                }
            },
            ["SNDK"],
            "outside",
        ),
    ],
)
def test_validate_payload_rejects_incomplete_or_out_of_range_data(
    payload, symbols, message
) -> None:
    with pytest.raises(RelayError, match=message):
        calendar = load_calendar(Path("config/calendar.yaml"))
        validate_payload(
            payload,
            symbols,
            "2026-07-01T13:30:00Z",
            "2026-07-01T20:00:00Z",
            calendar=calendar,
            day=date(2026, 7, 1),
            min_intraday_bars=2,
        )


def test_save_raw_writes_documented_multi_symbol_path(tmp_path) -> None:
    payload = _payload("SNXX", "SNDK")

    path = save_raw(
        payload,
        tmp_path / "raw" / "robinhood",
        date(2026, 7, 1),
        "retry-2",
    )

    assert path == (
        tmp_path
        / "raw"
        / "robinhood"
        / "MULTI"
        / "rh-relay-2026-07-01-retry-2.json"
    )
    assert json.loads(path.read_text()) == payload
    assert path.read_text().startswith('{\n "data"')
