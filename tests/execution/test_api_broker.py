"""Tests for the broker-API safety interlock and inert stub lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest

from trader.contracts import BrokerNotConfigured, MarketData, OrderTicket
from trader.execution.broker.api import ApiBroker
from trader.execution.config import ExecutionConfig, FillsConfig


def _execution_config(*, live_orders: bool) -> ExecutionConfig:
    return ExecutionConfig(
        broker="api",
        live_orders=live_orders,
        fills=FillsConfig(
            entry="market_next_open",
            stop_wins_ties=True,
            commission=0.0,
        ),
        slippage_bps={},
    )


def _ticket() -> OrderTicket:
    ts = datetime(2026, 8, 1, 14, 30, tzinfo=timezone.utc)
    return OrderTicket(
        ticket_id="ticket-1",
        algo_id="test-algo",
        intent_ts=ts,
        instrument="SNXX",
        side="long",
        shares=5,
        entry="market_next_open",
        stop=95.0,
        target=105.0,
        risk={"fixture": True},
        created_ts=ts,
    )


@pytest.mark.parametrize("live_orders", [False, True])
@pytest.mark.parametrize("trader_live", [False, True])
def test_submit_raises_until_all_interlocks_and_an_adapter_are_wired(
    monkeypatch: pytest.MonkeyPatch,
    live_orders: bool,
    trader_live: bool,
) -> None:
    if trader_live:
        monkeypatch.setenv("TRADER_LIVE", "1")
    else:
        monkeypatch.delenv("TRADER_LIVE", raising=False)
    broker = ApiBroker(_execution_config(live_orders=live_orders))

    with pytest.raises(BrokerNotConfigured, match=r"no adapter is wired"):
        broker.submit(_ticket())


def test_non_submit_methods_remain_inert_without_api_wiring() -> None:
    broker = ApiBroker(_execution_config(live_orders=True))
    asof = datetime(2026, 8, 1, 14, 35, tzinfo=timezone.utc)
    unused_data = cast(MarketData, object())

    assert broker.on_bar(asof, unused_data) == []
    broker.mark_reversal(asof, "short")
    broker.cancel_open("operator requested")
    assert broker.take_declined_tickets() == {}
    assert broker.force_flat(asof, unused_data) == []
