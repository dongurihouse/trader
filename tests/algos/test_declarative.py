from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from trader.algos.declarative import (
    DeclarativeAlgo,
    _stop_and_risk,
    heuristic_confidence,
)
from trader.contracts.testing import FakeMarketData


ASOF = datetime(2026, 7, 3, 14, 30, tzinfo=timezone.utc)


def _daily_frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-07-01", periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "o": closes,
            "h": [close + 1.0 for close in closes],
            "l": [close - 1.0 for close in closes],
            "c": closes,
            "v": [1_000.0 for _ in closes],
        },
        index=index,
    )


def _frames() -> dict[str, pd.DataFrame]:
    return {
        "SNDK": _daily_frame([98.0, 100.0, 102.0]),
        "SNXX": _daily_frame([48.0, 50.0, 52.0]),
        "SNDQ": _daily_frame([51.0, 50.0, 48.0]),
    }


def _params(**overrides: object) -> dict:
    params = {
        "values": {"stop_frac": 0.5, "target_r": 2.0},
        "phases": ["open"],
        "one_shot": True,
        "trigger": None,
        "sides": [
            {
                "direction": "long",
                "when": {"source": "go_long", "operator": ">", "value": 0.0},
            }
        ],
        "bracket": {
            "entry": "price",
            "stop": {"formula": "atr_frac", "frac_param": "stop_frac"},
            "target": {"formula": "r_multiple", "r_param": "target_r"},
        },
        "entry_cutoff_minutes_before_close": 30.0,
        "rules_version": "test-v1",
        "gates": [],
    }
    params.update(overrides)
    return params


def _signals(**overrides: float) -> dict[str, float]:
    signals = {
        "minutes_since_open": 30.0,
        "minutes_to_close": 360.0,
        "go_long": 1.0,
        "price": 101.0,
        "atr_px": 2.0,
    }
    signals.update(overrides)
    return signals


@pytest.mark.parametrize(
    ("minutes_since_open", "minutes_to_close"),
    [(90.0, 300.0), (180.0, 210.0), (300.0, 90.0), (360.0, 30.0)],
    ids=["morning", "lunch", "late", "closed"],
)
def test_open_phase_definition_rejects_every_other_derived_phase(
    minutes_since_open: float, minutes_to_close: float
) -> None:
    algo = DeclarativeAlgo("phase-test", "emitting", _params())
    data = FakeMarketData(
        frames=_frames(),
        signals=_signals(
            minutes_since_open=minutes_since_open,
            minutes_to_close=minutes_to_close,
        ),
    )

    assert algo.on_bar(ASOF, data) == []


def test_open_phase_definition_fires_during_derived_open_phase() -> None:
    algo = DeclarativeAlgo("phase-test", "emitting", _params())
    data = FakeMarketData(frames=_frames(), signals=_signals())

    assert len(algo.on_bar(ASOF, data)) == 1


def test_phases_except_is_a_deny_list() -> None:
    params = _params(phases_except=["open"])
    params.pop("phases")

    denied = DeclarativeAlgo("deny-open", "emitting", params)
    open_data = FakeMarketData(frames=_frames(), signals=_signals())
    assert denied.on_bar(ASOF, open_data) == []

    allowed = DeclarativeAlgo("deny-open", "emitting", params)
    lunch_data = FakeMarketData(
        frames=_frames(),
        signals=_signals(minutes_since_open=180.0, minutes_to_close=210.0),
    )
    assert len(allowed.on_bar(ASOF, lunch_data)) == 1


