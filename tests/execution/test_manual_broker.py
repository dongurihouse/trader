"""Tests for human-confirmed manual broker fills and notices."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from trader.contracts import Fill, OrderTicket
from trader.contracts.testing import FakeMarketData
from trader.execution.broker.manual import (
    ManualBroker,
    Style,
    record_fill,
    render_entry_ticket,
    render_exit_notice,
    style_for,
)


BAR_START = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)


def _ticket(
    ticket_id: str = "ticket-1",
    *,
    instrument: str = "SNXX",
    side: str = "long",
    shares: int = 10,
    stop: float = 95.0,
    target: float = 105.0,
) -> OrderTicket:
    return OrderTicket(
        ticket_id=ticket_id,
        algo_id="opening-breakout",
        intent_ts=BAR_START,
        instrument=instrument,
        side=side,
        shares=shares,
        entry="market_next_open",
        stop=stop,
        target=target,
        risk={"fixture": True},
        created_ts=BAR_START,
    )


def _frame(
    *rows: tuple[datetime, float, float, float, float]
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"o": open_, "h": high, "l": low, "c": close, "v": 1_000.0}
            for _ts, open_, high, low, close in rows
        ],
        index=pd.DatetimeIndex([row[0] for row in rows], name="ts"),
    )


def _data(
    *rows: tuple[datetime, float, float, float, float],
    instrument: str = "SNXX",
) -> FakeMarketData:
    return FakeMarketData({instrument: _frame(*rows)})


def _append_fill(
    state_dir: Path,
    *,
    ticket_id: str = "ticket-1",
    price: float = 100.25,
    shares: int = 10,
    kind: str = "entry",
    ts: datetime = BAR_START,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticket_id": ticket_id,
        "price": price,
        "shares": shares,
        "kind": kind,
        "ts": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    with (state_dir / "manual_fills.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


class _Tty:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_style_controls_ansi_paint_and_bell_together() -> None:
    plain = Style(enabled=False)
    colored = Style(enabled=True)

    assert plain.paint("ticket", "32") == "ticket"
    assert plain.bell == ""
    assert colored.paint("ticket", "32") == "\x1b[32mticket\x1b[0m"
    assert colored.bell == "\a"


def test_style_for_requires_tty_and_no_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert style_for(_Tty(True)).enabled is True
    assert style_for(_Tty(False)).enabled is False
    assert style_for(_Tty(True), no_color=True).enabled is False

    monkeypatch.setenv("NO_COLOR", "1")
    assert style_for(_Tty(True)).enabled is False


def test_entry_ticket_is_boxed_and_contains_only_known_order_details() -> None:
    ticket = _ticket(
        instrument="BEARX",
        side="short",
        shares=7,
        stop=91.25,
        target=108.75,
    )

    rendered = render_entry_ticket(ticket, style=Style(enabled=False))

    assert rendered
    assert "╔" in rendered and "╚" in rendered
    assert "BUY BEARX" in rendered
    assert "(short view via BEARX)" in rendered
    assert "shares 7" in rendered
    assert "stop" in rendered and "91.25" in rendered
    assert "target" in rendered and "108.75" in rendered
    assert "opening-breakout" in rendered
    assert "ticket-1" in rendered
    assert "2026-07-01" in rendered
    assert "why:" not in rendered
    assert "R)" not in rendered
    assert "\x1b[" not in rendered


def test_enabled_style_wraps_rendered_ticket_in_ansi_color() -> None:
    rendered = render_entry_ticket(_ticket(), style=Style(enabled=True))

    assert rendered.startswith("\x1b[")
    assert rendered.endswith("\x1b[0m")


@pytest.mark.parametrize("kind", ["stop", "target", "reversal", "eod"])
def test_exit_notice_is_explicitly_not_a_fill(kind: str) -> None:
    rendered = render_exit_notice(
        _ticket(),
        kind,
        style=Style(enabled=False),
    )

    assert rendered
    assert "SNXX" in rendered
    assert "ticket-1" in rendered
    assert kind in rendered.lower()
    assert "bracket" in rendered.lower()
    assert "NOTICE" in rendered
    assert "NOT A FILL" in rendered
    assert "trader fills record" in rendered
    assert "\x1b[" not in rendered


def test_record_fill_appends_exact_jsonl_shape_and_normalizes_timestamp(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "nested" / "session"
    supplied_ts = datetime(
        2026,
        7,
        1,
        9,
        35,
        tzinfo=timezone(timedelta(hours=-4)),
    )

    record_fill(
        state_dir,
        ticket_id="ticket-1",
        price=10.5,
        shares=5,
        kind="entry",
        ts=supplied_ts,
    )
    record_fill(
        state_dir,
        ticket_id="ticket-2",
        price=9.25,
        shares=3,
        kind="stop",
        ts=datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc),
    )
    record_fill(
        state_dir,
        ticket_id="ticket-3",
        kind="cancel",
        ts=datetime(2026, 7, 1, 14, 5, tzinfo=timezone.utc),
    )

    records = [
        json.loads(line)
        for line in (state_dir / "manual_fills.jsonl").read_text().splitlines()
    ]
    assert records == [
        {
            "ticket_id": "ticket-1",
            "price": 10.5,
            "shares": 5,
            "kind": "entry",
            "ts": "2026-07-01T13:35:00Z",
        },
        {
            "ticket_id": "ticket-2",
            "price": 9.25,
            "shares": 3,
            "kind": "stop",
            "ts": "2026-07-01T14:00:00Z",
        },
        {
            "ticket_id": "ticket-3",
            "kind": "cancel",
            "ts": "2026-07-01T14:05:00Z",
        },
    ]


def test_record_fill_wall_clock_default_is_recent_aware_utc(tmp_path: Path) -> None:
    before = datetime.now(timezone.utc)

    record_fill(
        tmp_path,
        ticket_id="ticket-1",
        price=10.5,
        shares=5,
        kind="entry",
    )

    after = datetime.now(timezone.utc)
    payload = json.loads((tmp_path / "manual_fills.jsonl").read_text())
    assert payload["ts"].endswith("Z")
    parsed = datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert before <= parsed <= after


def test_submit_prints_ticket_but_never_invents_an_entry_fill(tmp_path: Path) -> None:
    output: list[str] = []
    broker = ManualBroker(tmp_path, out=output.append, style=Style(enabled=False))

    broker.submit(_ticket())
    fills = broker.on_bar(
        BAR_START + timedelta(minutes=1),
        _data((BAR_START, 100.0, 101.0, 99.0, 100.0)),
    )

    assert fills == []
    assert len(output) == 1
    assert "BUY SNXX" in output[0]


def test_new_recorded_fill_is_returned_once_and_starts_position_monitoring(
    tmp_path: Path,
) -> None:
    asof = BAR_START + timedelta(minutes=1)
    broker = ManualBroker(tmp_path, out=lambda _message: None, style=Style(False))
    broker.submit(_ticket(shares=7))
    _append_fill(tmp_path, price=100.25, shares=7, ts=asof)

    first = broker.on_bar(
        asof,
        _data((BAR_START, 100.0, 104.0, 96.0, 101.0)),
    )
    second = broker.on_bar(
        asof,
        _data((BAR_START, 100.0, 104.0, 96.0, 101.0)),
    )

    assert first == [
        Fill(
            ticket_id="ticket-1",
            ts=asof,
            price=100.25,
            shares=7,
            kind="entry",
            book="real",
        )
    ]
    assert second == []


def test_cancel_record_declines_awaiting_ticket_once_without_later_notices(
    tmp_path: Path,
) -> None:
    asof = BAR_START + timedelta(minutes=1)
    output: list[str] = []
    broker = ManualBroker(tmp_path, out=output.append, style=Style(False))
    broker.submit(_ticket())
    output.clear()
    record_fill(
        tmp_path,
        ticket_id="ticket-1",
        kind="cancel",
        ts=asof,
    )

    first = broker.on_bar(
        asof,
        _data((BAR_START, 100.0, 104.0, 94.0, 99.0)),
    )
    first_declines = broker.take_declined_tickets()
    second = broker.on_bar(
        asof + timedelta(minutes=1),
        _data(
            (BAR_START, 100.0, 104.0, 94.0, 99.0),
            (BAR_START + timedelta(minutes=1), 100.0, 104.0, 94.0, 99.0),
        ),
    )
    second_declines = broker.take_declined_tickets()

    assert first == []
    assert second == []
    assert first_declines == {"ticket-1": "manual cancel"}
    assert second_declines == {}
    assert output == []


def test_open_position_stays_quiet_inside_bracket_then_notices_stop_once(
    tmp_path: Path,
) -> None:
    asof = BAR_START + timedelta(minutes=1)
    output: list[str] = []
    broker = ManualBroker(tmp_path, out=output.append, style=Style(False))
    broker.submit(_ticket())
    _append_fill(tmp_path, ts=asof)
    broker.on_bar(
        asof,
        _data((BAR_START, 100.0, 104.0, 96.0, 101.0)),
    )
    output.clear()

    breached = _data((BAR_START, 100.0, 104.0, 94.0, 99.0))
    broker.on_bar(asof, breached)
    broker.on_bar(asof, breached)
    broker.on_bar(asof, breached)

    assert len(output) == 1
    assert "stop" in output[0].lower()
    assert "NOT A FILL" in output[0]


def test_eod_condition_prints_one_notice_without_creating_a_fill(
    tmp_path: Path,
) -> None:
    close_bar = datetime(2026, 7, 1, 19, 59, tzinfo=timezone.utc)
    close_asof = close_bar + timedelta(minutes=1)
    output: list[str] = []
    broker = ManualBroker(tmp_path, out=output.append, style=Style(False))
    broker.submit(_ticket())
    _append_fill(tmp_path, ts=close_asof)

    fills = broker.on_bar(
        close_asof,
        _data((close_bar, 100.0, 104.0, 96.0, 101.0)),
    )
    broker.on_bar(
        close_asof,
        _data((close_bar, 100.0, 104.0, 96.0, 101.0)),
    )

    notices = [message for message in output if "NOTICE" in message]
    assert fills == [
        Fill(
            ticket_id="ticket-1",
            ts=close_asof,
            price=100.25,
            shares=10,
            kind="entry",
            book="real",
        )
    ]
    assert len(notices) == 1
    assert "eod" in notices[0].lower()


def test_recorded_exit_fill_is_returned_and_stops_further_notices(
    tmp_path: Path,
) -> None:
    asof = BAR_START + timedelta(minutes=1)
    output: list[str] = []
    broker = ManualBroker(tmp_path, out=output.append, style=Style(False))
    broker.submit(_ticket())
    _append_fill(tmp_path, ts=asof)
    inside = _data((BAR_START, 100.0, 104.0, 96.0, 101.0))
    broker.on_bar(asof, inside)
    output.clear()
    _append_fill(
        tmp_path,
        kind="stop",
        price=94.75,
        ts=asof + timedelta(minutes=1),
    )

    fills = broker.on_bar(asof, inside)
    broker.on_bar(asof, _data((BAR_START, 100.0, 104.0, 94.0, 99.0)))

    assert fills == [
        Fill(
            ticket_id="ticket-1",
            ts=asof + timedelta(minutes=1),
            price=94.75,
            shares=10,
            kind="stop",
            book="real",
        )
    ]
    assert output == []


def test_cancel_open_drops_awaiting_ticket_but_returns_later_stray_fill(
    tmp_path: Path,
) -> None:
    asof = BAR_START + timedelta(minutes=1)
    output: list[str] = []
    broker = ManualBroker(tmp_path, out=output.append, style=Style(False))
    broker.submit(_ticket())
    broker.cancel_open("operator canceled")
    output.clear()
    _append_fill(tmp_path, ts=asof)

    fills = broker.on_bar(
        asof,
        _data((BAR_START, 100.0, 104.0, 94.0, 99.0)),
    )

    # The append-only fill log is authoritative even for a no-longer-known ticket,
    # but a canceled ticket must not silently re-enter bracket monitoring.
    assert [fill.kind for fill in fills] == ["entry"]
    assert output == []


def test_cancel_open_keeps_confirmed_position_under_monitoring(
    tmp_path: Path,
) -> None:
    asof = BAR_START + timedelta(minutes=1)
    output: list[str] = []
    broker = ManualBroker(tmp_path, out=output.append, style=Style(False))
    broker.submit(_ticket())
    _append_fill(tmp_path, ts=asof)
    broker.on_bar(asof, _data((BAR_START, 100.0, 104.0, 96.0, 101.0)))
    broker.cancel_open("unfilled orders only")
    output.clear()

    broker.on_bar(asof, _data((BAR_START, 100.0, 104.0, 94.0, 99.0)))

    assert len(output) == 1
    assert "stop" in output[0].lower()


def test_reversal_mark_prints_one_notice_on_next_bar_without_fill(
    tmp_path: Path,
) -> None:
    entry_asof = BAR_START + timedelta(minutes=1)
    mark_asof = BAR_START + timedelta(minutes=2)
    notice_asof = BAR_START + timedelta(minutes=3)
    later_asof = BAR_START + timedelta(minutes=4)
    output: list[str] = []
    broker = ManualBroker(tmp_path, out=output.append, style=Style(False))
    broker.submit(_ticket())
    _append_fill(tmp_path, ts=entry_asof)
    broker.on_bar(
        entry_asof,
        _data((BAR_START, 100.0, 104.0, 96.0, 101.0)),
    )
    output.clear()

    broker.mark_reversal(mark_asof, "short")
    fills = broker.on_bar(
        notice_asof,
        _data(
            (BAR_START, 100.0, 104.0, 96.0, 101.0),
            (BAR_START + timedelta(minutes=1), 100.0, 104.0, 96.0, 101.0),
            (BAR_START + timedelta(minutes=2), 100.0, 104.0, 96.0, 101.0),
        ),
    )
    broker.on_bar(
        later_asof,
        _data(
            (BAR_START, 100.0, 104.0, 96.0, 101.0),
            (BAR_START + timedelta(minutes=1), 100.0, 104.0, 96.0, 101.0),
            (BAR_START + timedelta(minutes=2), 100.0, 104.0, 96.0, 101.0),
            (BAR_START + timedelta(minutes=3), 100.0, 104.0, 96.0, 101.0),
        ),
    )

    assert fills == []
    assert len(output) == 1
    assert "reversal" in output[0].lower()
    assert "NOT A FILL" in output[0]


def test_force_flat_notices_every_confirmed_position_as_urgent_and_never_fills(
    tmp_path: Path,
) -> None:
    asof = BAR_START + timedelta(minutes=1)
    output: list[str] = []
    broker = ManualBroker(tmp_path, out=output.append, style=Style(False))
    broker.submit(_ticket("ticket-1"))
    broker.submit(_ticket("ticket-2"))
    _append_fill(tmp_path, ticket_id="ticket-1", ts=asof)
    _append_fill(tmp_path, ticket_id="ticket-2", ts=asof)
    data = _data((BAR_START, 100.0, 104.0, 96.0, 101.0))
    broker.on_bar(asof, data)
    output.clear()

    fills = broker.force_flat(asof, data)

    assert fills == []
    assert len(output) == 2
    assert all("URGENT" in message for message in output)
    assert all("FORCE FLAT" in message for message in output)
    assert all("NOT A FILL" in message for message in output)
    assert "ticket-1" in output[0]
    assert "ticket-2" in output[1]
