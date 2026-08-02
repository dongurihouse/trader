"""Telemetry event records, metrics, and writer protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from .types import AlgoStatus, Mode, Side


@dataclass
class AlgoMetrics:
    algo_id: str
    status: AlgoStatus
    n_real: int
    n_shadow: int
    wins: int
    win_rate: float | None
    mean_r: float | None
    expectancy_r: float | None
    profit_factor: float | None
    max_drawdown_r: float | None
    cum_r: float
    updated_ts: datetime


@dataclass(frozen=True)
class SessionStartEvent:
    ev: Literal["session_start"]
    ts: datetime
    session: str
    mode: Mode
    config_sha256: str
    package_version: str
    symbols: list[str]
    roster: list[dict]


@dataclass(frozen=True)
class TickEvent:
    ev: Literal["tick"]
    ts: datetime
    session: str
    bar_ts: datetime


@dataclass(frozen=True)
class IntentEvent:
    ev: Literal["intent"]
    ts: datetime
    session: str
    algo_id: str
    action: Literal["open", "close"]
    side: Side | None
    signal_symbol: str
    instrument: str
    entry: Literal["market_next_open"]
    stop: float | None
    target: float | None
    confidence: float | None
    reason: str
    meta: dict


@dataclass(frozen=True)
class RejectionEvent:
    ev: Literal["rejection"]
    ts: datetime
    session: str
    algo_id: str
    action: Literal["open", "close"]
    side: Side | None
    signal_symbol: str
    instrument: str
    entry: Literal["market_next_open"]
    stop: float | None
    target: float | None
    confidence: float | None
    reason: str
    meta: dict
    rule: str
    detail: str


@dataclass(frozen=True)
class TicketEvent:
    ev: Literal["ticket"]
    ts: datetime
    session: str
    ticket_id: str
    algo_id: str
    intent_ts: datetime
    instrument: str
    side: Side
    shares: int
    entry: Literal["market_next_open"]
    stop: float | None
    target: float | None
    risk: dict
    created_ts: datetime


@dataclass(frozen=True)
class FillEvent:
    ev: Literal["fill"]
    ts: datetime
    session: str
    ticket_id: str
    price: float
    shares: int
    kind: Literal["entry", "stop", "target", "reversal", "eod"]
    book: Literal["real", "shadow"]
    tag: str | None


@dataclass(frozen=True)
class PositionClosedEvent:
    ev: Literal["position_closed"]
    ts: datetime
    session: str
    algo_id: str
    instrument: str
    r_multiple: float
    book: Literal["real", "shadow"]
    exit_kind: Literal["stop", "target", "reversal", "eod"]
    tag: str | None


@dataclass(frozen=True)
class MetricsEvent:
    ev: Literal["metrics"]
    ts: datetime
    session: str
    book: Literal["real", "shadow"]
    algo_id: str
    status: AlgoStatus
    n_real: int
    n_shadow: int
    wins: int
    win_rate: float | None
    mean_r: float | None
    expectancy_r: float | None
    profit_factor: float | None
    max_drawdown_r: float | None
    cum_r: float
    updated_ts: datetime


@dataclass(frozen=True)
class AlgoErrorEvent:
    ev: Literal["algo_error"]
    ts: datetime
    session: str
    algo_id: str
    error: str
    traceback: str


@dataclass(frozen=True)
class SessionEndEvent:
    ev: Literal["session_end"]
    ts: datetime
    session: str
    bars_processed: int
    real_trades: int
    shadow_trades: int
    final_equity: float


EVENT_TYPES: dict[str, type] = {
    "session_start": SessionStartEvent,
    "tick": TickEvent,
    "intent": IntentEvent,
    "rejection": RejectionEvent,
    "ticket": TicketEvent,
    "fill": FillEvent,
    "position_closed": PositionClosedEvent,
    "metrics": MetricsEvent,
    "algo_error": AlgoErrorEvent,
    "session_end": SessionEndEvent,
}


class TelemetryWriter(Protocol):
    def emit(self, record: dict) -> None: ...


__all__ = [
    "AlgoErrorEvent",
    "AlgoMetrics",
    "EVENT_TYPES",
    "FillEvent",
    "IntentEvent",
    "MetricsEvent",
    "PositionClosedEvent",
    "RejectionEvent",
    "SessionEndEvent",
    "SessionStartEvent",
    "TelemetryWriter",
    "TickEvent",
    "TicketEvent",
]