@pytest.mark.parametrize(
    ("minute", "expected_count"),
    [(4.0, 0), (5.0, 1), (10.0, 1), (11.0, 0)],
)
def test_minute_window_bounds_are_inclusive(
    minute: float, expected_count: int
) -> None:
    params = _params(
        values={
            "stop_frac": 0.5,
            "target_r": 2.0,
            "start_minute": 5.0,
            "end_minute": 10.0,
        },
        minute_window={"min_param": "start_minute", "max_param": "end_minute"},
    )
    algo = DeclarativeAlgo("window-test", "emitting", params)
    data = FakeMarketData(
        frames=_frames(), signals=_signals(minutes_since_open=minute)
    )

    assert len(algo.on_bar(ASOF, data)) == expected_count


def test_one_shot_blocks_later_bars_and_warmup_resets_for_a_new_day() -> None:
    algo = DeclarativeAlgo("one-shot", "emitting", _params())
    data = FakeMarketData(frames=_frames(), signals=_signals())

    assert len(algo.on_bar(ASOF, data)) == 1
    assert algo.on_bar(ASOF + timedelta(minutes=1), data) == []

    algo.warmup(date(2026, 7, 4), data)

    assert len(algo.on_bar(ASOF + timedelta(minutes=2), data)) == 1


def test_false_trigger_blocks_sides_without_consuming_one_shot() -> None:
    params = _params(
        trigger={"source": "trigger", "operator": ">", "value": 0.0}
    )
    algo = DeclarativeAlgo("triggered", "emitting", params)
    data = FakeMarketData(frames={}, signals={**_signals(), "trigger": 0.0})

    assert algo.on_bar(ASOF, data) == []
    assert algo._done is False


@pytest.mark.parametrize(
    ("formula", "formula_params", "direction", "entry", "signals", "expected"),
    [
        (
            "structural_or",
            {"window_param": "or_minutes"},
            "long",
            100.0,
            {"or5_low": 95.0},
            (95.0, 5.0),
        ),
        (
            "structural_or",
            {"window_param": "or_minutes"},
            "short",
            100.0,
            {"or5_high": 107.0},
            (107.0, 7.0),
        ),
        (
            "atr_frac",
            {"frac_param": "stop_frac"},
            "long",
            100.0,
            {"atr_px": 8.0},
            (98.0, 2.0),
        ),
        (
            "atr_frac",
            {"frac_param": "stop_frac"},
            "short",
            100.0,
            {"atr_px": 8.0},
            (102.0, 2.0),
        ),
        (
            "extreme_offset",
            {"frac_param": "offset_frac"},
            "long",
            100.0,
            {"atr_px": 4.0, "day_low": 94.0},
            (92.0, 8.0),
        ),
        (
            "extreme_offset",
            {"frac_param": "offset_frac"},
            "short",
            100.0,
            {"atr_px": 4.0, "day_high": 105.0},
            (107.0, 7.0),
        ),
        (
            "vwap_offset",
            {"frac_param": "offset_frac"},
            "long",
            100.0,
            {"atr_day": 0.02, "price": 100.0, "vwap": 105.0},
            (104.0, -4.0),
        ),
        (
            "vwap_offset",
            {"frac_param": "offset_frac"},
            "short",
            100.0,
            {"atr_day": 0.02, "price": 100.0, "vwap": 105.0},
            (106.0, 6.0),
        ),
    ],
)
def test_stop_formulas_preserve_hand_computed_sndk_stop_and_signed_risk(
    formula: str,
    formula_params: dict,
    direction: str,
    entry: float,
    signals: dict[str, float],
    expected: tuple[float, float],
) -> None:
    values = {"or_minutes": 5.0, "stop_frac": 0.25, "offset_frac": 0.5}
    data = FakeMarketData(frames={}, signals=signals)

    result = _stop_and_risk(
        {"formula": formula, **formula_params},
        values,
        direction,
        entry,
        data,
        ASOF,
    )

    assert result == pytest.approx(expected)


