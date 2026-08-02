"""Tests for capital-slot sizing and hard execution risk rails."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trader.contracts import (
    ContractViolation,
    Intent,
    LookaheadError,
    OrderTicket,
    PortfolioState,
    PositionState,
    Rejection,
)
from trader.contracts.testing import FakeMarketData
from trader.execution.config import (
    AccountConfig,
    DrawdownStopConfig,
    RailsConfig,
    RiskConfig,
)
from trader.execution.risk import RiskRails
import trader.execution.risk as risk_module


INTENT_TS = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
INSTRUMENT = "SNXX"


def _risk_config(
    *,
    equity: float = 10_000.0,
    capital_fraction: float = 1.0,
    day_slots: int = 2,
    max_entries_per_day: int = 2,
    one_position_at_a_time: bool = True,
    no_hedge: bool = True,
) -> RiskConfig:
    return RiskConfig(
        account=AccountConfig(
            equity=equity,
            capital_fraction=capital_fraction,
            day_slots=day_slots,
        ),
        rails=RailsConfig(
            max_entries_per_day=max_entries_per_day,
            one_position_at_a_time=one_position_at_a_time,
            no_hedge=no_hedge,
            mute_after_consecutive_stops=99,
            mute_after_cumulative_day_r=-99.0,
            reversal_cooldown_minutes=99,
        ),
        drawdown_stop=DrawdownStopConfig(max_session_drawdown_r=None),
    )


def _intent(**changes: object) -> Intent:
    intent = Intent(
        algo_id="opening-breakout",
        ts=INTENT_TS,
        action="open",
        side="long",
        signal_symbol="SNDK",
        instrument=INSTRUMENT,
        entry="market_next_open",
        stop=95.0,
        target=110.0,
        confidence=0.8,
        reason="controlled test intent",
        meta={"fixture": True},
    )
    return replace(intent, **changes)


def _position(*, instrument: str = INSTRUMENT) -> PositionState:
    return PositionState(
        instrument=instrument,
        side="long",
        shares=10,
        entry_price=90.0,
        entry_ts=INTENT_TS - timedelta(minutes=15),
        stop=85.0,
        target=105.0,
        algo_id="already-open",
    )


def _portfolio(**changes: object) -> PortfolioState:
    portfolio = PortfolioState(
        cash=10_000.0,
        equity=10_000.0,
        positions=[],
        entries_today=0,
        realized_r_today=0.0,
        muted_until=None,
    )
    return replace(portfolio, **changes)


def _frame(*rows: tuple[datetime, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "o": close - 0.25,
                "h": close + 0.5,
                "l": close - 0.5,
                "c": close,
                "v": 1_000.0,
            }
            for _ts, close in rows
        ],
        index=pd.DatetimeIndex([ts for ts, _close in rows], name="ts"),
    )


def _data(
    close: float = 100.0,
    *,
    bar_ts: datetime = INTENT_TS,
    later_close: float | None = None,
) -> FakeMarketData:
    rows = [(bar_ts, close)]
    if later_close is not None:
        rows.append((bar_ts + timedelta(minutes=1), later_close))
    return FakeMarketData({INSTRUMENT: _frame(*rows)})


def _assert_rejection(
    result: OrderTicket | Rejection,
    intent: Intent,
    rule: str,
) -> None:
    assert isinstance(result, Rejection)
    assert result.rule == rule
    assert result.intent is intent
    assert result.detail


def test_muted_rejects_future_deadline_but_past_and_none_are_controls() -> None:
    engine = RiskRails(_risk_config())
    intent = _intent()

    rejected = engine.check_and_size(
        intent,
        _portfolio(muted_until=INTENT_TS + timedelta(seconds=1)),
        _data(),
    )
    past_control = engine.check_and_size(
        intent,
        _portfolio(muted_until=INTENT_TS - timedelta(seconds=1)),
        _data(),
    )
    none_control = engine.check_and_size(intent, _portfolio(), _data())

    _assert_rejection(rejected, intent, "muted")
    assert isinstance(past_control, OrderTicket)
    assert isinstance(none_control, OrderTicket)


@pytest.mark.parametrize("entries_today", [2, 3])
def test_max_entries_rejects_at_or_over_limit_with_under_limit_control(
    entries_today: int,
) -> None:
    engine = RiskRails(_risk_config(max_entries_per_day=2))
    intent = _intent()

    rejected = engine.check_and_size(
        intent,
        _portfolio(entries_today=entries_today),
        _data(),
    )
    control = engine.check_and_size(
        intent,
        _portfolio(entries_today=1),
        _data(),
    )

    _assert_rejection(rejected, intent, "max_entries_per_day")
    assert isinstance(control, OrderTicket)


def test_one_position_at_a_time_rejects_existing_position_with_empty_control() -> None:
    engine = RiskRails(_risk_config())
    intent = _intent()

    rejected = engine.check_and_size(
        intent,
        _portfolio(positions=[_position()]),
        _data(),
    )
    control = engine.check_and_size(intent, _portfolio(positions=[]), _data())

    _assert_rejection(rejected, intent, "one_position_at_a_time")
    assert isinstance(control, OrderTicket)


def test_no_hedge_is_independent_when_one_position_rail_is_disabled() -> None:
    engine = RiskRails(
        _risk_config(one_position_at_a_time=False, no_hedge=True)
    )
    intent = _intent()

    rejected = engine.check_and_size(
        intent,
        _portfolio(positions=[_position(instrument="SNDQ")]),
        _data(),
    )
    control = engine.check_and_size(
        intent,
        _portfolio(positions=[_position(instrument=INSTRUMENT)]),
        _data(),
    )

    _assert_rejection(rejected, intent, "no_hedge")
    assert isinstance(control, OrderTicket)


def test_no_stop_rejects_missing_stop_with_present_stop_control() -> None:
    engine = RiskRails(_risk_config())
    rejected_intent = _intent(stop=None)
    control_intent = _intent(stop=95.0)

    rejected = engine.check_and_size(rejected_intent, _portfolio(), _data())
    control = engine.check_and_size(control_intent, _portfolio(), _data())

    _assert_rejection(rejected, rejected_intent, "no_stop")
    assert isinstance(control, OrderTicket)


def test_no_target_rejects_missing_target_with_present_target_control() -> None:
    engine = RiskRails(_risk_config())
    rejected_intent = _intent(target=None)
    control_intent = _intent(target=110.0)

    rejected = engine.check_and_size(rejected_intent, _portfolio(), _data())
    control = engine.check_and_size(control_intent, _portfolio(), _data())

    _assert_rejection(rejected, rejected_intent, "no_target")
    assert isinstance(control, OrderTicket)


def test_no_stop_precedes_no_target_when_both_are_missing() -> None:
    engine = RiskRails(_risk_config())
    intent = _intent(stop=None, target=None)

    rejected = engine.check_and_size(intent, _portfolio(), _data())

    _assert_rejection(rejected, intent, "no_stop")


def test_no_price_data_rejects_empty_visible_frame_with_visible_bar_control() -> None:
    engine = RiskRails(_risk_config())
    intent = _intent()
    not_yet_visible = _data(bar_ts=INTENT_TS + timedelta(minutes=1))

    rejected = engine.check_and_size(intent, _portfolio(), not_yet_visible)
    control = engine.check_and_size(intent, _portfolio(), _data())

    _assert_rejection(rejected, intent, "no_price_data")
    assert isinstance(control, OrderTicket)


def test_no_stop_distance_rejects_stop_equal_to_close_with_distance_control() -> None:
    engine = RiskRails(_risk_config())
    rejected_intent = _intent(stop=100.0)
    control_intent = _intent(stop=99.99)

    rejected = engine.check_and_size(rejected_intent, _portfolio(), _data(100.0))
    control = engine.check_and_size(control_intent, _portfolio(), _data(100.0))

    _assert_rejection(rejected, rejected_intent, "no_stop_distance")
    assert isinstance(control, OrderTicket)


def test_unsized_rejects_unaffordable_share_with_affordable_price_control() -> None:
    engine = RiskRails(_risk_config(equity=10.0, day_slots=2))
    intent = _intent(stop=4.0)

    rejected = engine.check_and_size(intent, _portfolio(), _data(1_000.0))
    control = engine.check_and_size(intent, _portfolio(), _data(5.0))

    _assert_rejection(rejected, intent, "unsized")
    assert isinstance(control, OrderTicket)
    assert control.shares == 1


def test_over_slot_defensive_rail_rejects_out_of_band_share_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = RiskRails(_risk_config())
    intent = _intent()
    portfolio = _portfolio()
    data = _data(100.0)

    control = engine.check_and_size(intent, portfolio, data)
    assert isinstance(control, OrderTicket)

    # The public floor cannot overspend mathematically. Force a corrupted
    # internal sizing result so this assertion-shaped final rail is reachable.
    monkeypatch.setattr(risk_module, "_shares_for_slot", lambda *_args: 51)
    rejected = engine.check_and_size(intent, portfolio, data)

    _assert_rejection(rejected, intent, "over_slot")


@pytest.mark.parametrize(
    ("equity", "capital_fraction", "day_slots", "close", "expected_shares"),
    [
        (10_000.0, 1.0, 2, 100.0, 50),
        (12_000.0, 0.75, 3, 37.25, 80),
    ],
)
def test_happy_path_floors_exact_slot_shares(
    equity: float,
    capital_fraction: float,
    day_slots: int,
    close: float,
    expected_shares: int,
) -> None:
    engine = RiskRails(
        _risk_config(
            equity=equity,
            capital_fraction=capital_fraction,
            day_slots=day_slots,
        )
    )

    result = engine.check_and_size(_intent(stop=35.0), _portfolio(), _data(close))

    assert isinstance(result, OrderTicket)
    assert result.shares == expected_shares


def test_happy_path_builds_exact_ticket_from_latest_visible_bar() -> None:
    engine = RiskRails(_risk_config())
    intent = _intent()
    portfolio = _portfolio(entries_today=1)

    result = engine.check_and_size(
        intent,
        portfolio,
        _data(100.004, later_close=1_000.0),
    )

    assert result == OrderTicket(
        ticket_id="opening-breakout-20260701T133000Z-1",
        algo_id="opening-breakout",
        intent_ts=INTENT_TS,
        instrument=INSTRUMENT,
        side="long",
        shares=50,
        entry="market_next_open",
        stop=95.0,
        target=110.0,
        risk={"slot": 2, "dollars": 250.0, "equity": 10_000.0},
        created_ts=INTENT_TS,
    )


def test_ticket_id_is_deterministic_across_separate_calls() -> None:
    engine = RiskRails(_risk_config())
    intent = _intent()
    first = engine.check_and_size(intent, _portfolio(), _data())
    second = engine.check_and_size(intent, _portfolio(), _data())

    assert isinstance(first, OrderTicket)
    assert isinstance(second, OrderTicket)
    assert first.ticket_id == second.ticket_id
    assert first == second


def test_market_data_request_reveals_intent_bar_without_future_bar() -> None:
    class RecordingFakeMarketData(FakeMarketData):
        def __init__(self) -> None:
            super().__init__(
                {INSTRUMENT: _frame((INTENT_TS, 100.0), (INTENT_TS + timedelta(minutes=1), 999.0))}
            )
            self.requests: list[tuple[str, datetime, int | None]] = []

        def bars_1m(
            self,
            symbol: str,
            *,
            asof: datetime,
            lookback_minutes: int | None = None,
        ) -> pd.DataFrame:
            self.requests.append((symbol, asof, lookback_minutes))
            return super().bars_1m(
                symbol,
                asof=asof,
                lookback_minutes=lookback_minutes,
            )

    data = RecordingFakeMarketData()

    result = RiskRails(_risk_config()).check_and_size(
        _intent(),
        _portfolio(),
        data,
    )

    assert isinstance(result, OrderTicket)
    assert result.shares == 50
    assert data.requests == [(INSTRUMENT, INTENT_TS + timedelta(minutes=1), 1)]


def test_lookahead_error_from_market_data_propagates() -> None:
    stale_data = _data(bar_ts=INTENT_TS - timedelta(minutes=1))

    with pytest.raises(LookaheadError):
        RiskRails(_risk_config()).check_and_size(
            _intent(),
            _portfolio(),
            stale_data,
        )


def test_first_rail_wins_when_multiple_conditions_are_invalid() -> None:
    intent = _intent(stop=None)
    portfolio = _portfolio(
        entries_today=2,
        positions=[_position()],
        muted_until=INTENT_TS + timedelta(minutes=1),
    )

    result = RiskRails(_risk_config()).check_and_size(intent, portfolio, _data())

    _assert_rejection(result, intent, "muted")


def test_close_intent_raises_contract_violation() -> None:
    close_intent = _intent(action="close", side=None, stop=None, target=None)

    with pytest.raises(
        ContractViolation,
        match=r"close intents.*session loop.*risk engine",
    ):
        RiskRails(_risk_config()).check_and_size(
            close_intent,
            _portfolio(),
            _data(),
        )
