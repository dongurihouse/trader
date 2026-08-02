"""Tests for post-session console results payloads."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from trader.console.dashboard import SHADOW_CAVEAT_TEXT
from trader.provider.store import write_1m_day


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            json.dump(record, handle)
            handle.write("\n")


def _intent(
    *,
    ts: str,
    algo_id: str,
    side: str,
    instrument: str,
    stop: float,
    target: float,
    confidence: float,
    reason: str,
    meta: dict,
) -> dict:
    return {
        "ev": "intent",
        "ts": ts,
        "session": "paper-20260701-000000",
        "algo_id": algo_id,
        "action": "open",
        "side": side,
        "signal_symbol": "SNDK",
        "instrument": instrument,
        "entry": "market_next_open",
        "stop": stop,
        "target": target,
        "confidence": confidence,
        "reason": reason,
        "meta": meta,
    }


def _ticket(
    *,
    ts: str,
    ticket_id: str,
    algo_id: str,
    intent_ts: str,
    instrument: str,
    side: str,
    stop: float,
    target: float,
) -> dict:
    return {
        "ev": "ticket",
        "ts": ts,
        "session": "paper-20260701-000000",
        "ticket_id": ticket_id,
        "algo_id": algo_id,
        "intent_ts": intent_ts,
        "instrument": instrument,
        "side": side,
        "shares": 10,
        "entry": "market_next_open",
        "stop": stop,
        "target": target,
        "risk": {"slot": 1, "dollars": 10.0},
        "created_ts": ts,
    }


def _fill(
    *,
    ts: str,
    ticket_id: str,
    price: float,
    kind: str,
    book: str,
    price_basis: str | None = None,
    tag: str | None = None,
) -> dict:
    record = {
        "ev": "fill",
        "ts": ts,
        "session": "paper-20260701-000000",
        "ticket_id": ticket_id,
        "price": price,
        "shares": 10,
        "kind": kind,
        "book": book,
        "tag": tag,
    }
    if price_basis is not None:
        record["price_basis"] = price_basis
    return record


def _closed(
    *,
    ts: str,
    algo_id: str,
    instrument: str,
    r_multiple: float,
    book: str,
    exit_kind: str,
    tag: str | None = None,
) -> dict:
    return {
        "ev": "position_closed",
        "ts": ts,
        "session": "paper-20260701-000000",
        "algo_id": algo_id,
        "instrument": instrument,
        "r_multiple": r_multiple,
        "book": book,
        "exit_kind": exit_kind,
        "tag": tag,
    }


def _metrics(
    *,
    ts: str,
    algo_id: str,
    book: str,
    status: str,
    n_real: int,
    n_shadow: int,
    wins: int,
    win_rate: float | None,
    mean_r: float | None,
    expectancy_r: float | None,
    profit_factor: float | None,
    max_drawdown_r: float | None,
    cum_r: float,
) -> dict:
    return {
        "ev": "metrics",
        "ts": ts,
        "session": "paper-20260701-000000",
        "book": book,
        "algo_id": algo_id,
        "status": status,
        "n_real": n_real,
        "n_shadow": n_shadow,
        "wins": wins,
        "win_rate": win_rate,
        "mean_r": mean_r,
        "expectancy_r": expectancy_r,
        "profit_factor": profit_factor,
        "max_drawdown_r": max_drawdown_r,
        "cum_r": cum_r,
        "updated_ts": ts,
    }


def _rich_telemetry() -> list[dict]:
    return [
        {
            "ev": "session_start",
            "ts": "2026-07-01T13:25:00Z",
            "session": "paper-20260701-000000",
            "mode": "paper",
            "config_sha256": "fixture-config",
            "package_version": "0.5.0",
            "symbols": ["SNDK"],
            "roster": [
                {"id": "orb5", "status": "emitting"},
                {"id": "gap", "status": "emitting"},
                {"id": "flat", "status": "disabled"},
            ],
        },
        {
            "ev": "tick",
            "ts": "2026-07-01T13:31:00Z",
            "session": "paper-20260701-000000",
            "bar_ts": "2026-07-01T13:30:00Z",
        },
        _intent(
            ts="2026-07-01T13:31:00Z",
            algo_id="orb5",
            side="long",
            instrument="SNDK",
            stop=99.0,
            target=104.0,
            confidence=0.82,
            reason="opening range broke upward",
            meta={
                "setup_id": "opening-range",
                "rules_version": "rules-v5",
                "rules_fired": ["orb_break", "volume_confirm"],
                "direction_votes": ["long:orb_break"],
                "gates_pass": True,
                "vetoed": False,
                "uncalibrated": False,
            },
        ),
        _ticket(
            ts="2026-07-01T13:32:00Z",
            ticket_id="orb5-ticket-1",
            algo_id="orb5",
            intent_ts="2026-07-01T13:31:00Z",
            instrument="SNDK",
            side="long",
            stop=99.0,
            target=104.0,
        ),
        _fill(
            ts="2026-07-01T13:33:00Z",
            ticket_id="orb5-ticket-1",
            price=100.0,
            kind="entry",
            book="real",
        ),
        _fill(
            ts="2026-07-01T14:05:00Z",
            ticket_id="orb5-ticket-1",
            price=103.0,
            kind="target",
            book="real",
        ),
        _closed(
            ts="2026-07-01T14:05:00Z",
            algo_id="orb5",
            instrument="SNDK",
            r_multiple=1.5,
            book="real",
            exit_kind="target",
        ),
        _metrics(
            ts="2026-07-01T14:05:00Z",
            algo_id="orb5",
            book="real",
            status="emitting",
            n_real=1,
            n_shadow=0,
            wins=1,
            win_rate=1.0,
            mean_r=1.5,
            expectancy_r=1.5,
            profit_factor=None,
            max_drawdown_r=0.0,
            cum_r=1.5,
        ),
        _metrics(
            ts="2026-07-01T14:05:00Z",
            algo_id="orb5",
            book="shadow",
            status="emitting",
            n_real=1,
            n_shadow=0,
            wins=0,
            win_rate=None,
            mean_r=None,
            expectancy_r=None,
            profit_factor=None,
            max_drawdown_r=None,
            cum_r=0.0,
        ),
        _intent(
            ts="2026-07-01T15:00:00Z",
            algo_id="orb5",
            side="short",
            instrument="SNDK",
            stop=102.0,
            target=98.0,
            confidence=0.41,
            reason="gate refused shadow candidate",
            meta={
                "setup_id": "opening-range",
                "rules_version": "rules-v5",
                "rules_fired": ["fade_attempt"],
                "direction_votes": ["short:fade_attempt"],
                "gates_pass": False,
                "vetoed": True,
                "uncalibrated": False,
            },
        ),
        _fill(
            ts="2026-07-01T15:01:00Z",
            ticket_id="shadow-orb5-20260701T150000Z-0",
            price=101.0,
            kind="entry",
            book="shadow",
            tag="gate_refused",
        ),
        _fill(
            ts="2026-07-01T15:30:00Z",
            ticket_id="shadow-orb5-20260701T150000Z-0",
            price=101.5,
            kind="stop",
            book="shadow",
            tag="gate_refused",
        ),
        _closed(
            ts="2026-07-01T15:30:00Z",
            algo_id="orb5",
            instrument="SNDK",
            r_multiple=-0.5,
            book="shadow",
            exit_kind="stop",
            tag="gate_refused",
        ),
        _metrics(
            ts="2026-07-01T15:30:00Z",
            algo_id="orb5",
            book="real",
            status="emitting",
            n_real=1,
            n_shadow=1,
            wins=1,
            win_rate=1.0,
            mean_r=1.5,
            expectancy_r=1.5,
            profit_factor=None,
            max_drawdown_r=0.0,
            cum_r=1.5,
        ),
        _metrics(
            ts="2026-07-01T15:30:00Z",
            algo_id="orb5",
            book="shadow",
            status="emitting",
            n_real=1,
            n_shadow=1,
            wins=0,
            win_rate=0.0,
            mean_r=-0.5,
            expectancy_r=-0.5,
            profit_factor=0.0,
            max_drawdown_r=0.5,
            cum_r=-0.5,
        ),
        {
            "ev": "tick",
            "ts": "2026-07-02T13:31:00Z",
            "session": "paper-20260701-000000",
            "bar_ts": "2026-07-02T13:30:00Z",
        },
        _intent(
            ts="2026-07-02T13:35:00Z",
            algo_id="gap",
            side="long",
            instrument="SNXX",
            stop=50.0,
            target=54.0,
            confidence=0.64,
            reason="gap continuation",
            meta={
                "setup_id": "gap-play",
                "rules_version": "rules-v5",
                "rules_fired": ["gap_up"],
                "direction_votes": ["long:gap_up"],
                "gates_pass": True,
                "vetoed": False,
                "uncalibrated": True,
            },
        ),
        _ticket(
            ts="2026-07-02T13:36:00Z",
            ticket_id="gap-ticket-1",
            algo_id="gap",
            intent_ts="2026-07-02T13:35:00Z",
            instrument="SNXX",
            side="long",
            stop=50.0,
            target=54.0,
        ),
        _fill(
            ts="2026-07-02T13:37:00Z",
            ticket_id="gap-ticket-1",
            price=51.0,
            kind="entry",
            book="real",
        ),
        _fill(
            ts="2026-07-02T14:10:00Z",
            ticket_id="gap-ticket-1",
            price=50.0,
            kind="stop",
            book="real",
        ),
        _closed(
            ts="2026-07-02T14:10:00Z",
            algo_id="gap",
            instrument="SNXX",
            r_multiple=-1.0,
            book="real",
            exit_kind="stop",
        ),
        _metrics(
            ts="2026-07-02T14:10:00Z",
            algo_id="gap",
            book="real",
            status="emitting",
            n_real=1,
            n_shadow=0,
            wins=0,
            win_rate=0.0,
            mean_r=-1.0,
            expectancy_r=-1.0,
            profit_factor=0.0,
            max_drawdown_r=1.0,
            cum_r=-1.0,
        ),
        _metrics(
            ts="2026-07-02T14:10:00Z",
            algo_id="gap",
            book="shadow",
            status="emitting",
            n_real=1,
            n_shadow=0,
            wins=0,
            win_rate=None,
            mean_r=None,
            expectancy_r=None,
            profit_factor=None,
            max_drawdown_r=None,
            cum_r=0.0,
        ),
        _intent(
            ts="2026-07-02T15:00:00Z",
            algo_id="orb5",
            side="long",
            instrument="SNDK",
            stop=101.0,
            target=106.0,
            confidence=0.71,
            reason="unfinished live entry",
            meta={
                "setup_id": "opening-range",
                "rules_version": "rules-v5",
                "rules_fired": ["late_break"],
                "direction_votes": ["long:late_break"],
                "gates_pass": True,
                "vetoed": False,
                "uncalibrated": False,
            },
        ),
        _ticket(
            ts="2026-07-02T15:01:00Z",
            ticket_id="orb5-open-ticket",
            algo_id="orb5",
            intent_ts="2026-07-02T15:00:00Z",
            instrument="SNDK",
            side="long",
            stop=101.0,
            target=106.0,
        ),
        _fill(
            ts="2026-07-02T15:02:00Z",
            ticket_id="orb5-open-ticket",
            price=102.0,
            kind="entry",
            book="real",
        ),
        {
            "ev": "day_skipped",
            "ts": "2026-07-03T13:25:00Z",
            "session": "paper-20260701-000000",
            "day": "2026-07-03",
            "reason": "market_closed",
        },
        {
            "ev": "session_end",
            "ts": "2026-07-02T20:01:00Z",
            "session": "paper-20260701-000000",
            "bars_processed": 2,
            "real_trades": 2,
            "shadow_trades": 1,
            "final_equity": 100250.0,
        },
    ]


def test_build_results_payload_joins_real_trades_and_summarizes_session(
    tmp_path: Path,
) -> None:
    from trader.console.results import build_results_payload

    session_dir = tmp_path / "paper-20260701-000000"
    _write_jsonl(session_dir / "telemetry.jsonl", _rich_telemetry())

    payload = build_results_payload(session_dir)

    assert payload["session"] == {
        "id": "paper-20260701-000000",
        "mode": "paper",
        "config_sha256": "fixture-config",
        "package_version": "0.5.0",
        "symbols": ["SNDK"],
    }
    assert payload["algos"] == [
        {"id": "orb5", "status": "emitting"},
        {"id": "gap", "status": "emitting"},
        {"id": "flat", "status": "disabled"},
    ]
    assert payload["shadow_caveat"] == SHADOW_CAVEAT_TEXT
    assert payload["days"] == [
        {"day": "2026-07-01", "status": "processed", "reason": None},
        {"day": "2026-07-02", "status": "processed", "reason": None},
        {"day": "2026-07-03", "status": "skipped", "reason": "market_closed"},
    ]
    assert payload["executive_summary"] == {
        "window_start": "2026-07-01",
        "window_end": "2026-07-03",
        "days_count": 2,
        "days_skipped_count": 1,
        "trades": 2,
        "win_rate": 0.5,
        "mean_r": 0.25,
        "profit_factor": 1.5,
        "cum_r": 0.5,
        "max_drawdown_r": 1.0,
        "final_equity": 100250.0,
    }
    assert [trade["id"] for trade in payload["trades"]] == [
        "orb5-ticket-1",
        "gap-ticket-1",
    ]
    assert payload["trades"][0] == {
        "id": "orb5-ticket-1",
        "algo_id": "orb5",
        "side": "long",
        "instrument": "SNDK",
        "day": "2026-07-01",
        "entry_ts": "2026-07-01T13:33:00Z",
        "entry_price": 100.0,
        "entry_price_basis": None,
        "stop": 99.0,
        "target": 104.0,
        "exit_ts": "2026-07-01T14:05:00Z",
        "exit_price": 103.0,
        "exit_price_basis": None,
        "exit_kind": "target",
        "r_multiple": 1.5,
        "confidence": 0.82,
        "reason": "opening range broke upward",
        "rule_trace": {
            "setup_id": "opening-range",
            "rules_version": "rules-v5",
            "rules_fired": ["orb_break", "volume_confirm"],
            "direction_votes": ["long:orb_break"],
            "gates_pass": True,
            "vetoed": False,
            "uncalibrated": False,
        },
    }
    assert payload["trades"][1]["r_multiple"] == -1.0
    assert payload["trades"][1]["rule_trace"]["uncalibrated"] is True
    assert payload["data_thin_warnings"] == []


def test_build_results_payload_carries_recorded_fill_price_basis(
    tmp_path: Path,
) -> None:
    from trader.console.results import build_results_payload

    records = _rich_telemetry()
    for record in records:
        if record.get("ev") != "fill" or record.get("ticket_id") != "orb5-ticket-1":
            continue
        if record["kind"] == "entry":
            record["price_basis"] = "synthetic"
        else:
            record["price_basis"] = "real"
    session_dir = tmp_path / "paper-20260701-000000"
    _write_jsonl(session_dir / "telemetry.jsonl", records)

    payload = build_results_payload(session_dir)

    first_trade = payload["trades"][0]
    assert first_trade["entry_price_basis"] == "synthetic"
    assert first_trade["exit_price_basis"] == "real"


def test_build_results_payload_uses_none_for_unrecorded_fill_price_basis(
    tmp_path: Path,
) -> None:
    from trader.console.results import build_results_payload

    session_dir = tmp_path / "paper-20260701-000000"
    _write_jsonl(session_dir / "telemetry.jsonl", _rich_telemetry())

    payload = build_results_payload(session_dir)

    for trade in payload["trades"]:
        assert trade["entry_price_basis"] is None
        assert trade["exit_price_basis"] is None


def test_build_results_payload_collects_data_thin_warnings(
    tmp_path: Path,
) -> None:
    from trader.console.results import build_results_payload

    records = _rich_telemetry()
    records.insert(
        1,
        {
            "ev": "data_thin",
            "ts": "2026-07-02T13:25:30Z",
            "session": "paper-20260701-000000",
            "symbol": "SNXX",
            "day": "2026-07-02",
            "count": 1,
        },
    )
    session_dir = tmp_path / "paper-20260701-000000"
    _write_jsonl(session_dir / "telemetry.jsonl", records)

    payload = build_results_payload(session_dir)

    assert payload["data_thin_warnings"] == [
        {
            "symbol": "SNXX",
            "day": "2026-07-02",
            "count": 1,
            "ts": "2026-07-02T13:25:30Z",
        }
    ]


def test_build_results_payload_uses_empty_data_thin_warnings_when_none_recorded(
    tmp_path: Path,
) -> None:
    from trader.console.results import build_results_payload

    session_dir = tmp_path / "paper-20260701-000000"
    _write_jsonl(session_dir / "telemetry.jsonl", _rich_telemetry())

    payload = build_results_payload(session_dir)

    assert payload["data_thin_warnings"] == []


def test_build_results_payload_uses_latest_metrics_and_zero_trade_roster_rows(
    tmp_path: Path,
) -> None:
    from trader.console.results import build_results_payload

    session_dir = tmp_path / "paper-20260701-000000"
    _write_jsonl(session_dir / "telemetry.jsonl", _rich_telemetry())

    payload = build_results_payload(session_dir)

    assert {
        (record["algo_id"], record["book"]): record
        for record in payload["per_algo_metrics"]
    } == {
        ("flat", None): {
            "algo_id": "flat",
            "book": None,
            "status": "disabled",
            "n_real": 0,
            "n_shadow": 0,
            "wins": 0,
            "win_rate": None,
            "mean_r": None,
            "expectancy_r": None,
            "profit_factor": None,
            "max_drawdown_r": None,
            "cum_r": 0.0,
        },
        ("gap", "real"): {
            "algo_id": "gap",
            "book": "real",
            "status": "emitting",
            "n_real": 1,
            "n_shadow": 0,
            "wins": 0,
            "win_rate": 0.0,
            "mean_r": -1.0,
            "expectancy_r": -1.0,
            "profit_factor": 0.0,
            "max_drawdown_r": 1.0,
            "cum_r": -1.0,
        },
        ("gap", "shadow"): {
            "algo_id": "gap",
            "book": "shadow",
            "status": "emitting",
            "n_real": 1,
            "n_shadow": 0,
            "wins": 0,
            "win_rate": None,
            "mean_r": None,
            "expectancy_r": None,
            "profit_factor": None,
            "max_drawdown_r": None,
            "cum_r": 0.0,
        },
        ("orb5", "real"): {
            "algo_id": "orb5",
            "book": "real",
            "status": "emitting",
            "n_real": 1,
            "n_shadow": 1,
            "wins": 1,
            "win_rate": 1.0,
            "mean_r": 1.5,
            "expectancy_r": 1.5,
            "profit_factor": None,
            "max_drawdown_r": 0.0,
            "cum_r": 1.5,
        },
        ("orb5", "shadow"): {
            "algo_id": "orb5",
            "book": "shadow",
            "status": "emitting",
            "n_real": 1,
            "n_shadow": 1,
            "wins": 0,
            "win_rate": 0.0,
            "mean_r": -0.5,
            "expectancy_r": -0.5,
            "profit_factor": 0.0,
            "max_drawdown_r": 0.5,
            "cum_r": -0.5,
        },
    }


def test_build_results_payload_raises_for_missing_or_incomplete_telemetry(
    tmp_path: Path,
) -> None:
    from trader.console.results import build_results_payload

    missing_session_dir = tmp_path / "missing-session"
    with pytest.raises(FileNotFoundError, match="missing-session"):
        build_results_payload(missing_session_dir)

    incomplete_session_dir = tmp_path / "incomplete-session"
    _write_jsonl(
        incomplete_session_dir / "telemetry.jsonl",
        [
            {
                "ev": "tick",
                "ts": "2026-07-01T13:31:00Z",
                "session": "incomplete-session",
                "bar_ts": "2026-07-01T13:30:00Z",
            }
        ],
    )

    with pytest.raises(ValueError, match="session_start.*incomplete-session"):
        build_results_payload(incomplete_session_dir)


def test_build_day_candles_reads_static_1m_bar_file(tmp_path: Path) -> None:
    from trader.console.results import build_day_candles

    day = date(2026, 7, 2)
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
    write_1m_day(tmp_path, "SNDK", day, frame)

    assert build_day_candles(tmp_path, "SNDK", day) == [
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
    assert build_day_candles(tmp_path, "SNDK", date(2026, 7, 3)) == []