def test_inverted_first_matching_side_is_skipped_for_the_next_side() -> None:
    params = _params(
        values={"offset_frac": 0.5, "target_r": 2.0},
        sides=[
            {
                "direction": "long",
                "when": {"source": "go_long", "operator": ">", "value": 0.0},
            },
            {
                "direction": "short",
                "when": {
                    "source": "go_short",
                    "operator": ">",
                    "value": 0.0,
                },
            },
        ],
        bracket={
            "entry": "entry_price",
            "stop": {"formula": "vwap_offset", "frac_param": "offset_frac"},
            "target": {"formula": "r_multiple", "r_param": "target_r"},
        },
    )
    algo = DeclarativeAlgo("inverted-first", "emitting", params)
    data = FakeMarketData(
        frames=_frames(),
        signals=_signals(
            go_short=1.0,
            entry_price=100.0,
            atr_day=0.02,
            price=100.0,
            vwap=105.0,
        ),
    )

    intents = algo.on_bar(ASOF, data)

    assert [intent.side for intent in intents] == ["short"]
    assert algo._done is True


def test_all_inverted_sides_return_nothing_without_consuming_one_shot() -> None:
    params = _params(
        values={"offset_frac": 0.5, "target_r": 2.0},
        bracket={
            "entry": "entry_price",
            "stop": {"formula": "vwap_offset", "frac_param": "offset_frac"},
            "target": {"formula": "r_multiple", "r_param": "target_r"},
        },
    )
    algo = DeclarativeAlgo("all-inverted", "emitting", params)
    data = FakeMarketData(
        frames={},
        signals=_signals(
            entry_price=100.0,
            atr_day=0.02,
            price=100.0,
            vwap=105.0,
        ),
    )

    assert algo.on_bar(ASOF, data) == []
    assert algo._done is False


def test_gate_block_consumes_one_shot_before_the_gate_verdict() -> None:
    params = _params(
        gates=[
            {
                "id": "entry-gate",
                "role": "gate",
                "source": "gate_signal",
                "operator": ">",
                "value": 0.0,
            }
        ]
    )
    algo = DeclarativeAlgo("gate-blocked", "emitting", params)
    data = FakeMarketData(
        frames=_frames(), signals={**_signals(), "gate_signal": 0.0}
    )

    assert algo.on_bar(ASOF, data) == []
    assert algo._done is True

    data._signals["gate_signal"] = 1.0

    assert algo.on_bar(ASOF + timedelta(minutes=1), data) == []


def test_successful_candidate_emits_translated_intent_with_rule_attribution() -> None:
    params = _params(
        gates=[
            {
                "id": "market-open",
                "role": "gate",
                "source": "gate_signal",
                "operator": ">",
                "value": 0.0,
            },
            {
                "id": "trend-long",
                "role": "direction",
                "direction": "long",
                "source": "trend",
                "operator": ">",
                "value": 0.0,
            },
            {
                "id": "liquid-tape",
                "role": "confirmation",
                "source": "liquidity",
                "operator": ">=",
                "value": 1.0,
            },
        ]
    )
    algo = DeclarativeAlgo("full-success", "emitting", params)
    data = FakeMarketData(
        frames=_frames(),
        signals={
            **_signals(),
            "gate_signal": 1.0,
            "trend": 1.0,
            "liquidity": 1.0,
        },
    )

    intents = algo.on_bar(ASOF, data)

    assert len(intents) == 1
    intent = intents[0]
    assert intent.algo_id == "full-success"
    assert intent.ts == ASOF
    assert intent.side == "long"
    assert intent.instrument == "SNXX"
    assert intent.signal_symbol == "SNDK"
    assert intent.action == "open"
    assert intent.entry == "market_next_open"
    assert intent.stop < intent.target
    assert intent.stop < 51.0 < intent.target
    assert intent.confidence == 0.65
    assert 0.0 <= intent.confidence <= 0.85
    assert intent.reason
    assert "full-success long candidate" in intent.reason
    assert "market-open" in intent.reason
    assert intent.meta == {
        "setup_id": "full-success",
        "rules_version": "test-v1",
        "rules_fired": ["market-open", "trend-long", "liquid-tape"],
        "direction_votes": ["trend-long"],
        "uncalibrated": True,
    }


