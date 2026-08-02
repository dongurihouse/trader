"""Integration tests for the execution session decision loop."""

from __future__ import annotations

from pathlib import Path

from trader.contracts import read_jsonl
from trader.execution.session import JsonlTelemetryWriter

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from trader.contracts import (
    Broker,
    AlgoStatus,
    Intent,
    MarketData,
    OrderTicket,
    PortfolioState,
    Rejection,
    RiskEngine,
    Side,
)
from trader.contracts.testing import CollectingTelemetry, FakeMarketData
from trader.execution.book import RealBook, ShadowBook
from trader.execution.broker import ApiBroker, ManualBroker, SimBroker
from trader.execution.broker.manual import Style, record_fill
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
    side: Side = "long",
    stop: float | None = 95.0,
    target: float | None = 105.0,
    meta: dict | None = None,
) -> Intent:
    return Intent(
        algo_id=algo_id,
        ts=ts,
        action="open",
        side=side,
        signal_symbol="SNXX",
        instrument="SNXX",
        entry="market_next_open",
        stop=stop,
        target=target,
        confidence=0.9,
        reason="scripted fixture",
        meta={"case": algo_id} if meta is None else meta,
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


def _risk_config(*, max_entries_per_day: int = 10) -> RiskConfig:
    return RiskConfig(
        account=AccountConfig(
            equity=10_000.0,
            capital_fraction=1.0,
            day_slots=2,
        ),
        rails=RailsConfig(
            max_entries_per_day=max_entries_per_day,
            one_position_at_a_time=True,
            no_hedge=True,
            mute_after_consecutive_stops=10,
            mute_after_cumulative_day_r=-99.0,
            reversal_cooldown_minutes=10,
        ),
        drawdown_stop=DrawdownStopConfig(max_session_drawdown_r=None),
    )


def _execution_config(*, slippage_bps: float = 0.0) -> ExecutionConfig:
    return ExecutionConfig(
        broker="sim",
        live_orders=False,
        fills=FillsConfig(
            entry="market_next_open",
            stop_wins_ties=True,
            commission=0.0,
        ),
        slippage_bps={"SNXX": {"2026-07": slippage_bps}},
    )


def _runner(
    *,
    data: FakeMarketData,
    telemetry: CollectingTelemetry,
    roster: list[RosterEntry],
    risk: RiskEngine | None = None,
    slippage_bps: float = 0.0,
    risk_config: RiskConfig | None = None,
    broker: Broker | None = None,
) -> tuple[SessionRunner, RealBook, ShadowBook]:
    risk_config = _risk_config() if risk_config is None else risk_config
    execution_config = _execution_config(slippage_bps=slippage_bps)
    real_book = RealBook(risk_config)
    shadow_book = ShadowBook(execution_config)
    runner = SessionRunner(
        session_id="backtest-20260701-scripted",
        mode="backtest",
        market_data=data,
        broker=SimBroker(execution_config) if broker is None else broker,
        risk=risk if risk is not None else RiskRails(risk_config),
        real_book=real_book,
        shadow_book=shadow_book,
        telemetry=telemetry,
        roster=roster,
        symbols=["SNXX"],
        config_sha256="fixture-sha256",
        package_version="0.1.0",
    )
    return runner, real_book, shadow_book


class _FailIfCalledRisk:
    def check_and_size(
        self,
        intent: Intent,
        portfolio: PortfolioState,
        data: MarketData,
    ) -> OrderTicket | Rejection:
        del intent, portfolio, data
        raise AssertionError("gate-refused intents must bypass RiskRails")


class _FailForAlgoRisk:
    def __init__(self, blocked_algo_id: str) -> None:
        self._blocked_algo_id = blocked_algo_id
        self._delegate = RiskRails(_risk_config())

    def check_and_size(
        self,
        intent: Intent,
        portfolio: PortfolioState,
        data: MarketData,
    ) -> OrderTicket | Rejection:
        if intent.algo_id == self._blocked_algo_id:
            raise AssertionError(
                f"{self._blocked_algo_id} intents must bypass RiskRails"
            )
        return self._delegate.check_and_size(intent, portfolio, data)


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
    assert real_book.state.entries_today == 0

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


def test_last_bar_pending_ticket_does_not_consume_slot_or_block_next_day() -> None:
    first_intent_ts = BAR_START + timedelta(minutes=1)
    next_day_start = BAR_START + timedelta(days=1)
    second_intent_ts = next_day_start + timedelta(minutes=1)
    second_fill_ts = second_intent_ts + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (next_day_start, 100.0, 101.0, 99.0, 100.0),
                (next_day_start + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
            )
        }
    )
    first = ScriptedAlgo(
        "first",
        {first_intent_ts: [_intent("first", first_intent_ts)]},
    )
    second = ScriptedAlgo(
        "second",
        {second_intent_ts: [_intent("second", second_intent_ts)]},
    )
    telemetry = CollectingTelemetry()
    runner, real_book, _shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(first, "emitting"),
            RosterEntry(second, "emitting"),
        ],
        risk_config=_risk_config(max_entries_per_day=1),
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(first_intent_ts)
    assert real_book.state.entries_today == 0

    runner.start_day(next_day_start.date())
    runner.process_bar(second_intent_ts)
    runner.process_bar(second_fill_ts)

    ticket_algos = [
        record["algo_id"] for record in telemetry.records if record["ev"] == "ticket"
    ]
    rejections = [
        record for record in telemetry.records if record["ev"] == "rejection"
    ]
    real_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "real"
    ]
    assert ticket_algos == ["first", "second"]
    assert rejections == []
    assert [
        (record["ticket_id"].startswith("second-"), record["kind"])
        for record in real_fills
    ] == [(True, "entry")]
    assert real_book.state.entries_today == 1


