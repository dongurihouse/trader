"""Integration tests for the execution session decision loop."""

from __future__ import annotations

from pathlib import Path

from trader.contracts import read_jsonl
from trader.execution.session import JsonlTelemetryWriter

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from trader.contracts import Intent, MarketData
from trader.contracts.testing import CollectingTelemetry, FakeMarketData
from trader.execution.book import RealBook, ShadowBook
from trader.execution.broker import SimBroker
from trader.execution.config import (
    AccountConfig,
    DrawdownStopConfig,
    ExecutionConfig,
    FillsConfig,
    RailsConfig,
    RiskConfig,
)
from trader.execution.risk import RiskRails
from trader.execution.session import RosterEntry, SessionRunner, SessionSummary


def test_jsonl_telemetry_writer_appends_and_flushes_each_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session" / "telemetry.jsonl"
    writer = JsonlTelemetryWriter(path)

    writer.emit({"ev": "first", "value": 1})
    assert list(read_jsonl(path)) == [{"ev": "first", "value": 1}]

    writer.emit({"ev": "second", "value": 2})
    assert list(read_jsonl(path)) == [
        {"ev": "first", "value": 1},
        {"ev": "second", "value": 2},
    ]


BAR_START = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)


class ScriptedAlgo:
    def __init__(
        self,
        algo_id: str,
        scripted_intents: dict[datetime, list[Intent]] | None = None,
        *,
        errors: set[datetime] | None = None,
        warmup_log: list[tuple[str, date]] | None = None,
    ) -> None:
        self.id = algo_id
        self._scripted_intents = scripted_intents or {}
        self._errors = errors or set()
        self._warmup_log = warmup_log
        self.on_bar_calls: list[datetime] = []

    def warmup(self, day: date, data: MarketData) -> None:
        del data
        if self._warmup_log is not None:
            self._warmup_log.append((self.id, day))

    def on_bar(self, asof: datetime, data: MarketData) -> list[Intent]:
        del data
        self.on_bar_calls.append(asof)
        if asof in self._errors:
            raise RuntimeError(f"scripted failure for {self.id}")
        return self._scripted_intents.get(asof, [])


def _intent(
    algo_id: str,
    ts: datetime,
    *,
    stop: float = 95.0,
    target: float = 105.0,
) -> Intent:
    return Intent(
        algo_id=algo_id,
        ts=ts,
        action="open",
        side="long",
        signal_symbol="SNXX",
        instrument="SNXX",
        entry="market_next_open",
        stop=stop,
        target=target,
        confidence=0.9,
        reason="scripted fixture",
        meta={"case": algo_id},
    )


def _frame(
    *rows: tuple[datetime, float, float, float, float]
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"o": open_, "h": high, "l": low, "c": close, "v": 1_000.0}
            for _ts, open_, high, low, close in rows
        ],
        index=pd.DatetimeIndex([row[0] for row in rows], name="ts"),
    )


def _risk_config() -> RiskConfig:
    return RiskConfig(
        account=AccountConfig(
            equity=10_000.0,
            capital_fraction=1.0,
            day_slots=2,
        ),
        rails=RailsConfig(
            max_entries_per_day=10,
            one_position_at_a_time=True,
            no_hedge=True,
            mute_after_consecutive_stops=10,
            mute_after_cumulative_day_r=-99.0,
            reversal_cooldown_minutes=10,
        ),
        drawdown_stop=DrawdownStopConfig(max_session_drawdown_r=None),
    )


def _execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        broker="sim",
        live_orders=False,
        fills=FillsConfig(
            entry="market_next_open",
            stop_wins_ties=True,
            commission=0.0,
        ),
        slippage_bps={"SNXX": {"2026-07": 0.0}},
    )


def _runner(
    *,
    data: FakeMarketData,
    telemetry: CollectingTelemetry,
    roster: list[RosterEntry],
) -> tuple[SessionRunner, RealBook, ShadowBook]:
    risk_config = _risk_config()
    execution_config = _execution_config()
    real_book = RealBook(risk_config)
    shadow_book = ShadowBook(execution_config)
    runner = SessionRunner(
        session_id="backtest-20260701-scripted",
        mode="backtest",
        market_data=data,
        broker=SimBroker(execution_config),
        risk=RiskRails(risk_config),
        real_book=real_book,
        shadow_book=shadow_book,
        telemetry=telemetry,
        roster=roster,
        symbols=["SNXX"],
        config_sha256="fixture-sha256",
        package_version="0.1.0",
    )
    return runner, real_book, shadow_book


