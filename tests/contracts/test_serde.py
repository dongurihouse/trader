"""Contract tests for JSON-safe record serialization and JSONL persistence."""

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
import importlib
import json

import pytest

from trader.contracts.intents import Intent
from trader.contracts.orders import (
    Fill,
    OrderTicket,
    PortfolioState,
    PositionState,
    Rejection,
)
from trader.contracts.telemetry import (
    AlgoErrorEvent,
    AlgoMetrics,
    DaySkippedEvent,
    FillEvent,
    IntentEvent,
    MetricsEvent,
    PositionClosedEvent,
    RejectionEvent,
    SessionEndEvent,
    SessionStartEvent,
    TicketEvent,
    TickEvent,
)
from trader.contracts.types import Bar


TS = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
BAR_TS = datetime(2026, 7, 1, 13, 29, tzinfo=timezone.utc)

REJECTION_INTENT = Intent(
    algo_id="breakout",
    ts=BAR_TS,
    action="open",
    side="long",
    signal_symbol="SNDK",
    instrument="SNXX",
    entry="market_next_open",
    stop=41.0,
    target=46.0,
    confidence=0.82,
    reason="price cleared the opening range",
    meta={
        "setup": "opening_range",
        "rules": {"version": 3, "confirmed": True},
        "levels": [41.0, 46.0],
    },
)

POSITION = PositionState(
    instrument="SNXX",
    side="long",
    shares=100,
    entry_price=43.25,
    entry_ts=TS,
    stop=None,
    target=None,
    algo_id="breakout",
)

RECORDS = [
    Bar(
        symbol="SNDK",
        ts=BAR_TS,
        open=84.0,
        high=85.5,
        low=83.75,
        close=85.0,
        volume=125_000.0,
    ),
    Intent(
        algo_id="mean-reversion",
        ts=BAR_TS,
        action="close",
        side=None,
        signal_symbol="SNDK",
        instrument="SNXX",
        entry="market_next_open",
        stop=None,
        target=None,
        confidence=None,
        reason="session risk window ended",
        meta={"setup": "vwap_reversion", "tags": ["close", "risk"]},
    ),
    OrderTicket(
        ticket_id="ticket-001",
        algo_id="breakout",
        intent_ts=BAR_TS,
        instrument="SNXX",
        side="long",
        shares=100,
        entry="market_next_open",
        stop=None,
        target=None,
        risk={"slot": 1, "dollars": 200.0, "equity": 100_000.0},
        created_ts=TS,
    ),
    Fill(
        ticket_id="ticket-001",
        ts=TS,
        price=43.25,
        shares=100,
        kind="entry",
        book="real",
        price_basis="real",
    ),
    Rejection(
        intent=REJECTION_INTENT,
        rule="daily_entry_cap",
        detail="three entries already submitted today",
    ),
    POSITION,
    PortfolioState(
        cash=95_675.0,
        equity=100_125.0,
        positions=[POSITION],
        entries_today=1,
        realized_r_today=0.5,
        muted_until=None,
    ),
    AlgoMetrics(
        algo_id="breakout",
        status="emitting",
        n_real=8,
        n_shadow=5,
        wins=6,
        win_rate=None,
        mean_r=None,
        expectancy_r=None,
        profit_factor=None,
        max_drawdown_r=None,
        cum_r=4.8,
        updated_ts=TS,
    ),
    SessionStartEvent(
        ev="session_start",
        ts=TS,
        session="session-001",
        mode="paper",
        config_sha256="abc123",
        package_version="0.1.0",
        symbols=["SNDK", "SNXX"],
        roster=[
            {"id": "breakout", "status": "emitting"},
            {"id": "mean-reversion", "status": "probe"},
        ],
    ),
    TickEvent(
        ev="tick",
        ts=TS,
        session="session-001",
        bar_ts=BAR_TS,
    ),
    DaySkippedEvent(
        ev="day_skipped",
        ts=TS,
        session="session-001",
        day="2026-07-01",
        reason="no_prev_session",
    ),
    IntentEvent(
        ev="intent",
        ts=TS,
        session="session-001",
        algo_id="mean-reversion",
        action="close",
        side=None,
        signal_symbol="SNDK",
        instrument="SNXX",
        entry="market_next_open",
        stop=None,
        target=None,
        confidence=None,
        reason="session risk window ended",
        meta={"setup": "vwap_reversion", "tags": ["close", "risk"]},
    ),
    RejectionEvent(
        ev="rejection",
        ts=TS,
        session="session-001",
        algo_id="breakout",
        action="open",
        side="long",
        signal_symbol="SNDK",
        instrument="SNXX",
        entry="market_next_open",
        stop=41.0,
        target=46.0,
        confidence=0.82,
        reason="price cleared the opening range",
        meta={"setup": "opening_range", "rules": {"version": 3}},
        rule="daily_entry_cap",
        detail="three entries already submitted today",
    ),
    TicketEvent(
        ev="ticket",
        ts=TS,
        session="session-001",
        ticket_id="ticket-001",
        algo_id="breakout",
        intent_ts=BAR_TS,
        instrument="SNXX",
        side="long",
        shares=100,
        entry="market_next_open",
        stop=41.0,
        target=46.0,
        risk={"slot": 1, "dollars": 200.0, "equity": 100_000.0},
        created_ts=TS,
    ),
    FillEvent(
        ev="fill",
        ts=TS,
        session="session-001",
        ticket_id="ticket-001",
        price=43.25,
        shares=100,
        kind="entry",
        book="real",
        tag=None,
    ),
    PositionClosedEvent(
        ev="position_closed",
        ts=TS,
        session="session-001",
        algo_id="breakout",
        instrument="SNXX",
        r_multiple=1.5,
        book="shadow",
        exit_kind="target",
        tag="probe",
    ),
    MetricsEvent(
        ev="metrics",
        ts=TS,
        session="session-001",
        book="real",
        algo_id="breakout",
        status="emitting",
        n_real=8,
        n_shadow=5,
        wins=6,
        win_rate=0.75,
        mean_r=0.6,
        expectancy_r=0.45,
        profit_factor=2.25,
        max_drawdown_r=-1.2,
        cum_r=4.8,
        updated_ts=TS,
    ),
    AlgoErrorEvent(
        ev="algo_error",
        ts=TS,
        session="session-001",
        algo_id="breakout",
        error="division by zero",
        traceback="Traceback (most recent call last): ...",
    ),
    SessionEndEvent(
        ev="session_end",
        ts=TS,
        session="session-001",
        bars_processed=390,
        real_trades=8,
        shadow_trades=5,
        final_equity=101_250.0,
    ),
]