def test_api_broker_submit_failure_emits_error_without_ticket_or_in_flight() -> None:
    first_intent_ts = BAR_START + timedelta(minutes=1)
    later_intent_ts = BAR_START + timedelta(minutes=2)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
            )
        }
    )
    first = ScriptedAlgo(
        "api-first",
        {first_intent_ts: [_intent("api-first", first_intent_ts)]},
    )
    same_bar = ScriptedAlgo(
        "api-same-bar",
        {first_intent_ts: [_intent("api-same-bar", first_intent_ts)]},
    )
    later = ScriptedAlgo(
        "api-later",
        {later_intent_ts: [_intent("api-later", later_intent_ts)]},
    )
    telemetry = CollectingTelemetry()
    runner, real_book, _shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(first, "emitting"),
            RosterEntry(same_bar, "emitting"),
            RosterEntry(later, "emitting"),
        ],
        broker=ApiBroker(_execution_config()),
    )
    entries_before = real_book.state.entries_today

    runner.start_day(BAR_START.date())
    try:
        runner.process_bar(first_intent_ts)
        runner.process_bar(later_intent_ts)
    except Exception as exc:
        pytest.fail(f"process_bar raised {exc!r}")

    errors = [
        record for record in telemetry.records if record["ev"] == "algo_error"
    ]
    assert [record["algo_id"] for record in errors] == [
        "api-first",
        "api-same-bar",
        "api-later",
    ]
    for record in errors:
        assert record["error"].startswith(
            "broker submit failed: API broker is not configured:"
        )
        assert "no adapter is wired" in record["error"]
        assert "BrokerNotConfigured" in record["traceback"]
    assert not any(record["ev"] == "ticket" for record in telemetry.records)
    assert not any(
        record["ev"] == "rejection"
        and record["rule"] == "position_in_flight"
        for record in telemetry.records
    )
    assert real_book.state.entries_today == entries_before == 0
    assert real_book.state.positions == []
    assert real_book.forget_unfilled_tickets() == {}
    assert runner._real_entry_in_flight is False


