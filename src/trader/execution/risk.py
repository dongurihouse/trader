"""Capital-slot position sizing and hard execution risk rails."""

from __future__ import annotations

from trader.contracts import (
    ContractViolation,
    Intent,
    MarketData,
    OrderTicket,
    PortfolioState,
    Rejection,
)

from .config import RiskConfig


_NO_MARGIN_TOLERANCE = 1e-9


def _shares_for_slot(slot_capital: float, entry_px: float) -> int:
    """Return whole shares funded by one settled-cash slot."""
    if entry_px <= 0:
        return 0
    return int(slot_capital // entry_px)


def _is_over_slot(shares: int, entry_px: float, slot_capital: float) -> bool:
    """Detect a notional that exceeds its settled-cash slot."""
    return shares * entry_px > slot_capital + _NO_MARGIN_TOLERANCE


class RiskRails:
    """Reject invalid open intents or size them from one capital slot."""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config

    def check_and_size(
        self,
        intent: Intent,
        portfolio: PortfolioState,
        data: MarketData,
    ) -> OrderTicket | Rejection:
        """Apply ordered hard rails and produce a deterministic order ticket."""
        if intent.action == "close":
            raise ContractViolation(
                "close intents are routed directly by the session loop and never "
                "reach the risk engine"
            )

        if (
            portfolio.muted_until is not None
            and intent.ts < portfolio.muted_until
        ):
            return Rejection(
                intent=intent,
                rule="muted",
                detail=f"portfolio is muted until {portfolio.muted_until.isoformat()}",
            )

        max_session_drawdown_r = (
            self._config.drawdown_stop.max_session_drawdown_r
        )
        if (
            max_session_drawdown_r is not None
            and portfolio.realized_r_today <= -max_session_drawdown_r
        ):
            return Rejection(
                intent=intent,
                rule="drawdown_stop",
                detail=(
                    f"portfolio realized {portfolio.realized_r_today:.2f}R today; "
                    f"session drawdown stop is {max_session_drawdown_r:.2f}R"
                ),
            )

        if portfolio.entries_today >= self._config.rails.max_entries_per_day:
            return Rejection(
                intent=intent,
                rule="max_entries_per_day",
                detail=(
                    f"portfolio already has {portfolio.entries_today} entries today; "
                    f"limit is {self._config.rails.max_entries_per_day}"
                ),
            )

        if (
            self._config.rails.one_position_at_a_time
            and len(portfolio.positions) > 0
        ):
            return Rejection(
                intent=intent,
                rule="one_position_at_a_time",
                detail="portfolio already has an open position",
            )

        if self._config.rails.no_hedge and any(
            position.instrument != intent.instrument
            for position in portfolio.positions
        ):
            held_instruments = sorted(
                {position.instrument for position in portfolio.positions}
            )
            return Rejection(
                intent=intent,
                rule="no_hedge",
                detail=(
                    f"portfolio holds {held_instruments}; cannot open "
                    f"{intent.instrument} while no-hedge rail is enabled"
                ),
            )

        if intent.stop is None:
            return Rejection(
                intent=intent,
                rule="no_stop",
                detail="open intents require a stop price",
            )

        if intent.target is None:
            return Rejection(
                intent=intent,
                rule="no_target",
                detail="open intents require a target price",
            )

        if (
            intent.side == "long"
            and intent.stop >= intent.target
        ) or (
            intent.side == "short"
            and intent.stop <= intent.target
        ):
            return Rejection(
                intent=intent,
                rule="degenerate_bracket",
                detail=(
                    f"{intent.side} bracket has no tradeable stop/target "
                    f"spread: stop {intent.stop:.2f}, target {intent.target:.2f}"
                ),
            )

        bars = data.bars_1m(
            intent.instrument,
            asof=intent.ts,
            lookback_minutes=1,
        )
        if bars.empty:
            return Rejection(
                intent=intent,
                rule="no_price_data",
                detail=(
                    "no completed one-minute bar is available for "
                    f"{intent.instrument} at {intent.ts.isoformat()}"
                ),
            )

        entry_px = round(float(bars.iloc[-1]["c"]), 2)
        risk = (
            entry_px - intent.stop
            if intent.side == "long"
            else intent.stop - entry_px
        )
        if risk <= 0:
            return Rejection(
                intent=intent,
                rule="degenerate_bracket",
                detail=(
                    f"{intent.side} stop {intent.stop:.2f} is not beyond "
                    f"entry reference price {entry_px:.2f}"
                ),
            )

        account = self._config.account
        slot_capital = (
            account.equity * account.capital_fraction / account.day_slots
        )
        shares = _shares_for_slot(slot_capital, entry_px)
        if shares <= 0:
            return Rejection(
                intent=intent,
                rule="unsized",
                detail=(
                    f"slot capital {slot_capital:.2f} cannot afford one share "
                    f"at {entry_px:.2f}"
                ),
            )

        if _is_over_slot(shares, entry_px, slot_capital):
            notional = shares * entry_px
            return Rejection(
                intent=intent,
                rule="over_slot",
                detail=(
                    f"{shares} shares at {entry_px:.2f} costs {notional:.2f}, "
                    f"exceeding slot capital {slot_capital:.2f}"
                ),
            )

        ticket_id = (
            f"{intent.algo_id}-{intent.ts.strftime('%Y%m%dT%H%M%SZ')}-"
            f"{portfolio.entries_today}"
        )
        return OrderTicket(
            ticket_id=ticket_id,
            algo_id=intent.algo_id,
            intent_ts=intent.ts,
            instrument=intent.instrument,
            side=intent.side,
            shares=shares,
            entry="market_next_open",
            stop=intent.stop,
            target=intent.target,
            risk={
                "slot": portfolio.entries_today + 1,
                "dollars": round(shares * risk, 2),
                "equity": account.equity,
            },
            created_ts=intent.ts,
        )


__all__ = ["RiskRails"]