def test_session_runner_executes_real_and_shadow_paths_in_exact_order() -> None:
    asofs = [BAR_START + timedelta(minutes=index) for index in range(1, 7)]
    accepted_ts = asofs[2]
    concurrent_ts = asofs[3]
    exit_ts = asofs[5]
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=2), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=3), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=4), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=5), 100.0, 106.0, 99.0, 105.0),
            )
        }
    )
    warmups: list[tuple[str, date]] = []
    accepted = ScriptedAlgo(
        "accepted",
        {accepted_ts: [_intent("accepted", accepted_ts)]},
        warmup_log=warmups,
    )
    broken = ScriptedAlgo(
        "broken",
        errors={concurrent_ts},
        warmup_log=warmups,
    )
    blocked = ScriptedAlgo(
        "blocked",
        {concurrent_ts: [_intent("blocked", concurrent_ts)]},
        warmup_log=warmups,
    )
    probe = ScriptedAlgo(
        "probe",
        {concurrent_ts: [_intent("probe", concurrent_ts)]},
        warmup_log=warmups,
    )
    disabled = ScriptedAlgo("disabled", warmup_log=warmups)
    telemetry = CollectingTelemetry()
    runner, real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(accepted, "emitting"),
            RosterEntry(broken, "emitting"),
            RosterEntry(blocked, "emitting"),
            RosterEntry(probe, "probe"),
            RosterEntry(disabled, "disabled"),
        ],
    )

    runner.start_session(BAR_START)
    runner.start_day(BAR_START.date())
    for asof in asofs:
        runner.process_bar(asof)
    summary = runner.end_session(exit_ts)

    assert [record["ev"] for record in telemetry.records] == [
        "session_start",
        "tick",
        "tick",
        "tick",
        "intent",
        "ticket",
        "tick",
        "fill",
        "algo_error",
        "intent",
        "rejection",
        "intent",
        "tick",
        "fill",
        "fill",
        "tick",
        "fill",
        "position_closed",
        "metrics",
        "metrics",
        "fill",
        "fill",
        "position_closed",
        "metrics",
        "metrics",
        "position_closed",
        "metrics",
        "metrics",
        "session_end",
    ]
    assert telemetry.records[0] == {
        "ev": "session_start",
        "ts": "2026-07-01T13:30:00Z",
        "session": "backtest-20260701-scripted",
        "mode": "backtest",
        "config_sha256": "fixture-sha256",
        "package_version": "0.1.0",
        "symbols": ["SNXX"],
        "roster": [
            {"id": "accepted", "status": "emitting"},
            {"id": "broken", "status": "emitting"},
            {"id": "blocked", "status": "emitting"},
            {"id": "probe", "status": "probe"},
            {"id": "disabled", "status": "disabled"},
        ],
    }
    assert [
        record["ev"]
        for record in telemetry.records[1:3]
    ] == ["tick", "tick"]
    assert telemetry.records[1]["bar_ts"] == "2026-07-01T13:30:00Z"
    assert telemetry.records[4]["algo_id"] == "accepted"
    assert telemetry.records[4]["ts"] == "2026-07-01T13:33:00Z"
    assert telemetry.records[5]["shares"] == 50
    assert telemetry.records[7]["kind"] == "entry"
    assert telemetry.records[7]["book"] == "real"
    assert telemetry.records[7]["ts"] == "2026-07-01T13:34:00Z"
    assert telemetry.records[8]["algo_id"] == "broken"
    assert telemetry.records[8]["error"] == "scripted failure for broken"
    assert "RuntimeError: scripted failure for broken" in telemetry.records[8][
        "traceback"
    ]
    assert telemetry.records[9]["algo_id"] == "blocked"
    assert telemetry.records[10]["rule"] == "position_in_flight"
    assert telemetry.records[10]["detail"] == (
        "a real entry is already open or pending fill"
    )
    assert telemetry.records[11]["algo_id"] == "probe"
    assert [
        (record["kind"], record["book"])
        for record in telemetry.records[13:15]
    ] == [("entry", "shadow"), ("entry", "shadow")]
    assert telemetry.records[16]["kind"] == "target"
    assert telemetry.records[17]["book"] == "real"
    assert telemetry.records[17]["r_multiple"] == pytest.approx(1.0)
    assert [
        (record["ev"], record.get("book"))
        for record in telemetry.records[17:20]
    ] == [
        ("position_closed", "real"),
        ("metrics", "real"),
        ("metrics", "shadow"),
    ]
    assert telemetry.records[18]["algo_id"] == "accepted"
    assert telemetry.records[18]["n_real"] == 1
    assert telemetry.records[18]["n_shadow"] == 0
    assert telemetry.records[18]["cum_r"] == pytest.approx(1.0)
    assert [record["algo_id"] for record in telemetry.records[22:28:3]] == [
        "blocked",
        "probe",
    ]
    assert shadow_book.resolved_r_multiples("blocked") == pytest.approx([1.0])
    assert shadow_book.resolved_r_multiples("probe") == pytest.approx([1.0])
    assert real_book.resolved_r_multiples("accepted") == pytest.approx([1.0])
    assert warmups == [
        ("accepted", BAR_START.date()),
        ("broken", BAR_START.date()),
        ("blocked", BAR_START.date()),
        ("probe", BAR_START.date()),
    ]
    assert disabled.on_bar_calls == []
    assert summary == SessionSummary(
        bars_processed=6,
        real_trades=1,
        shadow_trades=2,
        final_equity=10_250.0,
    )
    assert telemetry.records[-1]["ts"] == "2026-07-01T13:36:00Z"
    assert telemetry.records[-1]["final_equity"] == 10_250.0
    explicit_event_times = {
        "2026-07-01T13:30:00Z",
        *(asof.isoformat().replace("+00:00", "Z") for asof in asofs),
    }
    assert {record["ts"] for record in telemetry.records} <= explicit_event_times