def test_successful_submit_registers_ticket_and_sets_in_flight_latch() -> None:
    intent_ts = BAR_START + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
            )
        }
    )
    accepted = ScriptedAlgo(
        "sim-accepted",
        {intent_ts: [_intent("sim-accepted", intent_ts)]},
    )
    blocked = ScriptedAlgo(
        "sim-blocked",
        {intent_ts: [_intent("sim-blocked", intent_ts)]},
    )
    telemetry = CollectingTelemetry()
    runner, real_book, _shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(accepted, "emitting"),
            RosterEntry(blocked, "emitting"),
        ],
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(intent_ts)

    tickets = [
        record for record in telemetry.records if record["ev"] == "ticket"
    ]
    rejections = [
        record for record in telemetry.records if record["ev"] == "rejection"
    ]
    pending = real_book.forget_unfilled_tickets()
    assert [record["algo_id"] for record in tickets] == ["sim-accepted"]
    assert [(record["algo_id"], record["rule"]) for record in rejections] == [
        ("sim-blocked", "position_in_flight")
    ]
    assert list(pending) == [tickets[0]["ticket_id"]]
    assert real_book.state.entries_today == 0
    assert real_book.state.positions == []
    assert runner._real_entry_in_flight is True


def test_end_session_discards_pending_real_ticket_and_clears_in_flight() -> None:
    intent_ts = BAR_START + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
            )
        }
    )
    algo = ScriptedAlgo(
        "pending-at-close",
        {intent_ts: [_intent("pending-at-close", intent_ts)]},
    )
    telemetry = CollectingTelemetry()
    runner, real_book, _shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, "emitting")],
        risk_config=_risk_config(max_entries_per_day=1),
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(intent_ts)
    assert real_book.state.entries_today == 0

    summary = runner.end_session(intent_ts)

    assert summary.real_trades == 0
    assert not any(record["ev"] == "fill" for record in telemetry.records)
    assert real_book.state.entries_today == 0
    assert real_book.state.positions == []
    assert real_book.forget_unfilled_tickets() == {}
    assert runner._real_entry_in_flight is False


def test_manual_decline_clears_in_flight_discards_ticket_and_emits_telemetry(
    tmp_path: Path,
) -> None:
    first_intent_ts = BAR_START + timedelta(minutes=1)
    decline_ts = first_intent_ts + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
            )
        }
    )
    declined = ScriptedAlgo(
        "declined",
        {first_intent_ts: [_intent("declined", first_intent_ts)]},
    )
    replacement = ScriptedAlgo(
        "replacement",
        {decline_ts: [_intent("replacement", decline_ts)]},
    )
    telemetry = CollectingTelemetry()
    broker = ManualBroker(tmp_path, out=lambda _message: None, style=Style(False))
    runner, real_book, _shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(declined, "emitting"),
            RosterEntry(replacement, "emitting"),
        ],
        broker=broker,
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(first_intent_ts)
    first_ticket_id = next(
        record["ticket_id"]
        for record in telemetry.records
        if record["ev"] == "ticket"
    )
    record_fill(
        tmp_path,
        ticket_id=first_ticket_id,
        kind="cancel",
        ts=decline_ts,
    )
    runner.process_bar(decline_ts)

    declined_records = [
        record for record in telemetry.records if record["ev"] == "ticket_declined"
    ]
    ticket_algos = [
        record["algo_id"] for record in telemetry.records if record["ev"] == "ticket"
    ]
    rejections = [
        record for record in telemetry.records if record["ev"] == "rejection"
    ]
    assert declined_records == [
        {
            "ev": "ticket_declined",
            "ts": "2026-07-01T13:32:00Z",
            "session": "backtest-20260701-scripted",
            "ticket_id": first_ticket_id,
            "algo_id": "declined",
            "reason": "manual cancel",
        }
    ]
    assert ticket_algos == ["declined", "replacement"]
    assert rejections == []
    assert real_book.state.entries_today == 0
    assert real_book.forget_unfilled_tickets([first_ticket_id]) == {}


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
    assert summary == SessionSummary(2, 1, 0, 10_100.0)


