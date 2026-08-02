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
    ExecutionConfig,
    FillsConfig,
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
    max_session_drawdown_r: float | None = None,
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
        drawdown_stop=DrawdownStopConfig(
            max_session_drawdown_r=max_session_drawdown_r
        ),
    )


def _execution_config(
    *,
    etf_price_basis: str = "real",
    min_intraday_bars: int = 1,
) -> ExecutionConfig:
    return ExecutionConfig(
        broker="sim",
        live_orders=False,
        fills=FillsConfig(
            commission=0.0,
            etf_price_basis=etf_price_basis,
            min_intraday_bars=min_intraday_bars,
        ),
        slippage_bps={
            "SNXX": {"2026-07": 0.0},
            "SNDQ": {"2026-07": 0.0},
        },
    )


def _risk_rails(
    config: RiskConfig | None = None,
    *,
    execution_config: ExecutionConfig | None = None,
) -> RiskRails:
    return RiskRails(
        _risk_config() if config is None else config,
        _execution_config() if execution_config is None else execution_config,
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
    bar_ts: datetime = INTENT_TS - timedelta(minutes=1),
    later_close: float | None = None,
    symbol: str = INSTRUMENT,
) -> FakeMarketData:
    rows = [(bar_ts, close)]
    if later_close is not None:
        rows.append((bar_ts + timedelta(minutes=1), later_close))
    return FakeMarketData({symbol: _frame(*rows)})


def _basis_data(
    *,
    sndk_prev_close: float = 100.0,
    etf_prev_close: float = 50.0,
    sndk_close: float = 90.0,
    etf_close: float = 100.0,
    instrument: str = INSTRUMENT,
) -> FakeMarketData:
    previous_close_ts = INTENT_TS - timedelta(days=1)
    current_ts = INTENT_TS - timedelta(minutes=1)
    return FakeMarketData(
        {
            "SNDK": _frame(
                (previous_close_ts, sndk_prev_close),
                (current_ts, sndk_close),
            ),
            instrument: _frame(
                (previous_close_ts, etf_prev_close),
                (current_ts, etf_close),
            ),
        }
    )


class NoFutureAsOfMarketData:
    """Wrap a fake and reject any minute request after the allowed asof."""

    def __init__(self, wrapped: FakeMarketData, latest_asof: datetime) -> None:
        self._wrapped = wrapped
        self._latest_asof = latest_asof
        self.requests: list[tuple[str, datetime, int | None]] = []

    def bars_1m(
        self,
        symbol: str,
        *,
        asof: datetime,
        lookback_minutes: int | None = None,
    ) -> pd.DataFrame:
        if asof > self._latest_asof:
            raise LookaheadError(
                f"minute request {asof.isoformat()} exceeds intent close "
                f"{self._latest_asof.isoformat()}"
            )
        self.requests.append((symbol, asof, lookback_minutes))
        return self._wrapped.bars_1m(
            symbol,
            asof=asof,
            lookback_minutes=lookback_minutes,
        )

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)


class BarsForbiddenMarketData:
    """Raise if a rail that should run before price lookup asks for bars."""

    def bars_1m(
        self,
        symbol: str,
        *,
        asof: datetime,
        lookback_minutes: int | None = None,
    ) -> pd.DataFrame:
        del symbol, asof, lookback_minutes
        raise AssertionError("early rail should run before bars_1m")


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
    engine = _risk_rails(_risk_config())
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


def test_drawdown_stop_disabled_allows_portfolio_past_loss_threshold() -> None:
    engine = _risk_rails(_risk_config(max_session_drawdown_r=None))
    intent = _intent()

    result = engine.check_and_size(
        intent,
        _portfolio(realized_r_today=-999.0),
        _data(),
    )

    assert isinstance(result, OrderTicket)


@pytest.mark.parametrize("realized_r_today", [-3.0, -3.01])
def test_drawdown_stop_rejects_at_or_beyond_negative_threshold_before_price_lookup(
    realized_r_today: float,
) -> None:
    engine = _risk_rails(_risk_config(max_session_drawdown_r=3.0))
    intent = _intent()

    rejected = engine.check_and_size(
        intent,
        _portfolio(realized_r_today=realized_r_today),
        BarsForbiddenMarketData(),
    )
    control = engine.check_and_size(
        intent,
        _portfolio(realized_r_today=-2.99),
        _data(),
    )

    _assert_rejection(rejected, intent, "drawdown_stop")
    assert isinstance(control, OrderTicket)


