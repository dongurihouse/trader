"""Contract tests for telemetry records, metrics, and writer protocol."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import importlib
import inspect
from typing import Literal, get_args, get_origin, get_type_hints

import pytest


TS = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
BAR_TS = datetime(2026, 7, 1, 13, 29, tzinfo=timezone.utc)

EVENT_CASES = [
    (
        "SessionStartEvent",
        {
            "ev": "session_start",
            "ts": TS,
            "session": "session-001",
            "mode": "paper",
            "config_sha256": "abc123",
            "package_version": "0.1.0",
            "symbols": ["SNDK", "SNXX"],
            "etf_price_basis": "auto",
            "qualifying_day_counts": {"SNXX": 1, "SNDQ": 0},
            "roster": [
                {"id": "breakout", "status": "emitting"},
                {"id": "mean-reversion", "status": "probe"},
            ],
        },
    ),
    (
        "TickEvent",
        {
            "ev": "tick",
            "ts": TS,
            "session": "session-001",
            "bar_ts": BAR_TS,
        },
    ),
    (
        "DaySkippedEvent",
        {
            "ev": "day_skipped",
            "ts": TS,
            "session": "session-001",
            "day": "2026-07-01",
            "reason": "no_prev_session",
        },
    ),
    (
        "DataThinEvent",
        {
            "ev": "data_thin",
            "ts": TS,
            "session": "session-001",
            "symbol": "SNXX",
            "day": "2026-07-01",
            "bar_count": 42,
            "min_intraday_bars": 100,
        },
    ),
    (
        "IntentEvent",
        {
            "ev": "intent",
            "ts": TS,
            "session": "session-001",
            "algo_id": "breakout",
            "action": "open",
            "side": "long",
            "signal_symbol": "SNDK",
            "instrument": "SNXX",
            "entry": "market_next_open",
            "stop": 41.0,
            "target": 46.0,
            "confidence": 0.8,
            "reason": "price cleared the opening range",
            "meta": {"setup": "opening_range"},
        },
    ),
    (
        "RejectionEvent",
        {
            "ev": "rejection",
            "ts": TS,
            "session": "session-001",
            "algo_id": "breakout",
            "action": "open",
            "side": "long",
            "signal_symbol": "SNDK",
            "instrument": "SNXX",
            "entry": "market_next_open",
            "stop": 41.0,
            "target": 46.0,
            "confidence": 0.8,
            "reason": "price cleared the opening range",
            "meta": {"setup": "opening_range"},
            "rule": "daily_entry_cap",
            "detail": "three entries already submitted today",
        },
    ),
    (
        "TicketEvent",
        {
            "ev": "ticket",
            "ts": TS,
            "session": "session-001",
            "ticket_id": "ticket-001",
            "algo_id": "breakout",
            "intent_ts": BAR_TS,
            "instrument": "SNXX",
            "side": "long",
            "shares": 100,
            "entry": "market_next_open",
            "stop": 41.0,
            "target": 46.0,
            "risk": {"slot": 1, "dollars": 200.0, "equity": 100_000.0},
            "created_ts": TS,
        },
    ),
    (
        "FillEvent",
        {
            "ev": "fill",
            "ts": TS,
            "session": "session-001",
            "ticket_id": "ticket-001",
            "price": 43.25,
            "shares": 100,
            "kind": "entry",
            "book": "real",
            "price_basis": "real",
            "tag": None,
        },
    ),
    (
        "PositionClosedEvent",
        {
            "ev": "position_closed",
            "ts": TS,
            "session": "session-001",
            "algo_id": "breakout",
            "instrument": "SNXX",
            "r_multiple": 1.5,
            "book": "shadow",
            "exit_kind": "target",
            "tag": "probe",
        },
    ),
    (
        "MetricsEvent",
        {
            "ev": "metrics",
            "ts": TS,
            "session": "session-001",
            "book": "real",
            "algo_id": "breakout",
            "status": "emitting",
            "n_real": 8,
            "n_shadow": 5,
            "wins": 6,
            "win_rate": 0.75,
            "mean_r": 0.6,
            "expectancy_r": 0.45,
            "profit_factor": 2.25,
            "max_drawdown_r": -1.2,
            "cum_r": 4.8,
            "updated_ts": TS,
        },
    ),
    (
        "AlgoErrorEvent",
        {
            "ev": "algo_error",
            "ts": TS,
            "session": "session-001",
            "algo_id": "breakout",
            "error": "division by zero",
            "traceback": "Traceback (most recent call last): ...",
        },
    ),
    (
        "SessionEndEvent",
        {
            "ev": "session_end",
            "ts": TS,
            "session": "session-001",
            "bars_processed": 390,
            "real_trades": 8,
            "shadow_trades": 5,
            "final_equity": 101_250.0,
        },
    ),
]


def _telemetry_module():
    return importlib.import_module("trader.contracts.telemetry")


@pytest.mark.parametrize(("class_name", "values"), EVENT_CASES)
def test_event_constructs_with_exact_fields_and_values(
    class_name: str, values: dict
) -> None:
    event_type = getattr(_telemetry_module(), class_name)

    event = event_type(**values)

    assert vars(event) == values
    assert tuple(vars(event)) == tuple(values)


@pytest.mark.parametrize(("class_name", "values"), EVENT_CASES)
def test_event_has_only_required_fields_and_is_frozen(
    class_name: str, values: dict
) -> None:
    event_type = getattr(_telemetry_module(), class_name)
    parameters = inspect.signature(event_type).parameters

    assert tuple(parameters) == tuple(values)
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in parameters.values()
    )

    with pytest.raises(FrozenInstanceError):
        event_type(**values).session = "session-002"


def test_event_fields_have_exact_types() -> None:
    telemetry = _telemetry_module()
    contract_types = importlib.import_module("trader.contracts.types")
    common = {
        "ts": datetime,
        "session": str,
    }
    intent_payload = {
        "algo_id": str,
        "action": Literal["open", "close"],
        "side": contract_types.Side | None,
        "signal_symbol": str,
        "instrument": str,
        "entry": Literal["market_next_open"],
        "stop": float | None,
        "target": float | None,
        "confidence": float | None,
        "reason": str,
        "meta": dict,
    }

    assert get_type_hints(telemetry.SessionStartEvent) == {
        "ev": Literal["session_start"],
        **common,
        "mode": contract_types.Mode,
        "config_sha256": str,
        "package_version": str,
        "symbols": list[str],
        "etf_price_basis": Literal["auto", "synthetic", "real"],
        "qualifying_day_counts": dict[str, int],
        "roster": list[dict],
    }
    assert get_type_hints(telemetry.TickEvent) == {
        "ev": Literal["tick"],
        **common,
        "bar_ts": datetime,
    }
    assert get_type_hints(telemetry.DaySkippedEvent) == {
        "ev": Literal["day_skipped"],
        **common,
        "day": str,
        "reason": str,
    }
    assert get_type_hints(telemetry.DataThinEvent) == {
        "ev": Literal["data_thin"],
        **common,
        "symbol": str,
        "day": str,
        "bar_count": int,
        "min_intraday_bars": int,
    }
    assert get_type_hints(telemetry.IntentEvent) == {
        "ev": Literal["intent"],
        **common,
        **intent_payload,
    }
    assert get_type_hints(telemetry.RejectionEvent) == {
        "ev": Literal["rejection"],
        **common,
        **intent_payload,
        "rule": str,
        "detail": str,
    }
    assert get_type_hints(telemetry.TicketEvent) == {
        "ev": Literal["ticket"],
        **common,
        "ticket_id": str,
        "algo_id": str,
        "intent_ts": datetime,
        "instrument": str,
        "side": contract_types.Side,
        "shares": int,
        "entry": Literal["market_next_open"],
        "stop": float | None,
        "target": float | None,
        "risk": dict,
        "created_ts": datetime,
    }
    assert get_type_hints(telemetry.FillEvent) == {
        "ev": Literal["fill"],
        **common,
        "ticket_id": str,
        "price": float,
        "shares": int,
        "kind": Literal["entry", "stop", "target", "reversal", "eod"],
        "book": Literal["real", "shadow"],
        "price_basis": Literal["real", "synthetic"],
        "tag": str | None,
    }
    assert get_type_hints(telemetry.PositionClosedEvent) == {
        "ev": Literal["position_closed"],
        **common,
        "algo_id": str,
        "instrument": str,
        "r_multiple": float,
        "book": Literal["real", "shadow"],
        "exit_kind": Literal["stop", "target", "reversal", "eod"],
        "tag": str | None,
    }
    assert get_type_hints(telemetry.MetricsEvent) == {
        "ev": Literal["metrics"],
        **common,
        "book": Literal["real", "shadow"],
        "algo_id": str,
        "status": contract_types.AlgoStatus,
        "n_real": int,
        "n_shadow": int,
        "wins": int,
        "win_rate": float | None,
        "mean_r": float | None,
        "expectancy_r": float | None,
        "profit_factor": float | None,
        "max_drawdown_r": float | None,
        "cum_r": float,
        "updated_ts": datetime,
    }
    assert get_type_hints(telemetry.AlgoErrorEvent) == {
        "ev": Literal["algo_error"],
        **common,
        "algo_id": str,
        "error": str,
        "traceback": str,
    }
    assert get_type_hints(telemetry.SessionEndEvent) == {
        "ev": Literal["session_end"],
        **common,
        "bars_processed": int,
        "real_trades": int,
        "shadow_trades": int,
        "final_equity": float,
    }


@pytest.mark.parametrize(
    ("class_name", "field_name", "literal_values"),
    [
        ("SessionStartEvent", "ev", ("session_start",)),
        ("SessionStartEvent", "mode", ("backtest", "paper", "live")),
        ("SessionStartEvent", "etf_price_basis", ("auto", "synthetic", "real")),
        ("TickEvent", "ev", ("tick",)),
        ("DaySkippedEvent", "ev", ("day_skipped",)),
        ("DataThinEvent", "ev", ("data_thin",)),
        ("IntentEvent", "ev", ("intent",)),
        ("IntentEvent", "action", ("open", "close")),
        ("IntentEvent", "side", ("long", "short")),
        ("IntentEvent", "entry", ("market_next_open",)),
        ("RejectionEvent", "ev", ("rejection",)),
        ("RejectionEvent", "action", ("open", "close")),
        ("RejectionEvent", "side", ("long", "short")),
        ("RejectionEvent", "entry", ("market_next_open",)),
        ("TicketEvent", "ev", ("ticket",)),
        ("TicketEvent", "side", ("long", "short")),
        ("TicketEvent", "entry", ("market_next_open",)),
        ("FillEvent", "ev", ("fill",)),
        ("FillEvent", "kind", ("entry", "stop", "target", "reversal", "eod")),
        ("FillEvent", "book", ("real", "shadow")),
        ("FillEvent", "price_basis", ("real", "synthetic")),
        ("PositionClosedEvent", "ev", ("position_closed",)),
        ("PositionClosedEvent", "book", ("real", "shadow")),
        ("PositionClosedEvent", "exit_kind", ("stop", "target", "reversal", "eod")),
        ("MetricsEvent", "ev", ("metrics",)),
        ("MetricsEvent", "book", ("real", "shadow")),
        ("MetricsEvent", "status", ("emitting", "probe", "disabled")),
        ("AlgoErrorEvent", "ev", ("algo_error",)),
        ("SessionEndEvent", "ev", ("session_end",)),
    ],
)
def test_event_literal_field_has_exact_values(
    class_name: str, field_name: str, literal_values: tuple[str, ...]
) -> None:
    annotation = get_type_hints(getattr(_telemetry_module(), class_name))[field_name]
    if get_origin(annotation) is not Literal:
        annotation = next(
            argument
            for argument in get_args(annotation)
            if get_origin(argument) is Literal
        )

    assert get_args(annotation) == literal_values


def test_algo_metrics_constructs_with_exact_fields_types_and_is_mutable() -> None:
    metrics_type = _telemetry_module().AlgoMetrics
    contract_types = importlib.import_module("trader.contracts.types")
    metrics = metrics_type(
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
    )

    assert vars(metrics) == {
        "algo_id": "breakout",
        "status": "emitting",
        "n_real": 8,
        "n_shadow": 5,
        "wins": 6,
        "win_rate": 0.75,
        "mean_r": 0.6,
        "expectancy_r": 0.45,
        "profit_factor": 2.25,
        "max_drawdown_r": -1.2,
        "cum_r": 4.8,
        "updated_ts": TS,
    }
    assert get_type_hints(metrics_type) == {
        "algo_id": str,
        "status": contract_types.AlgoStatus,
        "n_real": int,
        "n_shadow": int,
        "wins": int,
        "win_rate": float | None,
        "mean_r": float | None,
        "expectancy_r": float | None,
        "profit_factor": float | None,
        "max_drawdown_r": float | None,
        "cum_r": float,
        "updated_ts": datetime,
    }
    assert get_args(get_type_hints(metrics_type)["status"]) == (
        "emitting",
        "probe",
        "disabled",
    )

    metrics.status = "probe"
    assert metrics.status == "probe"


def test_event_types_maps_exact_tags_to_event_classes() -> None:
    telemetry = _telemetry_module()
    expected = {
        "session_start": telemetry.SessionStartEvent,
        "tick": telemetry.TickEvent,
        "day_skipped": telemetry.DaySkippedEvent,
        "data_thin": telemetry.DataThinEvent,
        "intent": telemetry.IntentEvent,
        "rejection": telemetry.RejectionEvent,
        "ticket": telemetry.TicketEvent,
        "fill": telemetry.FillEvent,
        "position_closed": telemetry.PositionClosedEvent,
        "metrics": telemetry.MetricsEvent,
        "algo_error": telemetry.AlgoErrorEvent,
        "session_end": telemetry.SessionEndEvent,
    }

    assert telemetry.EVENT_TYPES == expected
    assert len(telemetry.EVENT_TYPES) == 12
    for tag, event_type in expected.items():
        assert telemetry.EVENT_TYPES[tag] is event_type


def test_telemetry_writer_is_a_protocol_with_exact_interface() -> None:
    writer_type = _telemetry_module().TelemetryWriter

    assert writer_type._is_protocol is True
    with pytest.raises(TypeError, match="Protocols cannot be instantiated"):
        writer_type()
    assert get_type_hints(writer_type.emit) == {
        "record": dict,
        "return": type(None),
    }


def test_telemetry_writer_protocol_can_be_implemented_and_used() -> None:
    writer_type = _telemetry_module().TelemetryWriter

    class CollectingWriter(writer_type):
        def __init__(self) -> None:
            self.records = []

        def emit(self, record: dict) -> None:
            self.records.append(record)

    writer = CollectingWriter()
    record = {"ev": "tick", "ts": "2026-07-01T13:30:00Z"}

    writer.emit(record)

    assert writer.records == [record]
