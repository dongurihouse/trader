"""Safety-gated stub for a future broker trading API adapter."""

from __future__ import annotations

from datetime import datetime
import os

from trader.contracts import BrokerNotConfigured, Fill, MarketData, OrderTicket
from trader.execution.config import ExecutionConfig


# No broker trading API adapter exists in this codebase yet. A future integration
# wave will replace this wiring check (or flip it when an adapter is installed).
_ADAPTER_WIRED = False


class ApiBroker:
    """Expose the broker interface without transmitting orders in v1."""

    def __init__(self, execution_config: ExecutionConfig) -> None:
        self._execution_config = execution_config

    def submit(self, ticket: OrderTicket) -> None:
        del ticket
        unmet: list[str] = []
        if not self._execution_config.live_orders:
            unmet.append("execution live_orders is not true")
        if os.environ.get("TRADER_LIVE") != "1":
            unmet.append("TRADER_LIVE is not 1")
        if not _ADAPTER_WIRED:
            unmet.append("no adapter is wired")

        if unmet:
            raise BrokerNotConfigured(
                "API broker is not configured: " + "; ".join(unmet)
            )

    def on_bar(self, asof: datetime, data: MarketData) -> list[Fill]:
        del asof, data
        return []

    def cancel_open(self, reason: str) -> None:
        del reason

    def take_declined_tickets(self) -> dict[str, str]:
        return {}

    def force_flat(self, asof: datetime, data: MarketData) -> list[Fill]:
        del asof, data
        return []


__all__ = ["ApiBroker"]
