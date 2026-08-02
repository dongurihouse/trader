"""Human-confirmed live execution with terminal tickets and exit notices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import textwrap
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.contracts import Fill, MarketData, OrderTicket, Side, append_jsonl

from .sim import check_exit


FilledKind = Literal["entry", "stop", "target", "reversal", "eod"]
FillKind = Literal["entry", "stop", "target", "reversal", "eod", "cancel"]
ExitNoticeKind = Literal["stop", "target", "reversal", "eod"]


@dataclass(frozen=True)
class Style:
    """Keep terminal color and the audible bell under one output switch."""

    enabled: bool

    def paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled else text

    @property
    def bell(self) -> str:
        return "\a" if self.enabled else ""


DIM, BOLD, GREEN, RED, CYAN = "2", "1", "32", "31", "36"


def style_for(stream=None, *, no_color: bool = False) -> Style:
    """Enable color and bell only for a TTY with no explicit color opt-out."""

    stream = sys.stdout if stream is None else stream
    enabled = (
        not no_color
        and not os.environ.get("NO_COLOR")
        and bool(getattr(stream, "isatty", lambda: False)())
    )
    return Style(enabled=enabled)


BOX_WIDTH = 76


def _box(head_left: str, head_right: str, lines: list[str]) -> str:
    """Render a content-sized, wrapping box capped at ``BOX_WIDTH`` columns."""

    wrapped: list[str] = []
    for line in lines:
        wrapped += textwrap.wrap(
            line,
            BOX_WIDTH,
            subsequent_indent="       ",
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]
    body = [f"  {line}  " for line in wrapped]
    inner = max(max(len(item) for item in body), len(head_left) + len(head_right) + 7)
    fill = inner - 6 - len(head_left) - len(head_right)
    top = f"╔═ {head_left} " + "─" * fill + f" {head_right} ═╗"
    output = [top]
    output += [f"║{item.ljust(inner)}║" for item in body]
    output += ["╚" + "═" * inner + "╝"]
    return "\n".join(output)


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("manual fill timestamps must be timezone-aware")
    return parsed


def _instrument_context(ticket: OrderTicket) -> str:
    return f"({ticket.side} view via {ticket.instrument})"


def _level(value: float | None) -> str:
    return "none" if value is None else f"{value:.4f}"


def render_entry_ticket(
    ticket: OrderTicket,
    *,
    style: Style | None = None,
) -> str:
    """Render the executable details known before a human broker fill exists."""

    resolved_style = style_for() if style is None else style
    lines = [
        f"BUY {ticket.instrument}        {_instrument_context(ticket)}",
        f"shares {ticket.shares}",
        f"stop   {_level(ticket.stop)}",
        f"target {_level(ticket.target)}",
        f"algo_id {ticket.algo_id}",
        f"ticket_id {ticket.ticket_id}",
        f"created_ts {_iso_z(ticket.created_ts)}",
    ]
    return resolved_style.paint(_box("▶ ENTER", "MANUAL", lines), GREEN)


def _render_exit_notice(
    ticket: OrderTicket,
    kind: ExitNoticeKind,
    *,
    style: Style,
    forced: bool,
) -> str:
    if forced:
        head_left = "‼ URGENT FORCE FLAT"
        head_right = "MANUAL ACTION"
        first_line = f"{ticket.instrument} must be flattened now (requested kind: {kind})"
    else:
        head_left = "⚠ NOTICE"
        head_right = kind.upper()
        first_line = f"{ticket.instrument} bracket/exit condition looks hit: {kind}"

    lines = [
        first_line,
        _instrument_context(ticket),
        "THIS IS A NOTICE, NOT A FILL.",
        "Confirm what actually happened at your broker.",
        "Then record it with: trader fills record",
        f"stop {_level(ticket.stop)}  ·  target {_level(ticket.target)}",
        f"algo_id {ticket.algo_id}  ·  ticket_id {ticket.ticket_id}",
    ]
    return style.paint(_box(head_left, head_right, lines), RED)


def render_exit_notice(
    ticket: OrderTicket,
    kind: ExitNoticeKind,
    *,
    style: Style | None = None,
) -> str:
    """Render an informational bracket/EOD notice that explicitly is not a fill."""

    resolved_style = style_for() if style is None else style
    return _render_exit_notice(ticket, kind, style=resolved_style, forced=False)


@dataclass
class _ConfirmedPosition:
    ticket: OrderTicket
    notice_shown: bool = False
    reversal_eligible_after: datetime | None = None
    reversal_notice_shown: bool = False


class ManualBroker:
    """Display live orders while accepting fills only from the operator's log."""

    def __init__(
        self,
        state_dir: Path,
        *,
        out: Callable[[str], None] = print,
        style: Style | None = None,
        timezone: str = "America/New_York",
    ) -> None:
        self._state_dir = Path(state_dir)
        self._fills_path = self._state_dir / "manual_fills.jsonl"
        self._out = out
        self._style = style_for() if style is None else style
        self._tz = ZoneInfo(timezone)
        self._fill_offset = 0
        self._awaiting: dict[str, OrderTicket] = {}
        self._confirmed: dict[str, _ConfirmedPosition] = {}
        self._declined: dict[str, str] = {}

    def _emit(self, rendered: str) -> None:
        self._out(self._style.bell + rendered)

    def submit(self, ticket: OrderTicket) -> None:
        self._emit(render_entry_ticket(ticket, style=self._style))
        self._awaiting[ticket.ticket_id] = ticket

    def _read_new_fills(self) -> list[Fill]:
        if not self._fills_path.exists():
            return []

        fills: list[Fill] = []
        with self._fills_path.open("rb") as handle:
            handle.seek(self._fill_offset)
            while True:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    handle.seek(line_start)
                    break

                payload = json.loads(line)
                if payload["kind"] == "cancel":
                    ticket_id = payload["ticket_id"]
                    if self._awaiting.pop(ticket_id, None) is not None:
                        self._declined[ticket_id] = "manual cancel"
                    self._fill_offset = handle.tell()
                    continue

                fill = Fill(
                    ticket_id=payload["ticket_id"],
                    ts=_parse_timestamp(payload["ts"]),
                    price=payload["price"],
                    shares=payload["shares"],
                    kind=cast(FilledKind, payload["kind"]),
                    book="real",
                    price_basis="real",
                )
                fills.append(fill)
                self._fill_offset = handle.tell()

        return fills

    def on_bar(self, asof: datetime, data: MarketData) -> list[Fill]:
        fills = self._read_new_fills()
        for fill in fills:
            if fill.kind == "entry":
                ticket = self._awaiting.pop(fill.ticket_id, None)
                if ticket is not None:
                    self._confirmed[fill.ticket_id] = _ConfirmedPosition(ticket)
            else:
                self._confirmed.pop(fill.ticket_id, None)

        for position in self._confirmed.values():
            if (
                position.reversal_eligible_after is not None
                and asof > position.reversal_eligible_after
                and not position.reversal_notice_shown
            ):
                self._emit(
                    render_exit_notice(
                        position.ticket,
                        "reversal",
                        style=self._style,
                    )
                )
                position.reversal_notice_shown = True
                position.notice_shown = True
                continue

            if position.notice_shown:
                continue

            ticket = position.ticket
            bars = data.bars_1m(
                ticket.instrument,
                asof=asof,
                lookback_minutes=1,
            )
            if bars.empty:
                continue

            bar = bars.iloc[0]
            exit_result = check_exit(
                stop=cast(float, ticket.stop),
                target=cast(float, ticket.target),
                bar_open=float(bar["o"]),
                bar_low=float(bar["l"]),
                bar_high=float(bar["h"]),
            )
            if exit_result is not None:
                exit_kind: ExitNoticeKind = exit_result[0]
            else:
                trading_day = asof.astimezone(self._tz).date()
                session_close = data.calendar().session_close(trading_day)
                if asof < session_close:
                    continue
                exit_kind = "eod"

            self._emit(render_exit_notice(ticket, exit_kind, style=self._style))
            position.notice_shown = True

        return fills

    def cancel_open(self, reason: str) -> None:
        del reason
        self._awaiting.clear()

    def take_declined_tickets(self) -> dict[str, str]:
        declined = self._declined
        self._declined = {}
        return declined

    def mark_reversal(self, asof: datetime, trigger_side: Side) -> None:
        for position in self._confirmed.values():
            if (
                position.ticket.side != trigger_side
                and position.reversal_eligible_after is None
                and not position.reversal_notice_shown
            ):
                position.reversal_eligible_after = asof

    def force_flat(self, asof: datetime, data: MarketData) -> list[Fill]:
        del asof, data
        for position in self._confirmed.values():
            self._emit(
                _render_exit_notice(
                    position.ticket,
                    "eod",
                    style=self._style,
                    forced=True,
                )
            )
        return []


def record_fill(
    state_dir: Path,
    *,
    ticket_id: str,
    kind: FillKind,
    price: float | None = None,
    shares: int | None = None,
    ts: datetime | None = None,
) -> None:
    """Append one operator-confirmed real fill to a session's manual fill log."""

    fill_ts = datetime.now(timezone.utc) if ts is None else ts
    record: dict[str, object] = {
        "ticket_id": ticket_id,
        "kind": kind,
        "ts": _iso_z(fill_ts),
    }
    if kind == "cancel":
        if price is not None or shares is not None:
            raise ValueError("cancel records must not include price or shares")
    else:
        if price is None or shares is None:
            raise ValueError("price and shares are required for fill records")
        record["price"] = price
        record["shares"] = shares

    append_jsonl(
        Path(state_dir) / "manual_fills.jsonl",
        record,
    )


__all__ = [
    "ManualBroker",
    "Style",
    "record_fill",
    "render_entry_ticket",
    "render_exit_notice",
    "style_for",
]
