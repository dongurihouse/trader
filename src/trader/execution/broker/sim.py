"""Deterministic simulated-broker fills for backtest and paper modes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.algos import bracket
from trader.contracts import Fill, LookaheadError, MarketData, OrderTicket, Side
from trader.execution.config import ExecutionConfig


FillPriceBasis = Literal["real", "synthetic"]


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


@dataclass(frozen=True)
class FillPriceBar:
    o: float
    h: float
    l: float
    c: float
    price_basis: FillPriceBasis


def _timestamp_day(timestamp: object, tz: ZoneInfo) -> date:
    if hasattr(timestamp, "to_pydatetime"):
        timestamp = timestamp.to_pydatetime()
    return cast(datetime, timestamp).astimezone(tz).date()


class FillPriceResolver:
    """Resolve real or synthetic ETF bars for deterministic fill pricing."""

    def __init__(
        self,
        execution_config: ExecutionConfig,
        *,
        timezone: str = "America/New_York",
    ) -> None:
        self._execution_config = execution_config
        self._tz = ZoneInfo(timezone)
        self._basis_by_day: dict[tuple[str, date], FillPriceBasis | None] = {}

    def bar(
        self,
        data: MarketData,
        *,
        instrument: str,
        asof: datetime,
    ) -> FillPriceBar | None:
        trading_day = asof.astimezone(self._tz).date()
        basis = self._basis_for_day(
            data,
            instrument=instrument,
            asof=asof,
            trading_day=trading_day,
        )
        if basis is None:
            return None
        if basis == "real":
            return self._real_bar(
                data,
                instrument=instrument,
                asof=asof,
                trading_day=trading_day,
            )
        return self._synthetic_bar(
            data,
            instrument=instrument,
            asof=asof,
            trading_day=trading_day,
        )

    def _basis_for_day(
        self,
        data: MarketData,
        *,
        instrument: str,
        asof: datetime,
        trading_day: date,
    ) -> FillPriceBasis | None:
        cache_key = (instrument, trading_day)
        if cache_key in self._basis_by_day:
            return self._basis_by_day[cache_key]

        mode = self._execution_config.fills.etf_price_basis
        if mode == "synthetic":
            basis: FillPriceBasis | None = "synthetic"
        else:
            count, cacheable = self._intraday_bar_count(
                data,
                instrument=instrument,
                asof=asof,
                trading_day=trading_day,
            )
            if count >= self._execution_config.fills.min_intraday_bars:
                basis = "real"
            elif mode == "auto":
                basis = "synthetic"
            else:
                basis = None

        if basis is None and mode == "real" and not cacheable:
            return None
        self._basis_by_day[cache_key] = basis
        return basis

    def _intraday_bar_count(
        self,
        data: MarketData,
        *,
        instrument: str,
        asof: datetime,
        trading_day: date,
    ) -> tuple[int, bool]:
        count_asof = data.calendar().session_close(trading_day)
        try:
            bars = data.bars_1m(
                instrument,
                asof=count_asof,
                lookback_minutes=None,
            )
            cacheable = True
        except (KeyError, LookaheadError):
            try:
                bars = data.bars_1m(
                    instrument,
                    asof=asof,
                    lookback_minutes=None,
                )
                cacheable = not bars.empty
            except (KeyError, LookaheadError):
                return 0, True

        if bars.empty:
            return 0, cacheable
        return (
            sum(
                1
                for timestamp in bars.index
                if _timestamp_day(timestamp, self._tz) == trading_day
            ),
            cacheable,
        )

    def _latest_completed_bar(
        self,
        data: MarketData,
        *,
        symbol: str,
        asof: datetime,
        trading_day: date,
    ) -> object | None:
        try:
            bars = data.bars_1m(
                symbol,
                asof=asof,
                lookback_minutes=1,
            )
        except (KeyError, LookaheadError):
            return None
        if bars.empty:
            return None
        if _timestamp_day(bars.index[-1], self._tz) != trading_day:
            return None
        return bars.iloc[-1]

    def _real_bar(
        self,
        data: MarketData,
        *,
        instrument: str,
        asof: datetime,
        trading_day: date,
    ) -> FillPriceBar | None:
        bar = self._latest_completed_bar(
            data,
            symbol=instrument,
            asof=asof,
            trading_day=trading_day,
        )
        if bar is None:
            return None
        return FillPriceBar(
            o=float(bar["o"]),
            h=float(bar["h"]),
            l=float(bar["l"]),
            c=float(bar["c"]),
            price_basis="real",
        )

    def _synthetic_bar(
        self,
        data: MarketData,
        *,
        instrument: str,
        asof: datetime,
        trading_day: date,
    ) -> FillPriceBar | None:
        sndk_bar = self._latest_completed_bar(
            data,
            symbol=bracket.UNDERLYING,
            asof=asof,
            trading_day=trading_day,
        )
        if sndk_bar is None:
            return None

        sndk_prev = bracket.previous_close(bracket.UNDERLYING, data, asof)
        etf_prev = bracket.previous_close(instrument, data, asof)
        if sndk_prev is None or etf_prev is None:
            return None
        if sndk_prev <= 0 or etf_prev <= 0:
            return None

        lev = bracket.leverage_for(instrument)
        high_candidate = bracket.etf_price(
            float(sndk_bar["h"]), sndk_prev, etf_prev, lev
        )
        low_candidate = bracket.etf_price(
            float(sndk_bar["l"]), sndk_prev, etf_prev, lev
        )
        return FillPriceBar(
            o=bracket.etf_price(
                float(sndk_bar["o"]), sndk_prev, etf_prev, lev
            ),
            h=max(high_candidate, low_candidate),
            l=min(high_candidate, low_candidate),
            c=bracket.etf_price(
                float(sndk_bar["c"]), sndk_prev, etf_prev, lev
            ),
            price_basis="synthetic",
        )


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
        self._prices = FillPriceResolver(
            execution_config,
            timezone=timezone,
        )
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

            bar = self._prices.bar(
                data,
                instrument=ticket.instrument,
                asof=asof,
            )
            if bar is None:
                still_pending.append(pending)
                continue

            bps = slippage_bps_for(
                self._execution_config,
                ticket.instrument,
                asof,
                tz=self._timezone,
            )
            entry_price = round(
                apply_slippage(bar.o, "buy", bps),
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
                    price_basis=bar.price_basis,
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

            bar = self._prices.bar(
                data,
                instrument=position.instrument,
                asof=asof,
            )
            if bar is None:
                still_open.append(position)
                continue

            fills.append(
                self._exit_fill(
                    position,
                    asof=asof,
                    raw_exit_price=bar.o,
                    exit_kind="reversal",
                    price_basis=bar.price_basis,
                )
            )

        for position in unmarked_positions:
            bar = self._prices.bar(
                data,
                instrument=position.instrument,
                asof=asof,
            )
            if bar is None:
                still_open.append(position)
                continue

            exit_result = check_exit(
                stop=position.stop,
                target=position.target,
                bar_open=bar.o,
                bar_low=bar.l,
                bar_high=bar.h,
            )
            if exit_result is None:
                trading_day = asof.astimezone(self._tz).date()
                session_close = data.calendar().session_close(trading_day)
                if asof < session_close:
                    still_open.append(position)
                    continue
                exit_kind: Literal["stop", "target", "eod"] = "eod"
                raw_exit_price = bar.c
            else:
                exit_kind, raw_exit_price = exit_result

            fills.append(
                self._exit_fill(
                    position,
                    asof=asof,
                    raw_exit_price=raw_exit_price,
                    exit_kind=exit_kind,
                    price_basis=bar.price_basis,
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
            bar = self._prices.bar(
                data,
                instrument=position.instrument,
                asof=asof,
            )
            if bar is None:
                still_open.append(position)
                continue

            fills.append(
                self._exit_fill(
                    position,
                    asof=asof,
                    raw_exit_price=bar.c,
                    exit_kind="eod",
                    price_basis=bar.price_basis,
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
        price_basis: FillPriceBasis,
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
            price_basis=price_basis,
        )


__all__ = [
    "FillPriceBar",
    "FillPriceBasis",
    "FillPriceResolver",
    "SimBroker",
    "apply_slippage",
    "check_exit",
    "slippage_bps_for",
]