def test_start_day_drops_pending_entry_and_resets_in_flight_guard() -> None:
    first_intent_ts = BAR_START + timedelta(minutes=1)
    next_day_start = BAR_START + timedelta(days=1)
    second_intent_ts = next_day_start + timedelta(minutes=1)
    second_fill_ts = second_intent_ts + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
                (next_day_start, 100.0, 101.0, 99.0, 100.0),
                (next_day_start + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
            )
        }
    )
    warmups: list[tuple[str, date]] = []
    active = ScriptedAlgo(
        "active",
        {
            first_intent_ts: [_intent("active", first_intent_ts)],
            second_intent_ts: [_intent("active", second_intent_ts)],
        },
        warmup_log=warmups,
    )
    disabled = ScriptedAlgo("disabled", warmup_log=warmups)
    telemetry = CollectingTelemetry()
    runner, real_book, _shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(active, "emitting"),
            RosterEntry(disabled, "disabled"),
        ],
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(first_intent_ts)
    assert real_book.state.entries_today == 1

    runner.start_day(next_day_start.date())
    assert real_book.state.entries_today == 0
    runner.process_bar(second_intent_ts)
    runner.process_bar(second_fill_ts)

    assert [
        record["ev"]
        for record in telemetry.records
    ] == ["tick", "intent", "ticket", "tick", "intent", "ticket", "tick", "fill"]
    assert [record["kind"] for record in telemetry.records if record["ev"] == "fill"] == [
        "entry"
    ]
    assert telemetry.records[5]["ticket_id"].startswith(
        "active-20260702T133100Z"
    )
    assert warmups == [
        ("active", BAR_START.date()),
        ("active", next_day_start.date()),
    ]
    assert disabled.on_bar_calls == []


def test_end_session_force_flats_real_position_and_emits_final_metrics() -> None:
    intent_ts = BAR_START + timedelta(minutes=1)
    entry_ts = intent_ts + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=1), 100.0, 103.0, 99.0, 102.0),
            )
        }
    )
    algo = ScriptedAlgo(
        "force-flat",
        {intent_ts: [_intent("force-flat", intent_ts)]},
    )
    telemetry = CollectingTelemetry()
    runner, real_book, _shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, "emitting")],
    )

    runner.start_session(BAR_START)
    runner.start_day(BAR_START.date())
    runner.process_bar(intent_ts)
    runner.process_bar(entry_ts)
    summary = runner.end_session(entry_ts)

    assert [record["ev"] for record in telemetry.records] == [
        "session_start",
        "tick",
        "intent",
        "ticket",
        "tick",
        "fill",
        "fill",
        "position_closed",
        "metrics",
        "metrics",
        "session_end",
    ]
    assert telemetry.records[6]["kind"] == "eod"
    assert telemetry.records[6]["price"] == pytest.approx(102.0)
    assert telemetry.records[7]["r_multiple"] == pytest.approx(0.4)
    assert telemetry.records[8]["book"] == "real"
    assert telemetry.records[9]["book"] == "shadow"
    assert real_book.state.positions == []
    assert summary == SessionSummary(2, 1, 0, 10_098.0)
