"""Entry-only broker test fake."""

from __future__ import annotations

from datetime import datetime

from trader.contracts.market import MarketData
from trader.contracts.orders import Fill, OrderTicket


class FakeBroker:
    """Fill pending entries at completed bars' raw opens with no slippage.

    Each execution cycle must call ``on_bar`` before submitting tickets decided
    during that cycle. Those new tickets are therefore first eligible on the
    subsequent ``on_bar`` call, reproducing next-bar-open ordering.
    """

    def __init__(self) -> None:
        self._pending: list[OrderTicket] = []

    def submit(self, ticket: OrderTicket) -> None:
        self._pending.append(ticket)

    def on_bar(self, asof: datetime, data: MarketData) -> list[Fill]:
        fills: list[Fill] = []
        still_pending: list[OrderTicket] = []
        for ticket in self._pending:
            bars = data.bars_1m(
                ticket.instrument,
                asof=asof,
                lookback_minutes=1,
            )
            if bars.empty:
                still_pending.append(ticket)
                continue

            fills.append(
                Fill(
                    ticket_id=ticket.ticket_id,
                    ts=asof,
                    price=float(bars.iloc[0]["o"]),
                    shares=ticket.shares,
                    kind="entry",
                    book="real",
                    price_basis="real",
                )
            )

        self._pending = still_pending
        return fills

    def cancel_open(self, reason: str) -> None:
        del reason
        self._pending.clear()


__all__ = ["FakeBroker"]