@pytest.mark.parametrize("status", ["emitting", "probe"])
def test_gate_refused_intent_routes_to_tagged_shadow_before_status_or_risk(
    status: AlgoStatus,
) -> None:
    intent_ts = BAR_START + timedelta(minutes=1)
    shadow_fill_ts = intent_ts + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (
                    BAR_START + timedelta(minutes=1),
                    100.0,
                    106.0,
                    99.0,
                    105.0,
                ),
            )
        }
    )
    intent = _intent(
        "gate-refused",
        intent_ts,
        meta={
            "case": "gate-refused",
            "gates_pass": False,
            "vetoed": "veto-entry-cutoff",
        },
    )
    algo = ScriptedAlgo("gate-refused", {intent_ts: [intent]})
    telemetry = CollectingTelemetry()
    runner, real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, status)],
        risk=_FailIfCalledRisk(),
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(intent_ts)
    runner.process_bar(shadow_fill_ts)

    assert [record["ev"] for record in telemetry.records].count("intent") == 1
    assert next(
        record for record in telemetry.records if record["ev"] == "intent"
    )["meta"] == intent.meta
    assert not any(record["ev"] == "ticket" for record in telemetry.records)
    assert not any(
        record["ev"] == "fill" and record["book"] == "real"
        for record in telemetry.records
    )
    assert real_book.state.positions == []
    shadow_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "shadow"
    ]
    assert [(record["kind"], record["tag"]) for record in shadow_fills] == [
        ("entry", "gate_refused"),
        ("target", "gate_refused"),
    ]
    shadow_closes = [
        record
        for record in telemetry.records
        if record["ev"] == "position_closed" and record["book"] == "shadow"
    ]
    assert [record["tag"] for record in shadow_closes] == ["gate_refused"]
    assert shadow_book.resolved_r_multiples("gate-refused") == pytest.approx([1.0])


@pytest.mark.parametrize(
    ("stop", "target"),
    [(None, 105.0), (95.0, None)],
)
def test_incomplete_gate_refusal_is_recorded_but_dropped_without_shadow_fill(
    stop: float | None,
    target: float | None,
) -> None:
    intent_ts = BAR_START + timedelta(minutes=1)
    next_asof = intent_ts + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (
                    BAR_START + timedelta(minutes=1),
                    100.0,
                    106.0,
                    99.0,
                    105.0,
                ),
            )
        }
    )
    intent = _intent(
        "incomplete-gate-refusal",
        intent_ts,
        stop=stop,
        target=target,
        meta={"gates_pass": False},
    )
    algo = ScriptedAlgo(
        "incomplete-gate-refusal",
        {intent_ts: [intent]},
    )
    telemetry = CollectingTelemetry()
    runner, real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, "emitting")],
        risk=_FailIfCalledRisk(),
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(intent_ts)
    runner.process_bar(next_asof)

    assert [record["ev"] for record in telemetry.records].count("intent") == 1
    assert not any(record["ev"] == "ticket" for record in telemetry.records)
    assert not any(record["ev"] == "fill" for record in telemetry.records)
    assert real_book.state.positions == []
    assert shadow_book.resolved_r_multiples("incomplete-gate-refusal") == []


@pytest.mark.parametrize(
    "meta",
    [
        pytest.param({"case": "control"}, id="gates-pass-absent"),
        pytest.param(
            {"case": "control", "gates_pass": True},
            id="gates-pass-true",
        ),
    ],
)
def test_non_refused_emitting_intent_reaches_real_book(meta: dict) -> None:
    intent_ts = BAR_START + timedelta(minutes=1)
    entry_ts = intent_ts + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (
                    BAR_START + timedelta(minutes=1),
                    100.0,
                    101.0,
                    99.0,
                    100.0,
                ),
            )
        }
    )
    algo = ScriptedAlgo(
        "control",
        {intent_ts: [_intent("control", intent_ts, meta=meta)]},
    )
    telemetry = CollectingTelemetry()
    runner, real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, "emitting")],
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(intent_ts)
    runner.process_bar(entry_ts)

    assert [record["ev"] for record in telemetry.records].count("ticket") == 1
    real_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "real"
    ]
    assert [(record["kind"], record["tag"]) for record in real_fills] == [
        ("entry", None)
    ]
    assert len(real_book.state.positions) == 1
    assert shadow_book.resolved_r_multiples("control") == []


