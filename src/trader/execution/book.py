"""Real/shadow portfolio bookkeeping and per-algo metrics."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.contracts import (
    AlgoMetrics,
    AlgoStatus,
    Fill,
    MarketData,
    OrderTicket,
    PortfolioState,
    PositionState,
)

from .broker.sim import apply_slippage, check_exit, slippage_bps_for
from .config import ExecutionConfig, RiskConfig


_ExitKind = Literal["stop", "target", "reversal", "eod"]
ShadowTag = Literal["probe", "rejected", "gate_refused"]


@dataclass(frozen=True)
class ClosedTrade:
    algo_id: str
    instrument: str
    r_multiple: float
    exit_kind: _ExitKind
    tag: ShadowTag | None = None


def _raw_entry_price(
    data: MarketData,
    *,
    instrument: str,
    entry_fill_ts: datetime,
) -> float:
    bars = data.bars_1m(
        instrument,
        asof=entry_fill_ts,
        lookback_minutes=1,
    )
    return float(bars.iloc[0]["o"])


def _r_multiple(
    *,
    slipped_exit_price: float,
    slipped_entry_price: float,
    raw_entry_price: float,
    stop: float,
) -> float:
    planned_dist = abs(raw_entry_price - stop)
    if planned_dist == 0:
        return 0.0
    return (slipped_exit_price - slipped_entry_price) / planned_dist


class RealBook:
    """Own the live portfolio and account for accepted-ticket fills."""

    def __init__(
        self,
        risk_config: RiskConfig,
        *,
        timezone: str = "America/New_York",
    ) -> None:
        equity = risk_config.account.equity
        self._state = PortfolioState(
            cash=equity,
            equity=equity,
            positions=[],
            entries_today=0,
            realized_r_today=0.0,
            muted_until=None,
        )
        self._risk_config = risk_config
        self._tz = ZoneInfo(timezone)
        self._tickets: dict[str, OrderTicket] = {}
        self._positions_by_ticket: dict[str, PositionState] = {}
        self._raw_entries: dict[str, float] = {}
        self._recently_closed: list[ClosedTrade] = []
        self._resolved_rs: dict[str, list[float]] = {}
        self._consecutive_losing_stops = 0

    @property
    def state(self) -> PortfolioState:
        return self._state

    def register_ticket(self, ticket: OrderTicket) -> None:
        self._tickets[ticket.ticket_id] = ticket

    def forget_unfilled_tickets(
        self,
        ticket_ids: Iterable[str] | None = None,
    ) -> dict[str, OrderTicket]:
        """Discard accepted tickets that never became open positions."""
        candidates = list(self._tickets) if ticket_ids is None else list(ticket_ids)
        discarded: dict[str, OrderTicket] = {}
        for ticket_id in candidates:
            if ticket_id in self._positions_by_ticket:
                continue
            ticket = self._tickets.pop(ticket_id, None)
            if ticket is not None:
                discarded[ticket_id] = ticket
        return discarded

    def apply_fills(self, fills: list[Fill], data: MarketData) -> None:
        for fill in fills:
            if fill.kind == "entry":
                self._apply_entry(fill, data)
            else:
                self._apply_exit(fill, data)

    def _apply_entry(self, fill: Fill, data: MarketData) -> None:
        ticket = self._tickets[fill.ticket_id]
        position = PositionState(
            instrument=ticket.instrument,
            side=ticket.side,
            shares=ticket.shares,
            entry_price=fill.price,
            entry_ts=fill.ts,
            stop=ticket.stop,
            target=ticket.target,
            algo_id=ticket.algo_id,
        )
        self._state.positions.append(position)
        self._positions_by_ticket[fill.ticket_id] = position
        self._raw_entries[fill.ticket_id] = _raw_entry_price(
            data,
            instrument=ticket.instrument,
            entry_fill_ts=fill.ts,
        )
        self._state.entries_today += 1

    def _apply_exit(self, fill: Fill, data: MarketData) -> None:
        position = self._positions_by_ticket[fill.ticket_id]
        raw_entry = self._raw_entries[fill.ticket_id]
        r_multiple = _r_multiple(
            slipped_exit_price=fill.price,
            slipped_entry_price=position.entry_price,
            raw_entry_price=raw_entry,
            stop=cast(float, position.stop),
        )
        dollar_pnl = position.shares * (fill.price - position.entry_price)
        self._state.cash += dollar_pnl
        self._state.equity += dollar_pnl
        exit_kind = cast(_ExitKind, fill.kind)
        closed = ClosedTrade(
            algo_id=position.algo_id,
            instrument=position.instrument,
            r_multiple=r_multiple,
            exit_kind=exit_kind,
        )

        self._positions_by_ticket.pop(fill.ticket_id)
        self._raw_entries.pop(fill.ticket_id)
        self._state.positions = [
            candidate
            for candidate in self._state.positions
            if candidate is not position
        ]
        self._recently_closed.append(closed)
        self._resolved_rs.setdefault(position.algo_id, []).append(r_multiple)
        self._update_mute_state(
            r_multiple=r_multiple,
            exit_kind=exit_kind,
            exit_ts=fill.ts,
            data=data,
        )
        self._tickets.pop(fill.ticket_id)

    def _update_mute_state(
        self,
        *,
        r_multiple: float,
        exit_kind: _ExitKind,
        exit_ts: datetime,
        data: MarketData,
    ) -> None:
        if exit_kind in ("stop", "reversal") and r_multiple < 0:
            self._consecutive_losing_stops += 1
        elif r_multiple > 0:
            self._consecutive_losing_stops = 0

        self._state.realized_r_today += r_multiple
        rails = self._risk_config.rails
        day_mute = (
            self._consecutive_losing_stops
            >= rails.mute_after_consecutive_stops
            or self._state.realized_r_today
            <= rails.mute_after_cumulative_day_r
        )
        if day_mute:
            trading_day = exit_ts.astimezone(self._tz).date()
            self._merge_mute_deadline(
                data.calendar().session_close(trading_day)
            )

        if exit_kind == "reversal":
            self._merge_mute_deadline(
                exit_ts
                + timedelta(minutes=rails.reversal_cooldown_minutes)
            )

    def _merge_mute_deadline(self, deadline: datetime) -> None:
        current = self._state.muted_until
        if current is None or deadline > current:
            self._state.muted_until = deadline

    def take_closed_trades(self) -> list[ClosedTrade]:
        closed = self._recently_closed
        self._recently_closed = []
        return closed

    def resolved_r_multiples(self, algo_id: str) -> list[float]:
        return list(self._resolved_rs.get(algo_id, ()))

    def start_new_day(self) -> None:
        """Reset daily rails; X7 must call this only while no position is open."""
        self._state.entries_today = 0
        self._state.realized_r_today = 0.0
        self._state.muted_until = None
        self._consecutive_losing_stops = 0


@dataclass(frozen=True)
class _ShadowEpisode:
    episode_id: str
    algo_id: str
    instrument: str
    stop: float | None
    target: float | None
    tag: ShadowTag
    opened_ts: datetime


@dataclass(frozen=True)
class _PendingShadow:
    episode: _ShadowEpisode
    eligible_after: datetime | None


@dataclass(frozen=True)
class _OpenShadow:
    episode: _ShadowEpisode
    entry_price: float


class ShadowBook:
    """Simulate probe and rejected intents without touching real capital."""

    def __init__(
        self,
        execution_config: ExecutionConfig,
        *,
        timezone: str = "America/New_York",
    ) -> None:
        self._execution_config = execution_config
        self._timezone = timezone
        self._tz = ZoneInfo(timezone)
        self._pending: list[_PendingShadow] = []
        self._positions: list[_OpenShadow] = []
        self._raw_entries: dict[str, float] = {}
        self._recently_closed: list[ClosedTrade] = []
        self._resolved_rs: dict[str, list[float]] = {}
        self._per_bar_counts: dict[tuple[str, datetime], int] = {}
        self._episode_tags: dict[str, ShadowTag] = {}
        self._last_asof: datetime | None = None

    def open(
        self,
        *,
        algo_id: str,
        instrument: str,
        stop: float | None,
        target: float | None,
        tag: ShadowTag,
        opened_ts: datetime,
    ) -> None:
        counter_key = (algo_id, opened_ts)
        counter = self._per_bar_counts.get(counter_key, 0)
        self._per_bar_counts[counter_key] = counter + 1
        episode = _ShadowEpisode(
            episode_id=(
                f"shadow-{algo_id}-{opened_ts.isoformat()}-{counter}"
            ),
            algo_id=algo_id,
            instrument=instrument,
            stop=stop,
            target=target,
            tag=tag,
            opened_ts=opened_ts,
        )
        self._episode_tags[episode.episode_id] = tag
        self._pending.append(
            _PendingShadow(
                episode=episode,
                eligible_after=self._last_asof,
            )
        )

    def fill_tag(self, fill: Fill) -> ShadowTag:
        """Return the refusal/probe tag for a shadow fill."""
        tag = self._episode_tags[fill.ticket_id]
        if fill.kind != "entry":
            self._episode_tags.pop(fill.ticket_id)
        return tag

    def cancel_pending(self) -> None:
        """Drop shadow intents that never reached their next-open fill."""
        for pending in self._pending:
            self._episode_tags.pop(pending.episode.episode_id, None)
        self._pending = []

    def on_bar(self, asof: datetime, data: MarketData) -> list[Fill]:
        self._last_asof = asof
        fills: list[Fill] = []
        still_pending: list[_PendingShadow] = []
        positions_to_check = list(self._positions)

        for pending in self._pending:
            episode = pending.episode
            if episode.stop is None or episode.target is None:
                # The intent event remains the refused-candidate ledger entry,
                # but an incomplete bracket has no simulatable shadow outcome.
                self._episode_tags.pop(episode.episode_id, None)
                continue
            if asof <= episode.opened_ts or (
                pending.eligible_after is not None
                and asof <= pending.eligible_after
            ):
                still_pending.append(pending)
                continue

            bars = data.bars_1m(
                episode.instrument,
                asof=asof,
                lookback_minutes=1,
            )
            if bars.empty:
                still_pending.append(pending)
                continue

            raw_entry = float(bars.iloc[0]["o"])
            bps = slippage_bps_for(
                self._execution_config,
                episode.instrument,
                asof,
                tz=self._timezone,
            )
            entry_price = round(
                apply_slippage(raw_entry, "buy", bps),
                4,
            )
            fills.append(
                Fill(
                    ticket_id=episode.episode_id,
                    ts=asof,
                    price=entry_price,
                    # Shadow episodes model prices only: no real shares are
                    # transacted for a probe or rejected candidate.
                    shares=0,
                    kind="entry",
                    book="shadow",
                )
            )
            self._raw_entries[episode.episode_id] = raw_entry
            positions_to_check.append(
                _OpenShadow(
                    episode=episode,
                    entry_price=entry_price,
                )
            )

        still_open: list[_OpenShadow] = []
        for position in positions_to_check:
            episode = position.episode
            bars = data.bars_1m(
                episode.instrument,
                asof=asof,
                lookback_minutes=1,
            )
            if bars.empty:
                still_open.append(position)
                continue

            bar = bars.iloc[0]
            exit_result = check_exit(
                stop=cast(float, episode.stop),
                target=cast(float, episode.target),
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
                self._close_position(
                    position,
                    asof=asof,
                    raw_exit_price=raw_exit_price,
                    exit_kind=exit_kind,
                )
            )

        self._pending = still_pending
        self._positions = still_open
        return fills

    def force_flat(self, asof: datetime, data: MarketData) -> list[Fill]:
        """Drop unfilled episodes and close filled episodes at the last close."""
        self.cancel_pending()
        fills: list[Fill] = []
        for position in self._positions:
            episode = position.episode
            bars = data.bars_1m(
                episode.instrument,
                asof=asof,
                lookback_minutes=1,
            )
            if bars.empty:
                raise RuntimeError(
                    "cannot force-flat a filled shadow episode without a known bar"
                )
            fills.append(
                self._close_position(
                    position,
                    asof=asof,
                    raw_exit_price=float(bars.iloc[-1]["c"]),
                    exit_kind="eod",
                )
            )
        self._positions = []
        return fills

    def _close_position(
        self,
        position: _OpenShadow,
        *,
        asof: datetime,
        raw_exit_price: float,
        exit_kind: _ExitKind,
    ) -> Fill:
        episode = position.episode
        bps = slippage_bps_for(
            self._execution_config,
            episode.instrument,
            asof,
            tz=self._timezone,
        )
        exit_price = round(
            apply_slippage(raw_exit_price, "sell", bps),
            4,
        )
        fill = Fill(
            ticket_id=episode.episode_id,
            ts=asof,
            price=exit_price,
            shares=0,
            kind=exit_kind,
            book="shadow",
        )
        r_multiple = _r_multiple(
            slipped_exit_price=exit_price,
            slipped_entry_price=position.entry_price,
            raw_entry_price=self._raw_entries.pop(episode.episode_id),
            stop=cast(float, episode.stop),
        )
        closed = ClosedTrade(
            algo_id=episode.algo_id,
            instrument=episode.instrument,
            r_multiple=r_multiple,
            exit_kind=exit_kind,
            tag=episode.tag,
        )
        self._recently_closed.append(closed)
        self._resolved_rs.setdefault(episode.algo_id, []).append(r_multiple)
        return fill

    def take_closed_trades(self) -> list[ClosedTrade]:
        closed = self._recently_closed
        self._recently_closed = []
        return closed

    def resolved_r_multiples(self, algo_id: str) -> list[float]:
        return list(self._resolved_rs.get(algo_id, ()))


@dataclass(frozen=True)
class _Aggregate:
    wins: int
    win_rate: float | None
    mean_r: float | None
    expectancy_r: float | None
    profit_factor: float | None
    max_drawdown_r: float | None
    cum_r: float


def _aggregate(rs: list[float]) -> _Aggregate:
    if not rs:
        return _Aggregate(
            wins=0,
            win_rate=None,
            mean_r=None,
            expectancy_r=None,
            profit_factor=None,
            max_drawdown_r=None,
            cum_r=0.0,
        )

    winning_rs = [r for r in rs if r > 0]
    losing_rs = [abs(r) for r in rs if r < 0]
    cum_r = sum(rs)
    mean_r = cum_r / len(rs)

    running = 0.0
    peak = 0.0
    max_drawdown_r = 0.0
    for r_multiple in rs:
        running += r_multiple
        peak = max(peak, running)
        max_drawdown_r = max(max_drawdown_r, peak - running)

    return _Aggregate(
        wins=len(winning_rs),
        win_rate=len(winning_rs) / len(rs),
        mean_r=mean_r,
        # dt exposes the same arithmetic under these two interface names.
        expectancy_r=mean_r,
        profit_factor=(
            sum(winning_rs) / sum(losing_rs) if losing_rs else None
        ),
        max_drawdown_r=max_drawdown_r,
        cum_r=cum_r,
    )


def build_algo_metrics(
    *,
    algo_id: str,
    status: AlgoStatus,
    real_rs: list[float],
    shadow_rs: list[float],
    updated_ts: datetime,
) -> tuple[AlgoMetrics, AlgoMetrics]:
    """Return independently aggregated real/shadow metrics with shared counts."""
    real = _aggregate(real_rs)
    shadow = _aggregate(shadow_rs)
    shared = {
        "algo_id": algo_id,
        "status": status,
        "n_real": len(real_rs),
        "n_shadow": len(shadow_rs),
        "updated_ts": updated_ts,
    }
    return (
        AlgoMetrics(
            **shared,
            wins=real.wins,
            win_rate=real.win_rate,
            mean_r=real.mean_r,
            expectancy_r=real.expectancy_r,
            profit_factor=real.profit_factor,
            max_drawdown_r=real.max_drawdown_r,
            cum_r=real.cum_r,
        ),
        AlgoMetrics(
            **shared,
            wins=shadow.wins,
            win_rate=shadow.win_rate,
            mean_r=shadow.mean_r,
            expectancy_r=shadow.expectancy_r,
            profit_factor=shadow.profit_factor,
            max_drawdown_r=shadow.max_drawdown_r,
            cum_r=shadow.cum_r,
        ),
    )


__all__ = [
    "ClosedTrade",
    "RealBook",
    "ShadowTag",
    "ShadowBook",
    "build_algo_metrics",
]