@pytest.mark.parametrize("entries_today", [2, 3])
def test_max_entries_rejects_at_or_over_limit_with_under_limit_control(
    entries_today: int,
) -> None:
    engine = _risk_rails(_risk_config(max_entries_per_day=2))
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
    engine = _risk_rails(_risk_config())
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
    engine = _risk_rails(
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
    engine = _risk_rails(_risk_config())
    rejected_intent = _intent(stop=None)
    control_intent = _intent(stop=95.0)

    rejected = engine.check_and_size(rejected_intent, _portfolio(), _data())
    control = engine.check_and_size(control_intent, _portfolio(), _data())

    _assert_rejection(rejected, rejected_intent, "no_stop")
    assert isinstance(control, OrderTicket)


def test_no_target_rejects_missing_target_with_present_target_control() -> None:
    engine = _risk_rails(_risk_config())
    rejected_intent = _intent(target=None)
    control_intent = _intent(target=110.0)

    rejected = engine.check_and_size(rejected_intent, _portfolio(), _data())
    control = engine.check_and_size(control_intent, _portfolio(), _data())

    _assert_rejection(rejected, rejected_intent, "no_target")
    assert isinstance(control, OrderTicket)


def test_no_stop_precedes_no_target_when_both_are_missing() -> None:
    engine = _risk_rails(_risk_config())
    intent = _intent(stop=None, target=None)

    rejected = engine.check_and_size(intent, _portfolio(), _data())

    _assert_rejection(rejected, intent, "no_stop")


def test_no_price_data_rejects_empty_visible_frame_with_visible_bar_control() -> None:
    engine = _risk_rails(_risk_config())
    intent = _intent()
    not_yet_visible = _data(bar_ts=INTENT_TS + timedelta(minutes=1))

    rejected = engine.check_and_size(intent, _portfolio(), not_yet_visible)
    control = engine.check_and_size(intent, _portfolio(), _data())

    _assert_rejection(rejected, intent, "no_price_data")
    assert isinstance(control, OrderTicket)


@pytest.mark.parametrize(
    ("side", "stop", "target"),
    [
        ("long", 101.0, 110.0),
        ("short", 101.0, 110.0),
    ],
    ids=["long-stop-above-entry", "short-stop-above-entry"],
)
def test_degenerate_bracket_rejects_stop_on_wrong_side_of_entry_reference(
    side: str,
    stop: float,
    target: float,
) -> None:
    engine = _risk_rails(_risk_config())
    intent = _intent(side=side, stop=stop, target=target)
    data = NoFutureAsOfMarketData(
        _data(100.0),
        latest_asof=INTENT_TS,
    )

    rejected = engine.check_and_size(intent, _portfolio(), data)

    _assert_rejection(rejected, intent, "degenerate_bracket")
    assert data.requests == [
        (INSTRUMENT, INTENT_TS, None),
        (INSTRUMENT, INTENT_TS, 1),
    ]


@pytest.mark.parametrize(
    ("side", "target"),
    [
        ("long", 110.0),
        ("short", 110.0),
    ],
    ids=["long-stop-equals-entry", "short-stop-equals-entry"],
)
def test_degenerate_bracket_rejects_stop_equal_to_entry_reference(
    side: str,
    target: float,
) -> None:
    engine = _risk_rails(_risk_config())
    intent = _intent(side=side, stop=100.0, target=target)
    data = NoFutureAsOfMarketData(
        _data(100.0),
        latest_asof=INTENT_TS,
    )

    rejected = engine.check_and_size(intent, _portfolio(), data)

    _assert_rejection(rejected, intent, "degenerate_bracket")
    assert data.requests == [
        (INSTRUMENT, INTENT_TS, None),
        (INSTRUMENT, INTENT_TS, 1),
    ]


@pytest.mark.parametrize(
    ("side", "stop", "target"),
    [
        ("long", 95.0, 95.0),
        ("short", 95.0, 95.0),
    ],
    ids=["long-target-not-above-stop", "short-target-not-above-stop"],
)
def test_degenerate_bracket_rejects_structural_stop_target_without_price_data(
    side: str,
    stop: float,
    target: float,
) -> None:
    engine = _risk_rails(_risk_config())
    intent = _intent(side=side, stop=stop, target=target)

    rejected = engine.check_and_size(
        intent,
        _portfolio(),
        BarsForbiddenMarketData(),
    )

    _assert_rejection(rejected, intent, "degenerate_bracket")


@pytest.mark.parametrize(
    "side",
    ["long", "short"],
    ids=["long", "short"],
)
def test_degenerate_bracket_rejects_inverted_instrument_bracket_for_any_side(
    side: str,
) -> None:
    engine = _risk_rails(_risk_config())
    intent = _intent(side=side, stop=110.0, target=100.0)

    rejected = engine.check_and_size(
        intent,
        _portfolio(),
        BarsForbiddenMarketData(),
    )

    _assert_rejection(rejected, intent, "degenerate_bracket")


@pytest.mark.parametrize(
    ("side", "stop", "target", "expected_risk_dollars"),
    [
        ("long", 95.0, 110.0, 250.0),
        ("short", 95.0, 110.0, 250.0),
    ],
    ids=["long", "short"],
)
def test_valid_brackets_preserve_risk_dollars(
    side: str,
    stop: float,
    target: float,
    expected_risk_dollars: float,
) -> None:
    engine = _risk_rails(_risk_config())
    intent = _intent(side=side, stop=stop, target=target)

    result = engine.check_and_size(intent, _portfolio(), _data(100.0))

    assert isinstance(result, OrderTicket)
    assert result.shares == 50
    assert result.risk["dollars"] == expected_risk_dollars


def test_sizes_from_synthetic_basis_on_thin_etf_day() -> None:
    engine = _risk_rails(
        _risk_config(),
        execution_config=_execution_config(
            etf_price_basis="auto",
            min_intraday_bars=2,
        ),
    )
    intent = _intent(stop=35.0, target=60.0)

    result = engine.check_and_size(
        intent,
        _portfolio(),
        _basis_data(
            sndk_prev_close=100.0,
            etf_prev_close=50.0,
            sndk_close=90.0,
            etf_close=100.0,
        ),
    )

    assert isinstance(result, OrderTicket)
    assert result.shares == 125
    assert result.risk["dollars"] == 625.0


def test_synthetic_basis_rejects_no_price_data_without_previous_close_anchors() -> None:
    engine = _risk_rails(
        _risk_config(),
        execution_config=_execution_config(etf_price_basis="synthetic"),
    )
    intent = _intent(stop=35.0, target=60.0)
    data = FakeMarketData(
        {
            "SNDK": _frame((INTENT_TS - timedelta(minutes=1), 90.0)),
            INSTRUMENT: _frame((INTENT_TS - timedelta(minutes=1), 100.0)),
        }
    )

    rejected = engine.check_and_size(intent, _portfolio(), data)

    _assert_rejection(rejected, intent, "no_price_data")


@pytest.mark.parametrize(
    (
        "algo_id",
        "intent_ts",
        "stop",
        "target",
        "entry_ref",
        "expected_shares",
        "expected_risk_dollars",
    ),
    [
        (
            "lateday_momentum",
            datetime(2026, 7, 27, 18, 1, tzinfo=timezone.utc),
            40.92,
            43.17,
            41.25,
            121,
            39.93,
        ),
        (
            "gap_play",
            datetime(2026, 7, 28, 13, 48, tzinfo=timezone.utc),
            50.23,
            56.17,
            53.20,
            93,
            276.21,
        ),
        (
            "lateday_momentum",
            datetime(2026, 7, 28, 18, 1, tzinfo=timezone.utc),
            52.61,
            55.51,
            53.20,
            93,
            54.87,
        ),
        (
            "orb5",
            datetime(2026, 7, 29, 13, 38, tzinfo=timezone.utc),
            50.69,
            65.93,
            60.79,
            82,
            828.20,
        ),
        (
            "lateday_momentum",
            datetime(2026, 7, 29, 18, 1, tzinfo=timezone.utc),
            53.62,
            58.32,
            60.79,
            82,
            587.94,
        ),
    ],
)
def test_degenerate_bracket_accepts_translated_short_sndq_backtest_intents(
    algo_id: str,
    intent_ts: datetime,
    stop: float,
    target: float,
    entry_ref: float,
    expected_shares: int,
    expected_risk_dollars: float,
) -> None:
    # These are real rejected intents from backtest session
    # backtest-20260802-061527. The SNDQ cases were the regression:
    # correctly translated short-signal brackets that the side-branching rail
    # wrongly refused.
    engine = _risk_rails(_risk_config())
    intent = _intent(
        algo_id=algo_id,
        ts=intent_ts,
        side="short",
        instrument="SNDQ",
        stop=stop,
        target=target,
    )

    result = engine.check_and_size(
        intent,
        _portfolio(),
        _data(
            entry_ref,
            bar_ts=intent_ts - timedelta(minutes=1),
            symbol="SNDQ",
        ),
    )

    assert isinstance(result, OrderTicket)
    assert result.instrument == "SNDQ"
    assert result.side == "short"
    assert result.shares == expected_shares
    assert result.stop == stop
    assert result.target == target
    assert result.risk["dollars"] == expected_risk_dollars


@pytest.mark.parametrize(
    ("algo_id", "intent_ts", "side", "instrument", "stop", "target", "entry_ref"),
    [
        (
            "orb5",
            datetime(2026, 7, 28, 13, 36, tzinfo=timezone.utc),
            "long",
            "SNXX",
            8.99,
            10.72,
            8.04,
        ),
    ],
)
def test_degenerate_bracket_rejects_backtest_intent_with_stop_above_entry_ref(
    algo_id: str,
    intent_ts: datetime,
    side: str,
    instrument: str,
    stop: float,
    target: float,
    entry_ref: float,
) -> None:
    engine = _risk_rails(_risk_config())
    intent = _intent(
        algo_id=algo_id,
        ts=intent_ts,
        side=side,
        instrument=instrument,
        stop=stop,
        target=target,
    )

    rejected = engine.check_and_size(
        intent,
        _portfolio(),
        _data(
            entry_ref,
            bar_ts=intent_ts - timedelta(minutes=1),
            symbol=instrument,
        ),
    )

    _assert_rejection(rejected, intent, "degenerate_bracket")
    assert f"{stop:.2f}" in rejected.detail
    assert f"{entry_ref:.2f}" in rejected.detail


def test_unsized_rejects_unaffordable_share_with_affordable_price_control() -> None:
    engine = _risk_rails(_risk_config(equity=10.0, day_slots=2))
    intent = _intent(stop=4.0)

    rejected = engine.check_and_size(intent, _portfolio(), _data(1_000.0))
    control = engine.check_and_size(intent, _portfolio(), _data(5.0))

    _assert_rejection(rejected, intent, "unsized")
    assert isinstance(control, OrderTicket)
    assert control.shares == 1


def test_over_slot_defensive_rail_rejects_out_of_band_share_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _risk_rails(_risk_config())
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
    engine = _risk_rails(
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
    engine = _risk_rails(_risk_config())
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
    engine = _risk_rails(_risk_config())
    intent = _intent()
    first = engine.check_and_size(intent, _portfolio(), _data())
    second = engine.check_and_size(intent, _portfolio(), _data())

    assert isinstance(first, OrderTicket)
    assert isinstance(second, OrderTicket)
    assert first.ticket_id == second.ticket_id
    assert first == second


def test_market_data_request_never_advances_past_intent_close() -> None:
    data = NoFutureAsOfMarketData(
        _data(100.0, later_close=999.0),
        latest_asof=INTENT_TS,
    )

    result = _risk_rails(_risk_config()).check_and_size(
        _intent(),
        _portfolio(),
        data,
    )

    assert isinstance(result, OrderTicket)
    assert result.shares == 50
    assert data.requests == [
        (INSTRUMENT, INTENT_TS, None),
        (INSTRUMENT, INTENT_TS, 1),
    ]


def test_sizes_from_completed_intent_bar_not_following_bar() -> None:
    t0 = INTENT_TS - timedelta(minutes=2)
    t1 = INTENT_TS - timedelta(minutes=1)
    t2 = INTENT_TS
    data = FakeMarketData(
        {
            INSTRUMENT: _frame(
                (t0, 100.0),
                (t1, 101.0),
                (t2, 102.0),
            )
        }
    )

    result = _risk_rails(_risk_config(equity=10_100.0)).check_and_size(
        _intent(ts=t2),
        _portfolio(cash=10_100.0, equity=10_100.0),
        data,
    )

    assert isinstance(result, OrderTicket)
    assert result.shares == 50
    assert result.risk == {
        "slot": 1,
        "dollars": 300.0,
        "equity": 10_100.0,
    }


def test_unresolved_stale_market_data_rejects_no_price_data() -> None:
    stale_data = _data(bar_ts=INTENT_TS - timedelta(minutes=2))
    intent = _intent()

    rejected = _risk_rails(_risk_config()).check_and_size(
        intent,
        _portfolio(),
        stale_data,
    )

    _assert_rejection(rejected, intent, "no_price_data")


def test_first_rail_wins_when_multiple_conditions_are_invalid() -> None:
    intent = _intent(stop=None)
    portfolio = _portfolio(
        entries_today=2,
        positions=[_position()],
        muted_until=INTENT_TS + timedelta(minutes=1),
    )

    result = _risk_rails(_risk_config()).check_and_size(intent, portfolio, _data())

    _assert_rejection(result, intent, "muted")


def test_close_intent_raises_contract_violation() -> None:
    close_intent = _intent(action="close", side=None, stop=None, target=None)

    with pytest.raises(
        ContractViolation,
        match=r"close intents.*session loop.*risk engine",
    ):
        _risk_rails(_risk_config()).check_and_size(
            close_intent,
            _portfolio(),
            _data(),
        )
