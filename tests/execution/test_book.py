"""Tests for real/shadow book accounting and per-algo metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trader.contracts import Fill, OrderTicket, PositionState
from trader.contracts.testing import FakeMarketData
from trader.execution.book import (
    ClosedTrade,
    RealBook,
    ShadowBook,
    build_algo_metrics,
)
from trader.execution.broker import SimBroker, apply_slippage
from trader.execution.config import (
    AccountConfig,
    DrawdownStopConfig,
    ExecutionConfig,
    FillsConfig,
    RailsConfig,
    RiskConfig,
)


BAR_START = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
SESSION_CLOSE = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)


def _risk_config(
    *,
    equity: float = 10_000.0,
    mute_after_consecutive_stops: int = 2,
    mute_after_cumulative_day_r: float = -99.0,
    reversal_cooldown_minutes: int = 10,
) -> RiskConfig:
    return RiskConfig(
        account=AccountConfig(
            equity=equity,
            capital_fraction=1.0,
            day_slots=2,
        ),
        rails=RailsConfig(
            max_entries_per_day=3,
            one_position_at_a_time=True,
            no_hedge=True,
            mute_after_consecutive_stops=mute_after_consecutive_stops,
            mute_after_cumulative_day_r=mute_after_cumulative_day_r,
            reversal_cooldown_minutes=reversal_cooldown_minutes,
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
        slippage_bps={
            "SNXX": {"2026-07": 25.0},
            "SNDQ": {"2026-07": 10.0},
        },
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


def _data_for_raw_opens(*raw_opens: float) -> FakeMarketData:
    return FakeMarketData(
        {
            "SNXX": _frame(
                *(
                    (
                        BAR_START + timedelta(minutes=index),
                        raw_open,
                        raw_open + 1.0,
                        raw_open - 1.0,
                        raw_open,
                    )
                    for index, raw_open in enumerate(raw_opens)
                )
            )
        }
    )


def _ticket(
    ticket_id: str = "ticket-1",
    *,
    algo_id: str = "breakout",
    instrument: str = "SNXX",
    shares: int = 10,
    stop: float = 95.0,
    target: float = 105.0,
    created_ts: datetime = BAR_START,
) -> OrderTicket:
    return OrderTicket(
        ticket_id=ticket_id,
        algo_id=algo_id,
        intent_ts=created_ts,
        instrument=instrument,
        side="long",
        shares=shares,
        entry="market_next_open",
        stop=stop,
        target=target,
        risk={"fixture": True},
        created_ts=created_ts,
    )


def _apply_real_round_trip(
    book: RealBook,
    data: FakeMarketData,
    ticket: OrderTicket,
    *,
    entry_ts: datetime,
    entry_price: float,
    exit_ts: datetime,
    exit_price: float,
    exit_kind: str,
) -> None:
    book.register_ticket(ticket)
    book.apply_fills(
        [
            Fill(
                ticket_id=ticket.ticket_id,
                ts=entry_ts,
                price=entry_price,
                shares=ticket.shares,
                kind="entry",
                book="real",
            )
        ],
        data,
    )
    book.apply_fills(
        [
            Fill(
                ticket_id=ticket.ticket_id,
                ts=exit_ts,
                price=exit_price,
                shares=ticket.shares,
                kind=exit_kind,
                book="real",
            )
        ],
        data,
    )


def test_real_book_starts_with_one_stable_empty_portfolio() -> None:
    config = _risk_config(equity=12_345.0)
    book = RealBook(config)

    assert book.state is book.state
    assert book.state.cash == 12_345.0
    assert book.state.equity == 12_345.0
    assert book.state.positions == []
    assert book.state.entries_today == 0
    assert book.state.realized_r_today == 0.0
    assert book.state.muted_until is None


def test_register_ticket_consumes_one_daily_entry_without_opening_position() -> None:
    book = RealBook(_risk_config())

    book.register_ticket(_ticket("ticket-a"))
    assert book.state.entries_today == 1
    assert book.state.positions == []

    book.register_ticket(_ticket("ticket-b"))
    assert book.state.entries_today == 2
    assert book.state.positions == []


def test_entry_fill_builds_position_from_ticket_and_slipped_fill() -> None:
    book = RealBook(_risk_config())
    ticket = _ticket(shares=17, stop=94.0, target=108.0)
    entry_ts = BAR_START + timedelta(minutes=1)
    data = _data_for_raw_opens(100.0)
    book.register_ticket(ticket)

    book.apply_fills(
        [
            Fill(
                ticket_id=ticket.ticket_id,
                ts=entry_ts,
                price=100.25,
                shares=17,
                kind="entry",
                book="real",
            )
        ],
        data,
    )

    assert book.state.positions == [
        PositionState(
            instrument="SNXX",
            side="long",
            shares=17,
            entry_price=100.25,
            entry_ts=entry_ts,
            stop=94.0,
            target=108.0,
            algo_id="breakout",
        )
    ]


def test_target_exit_resolves_r_removes_position_and_drains_closed_trade() -> None:
    book = RealBook(_risk_config())
    ticket = _ticket(stop=95.0, target=105.0)
    entry_ts = BAR_START + timedelta(minutes=1)
    exit_ts = entry_ts + timedelta(minutes=1)
    data = _data_for_raw_opens(100.0, 100.0)
    book.register_ticket(ticket)
    book.apply_fills(
        [
            Fill(
                ticket_id=ticket.ticket_id,
                ts=entry_ts,
                price=100.25,
                shares=10,
                kind="entry",
                book="real",
            ),
            Fill(
                ticket_id=ticket.ticket_id,
                ts=exit_ts,
                price=104.7375,
                shares=10,
                kind="target",
                book="real",
            ),
        ],
        data,
    )

    # (104.7375 slipped exit - 100.25 slipped entry) /
    # abs(100.0 raw entry - 95.0 stop) = 0.8975R.
    expected_r = 0.8975
    assert book.state.positions == []
    assert book.resolved_r_multiples("breakout") == pytest.approx([expected_r])
    assert book.take_closed_trades() == [
        ClosedTrade(
            algo_id="breakout",
            instrument="SNXX",
            r_multiple=pytest.approx(expected_r),
            exit_kind="target",
        )
    ]
    assert book.take_closed_trades() == []


def test_realized_r_today_accumulates_sequential_round_trips() -> None:
    book = RealBook(_risk_config())
    data = _data_for_raw_opens(100.0, 110.0)

    _apply_real_round_trip(
        book,
        data,
        _ticket("ticket-a", stop=95.0),
        entry_ts=BAR_START + timedelta(minutes=1),
        entry_price=100.25,
        exit_ts=BAR_START + timedelta(minutes=1, seconds=30),
        exit_price=104.75,
        exit_kind="target",
    )
    _apply_real_round_trip(
        book,
        data,
        _ticket(
            "ticket-b",
            stop=105.0,
            target=115.0,
            created_ts=BAR_START + timedelta(minutes=1),
        ),
        entry_ts=BAR_START + timedelta(minutes=2),
        entry_price=110.275,
        exit_ts=BAR_START + timedelta(minutes=2),
        exit_price=107.775,
        exit_kind="eod",
    )

    # First trade is +0.9R and the second is -0.5R.
    assert book.state.realized_r_today == pytest.approx(0.4)


def test_cash_and_equity_accumulate_dollar_pnl_across_round_trips() -> None:
    book = RealBook(_risk_config(equity=12_345.0))
    data = _data_for_raw_opens(100.0, 110.0)

    _apply_real_round_trip(
        book,
        data,
        _ticket("ticket-a", shares=10, stop=95.0),
        entry_ts=BAR_START + timedelta(minutes=1),
        entry_price=100.25,
        exit_ts=BAR_START + timedelta(minutes=1, seconds=30),
        exit_price=94.75,
        exit_kind="stop",
    )
    _apply_real_round_trip(
        book,
        data,
        _ticket(
            "ticket-b",
            shares=20,
            stop=105.0,
            target=115.0,
            created_ts=BAR_START + timedelta(minutes=1),
        ),
        entry_ts=BAR_START + timedelta(minutes=2),
        entry_price=110.25,
        exit_ts=BAR_START + timedelta(minutes=2),
        exit_price=114.75,
        exit_kind="target",
    )

    # 10 * (94.75 - 100.25) + 20 * (114.75 - 110.25) = $35.00.
    assert book.state.cash == 12_380.0
    assert book.state.equity == 12_380.0


def test_two_consecutive_losing_stops_mute_at_session_close() -> None:
    book = RealBook(_risk_config(mute_after_consecutive_stops=2))
    data = _data_for_raw_opens(100.0, 100.0)

    _apply_real_round_trip(
        book,
        data,
        _ticket("ticket-a"),
        entry_ts=BAR_START + timedelta(minutes=1),
        entry_price=100.25,
        exit_ts=BAR_START + timedelta(minutes=1),
        exit_price=94.75,
        exit_kind="stop",
    )
    assert book.state.muted_until is None

    _apply_real_round_trip(
        book,
        data,
        _ticket("ticket-b", created_ts=BAR_START + timedelta(minutes=1)),
        entry_ts=BAR_START + timedelta(minutes=2),
        entry_price=100.25,
        exit_ts=BAR_START + timedelta(minutes=2),
        exit_price=94.75,
        exit_kind="stop",
    )

    assert book.state.muted_until == SESSION_CLOSE


def test_winning_exit_resets_consecutive_losing_stop_streak() -> None:
    book = RealBook(_risk_config(mute_after_consecutive_stops=2))
    data = _data_for_raw_opens(100.0, 100.0, 100.0)
    trades = [
        ("ticket-a", "stop", 94.75),
        ("ticket-b", "target", 104.75),
        ("ticket-c", "stop", 94.75),
    ]

    for index, (ticket_id, kind, exit_price) in enumerate(trades, start=1):
        _apply_real_round_trip(
            book,
            data,
            _ticket(
                ticket_id,
                created_ts=BAR_START + timedelta(minutes=index - 1),
            ),
            entry_ts=BAR_START + timedelta(minutes=index),
            entry_price=100.25,
            exit_ts=BAR_START + timedelta(minutes=index),
            exit_price=exit_price,
            exit_kind=kind,
        )

    assert book.state.muted_until is None


def test_cumulative_day_r_loss_mutes_independently_of_stop_streak() -> None:
    book = RealBook(
        _risk_config(
            mute_after_consecutive_stops=99,
            mute_after_cumulative_day_r=-1.0,
        )
    )
    data = _data_for_raw_opens(100.0)

    _apply_real_round_trip(
        book,
        data,
        _ticket(),
        entry_ts=BAR_START + timedelta(minutes=1),
        entry_price=100.25,
        exit_ts=BAR_START + timedelta(minutes=1),
        exit_price=94.0,
        exit_kind="eod",
    )

    assert book.state.realized_r_today == pytest.approx(-1.25)
    assert book.state.muted_until == SESSION_CLOSE


def test_losing_eod_leaves_consecutive_stop_streak_unchanged() -> None:
    book = RealBook(_risk_config(mute_after_consecutive_stops=2))
    data = _data_for_raw_opens(100.0, 100.0, 100.0)
    trades = [
        ("ticket-a", "stop"),
        ("ticket-b", "eod"),
        ("ticket-c", "stop"),
    ]

    for index, (ticket_id, kind) in enumerate(trades, start=1):
        _apply_real_round_trip(
            book,
            data,
            _ticket(
                ticket_id,
                created_ts=BAR_START + timedelta(minutes=index - 1),
            ),
            entry_ts=BAR_START + timedelta(minutes=index),
            entry_price=100.25,
            exit_ts=BAR_START + timedelta(minutes=index),
            exit_price=94.75,
            exit_kind=kind,
        )
        if index == 2:
            assert book.state.muted_until is None

    assert book.state.muted_until == SESSION_CLOSE


def test_reversal_sets_cooldown_even_for_winning_exit() -> None:
    book = RealBook(
        _risk_config(
            mute_after_consecutive_stops=99,
            mute_after_cumulative_day_r=-99.0,
            reversal_cooldown_minutes=10,
        )
    )
    data = _data_for_raw_opens(100.0)
    exit_ts = BAR_START + timedelta(minutes=5)

    _apply_real_round_trip(
        book,
        data,
        _ticket(),
        entry_ts=BAR_START + timedelta(minutes=1),
        entry_price=100.25,
        exit_ts=exit_ts,
        exit_price=100.50,
        exit_kind="reversal",
    )

    assert book.state.realized_r_today > 0
    assert book.state.muted_until == exit_ts + timedelta(minutes=10)


def test_reversal_cooldown_does_not_shorten_existing_day_mute() -> None:
    book = RealBook(
        _risk_config(
            mute_after_consecutive_stops=99,
            mute_after_cumulative_day_r=-1.0,
            reversal_cooldown_minutes=10,
        )
    )
    data = _data_for_raw_opens(100.0, 100.0)
    _apply_real_round_trip(
        book,
        data,
        _ticket("ticket-loss"),
        entry_ts=BAR_START + timedelta(minutes=1),
        entry_price=100.25,
        exit_ts=BAR_START + timedelta(minutes=1),
        exit_price=94.0,
        exit_kind="eod",
    )
    assert book.state.muted_until == SESSION_CLOSE

    _apply_real_round_trip(
        book,
        data,
        _ticket(
            "ticket-reversal",
            created_ts=BAR_START + timedelta(minutes=1),
        ),
        entry_ts=BAR_START + timedelta(minutes=2),
        entry_price=100.25,
        exit_ts=BAR_START + timedelta(minutes=4),
        exit_price=100.50,
        exit_kind="reversal",
    )

    assert book.state.muted_until == SESSION_CLOSE


def test_start_new_day_resets_rails_but_preserves_resolved_history() -> None:
    book = RealBook(
        _risk_config(mute_after_consecutive_stops=2)
    )
    data = _data_for_raw_opens(100.0, 100.0)
    for index in range(2):
        _apply_real_round_trip(
            book,
            data,
            _ticket(
                f"ticket-{index}",
                created_ts=BAR_START + timedelta(minutes=index),
            ),
            entry_ts=BAR_START + timedelta(minutes=index + 1),
            entry_price=100.25,
            exit_ts=BAR_START + timedelta(minutes=index + 1),
            exit_price=94.75,
            exit_kind="stop",
        )
    history = book.resolved_r_multiples("breakout")
    assert book.state.entries_today == 2
    assert book.state.realized_r_today < 0
    assert book.state.muted_until == SESSION_CLOSE

    book.start_new_day()

    assert book.state.entries_today == 0
    assert book.state.realized_r_today == 0.0
    assert book.state.muted_until is None
    assert book.resolved_r_multiples("breakout") == history


def test_shadow_entry_and_exit_use_x4_slippage_and_zero_real_shares() -> None:
    config = _execution_config()
    book = ShadowBook(config)
    entry_asof = BAR_START + timedelta(minutes=1)
    exit_asof = BAR_START + timedelta(minutes=2)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 104.0, 96.0, 100.0),
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
    book.open(
        algo_id="probe-algo",
        instrument="SNXX",
        stop=95.0,
        target=105.0,
        tag="probe",
        opened_ts=BAR_START,
    )

    entry_fills = book.on_bar(entry_asof, data)
    exit_fills = book.on_bar(exit_asof, data)

    expected_entry = round(apply_slippage(100.0, "buy", 25.0), 4)
    expected_exit = round(apply_slippage(105.0, "sell", 25.0), 4)
    assert len(entry_fills) == 1
    assert entry_fills[0].price == expected_entry
    assert entry_fills[0].kind == "entry"
    assert entry_fills[0].book == "shadow"
    assert entry_fills[0].shares == 0
    assert exit_fills == [
        Fill(
            ticket_id=entry_fills[0].ticket_id,
            ts=exit_asof,
            price=expected_exit,
            shares=0,
            kind="target",
            book="shadow",
        )
    ]
    assert book.resolved_r_multiples("probe-algo") == pytest.approx([0.8975])
    assert book.take_closed_trades() == [
        ClosedTrade(
            algo_id="probe-algo",
            instrument="SNXX",
            r_multiple=pytest.approx(0.8975),
            exit_kind="target",
        )
    ]
    assert book.take_closed_trades() == []


def test_shadow_concurrent_algos_resolve_independently_on_different_bars() -> None:
    book = ShadowBook(_execution_config())
    entry_asof = BAR_START + timedelta(minutes=1)
    first_exit_asof = BAR_START + timedelta(minutes=2)
    second_exit_asof = BAR_START + timedelta(minutes=3)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 104.0, 96.0, 100.0),
                (
                    BAR_START + timedelta(minutes=1),
                    100.0,
                    104.0,
                    94.0,
                    100.0,
                ),
                (
                    BAR_START + timedelta(minutes=2),
                    100.0,
                    104.0,
                    96.0,
                    100.0,
                ),
            ),
            "SNDQ": _frame(
                (BAR_START, 100.0, 104.0, 96.0, 100.0),
                (
                    BAR_START + timedelta(minutes=1),
                    100.0,
                    104.0,
                    96.0,
                    100.0,
                ),
                (
                    BAR_START + timedelta(minutes=2),
                    100.0,
                    106.0,
                    96.0,
                    105.0,
                ),
            ),
        }
    )
    book.open(
        algo_id="stop-algo",
        instrument="SNXX",
        stop=95.0,
        target=105.0,
        tag="probe",
        opened_ts=BAR_START,
    )
    book.open(
        algo_id="target-algo",
        instrument="SNDQ",
        stop=95.0,
        target=105.0,
        tag="rejected",
        opened_ts=BAR_START,
    )

    assert len(book.on_bar(entry_asof, data)) == 2
    first_exit = book.on_bar(first_exit_asof, data)
    second_exit = book.on_bar(second_exit_asof, data)

    assert [(fill.kind, fill.book) for fill in first_exit] == [("stop", "shadow")]
    assert [(fill.kind, fill.book) for fill in second_exit] == [
        ("target", "shadow")
    ]
    assert book.resolved_r_multiples("stop-algo") == pytest.approx([-1.0975])
    assert book.resolved_r_multiples("target-algo") == pytest.approx([0.959])


def test_shadow_same_algo_same_bar_episode_ids_are_distinct_and_deterministic() -> None:
    config = _execution_config()
    data = _data_for_raw_opens(100.0)

    first_book = ShadowBook(config)
    second_book = ShadowBook(config)
    for book in (first_book, second_book):
        for tag in ("probe", "rejected"):
            book.open(
                algo_id="same-algo",
                instrument="SNXX",
                stop=90.0,
                target=110.0,
                tag=tag,
                opened_ts=BAR_START,
            )

    first_ids = [
        fill.ticket_id
        for fill in first_book.on_bar(BAR_START + timedelta(minutes=1), data)
    ]
    second_ids = [
        fill.ticket_id
        for fill in second_book.on_bar(BAR_START + timedelta(minutes=1), data)
    ]

    assert len(set(first_ids)) == 2
    assert first_ids == second_ids


def test_shadow_and_sim_broker_fill_prices_match_exactly() -> None:
    config = _execution_config()
    broker = SimBroker(config)
    shadow = ShadowBook(config)
    ticket = _ticket(shares=23)
    entry_asof = BAR_START + timedelta(minutes=1)
    exit_asof = BAR_START + timedelta(minutes=2)
    data = FakeMarketData(
        {
            "SNXX": _frame(
                (BAR_START, 100.0, 104.0, 96.0, 100.0),
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
    broker.submit(ticket)
    shadow.open(
        algo_id=ticket.algo_id,
        instrument=ticket.instrument,
        stop=95.0,
        target=105.0,
        tag="rejected",
        opened_ts=ticket.created_ts,
    )

    real_entry = broker.on_bar(entry_asof, data)
    shadow_entry = shadow.on_bar(entry_asof, data)
    real_exit = broker.on_bar(exit_asof, data)
    shadow_exit = shadow.on_bar(exit_asof, data)

    assert [fill.price for fill in shadow_entry] == [
        fill.price for fill in real_entry
    ]
    assert [fill.price for fill in shadow_exit] == [
        fill.price for fill in real_exit
    ]


@pytest.mark.parametrize(
    ("real_rs", "shadow_rs"),
    [([], [1.0, -0.5]), ([1.0, -0.5], [])],
)
def test_metrics_share_counts_while_empty_book_fields_remain_nullable(
    real_rs: list[float],
    shadow_rs: list[float],
) -> None:
    updated_ts = BAR_START

    real, shadow = build_algo_metrics(
        algo_id="breakout",
        status="emitting",
        real_rs=real_rs,
        shadow_rs=shadow_rs,
        updated_ts=updated_ts,
    )

    for metrics in (real, shadow):
        assert metrics.algo_id == "breakout"
        assert metrics.status == "emitting"
        assert metrics.n_real == len(real_rs)
        assert metrics.n_shadow == len(shadow_rs)
        assert metrics.updated_ts == updated_ts

    empty = real if not real_rs else shadow
    populated = shadow if shadow_rs else real
    assert empty.wins == 0
    assert empty.cum_r == 0.0
    assert empty.win_rate is None
    assert empty.mean_r is None
    assert empty.expectancy_r is None
    assert empty.profit_factor is None
    assert empty.max_drawdown_r is None
    assert populated.wins == 1
    assert populated.win_rate == 0.5
    assert populated.mean_r == 0.25
    assert populated.expectancy_r == 0.25
    assert populated.profit_factor == 2.0
    assert populated.max_drawdown_r == 0.5
    assert populated.cum_r == 0.5


def test_metrics_match_hand_computed_sequence_arithmetic() -> None:
    # Cumulative path: 2, 1, 2, 1. Its peak is 2 and deepest pullback is 1R.
    rs = [2.0, -1.0, 1.0, -1.0]

    real, _shadow = build_algo_metrics(
        algo_id="breakout",
        status="emitting",
        real_rs=rs,
        shadow_rs=[],
        updated_ts=BAR_START,
    )

    assert real.wins == 2
    assert real.win_rate == 0.5
    assert real.mean_r == 0.25
    assert real.expectancy_r == 0.25
    assert real.profit_factor == 1.5
    assert real.max_drawdown_r == 1.0
    assert real.cum_r == 1.0


def test_metrics_with_wins_and_no_losses_use_null_profit_factor() -> None:
    _real, shadow = build_algo_metrics(
        algo_id="probe-algo",
        status="probe",
        real_rs=[],
        shadow_rs=[1.0, 2.0],
        updated_ts=BAR_START,
    )

    assert shadow.profit_factor is None
    assert shadow.max_drawdown_r == 0.0
    assert shadow.expectancy_r == shadow.mean_r
