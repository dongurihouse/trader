"""Deterministic simulated-broker fills for backtest and paper modes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.contracts import Fill, MarketData, OrderTicket, Side
from trader.execution.config import ExecutionConfig


@dataclass(frozen=True)
class _PendingEntry:
    ticket: OrderTicket
    eligible_after: datetime | None


@dataclass(frozen=True)
class _OpenPosition:
    ticket_id: str
    instrument: str
    side: Side
    shares: int
    stop: float
    target: float
    reversal_eligible_after: datetime | None = None


def apply_slippage(
    price: float,
    side: Literal["buy", "sell"],
    bps: float,
) -> float:
    """Apply one-way basis-point slippage to a raw fill price."""
    multiplier = bps / 10_000
    return price * (1 + multiplier) if side == "buy" else price * (1 - multiplier)


def slippage_bps_for(
    execution_config: ExecutionConfig,
    instrument: str,
    asof: datetime,
    *,
    tz: str,
) -> float:
    """Return configured slippage for an instrument's local calendar month."""
    month = asof.astimezone(ZoneInfo(tz)).strftime("%Y-%m")
    try:
        return float(execution_config.slippage_bps[instrument][month])
    except KeyError:
        raise KeyError(f"no slippage entry for {instrument} {month}") from None


def check_exit(
    *,
    stop: float,
    target: float,
    bar_open: float,
    bar_low: float,
    bar_high: float,
) -> tuple[Literal["stop", "target"], float] | None:
    """Resolve a long bracket using open clamps and stop-wins-ties ordering."""
    if bar_open <= stop:
        return ("stop", bar_open)
    if bar_open >= target:
        return ("target", bar_open)
    if bar_low <= stop:
        return ("stop", stop)
    if bar_high >= target:
        return ("target", target)
    return None


class SimBroker:
    """Simulate next-bar entries and bracket exits against real instrument bars."""

    def __init__(
        self,
        execution_config: ExecutionConfig,
        *,
        timezone: str = "America/New_York",
    ) -> None:
        self._execution_config = execution_config
        self._timezone = timezone
        self._tz = ZoneInfo(timezone)
        self._pending: list[_PendingEntry] = []
        self._positions: list[_OpenPosition] = []
        self._last_asof: datetime | None = None

    def submit(self, ticket: OrderTicket) -> None:
        self._pending.append(
            _PendingEntry(ticket=ticket, eligible_after=self._last_asof)
        )

    def on_bar(self, asof: datetime, data: MarketData) -> list[Fill]:
        self._last_asof = asof
        fills: list[Fill] = []
        still_pending: list[_PendingEntry] = []
        positions_to_check = list(self._positions)

        for pending in self._pending:
            ticket = pending.ticket
            if asof <= ticket.created_ts or (
                pending.eligible_after is not None
                and asof <= pending.eligible_after
            ):
                still_pending.append(pending)
                continue

            bars = data.bars_1m(
                ticket.instrument,
                asof=asof,
                lookback_minutes=1,
            )
            if bars.empty:
                still_pending.append(pending)
                continue

            bar = bars.iloc[0]
            bps = slippage_bps_for(
                self._execution_config,
                ticket.instrument,
                asof,
                tz=self._timezone,
            )
            entry_price = round(
                apply_slippage(float(bar["o"]), "buy", bps),
                4,
            )
            fills.append(
                Fill(
                    ticket_id=ticket.ticket_id,
                    ts=asof,
                    price=entry_price,
                    shares=ticket.shares,
                    kind="entry",
                    book="real",
                )
            )
            positions_to_check.append(
                _OpenPosition(
                    ticket_id=ticket.ticket_id,
                    instrument=ticket.instrument,
                    side=ticket.side,
                    shares=ticket.shares,
                    stop=cast(float, ticket.stop),
                    target=cast(float, ticket.target),
                )
            )

        still_open: list[_OpenPosition] = []
        unmarked_positions: list[_OpenPosition] = []
        for position in positions_to_check:
            if position.reversal_eligible_after is None:
                unmarked_positions.append(position)
                continue

            if asof <= position.reversal_eligible_after:
                still_open.append(position)
                continue

            bars = data.bars_1m(
                position.instrument,
                asof=asof,
                lookback_minutes=1,
            )
            if bars.empty:
                still_open.append(position)
                continue

            fills.append(
                self._exit_fill(
                    position,
                    asof=asof,
                    raw_exit_price=float(bars.iloc[0]["o"]),
                    exit_kind="reversal",
                )
            )

        for position in unmarked_positions:
            bars = data.bars_1m(
                position.instrument,
                asof=asof,
                lookback_minutes=1,
            )
            if bars.empty:
                still_open.append(position)
                continue

            bar = bars.iloc[0]
            exit_result = check_exit(
                stop=position.stop,
                target=position.target,
                bar_open=float(bar["o"]),
                bar_low=float(bar["l"]),
                bar_high=float(bar["h"]),
            )
            if exit_result is None:
                trading_day = asof.astimezone(self._tz).date()
                session_close = data.calendar().session_close(trading_day)
                if asof < session_close:
                    still_open.append(position)
                    continue
                exit_kind: Literal["stop", "target", "eod"] = "eod"
                raw_exit_price = float(bar["c"])
            else:
                exit_kind, raw_exit_price = exit_result

            fills.append(
                self._exit_fill(
                    position,
                    asof=asof,
                    raw_exit_price=raw_exit_price,
                    exit_kind=exit_kind,
                )
            )

        self._pending = still_pending
        self._positions = still_open
        return fills

    def cancel_open(self, reason: str) -> None:
        del reason
        self._pending.clear()

    def take_declined_tickets(self) -> dict[str, str]:
        return {}

    def mark_reversal(self, asof: datetime, trigger_side: Side) -> None:
        """Schedule conflicting open positions to exit at a later bar's open."""
        marked: list[_OpenPosition] = []
        for position in self._positions:
            if (
                position.side != trigger_side
                and position.reversal_eligible_after is None
            ):
                marked.append(
                    replace(position, reversal_eligible_after=asof)
                )
                continue
            marked.append(position)
        self._positions = marked

    def force_flat(self, asof: datetime, data: MarketData) -> list[Fill]:
        """Close every priceable position at the current bar's close."""
        fills: list[Fill] = []
        still_open: list[_OpenPosition] = []
        for position in self._positions:
            bars = data.bars_1m(
                position.instrument,
                asof=asof,
                lookback_minutes=1,
            )
            if bars.empty:
                still_open.append(position)
                continue

            raw_exit_price = float(bars.iloc[0]["c"])
            fills.append(
                self._exit_fill(
                    position,
                    asof=asof,
                    raw_exit_price=raw_exit_price,
                    exit_kind="eod",
                )
            )

        self._positions = still_open
        return fills

    def _exit_fill(
        self,
        position: _OpenPosition,
        *,
        asof: datetime,
        raw_exit_price: float,
        exit_kind: Literal["stop", "target", "reversal", "eod"],
    ) -> Fill:
        bps = slippage_bps_for(
            self._execution_config,
            position.instrument,
            asof,
            tz=self._timezone,
        )
        return Fill(
            ticket_id=position.ticket_id,
            ts=asof,
            price=round(
                apply_slippage(raw_exit_price, "sell", bps),
                4,
            ),
            shares=position.shares,
            kind=exit_kind,
            book="real",
        )


__all__ = ["SimBroker", "apply_slippage", "check_exit", "slippage_bps_for"]
