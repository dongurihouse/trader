"""Run deterministic execution sessions and emit their telemetry.

Every telemetry timestamp emitted here must come from an ``asof`` or explicitly
passed ``ts`` argument.  Wall-clock timestamps (including ``datetime.now()``)
must never be used, so repeated backtests can produce byte-identical JSONL.
"""

from __future__ import annotations

from pathlib import Path

from trader.contracts import append_jsonl

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import traceback
from typing import Literal

from trader.contracts import (
    Algo,
    AlgoErrorEvent,
    AlgoMetrics,
    AlgoStatus,
    Broker,
    DaySkippedEvent,
    Fill,
    FillEvent,
    Intent,
    IntentEvent,
    MarketData,
    MetricsEvent,
    Mode,
    OrderTicket,
    PositionClosedEvent,
    Rejection,
    RejectionEvent,
    RiskEngine,
    SessionEndEvent,
    SessionStartEvent,
    TelemetryWriter,
    TickEvent,
    TicketEvent,
    record_to_json,
)
from trader.execution.book import (
    ClosedTrade,
    RealBook,
    ShadowBook,
    ShadowTag,
    build_algo_metrics,
)


class JsonlTelemetryWriter:
    """Append one flushed JSONL line per emitted record."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, record: dict) -> None:
        append_jsonl(self._path, record)


@dataclass(frozen=True)
class RosterEntry:
    algo: Algo
    status: AlgoStatus


@dataclass(frozen=True)
class SessionSummary:
    bars_processed: int
    real_trades: int
    shadow_trades: int
    final_equity: float


def _intent_fields(intent: Intent) -> dict:
    """Return the fields shared by intent and rejection telemetry."""
    return {
        "ts": intent.ts,
        "algo_id": intent.algo_id,
        "action": intent.action,
        "side": intent.side,
        "signal_symbol": intent.signal_symbol,
        "instrument": intent.instrument,
        "entry": intent.entry,
        "stop": intent.stop,
        "target": intent.target,
        "confidence": intent.confidence,
        "reason": intent.reason,
        "meta": intent.meta,
    }


def _datetime_to_json(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("telemetry timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SessionRunner:
    """Run one deterministic execution session over caller-supplied bars."""

    def __init__(
        self,
        *,
        session_id: str,
        mode: Mode,
        market_data: MarketData,
        broker: Broker,
        risk: RiskEngine,
        real_book: RealBook,
        shadow_book: ShadowBook,
        telemetry: TelemetryWriter,
        roster: list[RosterEntry],
        symbols: list[str],
        config_sha256: str,
        package_version: str,
    ) -> None:
        self._session_id = session_id
        self._mode = mode
        self._market_data = market_data
        # Broker is the shared contract type, while every concrete broker passed
        # to the runner is also expected to provide force_flat(asof, data).
        self._broker = broker
        self._risk = risk
        self._real_book = real_book
        self._shadow_book = shadow_book
        self._telemetry = telemetry
        self._roster = list(roster)
        self._symbols = list(symbols)
        self._config_sha256 = config_sha256
        self._package_version = package_version
        self._status_by_algo = {
            entry.algo.id: entry.status for entry in self._roster
        }
        self._real_entry_in_flight = False
        self._bars_processed = 0
        self._real_trades = 0
        self._shadow_trades = 0

    def start_session(self, ts: datetime) -> None:
        self._emit(
            SessionStartEvent(
                ev="session_start",
                ts=ts,
                session=self._session_id,
                mode=self._mode,
                config_sha256=self._config_sha256,
                package_version=self._package_version,
                symbols=self._symbols,
                roster=[
                    {"id": entry.algo.id, "status": entry.status}
                    for entry in self._roster
                ],
            )
        )

    def record_day_skipped(self, day: date, ts: datetime, reason: str) -> None:
        self._emit(
            DaySkippedEvent(
                ev="day_skipped",
                ts=ts,
                session=self._session_id,
                day=day.isoformat(),
                reason=reason,
            )
        )

    def start_day(self, day: date) -> None:
        self._broker.cancel_open("new trading day")
        self._real_book.start_new_day()
        # Day roll cancels only unfilled shadow entries. Filled shadow episodes
        # are kept explicit; callers invoke start_day only when the real book is flat.
        self._shadow_book.cancel_pending()
        self._real_entry_in_flight = False
        for entry in self._roster:
            if entry.status != "disabled":
                entry.algo.warmup(day, self._market_data)

    def process_bar(self, asof: datetime) -> None:
        self._emit(
            TickEvent(
                ev="tick",
                ts=asof,
                session=self._session_id,
                bar_ts=asof - timedelta(minutes=1),
            )
        )

        real_fills = self._broker.on_bar(asof, self._market_data)
        self._real_book.apply_fills(real_fills, self._market_data)
        for fill in real_fills:
            self._emit_fill(fill, tag=None)
        if any(fill.kind == "entry" for fill in real_fills):
            self._real_entry_in_flight = False
        for closed in self._real_book.take_closed_trades():
            self._emit_closed_trade(closed, "real", asof)

        declined_tickets = self._broker.take_declined_tickets()  # type: ignore[attr-defined]
        for ticket_id, reason in declined_tickets.items():
            self._real_entry_in_flight = False
            discarded = self._real_book.forget_unfilled_tickets([ticket_id])
            ticket = discarded.get(ticket_id)
            if ticket is None:
                continue
            self._telemetry.emit(
                {
                    "ev": "ticket_declined",
                    "ts": _datetime_to_json(asof),
                    "session": self._session_id,
                    "ticket_id": ticket.ticket_id,
                    "algo_id": ticket.algo_id,
                    "reason": reason,
                }
            )

        shadow_fills = self._shadow_book.on_bar(asof, self._market_data)
        for fill in shadow_fills:
            self._emit_fill(fill, tag=self._shadow_book.fill_tag(fill))
        for closed in self._shadow_book.take_closed_trades():
            self._emit_closed_trade(closed, "shadow", asof)

        for entry in self._roster:
            if entry.status == "disabled":
                continue
            try:
                intents = entry.algo.on_bar(asof, self._market_data)
            except Exception as exc:
                self._emit(
                    AlgoErrorEvent(
                        ev="algo_error",
                        ts=asof,
                        session=self._session_id,
                        algo_id=entry.algo.id,
                        error=str(exc),
                        traceback=traceback.format_exc(),
                    )
                )
                continue

            for intent in intents:
                self._emit_intent(intent)
                if intent.action == "close":
                    # OrderTicket cannot identify a specific open position, and
                    # config/algos.yaml still contains only PORT placeholders;
                    # no shipped algo emits close intents pending a later wave.
                    # V1 therefore records this intent without speculating about
                    # closing machinery for a path nothing currently exercises.
                    continue

                if intent.meta.get("gates_pass") is False:
                    self._open_shadow(intent, "gate_refused")
                    continue

                if entry.status == "probe":
                    self._open_shadow(intent, "probe")
                    continue

                if (
                    self._real_entry_in_flight
                    or len(self._real_book.state.positions) > 0
                ):
                    self._reject(
                        Rejection(
                            intent=intent,
                            rule="position_in_flight",
                            detail=(
                                "a real entry is already open or pending fill"
                            ),
                        ),
                    )
                    continue

                decision = self._risk.check_and_size(
                    intent,
                    self._real_book.state,
                    self._market_data,
                )
                if isinstance(decision, Rejection):
                    self._reject(decision)
                    continue

                try:
                    self._broker.submit(decision)
                except Exception as exc:
                    self._emit(
                        AlgoErrorEvent(
                            ev="algo_error",
                            ts=asof,
                            session=self._session_id,
                            algo_id=decision.algo_id,
                            error=f"broker submit failed: {exc}",
                            traceback=traceback.format_exc(),
                        )
                    )
                    continue

                self._emit_ticket(decision, asof)
                self._real_book.register_ticket(decision)
                self._real_entry_in_flight = True

        self._bars_processed += 1

    def end_session(self, asof: datetime) -> SessionSummary:
        self._broker.cancel_open("session end")
        self._real_book.forget_unfilled_tickets()
        self._real_entry_in_flight = False
        # force_flat is an expected extension implemented by every concrete
        # broker accepted by SessionRunner (SimBroker, ManualBroker, ApiBroker).
        real_fills = self._broker.force_flat(  # type: ignore[attr-defined]
            asof,
            self._market_data,
        )
        self._real_book.apply_fills(real_fills, self._market_data)
        for fill in real_fills:
            self._emit_fill(fill, tag=None)
        for closed in self._real_book.take_closed_trades():
            self._emit_closed_trade(closed, "real", asof)

        shadow_fills = self._shadow_book.force_flat(asof, self._market_data)
        for fill in shadow_fills:
            self._emit_fill(fill, tag=self._shadow_book.fill_tag(fill))
        for closed in self._shadow_book.take_closed_trades():
            self._emit_closed_trade(closed, "shadow", asof)

        summary = SessionSummary(
            bars_processed=self._bars_processed,
            real_trades=self._real_trades,
            shadow_trades=self._shadow_trades,
            final_equity=self._real_book.state.equity,
        )
        self._emit(
            SessionEndEvent(
                ev="session_end",
                ts=asof,
                session=self._session_id,
                bars_processed=summary.bars_processed,
                real_trades=summary.real_trades,
                shadow_trades=summary.shadow_trades,
                final_equity=summary.final_equity,
            )
        )
        return summary

    def _emit(self, event: object) -> None:
        self._telemetry.emit(record_to_json(event))

    def _emit_intent(self, intent: Intent) -> None:
        self._emit(
            IntentEvent(
                ev="intent",
                session=self._session_id,
                **_intent_fields(intent),
            )
        )

    def _reject(self, rejection: Rejection) -> None:
        self._emit(
            RejectionEvent(
                ev="rejection",
                session=self._session_id,
                **_intent_fields(rejection.intent),
                rule=rejection.rule,
                detail=rejection.detail,
            )
        )
        if rejection.intent.stop is not None and rejection.intent.target is not None:
            self._open_shadow(rejection.intent, "rejected")

    def _open_shadow(
        self,
        intent: Intent,
        tag: ShadowTag,
    ) -> None:
        self._shadow_book.open(
            algo_id=intent.algo_id,
            instrument=intent.instrument,
            stop=intent.stop,
            target=intent.target,
            tag=tag,
            opened_ts=intent.ts,
        )

    def _emit_ticket(self, ticket: OrderTicket, asof: datetime) -> None:
        self._emit(
            TicketEvent(
                ev="ticket",
                ts=asof,
                session=self._session_id,
                ticket_id=ticket.ticket_id,
                algo_id=ticket.algo_id,
                intent_ts=ticket.intent_ts,
                instrument=ticket.instrument,
                side=ticket.side,
                shares=ticket.shares,
                entry=ticket.entry,
                stop=ticket.stop,
                target=ticket.target,
                risk=ticket.risk,
                created_ts=ticket.created_ts,
            )
        )

    def _emit_fill(self, fill: Fill, *, tag: str | None) -> None:
        self._emit(
            FillEvent(
                ev="fill",
                ts=fill.ts,
                session=self._session_id,
                ticket_id=fill.ticket_id,
                price=fill.price,
                shares=fill.shares,
                kind=fill.kind,
                book=fill.book,
                tag=tag,
            )
        )

    def _emit_closed_trade(
        self,
        closed: ClosedTrade,
        book: Literal["real", "shadow"],
        asof: datetime,
    ) -> None:
        self._emit(
            PositionClosedEvent(
                ev="position_closed",
                ts=asof,
                session=self._session_id,
                algo_id=closed.algo_id,
                instrument=closed.instrument,
                r_multiple=closed.r_multiple,
                book=book,
                exit_kind=closed.exit_kind,
                tag=closed.tag,
            )
        )
        if book == "real":
            self._real_trades += 1
        else:
            self._shadow_trades += 1
        self._emit_metrics(closed.algo_id, asof)

    def _emit_metrics(self, algo_id: str, asof: datetime) -> None:
        real_metrics, shadow_metrics = build_algo_metrics(
            algo_id=algo_id,
            status=self._status_by_algo[algo_id],
            real_rs=self._real_book.resolved_r_multiples(algo_id),
            shadow_rs=self._shadow_book.resolved_r_multiples(algo_id),
            updated_ts=asof,
        )
        self._emit_metrics_side(real_metrics, "real", asof)
        self._emit_metrics_side(shadow_metrics, "shadow", asof)

    def _emit_metrics_side(
        self,
        metrics: AlgoMetrics,
        book: Literal["real", "shadow"],
        asof: datetime,
    ) -> None:
        self._emit(
            MetricsEvent(
                ev="metrics",
                ts=asof,
                session=self._session_id,
                book=book,
                algo_id=metrics.algo_id,
                status=metrics.status,
                n_real=metrics.n_real,
                n_shadow=metrics.n_shadow,
                wins=metrics.wins,
                win_rate=metrics.win_rate,
                mean_r=metrics.mean_r,
                expectancy_r=metrics.expectancy_r,
                profit_factor=metrics.profit_factor,
                max_drawdown_r=metrics.max_drawdown_r,
                cum_r=metrics.cum_r,
                updated_ts=metrics.updated_ts,
            )
        )