def test_opposite_emitting_intent_stays_rejected_but_exits_real_position_by_reversal() -> None:
    long_intent_ts = BAR_START + timedelta(minutes=1)
    long_entry_ts = BAR_START + timedelta(minutes=2)
    reversal_intent_ts = BAR_START + timedelta(minutes=3)
    reversal_exit_ts = BAR_START + timedelta(minutes=4)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=2), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=3), 102.0, 110.0, 90.0, 102.0),
            )
        }
    )
    holder = ScriptedAlgo(
        "holder",
        {long_intent_ts: [_intent("holder", long_intent_ts)]},
    )
    reverser = ScriptedAlgo(
        "reverser",
        {
            reversal_intent_ts: [
                _intent(
                    "reverser",
                    reversal_intent_ts,
                    side="short",
                    meta={"case": "reverser"},
                )
            ]
        },
    )
    telemetry = CollectingTelemetry()
    runner, real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(holder, "emitting"),
            RosterEntry(reverser, "emitting"),
        ],
    )

    runner.start_day(BAR_START.date())
    for asof in (
        long_intent_ts,
        long_entry_ts,
        reversal_intent_ts,
        reversal_exit_ts,
    ):
        runner.process_bar(asof)

    rejections = [
        record for record in telemetry.records if record["ev"] == "rejection"
    ]
    assert [(record["algo_id"], record["rule"]) for record in rejections] == [
        ("reverser", "position_in_flight")
    ]
    shadow_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "shadow"
    ]
    assert [(record["kind"], record["tag"]) for record in shadow_fills] == [
        ("entry", "rejected"),
        ("stop", "rejected"),
    ]
    real_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "real"
    ]
    assert [(record["kind"], record["price"]) for record in real_fills] == [
        ("entry", 100.0),
        ("reversal", 102.0),
    ]
    real_closes = [
        record
        for record in telemetry.records
        if record["ev"] == "position_closed" and record["book"] == "real"
    ]
    assert [(record["algo_id"], record["exit_kind"]) for record in real_closes] == [
        ("holder", "reversal")
    ]
    assert real_book.state.positions == []
    assert real_book.resolved_r_multiples("holder") == pytest.approx([0.4])
    assert shadow_book.resolved_r_multiples("reverser") == pytest.approx([-1.0])


def test_gate_refused_opposite_intent_still_arms_real_reversal() -> None:
    long_intent_ts = BAR_START + timedelta(minutes=1)
    long_entry_ts = BAR_START + timedelta(minutes=2)
    reversal_intent_ts = BAR_START + timedelta(minutes=3)
    reversal_exit_ts = BAR_START + timedelta(minutes=4)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=2), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=3), 102.0, 110.0, 90.0, 102.0),
            )
        }
    )
    holder = ScriptedAlgo(
        "gate-holder",
        {long_intent_ts: [_intent("gate-holder", long_intent_ts)]},
    )
    reverser = ScriptedAlgo(
        "gate-refused-reverser",
        {
            reversal_intent_ts: [
                _intent(
                    "gate-refused-reverser",
                    reversal_intent_ts,
                    side="short",
                    meta={"case": "gate-refused", "gates_pass": False},
                )
            ]
        },
    )
    telemetry = CollectingTelemetry()
    runner, real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(holder, "emitting"),
            RosterEntry(reverser, "emitting"),
        ],
        risk=_FailForAlgoRisk("gate-refused-reverser"),
    )

    runner.start_day(BAR_START.date())
    for asof in (
        long_intent_ts,
        long_entry_ts,
        reversal_intent_ts,
        reversal_exit_ts,
    ):
        runner.process_bar(asof)

    assert not any(record["ev"] == "rejection" for record in telemetry.records)
    shadow_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "shadow"
    ]
    assert [(record["kind"], record["tag"]) for record in shadow_fills] == [
        ("entry", "gate_refused"),
        ("stop", "gate_refused"),
    ]
    real_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "real"
    ]
    assert [(record["kind"], record["price"]) for record in real_fills] == [
        ("entry", 100.0),
        ("reversal", 102.0),
    ]
    real_closes = [
        record
        for record in telemetry.records
        if record["ev"] == "position_closed" and record["book"] == "real"
    ]
    assert [(record["algo_id"], record["exit_kind"]) for record in real_closes] == [
        ("gate-holder", "reversal")
    ]
    assert real_book.state.positions == []
    assert real_book.resolved_r_multiples("gate-holder") == pytest.approx([0.4])
    assert shadow_book.resolved_r_multiples("gate-refused-reverser") == pytest.approx(
        [-1.0]
    )


