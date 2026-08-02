"""Tests for execution session composition and driving loops."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from trader.contracts import Intent, LookaheadError, MarketData
from trader.contracts.errors import ContractViolation
from trader.contracts.testing import CollectingTelemetry, FakeClock, FakeMarketData
from trader.execution.book import RealBook, ShadowBook
from trader.execution.broker import ApiBroker, ManualBroker, SimBroker
from trader.execution.clock import BacktestClock, LiveClock
from trader.execution.config import (
    AccountConfig,
    DrawdownStopConfig,
    ExecutionConfig,
    FillsConfig,
    RailsConfig,
    RiskConfig,
    load_config,
)
from trader.execution.driver import (
    compose_algos_from_roster,
    compose_real_market_data,
    generate_session_id,
    run_backtest,
    run_live_cadence,
    run_session_command,
    select_broker,
    select_clock,
)
from trader.execution.risk import RiskRails
from trader.execution.session import RosterEntry, SessionRunner, SessionSummary


REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
UTC = timezone.utc


class ScriptedAlgo:
    """Record runner calls and optionally interrupt at one bar."""

    def __init__(
        self,
        algo_id: str,
        *,
        warmup_log: list[tuple[str, date]],
        interrupt_at: datetime | None = None,
    ) -> None:
        self.id = algo_id
        self._warmup_log = warmup_log
        self._interrupt_at = interrupt_at
        self.on_bar_calls: list[datetime] = []

    def warmup(self, day: date, data: MarketData) -> None:
        del data
        self._warmup_log.append((self.id, day))

    def on_bar(self, asof: datetime, data: MarketData) -> list[Intent]:
        del data
        self.on_bar_calls.append(asof)
        if asof == self._interrupt_at:
            raise KeyboardInterrupt
        return []


class SwappableMarketData:
    """Expose a progressively replaced FakeMarketData snapshot."""

    def __init__(self, calendar_source: FakeMarketData) -> None:
        self._calendar = calendar_source.calendar()
        self.current: FakeMarketData | None = None

    def bars_1m(
        self,
        symbol: str,
        *,
        asof: datetime,
        lookback_minutes: int | None = None,
    ) -> pd.DataFrame:
        if self.current is None:
            raise LookaheadError("minute data has not arrived")
        return self.current.bars_1m(
            symbol,
            asof=asof,
            lookback_minutes=lookback_minutes,
        )

    def bars_1d(
        self,
        symbol: str,
        *,
        asof: date,
        lookback_days: int,
    ) -> pd.DataFrame:
        if self.current is None:
            raise LookaheadError("daily data has not arrived")
        return self.current.bars_1d(
            symbol,
            asof=asof,
            lookback_days=lookback_days,
        )

    def signal(
        self,
        name: str,
        *,
        asof: datetime,
        params: Mapping[str, object] | None = None,
    ) -> float:
        if self.current is None:
            raise LookaheadError("signals have not arrived")
        return self.current.signal(name, asof=asof, params=params)

    def event(self, kind: str, *, asof: datetime) -> dict | None:
        if self.current is None:
            raise LookaheadError("events have not arrived")
        return self.current.event(kind, asof=asof)

    def calendar(self):
        return self._calendar


def _frame(*bar_starts: datetime) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1_000.0}
            for _ in bar_starts
        ],
        index=pd.DatetimeIndex(bar_starts, name="ts"),
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


def _execution_config(*, broker: str = "sim") -> ExecutionConfig:
    return ExecutionConfig(
        broker=broker,
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
    mode: str,
    data: MarketData,
    telemetry: CollectingTelemetry,
    algo: ScriptedAlgo,
) -> SessionRunner:
    risk_config = _risk_config()
    execution_config = _execution_config()
    return SessionRunner(
        session_id=f"{mode}-fixed-session",
        mode=mode,
        market_data=data,
        broker=SimBroker(execution_config),
        risk=RiskRails(risk_config),
        real_book=RealBook(risk_config),
        shadow_book=ShadowBook(execution_config),
        telemetry=telemetry,
        roster=[RosterEntry(algo=algo, status="emitting")],
        symbols=["SNXX"],
        config_sha256="fixture-sha256",
        package_version="0.1.0",
    )


@pytest.mark.parametrize(
    ("mode", "now", "expected"),
    [
        (
            "backtest",
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            "backtest-20260102-030405",
        ),
        (
            "paper",
            datetime(2026, 11, 12, 13, 14, 15, tzinfo=UTC),
            "paper-20261112-131415",
        ),
        (
            "live",
            datetime(2027, 9, 8, 7, 6, 5, tzinfo=UTC),
            "live-20270908-070605",
        ),
    ],
)
def test_generate_session_id_uses_mode_and_zero_padded_wall_clock(
    mode: str,
    now: datetime,
    expected: str,
) -> None:
    assert generate_session_id(mode, now=now) == expected


def test_select_clock_builds_backtest_clock_at_required_start() -> None:
    start = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)

    clock = select_clock("backtest", start=start)

    assert isinstance(clock, BacktestClock)
    assert clock.now() == start


def test_select_clock_rejects_backtest_without_start() -> None:
    with pytest.raises(ContractViolation, match="backtest.*start"):
        select_clock("backtest")


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_select_clock_builds_injected_live_clock(mode: str) -> None:
    fixed_now = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    sleeps: list[float] = []

    clock = select_clock(mode, now_fn=lambda: fixed_now, sleep=sleeps.append)

    assert isinstance(clock, LiveClock)
    assert clock.now() == fixed_now
    clock.sleep_until(fixed_now + timedelta(seconds=7))
    assert sleeps == [7.0]


@pytest.mark.parametrize(
    ("broker_name", "broker_type"),
    [
        ("sim", SimBroker),
        ("manual", ManualBroker),
        ("api", ApiBroker),
    ],
)
def test_select_broker_dispatches_configured_implementation(
    broker_name: str,
    broker_type: type,
    tmp_path: Path,
) -> None:
    broker = select_broker(
        _execution_config(broker=broker_name),
        state_dir=tmp_path,
        timezone="America/New_York",
    )

    assert isinstance(broker, broker_type)


def test_select_broker_rejects_unknown_name(tmp_path: Path) -> None:
    invalid = replace(_execution_config(), broker="carrier-pigeon")

    with pytest.raises(ContractViolation, match="carrier-pigeon"):
        select_broker(
            invalid,
            state_dir=tmp_path,
            timezone="America/New_York",
        )


def test_run_backtest_drives_every_bar_and_uses_first_bar_for_session_start() -> None:
    first_day = date(2026, 7, 1)
    second_day = date(2026, 7, 2)
    first_open = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    second_open = datetime(2026, 7, 2, 13, 30, tzinfo=UTC)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                *(first_open + timedelta(minutes=index) for index in range(3)),
                *(second_open + timedelta(minutes=index) for index in range(3)),
            )
        }
    )
    warmups: list[tuple[str, date]] = []
    algo = ScriptedAlgo("scripted", warmup_log=warmups)
    telemetry = CollectingTelemetry()
    runner = _runner(
        mode="backtest",
        data=data,
        telemetry=telemetry,
        algo=algo,
    )

    summary = run_backtest(
        runner,
        trading_days=[first_day, second_day],
        rth_open=time(9, 30),
        rth_close=time(9, 33),
        timezone="America/New_York",
    )

    expected_asofs = [
        *(first_open + timedelta(minutes=index) for index in range(1, 4)),
        *(second_open + timedelta(minutes=index) for index in range(1, 4)),
    ]
    assert warmups == [("scripted", first_day), ("scripted", second_day)]
    assert algo.on_bar_calls == expected_asofs
    assert summary == SessionSummary(
        bars_processed=6,
        real_trades=0,
        shadow_trades=0,
        final_equity=10_000.0,
    )
    assert telemetry.records[0]["ev"] == "session_start"
    assert telemetry.records[0]["ts"] == "2026-07-01T13:31:00Z"
    assert telemetry.records[-1]["ev"] == "session_end"
    assert telemetry.records[-1]["ts"] == "2026-07-02T13:33:00Z"


def test_run_backtest_ends_session_once_after_keyboard_interrupt() -> None:
    day = date(2026, 7, 1)
    market_open = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    interrupt_at = market_open + timedelta(minutes=2)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                *(market_open + timedelta(minutes=index) for index in range(3))
            )
        }
    )
    warmups: list[tuple[str, date]] = []
    algo = ScriptedAlgo(
        "interrupting",
        warmup_log=warmups,
        interrupt_at=interrupt_at,
    )
    telemetry = CollectingTelemetry()
    runner = _runner(
        mode="backtest",
        data=data,
        telemetry=telemetry,
        algo=algo,
    )

    summary = run_backtest(
        runner,
        trading_days=[day],
        rth_open=time(9, 30),
        rth_close=time(9, 33),
        timezone="America/New_York",
    )

    assert summary.bars_processed == 1
    assert algo.on_bar_calls == [
        market_open + timedelta(minutes=1),
        interrupt_at,
    ]
    session_end_records = [
        record for record in telemetry.records if record["ev"] == "session_end"
    ]
    assert len(session_end_records) == 1
    assert session_end_records[0]["ts"] == "2026-07-01T13:32:00Z"


def test_run_live_cadence_processes_only_progressively_visible_bars() -> None:
    session_start = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    session_close = session_start + timedelta(minutes=6)
    final_frame = _frame(
        *(session_start + timedelta(minutes=index) for index in range(1, 6))
    )
    data = SwappableMarketData(FakeMarketData({"SNXX": final_frame}))
    warmups: list[tuple[str, date]] = []
    algo = ScriptedAlgo("live-scripted", warmup_log=warmups)
    telemetry = CollectingTelemetry()
    runner = _runner(mode="paper", data=data, telemetry=telemetry, algo=algo)
    clock = FakeClock(session_start)
    cycles: list[datetime] = []

    def on_cycle() -> None:
        cycles.append(clock.now())
        if len(cycles) == 2:
            data.current = FakeMarketData(
                {
                    "SNXX": _frame(
                        session_start + timedelta(minutes=1),
                        session_start + timedelta(minutes=2),
                    )
                }
            )
        elif len(cycles) == 3:
            data.current = FakeMarketData({"SNXX": final_frame})

    summary = run_live_cadence(
        runner,
        clock=clock,
        market_data=data,
        primary_symbol="SNXX",
        cycle_minutes=2,
        trading_day=session_start.date(),
        session_start=session_start,
        session_close=session_close,
        on_cycle=on_cycle,
    )

    assert cycles == [
        session_start + timedelta(minutes=2),
        session_start + timedelta(minutes=4),
        session_start + timedelta(minutes=6),
    ]
    assert warmups == [("live-scripted", session_start.date())]
    assert algo.on_bar_calls == [
        session_start + timedelta(minutes=index) for index in range(1, 6)
    ]
    assert summary.bars_processed == 5
    assert clock.now() == session_close
    assert telemetry.records[0]["ts"] == "2026-07-01T13:30:00Z"
    assert telemetry.records[-1]["ts"] == "2026-07-01T13:35:00Z"


def test_run_live_cadence_ends_session_once_after_keyboard_interrupt() -> None:
    session_start = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    session_close = session_start + timedelta(minutes=6)
    interrupt_at = session_start + timedelta(minutes=2)
    frame = _frame(
        *(session_start + timedelta(minutes=index) for index in range(1, 5))
    )
    data = FakeMarketData({"SNXX": frame})
    warmups: list[tuple[str, date]] = []
    algo = ScriptedAlgo(
        "interrupting-live",
        warmup_log=warmups,
        interrupt_at=interrupt_at,
    )
    telemetry = CollectingTelemetry()
    runner = _runner(mode="live", data=data, telemetry=telemetry, algo=algo)
    cycle_calls = 0

    def on_cycle() -> None:
        nonlocal cycle_calls
        cycle_calls += 1

    summary = run_live_cadence(
        runner,
        clock=FakeClock(session_start),
        market_data=data,
        primary_symbol="SNXX",
        cycle_minutes=2,
        trading_day=session_start.date(),
        session_start=session_start,
        session_close=session_close,
        on_cycle=on_cycle,
    )

    assert cycle_calls == 1
    assert summary.bars_processed == 1
    session_end_records = [
        record for record in telemetry.records if record["ev"] == "session_end"
    ]
    assert len(session_end_records) == 1
    assert session_end_records[0]["ts"] == "2026-07-01T13:32:00Z"


def test_compose_real_market_data_wraps_missing_provider_import() -> None:
    resolved = load_config(REPO_CONFIG_DIR)

    with pytest.raises(ContractViolation, match=r"trader\.provider"):
        compose_real_market_data(resolved)


def test_compose_algos_from_roster_wraps_missing_algos_import() -> None:
    resolved = load_config(REPO_CONFIG_DIR)

    with pytest.raises(ContractViolation, match=r"trader\.algos"):
        compose_algos_from_roster(resolved)


def test_register_parses_run_arguments_and_attaches_session_handler() -> None:
    from trader.execution.cli import register

    parser = argparse.ArgumentParser(prog="trader")
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)

    args = parser.parse_args(
        [
            "run",
            "--mode",
            "backtest",
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-01",
        ]
    )

    assert args.func is run_session_command
    assert args.mode == "backtest"
    assert args.start == "2026-07-01"
    assert args.end == "2026-07-01"
