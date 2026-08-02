"""Tests for simulated broker fill mechanics and lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trader.contracts import Fill, OrderTicket, Side
from trader.contracts.testing import FakeMarketData
from trader.execution.broker import (
    SimBroker,
    apply_slippage,
    check_exit,
    slippage_bps_for,
)
from trader.execution.config import ExecutionConfig, FillsConfig


BAR_START = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)


def _execution_config(
    slippage_bps: dict[str, dict[str, float]] | None = None,
) -> ExecutionConfig:
    return ExecutionConfig(
        broker="sim",
        live_orders=False,
        fills=FillsConfig(commission=0.0),
        slippage_bps=slippage_bps
        if slippage_bps is not None
        else {
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


def _data(
    *rows: tuple[datetime, float, float, float, float],
    instrument: str = "SNXX",
) -> FakeMarketData:
    return FakeMarketData({instrument: _frame(*rows)})


def _ticket(
    ticket_id: str = "ticket-1",
    *,
    instrument: str = "SNXX",
    side: Side = "long",
    shares: int = 10,
    stop: float = 95.0,
    target: float = 105.0,
    created_ts: datetime = BAR_START,
) -> OrderTicket:
    return OrderTicket(
        ticket_id=ticket_id,
        algo_id="test-algo",
        intent_ts=created_ts,
        instrument=instrument,
        side=side,
        shares=shares,
        entry="market_next_open",
        stop=stop,
        target=target,
        risk={"fixture": True},
        created_ts=created_ts,
    )


def test_apply_slippage_charges_buyers_up_and_sellers_down() -> None:
    assert apply_slippage(100.0, "buy", 25.0) == 100.25
    assert apply_slippage(100.0, "sell", 25.0) == 99.75


def test_slippage_lookup_uses_calendar_month_in_requested_timezone() -> None:
    asof = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)

    bps = slippage_bps_for(
        _execution_config(),
        "SNXX",
        asof,
        tz="America/New_York",
    )

    assert bps == 25.0


def test_slippage_lookup_fails_loud_when_month_is_unmodelled() -> None:
    asof = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)

    with pytest.raises(KeyError, match=r"no slippage entry for SNXX 2026-08"):
        slippage_bps_for(
            _execution_config(),
            "SNXX",
            asof,
            tz="America/New_York",
        )


def test_check_exit_returns_none_for_range_strictly_inside_bracket() -> None:
    assert check_exit(
        stop=95.0,
        target=105.0,
        bar_open=100.0,
        bar_low=95.01,
        bar_high=104.99,
    ) is None


@pytest.mark.parametrize(
    (
        "bar_open",
        "bar_low",
        "bar_high",
        "expected",
    ),
    [
        (94.0, 96.0, 104.0, ("stop", 94.0)),
        (106.0, 96.0, 104.0, ("target", 106.0)),
        (100.0, 94.0, 104.0, ("stop", 95.0)),
        (100.0, 96.0, 106.0, ("target", 105.0)),
    ],
    ids=[
        "open-at-or-below-stop",
        "open-at-or-above-target",
        "low-at-or-below-stop",
        "high-at-or-above-target",
    ],
)
def test_check_exit_four_ordered_branches(
    bar_open: float,
    bar_low: float,
    bar_high: float,
    expected: tuple[str, float],
) -> None:
    assert check_exit(
        stop=95.0,
        target=105.0,
        bar_open=bar_open,
        bar_low=bar_low,
        bar_high=bar_high,
    ) == expected


def test_entry_fills_at_visible_bar_open_with_buy_slippage() -> None:
    asof = BAR_START + timedelta(minutes=1)
    broker = SimBroker(_execution_config())
    ticket = _ticket(shares=7, stop=90.0, target=110.0)
    data = _data((BAR_START, 100.0, 101.0, 99.0, 100.5))

    broker.submit(ticket)
    fills = broker.on_bar(asof, data)

    assert fills == [
        Fill(
            ticket_id="ticket-1",
            ts=asof,
            price=100.25,
            shares=7,
            kind="entry",
            book="real",
        )
    ]


def test_ticket_submitted_after_cycle_cannot_fill_on_repeated_same_bar() -> None:
    first_asof = BAR_START + timedelta(minutes=1)
    second_asof = BAR_START + timedelta(minutes=2)
    broker = SimBroker(_execution_config())
    ticket = _ticket(
        stop=90.0,
        target=110.0,
        created_ts=first_asof,
    )
    data = _data(
        (BAR_START, 100.0, 101.0, 99.0, 100.0),
        (BAR_START + timedelta(minutes=1), 101.0, 102.0, 100.0, 101.0),
    )

    assert broker.on_bar(first_asof, data) == []
    broker.submit(ticket)

    assert broker.on_bar(first_asof, data) == []
    assert broker.on_bar(second_asof, data) == [
        Fill(
            ticket_id="ticket-1",
            ts=second_asof,
            price=101.2525,
            shares=10,
            kind="entry",
            book="real",
        )
    ]


def test_pending_entry_waits_for_an_available_instrument_bar() -> None:
    first_asof = BAR_START + timedelta(minutes=1)
    second_asof = BAR_START + timedelta(minutes=2)
    broker = SimBroker(_execution_config())
    ticket = _ticket(stop=90.0, target=110.0)
    data = _data(
        (BAR_START + timedelta(minutes=1), 101.0, 102.0, 100.0, 101.0),
    )

    broker.submit(ticket)

    assert broker.on_bar(first_asof, data) == []
    assert broker.on_bar(second_asof, data) == [
        Fill(
            ticket_id="ticket-1",
            ts=second_asof,
            price=101.2525,
            shares=10,
            kind="entry",
            book="real",
        )
    ]


def test_stop_wins_when_same_bar_range_spans_both_levels() -> None:
    entry_asof = BAR_START + timedelta(minutes=1)
    exit_asof = BAR_START + timedelta(minutes=2)
    broker = SimBroker(_execution_config())
    data = _data(
        (BAR_START, 100.0, 101.0, 99.0, 100.0),
        (BAR_START + timedelta(minutes=1), 100.0, 106.0, 94.0, 100.0),
    )
    broker.submit(_ticket())

    assert [fill.kind for fill in broker.on_bar(entry_asof, data)] == ["entry"]
    fills = broker.on_bar(exit_asof, data)

    assert fills == [
        Fill(
            ticket_id="ticket-1",
            ts=exit_asof,
            price=94.7625,
            shares=10,
            kind="stop",
            book="real",
        )
    ]


@pytest.mark.parametrize(
    ("gap_open", "expected_kind", "expected_price"),
    [
        (94.0, "stop", 93.765),
        (106.0, "target", 105.735),
    ],
)
def test_open_beyond_bracket_exits_at_sell_slipped_open(
    gap_open: float,
    expected_kind: str,
    expected_price: float,
) -> None:
    entry_asof = BAR_START + timedelta(minutes=1)
    exit_asof = BAR_START + timedelta(minutes=2)
    broker = SimBroker(_execution_config())
    data = _data(
        (BAR_START, 100.0, 101.0, 99.0, 100.0),
        (
            BAR_START + timedelta(minutes=1),
            gap_open,
            gap_open + 1.0,
            gap_open - 1.0,
            gap_open,
        ),
    )
    broker.submit(_ticket())
    broker.on_bar(entry_asof, data)

    fills = broker.on_bar(exit_asof, data)

    assert len(fills) == 1
    assert fills[0].kind == expected_kind
    assert fills[0].price == expected_price


@pytest.mark.parametrize(
    (
        "bar_high",
        "bar_low",
        "expected_kind",
        "expected_price",
    ),
    [
        (104.0, 94.0, "stop", 94.7625),
        (106.0, 96.0, "target", 104.7375),
    ],
)
def test_mid_bar_bracket_hit_exits_at_sell_slipped_level(
    bar_high: float,
    bar_low: float,
    expected_kind: str,
    expected_price: float,
) -> None:
    entry_asof = BAR_START + timedelta(minutes=1)
    exit_asof = BAR_START + timedelta(minutes=2)
    broker = SimBroker(_execution_config())
    data = _data(
        (BAR_START, 100.0, 101.0, 99.0, 100.0),
        (BAR_START + timedelta(minutes=1), 100.0, bar_high, bar_low, 100.0),
    )
    broker.submit(_ticket())
    broker.on_bar(entry_asof, data)

    fills = broker.on_bar(exit_asof, data)

    assert len(fills) == 1
    assert fills[0].kind == expected_kind
    assert fills[0].price == expected_price


def test_entry_bar_can_immediately_stop_and_returns_entry_first() -> None:
    asof = BAR_START + timedelta(minutes=1)
    broker = SimBroker(_execution_config())
    data = _data((BAR_START, 100.0, 106.0, 94.0, 100.0))
    broker.submit(_ticket())

    fills = broker.on_bar(asof, data)

    assert fills == [
        Fill(
            ticket_id="ticket-1",
            ts=asof,
            price=100.25,
            shares=10,
            kind="entry",
            book="real",
        ),
        Fill(
            ticket_id="ticket-1",
            ts=asof,
            price=94.7625,
            shares=10,
            kind="stop",
            book="real",
        ),
    ]


def test_session_close_falls_back_to_sell_slipped_bar_close() -> None:
    entry_start = datetime(2026, 7, 1, 19, 58, tzinfo=timezone.utc)
    close_start = entry_start + timedelta(minutes=1)
    entry_asof = entry_start + timedelta(minutes=1)
    close_asof = close_start + timedelta(minutes=1)
    broker = SimBroker(_execution_config())
    data = _data(
        (entry_start, 100.0, 101.0, 99.0, 100.0),
        (close_start, 101.0, 103.0, 100.0, 102.0),
    )
    broker.submit(
        _ticket(stop=90.0, target=110.0, created_ts=entry_start)
    )
    broker.on_bar(entry_asof, data)

    fills = broker.on_bar(close_asof, data)

    assert fills == [
        Fill(
            ticket_id="ticket-1",
            ts=close_asof,
            price=101.745,
            shares=10,
            kind="eod",
            book="real",
        )
    ]


def test_force_flat_closes_at_current_close_once() -> None:
    entry_asof = BAR_START + timedelta(minutes=1)
    flat_asof = BAR_START + timedelta(minutes=2)
    later_asof = BAR_START + timedelta(minutes=3)
    broker = SimBroker(_execution_config())
    data = _data(
        (BAR_START, 100.0, 101.0, 99.0, 100.0),
        (BAR_START + timedelta(minutes=1), 101.0, 103.0, 100.0, 102.0),
        (BAR_START + timedelta(minutes=2), 102.0, 103.0, 101.0, 102.0),
    )
    broker.submit(_ticket(stop=90.0, target=110.0))
    broker.on_bar(entry_asof, data)

    fills = broker.force_flat(flat_asof, data)

    assert fills == [
        Fill(
            ticket_id="ticket-1",
            ts=flat_asof,
            price=101.745,
            shares=10,
            kind="eod",
            book="real",
        )
    ]
    assert broker.on_bar(later_asof, data) == []


def test_cancel_open_discards_pending_entry() -> None:
    asof = BAR_START + timedelta(minutes=1)
    broker = SimBroker(_execution_config())
    data = _data((BAR_START, 100.0, 101.0, 99.0, 100.0))
    broker.submit(_ticket())

    broker.cancel_open("test")

    assert broker.on_bar(asof, data) == []
    assert broker.take_declined_tickets() == {}


def test_cancel_open_keeps_filled_position_under_exit_monitoring() -> None:
    entry_asof = BAR_START + timedelta(minutes=1)
    exit_asof = BAR_START + timedelta(minutes=2)
    broker = SimBroker(_execution_config())
    data = _data(
        (BAR_START, 100.0, 101.0, 99.0, 100.0),
        (BAR_START + timedelta(minutes=1), 100.0, 104.0, 94.0, 100.0),
    )
    broker.submit(_ticket())
    broker.on_bar(entry_asof, data)

    broker.cancel_open("test")
    fills = broker.on_bar(exit_asof, data)

    assert [fill.kind for fill in fills] == ["stop"]


def test_entry_fill_raises_when_slippage_month_is_missing() -> None:
    asof = BAR_START + timedelta(minutes=1)
    config = _execution_config({"SNXX": {"2026-06": 25.0}})
    broker = SimBroker(config)
    broker.submit(_ticket())

    with pytest.raises(KeyError, match=r"no slippage entry for SNXX 2026-07"):
        broker.on_bar(
            asof,
            _data((BAR_START, 100.0, 101.0, 99.0, 100.0)),
        )


def test_exit_fill_raises_when_new_month_has_no_slippage_model() -> None:
    entry_start = datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc)
    exit_start = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
    entry_asof = entry_start + timedelta(minutes=1)
    exit_asof = exit_start + timedelta(minutes=1)
    config = _execution_config({"SNXX": {"2026-07": 25.0}})
    broker = SimBroker(config)
    data = _data(
        (entry_start, 100.0, 101.0, 99.0, 100.0),
        (exit_start, 100.0, 106.0, 99.0, 105.0),
    )
    broker.submit(_ticket(created_ts=entry_start))
    broker.on_bar(entry_asof, data)

    with pytest.raises(KeyError, match=r"no slippage entry for SNXX 2026-08"):
        broker.on_bar(exit_asof, data)


def test_concurrent_positions_exit_independently() -> None:
    entry_asof = BAR_START + timedelta(minutes=1)
    first_exit_asof = BAR_START + timedelta(minutes=2)
    second_exit_asof = BAR_START + timedelta(minutes=3)
    broker = SimBroker(_execution_config())
    data = _data(
        (BAR_START, 100.0, 101.0, 99.0, 100.0),
        (BAR_START + timedelta(minutes=1), 100.0, 103.0, 97.0, 100.0),
        (BAR_START + timedelta(minutes=2), 100.0, 106.0, 96.0, 105.0),
    )
    broker.submit(_ticket("ticket-a", stop=98.0, target=110.0))
    broker.submit(_ticket("ticket-b", stop=95.0, target=105.0))

    entry_fills = broker.on_bar(entry_asof, data)
    first_exit = broker.on_bar(first_exit_asof, data)
    second_exit = broker.on_bar(second_exit_asof, data)

    assert [fill.ticket_id for fill in entry_fills] == ["ticket-a", "ticket-b"]
    assert [(fill.ticket_id, fill.kind) for fill in first_exit] == [
        ("ticket-a", "stop")
    ]
    assert [(fill.ticket_id, fill.kind) for fill in second_exit] == [
        ("ticket-b", "target")
    ]


def test_reversal_mark_waits_for_later_open_and_preempts_bracket_checks() -> None:
    entry_asof = BAR_START + timedelta(minutes=1)
    mark_asof = BAR_START + timedelta(minutes=2)
    reversal_asof = BAR_START + timedelta(minutes=3)
    later_asof = BAR_START + timedelta(minutes=4)
    broker = SimBroker(_execution_config())
    data = _data(
        (BAR_START, 100.0, 101.0, 99.0, 100.0),
        (BAR_START + timedelta(minutes=1), 100.0, 106.0, 94.0, 100.0),
        (BAR_START + timedelta(minutes=2), 99.0, 110.0, 90.0, 99.0),
        (BAR_START + timedelta(minutes=3), 100.0, 106.0, 94.0, 100.0),
    )
    broker.submit(_ticket(stop=95.0, target=105.0))
    assert [fill.kind for fill in broker.on_bar(entry_asof, data)] == ["entry"]

    broker.mark_reversal(mark_asof, "short")
    broker.mark_reversal(mark_asof, "short")

    assert broker.on_bar(mark_asof, data) == []
    assert broker.on_bar(reversal_asof, data) == [
        Fill(
            ticket_id="ticket-1",
            ts=reversal_asof,
            price=98.7525,
            shares=10,
            kind="reversal",
            book="real",
        )
    ]
    assert broker.on_bar(later_asof, data) == []


def test_reversal_mark_with_same_side_leaves_position_under_bracket_checks() -> None:
    entry_asof = BAR_START + timedelta(minutes=1)
    mark_asof = BAR_START + timedelta(minutes=2)
    exit_asof = BAR_START + timedelta(minutes=3)
    broker = SimBroker(_execution_config())
    data = _data(
        (BAR_START, 100.0, 101.0, 99.0, 100.0),
        (BAR_START + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
        (BAR_START + timedelta(minutes=2), 100.0, 106.0, 99.0, 105.0),
    )
    broker.submit(_ticket(stop=95.0, target=105.0))
    broker.on_bar(entry_asof, data)

    broker.mark_reversal(mark_asof, "long")

    assert broker.on_bar(mark_asof, data) == []
    assert broker.on_bar(exit_asof, data) == [
        Fill(
            ticket_id="ticket-1",
            ts=exit_asof,
            price=104.7375,
            shares=10,
            kind="target",
            book="real",
        )
    ]