def test_reversal_cooldown_globally_rejects_original_and_new_direction_intents() -> None:
    long_intent_ts = BAR_START + timedelta(minutes=1)
    long_entry_ts = BAR_START + timedelta(minutes=2)
    reversal_intent_ts = BAR_START + timedelta(minutes=3)
    reversal_exit_ts = BAR_START + timedelta(minutes=4)
    cooldown_deadline = reversal_exit_ts + timedelta(minutes=10)
    rows = []
    for index in range(14):
        bar_ts = BAR_START + timedelta(minutes=index)
        open_ = (
            102.0
            if bar_ts == reversal_exit_ts - timedelta(minutes=1)
            else 100.0
        )
        rows.append((bar_ts, open_, open_ + 1.0, open_ - 1.0, open_))
    data = FakeMarketData({"SNXX": _frame(*rows)})
    holder = ScriptedAlgo(
        "holder",
        {long_intent_ts: [_intent("holder", long_intent_ts)]},
    )
    reverser = ScriptedAlgo(
        "reverser",
        {
            reversal_intent_ts: [
                _intent(
                    "reverser",
                    reversal_intent_ts,
                    side="short",
                    meta={"case": "reverser"},
                )
            ]
        },
    )
    cooldown_original = ScriptedAlgo(
        "cooldown-original",
        {
            reversal_exit_ts: [
                _intent(
                    "cooldown-original",
                    reversal_exit_ts,
                    side="long",
                )
            ]
        },
    )
    cooldown_new = ScriptedAlgo(
        "cooldown-new",
        {
            reversal_exit_ts: [
                _intent(
                    "cooldown-new",
                    reversal_exit_ts,
                    side="short",
                    meta={"case": "cooldown-new"},
                )
            ]
        },
    )
    after_cooldown = ScriptedAlgo(
        "after-cooldown",
        {cooldown_deadline: [_intent("after-cooldown", cooldown_deadline)]},
    )
    telemetry = CollectingTelemetry()
    runner, real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(holder, "emitting"),
            RosterEntry(reverser, "emitting"),
            RosterEntry(cooldown_original, "emitting"),
            RosterEntry(cooldown_new, "emitting"),
            RosterEntry(after_cooldown, "emitting"),
        ],
    )

    runner.start_day(BAR_START.date())
    for asof in (
        long_intent_ts,
        long_entry_ts,
        reversal_intent_ts,
        reversal_exit_ts,
        cooldown_deadline,
    ):
        runner.process_bar(asof)

    rejections = [
        record for record in telemetry.records if record["ev"] == "rejection"
    ]
    assert [
        (record["algo_id"], record["side"], record["rule"])
        for record in rejections
    ] == [
        ("reverser", "short", "position_in_flight"),
        ("cooldown-original", "long", "muted"),
        ("cooldown-new", "short", "muted"),
    ]
    real_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "real"
    ]
    assert [(record["kind"], record["price"]) for record in real_fills] == [
        ("entry", 100.0),
        ("reversal", 102.0),
    ]
    assert real_book.state.positions == []
    assert real_book.state.muted_until == cooldown_deadline
    assert real_book.resolved_r_multiples("holder") == pytest.approx([0.4])
    assert shadow_book.resolved_r_multiples("cooldown-original") == []
    assert shadow_book.resolved_r_multiples("cooldown-new") == []
    tickets = [record for record in telemetry.records if record["ev"] == "ticket"]
    assert [record["algo_id"] for record in tickets] == [
        "holder",
        "after-cooldown",
    ]
    assert not any(
        record["ev"] == "rejection" and record["algo_id"] == "after-cooldown"
        for record in telemetry.records
    )