@pytest.mark.parametrize(
    ("sign", "expected_side", "expected_instrument"),
    [(1.0, "long", "SNXX"), (0.0, "short", "SNDQ"), (-1.0, "short", "SNDQ")],
)
def test_sign_of_uses_long_for_positive_and_short_for_non_positive(
    sign: float, expected_side: str, expected_instrument: str
) -> None:
    params = _params(sides=[{"direction": {"sign_of": "bias"}}])
    algo = DeclarativeAlgo("signed", "emitting", params)
    data = FakeMarketData(frames=_frames(), signals={**_signals(), "bias": sign})

    intent = algo.on_bar(ASOF, data)[0]

    assert intent.side == expected_side
    assert intent.instrument == expected_instrument


def test_local_param_references_and_signal_templates_materialize_once() -> None:
    params = _params(
        values={
            "confirm_minutes": 15.0,
            "threshold": 2.0,
            "scale": 1.5,
            "stop_frac": 0.5,
            "target_r": 2.0,
        },
        trigger={
            "all": [
                {
                    "source": "or{confirm_minutes}_high",
                    "operator": ">",
                    "value_param": "-threshold",
                },
                {
                    "source": "lhs",
                    "operator": ">",
                    "value_signal": "or{confirm_minutes}_low",
                    "value_scale_param": "scale",
                },
            ]
        },
        sides=[
            {
                "direction": "long",
                "when": {
                    "source": "side_{confirm_minutes}",
                    "operator": ">",
                    "value": 0.0,
                },
            }
        ],
        bracket={
            "entry": "entry_{confirm_minutes}",
            "stop": {"formula": "atr_frac", "frac_param": "stop_frac"},
            "target": {"formula": "r_multiple", "r_param": "target_r"},
        },
    )
    algo = DeclarativeAlgo("materialized", "emitting", params)
    params["values"].update(
        {"confirm_minutes": 30.0, "threshold": 99.0, "scale": 99.0}
    )
    data = FakeMarketData(
        frames=_frames(),
        signals={
            **_signals(),
            "or15_high": -1.0,
            "lhs": 7.0,
            "or15_low": 4.0,
            "side_15": 1.0,
            "entry_15": 101.0,
        },
    )

    assert len(algo.on_bar(ASOF, data)) == 1


def test_confidence_formula_is_verbatim_and_capped() -> None:
    assert heuristic_confidence(0, 0) == pytest.approx(0.55)
    assert heuristic_confidence(1, 2) == pytest.approx(0.70)
    assert heuristic_confidence(10, 10) == pytest.approx(0.85)


def _invalid_case(name: str) -> dict:
    params = deepcopy(_params())
    if name == "unknown phase":
        params["phases"] = ["afterparty"]
    elif name == "both phase selectors":
        params["phases_except"] = ["closed"]
    elif name == "neither phase selector":
        params.pop("phases")
    elif name == "unknown minute parameter":
        params["minute_window"] = {"min_param": "missing"}
    elif name == "unknown stop formula":
        params["bracket"]["stop"]["formula"] = "magic"
    elif name == "unknown target formula":
        params["bracket"]["target"]["formula"] = "fixed_price"
    elif name == "invalid side direction":
        params["sides"][0]["direction"] = "sideways"
    else:
        raise AssertionError(f"unknown test case {name}")
    return params


@pytest.mark.parametrize(
    "case",
    [
        "unknown phase",
        "both phase selectors",
        "neither phase selector",
        "unknown minute parameter",
        "unknown stop formula",
        "unknown target formula",
        "invalid side direction",
    ],
)
def test_constructor_rejects_structural_definition_errors(case: str) -> None:
    with pytest.raises(ValueError, match="phase|minute|stop|target|direction"):
        DeclarativeAlgo("invalid", "emitting", _invalid_case(case))
