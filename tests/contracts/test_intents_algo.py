"""Contract tests for strategy intents and algorithm boundaries."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
import importlib
from typing import Literal, get_args, get_type_hints

import pytest


def _intents_module():
    return importlib.import_module("trader.contracts.intents")


def _algo_module():
    return importlib.import_module("trader.contracts.algo")


def _sample_intent():
    return _intents_module().Intent(
        algo_id="breakout",
        ts=datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
        action="open",
        side="long",
        signal_symbol="SNDK",
        instrument="SNXX",
        entry="market_next_open",
        stop=41.0,
        target=46.0,
        confidence=0.8,
        reason="price cleared the opening range",
        meta={"setup": "opening_range", "rules_version": 1},
    )


def test_intent_constructs_with_exact_fields_and_values() -> None:
    intent = _sample_intent()

    assert vars(intent) == {
        "algo_id": "breakout",
        "ts": datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
        "action": "open",
        "side": "long",
        "signal_symbol": "SNDK",
        "instrument": "SNXX",
        "entry": "market_next_open",
        "stop": 41.0,
        "target": 46.0,
        "confidence": 0.8,
        "reason": "price cleared the opening range",
        "meta": {"setup": "opening_range", "rules_version": 1},
    }


def test_intent_has_exact_field_types_and_literal_values() -> None:
    intent_type = _intents_module().Intent
    side_type = importlib.import_module("trader.contracts.types").Side

    hints = get_type_hints(intent_type)

    assert hints == {
        "algo_id": str,
        "ts": datetime,
        "action": Literal["open", "close"],
        "side": side_type | None,
        "signal_symbol": str,
        "instrument": str,
        "entry": Literal["market_next_open"],
        "stop": float | None,
        "target": float | None,
        "confidence": float | None,
        "reason": str,
        "meta": dict,
    }
    assert get_args(hints["action"]) == ("open", "close")
    assert get_args(hints["entry"]) == ("market_next_open",)


def test_intent_is_frozen() -> None:
    intent = _sample_intent()

    with pytest.raises(FrozenInstanceError):
        intent.action = "close"


def test_algo_spec_constructs_with_exact_fields_and_is_mutable() -> None:
    algo_spec_type = _algo_module().AlgoSpec
    algo_status_type = importlib.import_module("trader.contracts.types").AlgoStatus
    spec = algo_spec_type(
        id="breakout",
        factory="trader.algos.breakout:BreakoutAlgo",
        params={"window": 15},
        status="probe",
    )

    assert vars(spec) == {
        "id": "breakout",
        "factory": "trader.algos.breakout:BreakoutAlgo",
        "params": {"window": 15},
        "status": "probe",
    }
    assert get_type_hints(algo_spec_type) == {
        "id": str,
        "factory": str,
        "params": dict,
        "status": algo_status_type,
    }

    spec.status = "emitting"
    assert spec.status == "emitting"


def test_algo_is_a_protocol_with_exact_interface() -> None:
    algo_type = _algo_module().Algo
    market_data_type = importlib.import_module("trader.contracts.market").MarketData
    intent_type = _intents_module().Intent

    assert algo_type._is_protocol is True
    with pytest.raises(TypeError, match="Protocols cannot be instantiated"):
        algo_type()
    assert isinstance(algo_type.id, property)
    assert get_type_hints(algo_type.id.fget) == {"return": str}
    assert get_type_hints(algo_type.warmup) == {
        "day": date,
        "data": market_data_type,
        "return": type(None),
    }
    assert get_type_hints(algo_type.on_bar) == {
        "asof": datetime,
        "data": market_data_type,
        "return": list[intent_type],
    }


def test_algo_protocol_can_be_implemented_and_used() -> None:
    algo_type = _algo_module().Algo
    intent = _sample_intent()

    class EchoAlgo(algo_type):
        @property
        def id(self) -> str:
            return "breakout"

        def warmup(self, day: date, data: object) -> None:
            self.warmed_day = day

        def on_bar(self, asof: datetime, data: object) -> list:
            return [intent]

    algo = EchoAlgo()
    day = date(2026, 7, 1)
    algo.warmup(day, object())

    assert algo.id == "breakout"
    assert algo.warmed_day == day
    assert algo.on_bar(intent.ts, object()) == [intent]