def test_probe_opposite_intent_does_not_trigger_real_reversal() -> None:
    long_intent_ts = BAR_START + timedelta(minutes=1)
    long_entry_ts = BAR_START + timedelta(minutes=2)
    probe_intent_ts = BAR_START + timedelta(minutes=3)
    later_ts = BAR_START + timedelta(minutes=4)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=2), 100.0, 101.0, 99.0, 100.0),
                (BAR_START + timedelta(minutes=3), 100.0, 101.0, 99.0, 100.0),
            )
        }
    )
    holder = ScriptedAlgo(
        "probe-holder",
        {long_intent_ts: [_intent("probe-holder", long_intent_ts)]},
    )
    probe = ScriptedAlgo(
        "probe-reverser",
        {
            probe_intent_ts: [
                _intent(
                    "probe-reverser",
                    probe_intent_ts,
                    side="short",
                    meta={"case": "probe-reverser"},
                )
            ]
        },
    )
    telemetry = CollectingTelemetry()
    runner, real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[
            RosterEntry(holder, "emitting"),
            RosterEntry(probe, "probe"),
        ],
    )

    runner.start_day(BAR_START.date())
    for asof in (
        long_intent_ts,
        long_entry_ts,
        probe_intent_ts,
        later_ts,
    ):
        runner.process_bar(asof)

    real_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "real"
    ]
    assert [(record["kind"], record["price"]) for record in real_fills] == [
        ("entry", 100.0)
    ]
    assert not any(
        record["ev"] == "position_closed" and record["book"] == "real"
        for record in telemetry.records
    )
    shadow_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "shadow"
    ]
    assert [(record["kind"], record["tag"]) for record in shadow_fills] == [
        ("entry", "probe")
    ]
    assert len(real_book.state.positions) == 1
    assert shadow_book.resolved_r_multiples("probe-reverser") == []


def test_end_session_force_flats_filled_shadow_episode() -> None:
    intent_ts = BAR_START + timedelta(minutes=1)
    entry_ts = intent_ts + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (
                    BAR_START + timedelta(minutes=1),
                    100.0,
                    103.0,
                    99.0,
                    102.0,
                ),
            )
        }
    )
    algo = ScriptedAlgo(
        "truncated-probe",
        {intent_ts: [_intent("truncated-probe", intent_ts)]},
    )
    telemetry = CollectingTelemetry()
    runner, _real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, "probe")],
        slippage_bps=25.0,
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(intent_ts)
    runner.process_bar(entry_ts)
    summary = runner.end_session(entry_ts)

    shadow_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "shadow"
    ]
    assert [(record["kind"], record["tag"]) for record in shadow_fills] == [
        ("entry", "probe"),
        ("eod", "probe"),
    ]
    shadow_closes = [
        record
        for record in telemetry.records
        if record["ev"] == "position_closed" and record["book"] == "shadow"
    ]
    assert len(shadow_closes) == 1
    assert shadow_closes[0]["exit_kind"] == "eod"
    assert shadow_closes[0]["tag"] == "probe"
    assert shadow_fills[-1]["price"] == pytest.approx(101.745)
    assert shadow_closes[0]["r_multiple"] == pytest.approx(0.299)
    assert shadow_book.resolved_r_multiples("truncated-probe") == pytest.approx(
        [0.299]
    )
    assert summary.shadow_trades == 1
    assert telemetry.records[-1]["shadow_trades"] == 1


def test_start_day_drops_pending_shadow_without_cross_day_fill() -> None:
    day_one_intent_ts = BAR_START + timedelta(minutes=1)
    day_two_bar = BAR_START + timedelta(days=1)
    day_two_asof = day_two_bar + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (day_two_bar, 50.0, 51.0, 49.0, 50.0),
            )
        }
    )
    algo = ScriptedAlgo(
        "overnight-probe",
        {day_one_intent_ts: [_intent("overnight-probe", day_one_intent_ts)]},
    )
    telemetry = CollectingTelemetry()
    runner, _real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, "probe")],
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(day_one_intent_ts)
    runner.start_day(day_two_bar.date())
    runner.process_bar(day_two_asof)

    assert not any(record["ev"] == "fill" for record in telemetry.records)
    assert shadow_book.resolved_r_multiples("overnight-probe") == []