def _serde_module():
    return importlib.import_module("trader.contracts.serde")


@pytest.mark.parametrize("record", RECORDS, ids=lambda record: type(record).__name__)
def test_record_round_trip_reconstructs_each_supported_dataclass(
    record: object,
) -> None:
    serde = _serde_module()

    encoded = serde.record_to_json(record)

    assert type(encoded) is dict
    json.dumps(encoded)
    restored = serde.record_from_json(encoded)
    assert type(restored) is type(record)
    assert restored == record


def test_record_type_tags_preserve_flat_telemetry_envelopes() -> None:
    serde = _serde_module()

    for record in RECORDS[:8]:
        assert serde.record_to_json(record)["__type__"] == type(record).__name__
    for event in RECORDS[8:]:
        assert "__type__" not in serde.record_to_json(event)


def _assert_datetime_fields_use_z(original: object, encoded: object) -> None:
    if isinstance(original, datetime):
        assert isinstance(encoded, str)
        assert encoded.endswith("Z")
        assert "+00:00" not in encoded
    elif is_dataclass(original):
        assert isinstance(encoded, dict)
        for field in fields(original):
            _assert_datetime_fields_use_z(
                getattr(original, field.name), encoded[field.name]
            )
    elif isinstance(original, list):
        assert isinstance(encoded, list)
        for original_item, encoded_item in zip(original, encoded, strict=True):
            _assert_datetime_fields_use_z(original_item, encoded_item)
    elif isinstance(original, dict):
        assert isinstance(encoded, dict)
        for key, original_value in original.items():
            _assert_datetime_fields_use_z(original_value, encoded[key])


@pytest.mark.parametrize("record", RECORDS, ids=lambda record: type(record).__name__)
def test_every_datetime_field_serializes_as_utc_z(record: object) -> None:
    encoded = _serde_module().record_to_json(record)

    _assert_datetime_fields_use_z(record, encoded)
    assert "+00:00" not in json.dumps(encoded)


def test_bar_timestamp_uses_literal_z_suffix() -> None:
    encoded = _serde_module().record_to_json(RECORDS[0])

    assert encoded["ts"].endswith("Z")
    assert "+00:00" not in encoded["ts"]


def test_append_and_read_jsonl_preserve_plain_dicts_in_order(tmp_path) -> None:
    serde = _serde_module()
    path = tmp_path / "nested" / "records.jsonl"
    records = [serde.record_to_json(record) for record in RECORDS[:3]]

    for record in records:
        serde.append_jsonl(path, record)

    restored = list(serde.read_jsonl(path))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert restored == records
    assert all(type(record) is dict for record in restored)
    assert len(lines) == len(records)
    assert [json.loads(line) for line in lines] == records


def test_append_jsonl_never_truncates_existing_records(tmp_path) -> None:
    serde = _serde_module()
    path = tmp_path / "records.jsonl"
    first = serde.record_to_json(RECORDS[0])
    second = serde.record_to_json(RECORDS[1])

    serde.append_jsonl(path, first)
    serde.append_jsonl(path, second)

    assert list(serde.read_jsonl(path)) == [first, second]