def test_end_session_drops_shadow_pending_without_fill_or_outcome() -> None:
    intent_ts = BAR_START + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
            )
        }
    )
    algo = ScriptedAlgo(
        "unfilled-probe",
        {intent_ts: [_intent("unfilled-probe", intent_ts)]},
    )
    telemetry = CollectingTelemetry()
    runner, _real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, "probe")],
    )

    runner.start_day(BAR_START.date())
    runner.process_bar(intent_ts)
    summary = runner.end_session(intent_ts)

    assert not any(record["ev"] == "fill" for record in telemetry.records)
    assert not any(
        record["ev"] == "position_closed" for record in telemetry.records
    )
    assert shadow_book.resolved_r_multiples("unfilled-probe") == []
    assert summary.shadow_trades == 0


@pytest.mark.parametrize(
    ("stop", "target", "rule"),
    [(None, None, "no_stop"), (95.0, None, "no_target")],
)
def test_incomplete_bracket_rejection_never_opens_shadow_episode(
    stop: float | None,
    target: float | None,
    rule: str,
) -> None:
    asofs = [BAR_START + timedelta(minutes=index) for index in range(1, 4)]
    intent_ts = asofs[0]
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (
                    BAR_START + timedelta(minutes=1),
                    100.0,
                    101.0,
                    99.0,
                    100.0,
                ),
                (
                    BAR_START + timedelta(minutes=2),
                    100.0,
                    101.0,
                    99.0,
                    100.0,
                ),
            )
        }
    )
    algo = ScriptedAlgo(
        "stopless",
        {intent_ts: [_intent("stopless", intent_ts, stop=stop, target=target)]},
    )
    telemetry = CollectingTelemetry()
    runner, _real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, "emitting")],
    )

    runner.start_day(BAR_START.date())
    for asof in asofs:
        runner.process_bar(asof)
    summary = runner.end_session(asofs[-1])

    rejections = [
        record for record in telemetry.records if record["ev"] == "rejection"
    ]
    assert [record["rule"] for record in rejections] == [rule]
    assert not any(
        record["ev"] == "fill" and record["book"] == "shadow"
        for record in telemetry.records
    )
    assert shadow_book.resolved_r_multiples("stopless") == []
    assert summary.shadow_trades == 0


def test_bracketed_risk_rejection_opens_tagged_shadow_episode() -> None:
    intent_ts = BAR_START + timedelta(minutes=1)
    shadow_fill_ts = intent_ts + timedelta(minutes=1)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 101.0, 99.0, 100.0),
                (
                    BAR_START + timedelta(minutes=1),
                    100.0,
                    106.0,
                    99.0,
                    105.0,
                ),
            )
        }
    )
    algo = ScriptedAlgo(
        "bracketed-rejection",
        {intent_ts: [_intent("bracketed-rejection", intent_ts)]},
    )
    telemetry = CollectingTelemetry()
    runner, real_book, shadow_book = _runner(
        data=data,
        telemetry=telemetry,
        roster=[RosterEntry(algo, "emitting")],
    )

    runner.start_day(BAR_START.date())
    real_book.state.entries_today = _risk_config().rails.max_entries_per_day
    runner.process_bar(intent_ts)
    runner.process_bar(shadow_fill_ts)

    rejections = [
        record for record in telemetry.records if record["ev"] == "rejection"
    ]
    assert [record["rule"] for record in rejections] == ["max_entries_per_day"]
    shadow_fills = [
        record
        for record in telemetry.records
        if record["ev"] == "fill" and record["book"] == "shadow"
    ]
    assert [(record["kind"], record["tag"]) for record in shadow_fills] == [
        ("entry", "rejected"),
        ("target", "rejected"),
    ]
    shadow_closes = [
        record
        for record in telemetry.records
        if record["ev"] == "position_closed" and record["book"] == "shadow"
    ]
    assert [record["tag"] for record in shadow_closes] == ["rejected"]
    assert shadow_book.resolved_r_multiples("bracketed-rejection") == pytest.approx(
        [1.0]
    )
