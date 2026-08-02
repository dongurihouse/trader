from datetime import datetime, timezone

import pytest

from trader.algos.rules import (
    OPERATORS,
    CandidateEval,
    RuleEval,
    RuleSet,
    check_clause,
    check_group,
)
from trader.contracts.testing import FakeMarketData


ASOF = datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc)


def test_candidate_eval_preserves_the_previous_constructor_signature() -> None:
    candidate = CandidateEval("algo", "long", True, False, [], 0, [])

    assert candidate.direction_votes == []
    assert candidate.n_confirmations == 0
    assert candidate.rules_fired == []
    assert candidate.vetoed_rule_id is None


@pytest.mark.parametrize(
    ("operator", "lhs", "rhs", "expected"),
    [
        (">", 2.0, 1.0, True),
        (">", 1.0, 2.0, False),
        ("<", 1.0, 2.0, True),
        ("<", 2.0, 1.0, False),
        (">=", 2.0, 2.0, True),
        (">=", 1.0, 2.0, False),
        ("<=", 2.0, 2.0, True),
        ("<=", 2.0, 1.0, False),
        ("==", 2.0, 2.0, True),
        ("==", 2.0, 1.0, False),
        ("abs>", -2.0, 1.0, True),
        ("abs>", -1.0, 2.0, False),
        ("abs<", -1.0, 2.0, True),
        ("abs<", -2.0, 1.0, False),
        ("abs>=", -2.0, 2.0, True),
        ("abs>=", -1.0, 2.0, False),
        ("abs<=", -2.0, 2.0, True),
        ("abs<=", -2.0, 1.0, False),
    ],
)
def test_each_operator_compares_the_source_value(
    operator: str, lhs: float, rhs: float, expected: bool
) -> None:
    data = FakeMarketData(frames={}, signals={"source": lhs})

    result = check_clause(
        {"source": "source", "operator": operator, "value": rhs}, data, ASOF
    )

    assert result is expected


def test_operators_public_api_has_the_complete_ordered_vocabulary() -> None:
    assert OPERATORS == (
        ">",
        "<",
        ">=",
        "<=",
        "==",
        "abs>",
        "abs<",
        "abs>=",
        "abs<=",
    )


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ({"source": "lhs", "operator": ">", "value_signal": "rhs"}, True),
        (
            {
                "source": "lhs",
                "operator": ">",
                "value_signal": "rhs",
                "value_scale": 2.0,
            },
            False,
        ),
    ],
)
def test_value_signal_is_read_at_the_same_asof_and_optionally_scaled(
    clause: dict, expected: bool
) -> None:
    data = FakeMarketData(frames={}, signals={"lhs": 3.0, "rhs": 2.0})

    assert check_clause(clause, data, ASOF) is expected


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        (
            {
                "all": [
                    {"source": "high", "operator": ">", "value": 2.0},
                    {"source": "low", "operator": "<", "value": 0.0},
                ]
            },
            False,
        ),
        (
            {
                "any": [
                    {"source": "high", "operator": ">", "value": 2.0},
                    {"source": "low", "operator": "<", "value": 0.0},
                ]
            },
            True,
        ),
    ],
)
def test_group_composition_uses_all_or_any(group: dict, expected: bool) -> None:
    data = FakeMarketData(frames={}, signals={"high": 3.0, "low": 1.0})

    assert check_group(group, data, ASOF) is expected


def test_all_group_evaluates_a_raising_clause_after_a_false_clause() -> None:
    data = FakeMarketData(frames={}, signals={"false": 0.0})
    group = {
        "all": [
            {"source": "false", "operator": ">", "value": 0.0},
            {"source": "missing", "operator": ">", "value": 0.0},
        ]
    }

    with pytest.raises(KeyError):
        check_group(group, data, ASOF)


def test_unknown_operator_fails_before_reading_a_signal() -> None:
    data = FakeMarketData(frames={}, signals={})

    with pytest.raises(ValueError, match="operator"):
        check_clause(
            {"source": "missing", "operator": "!=", "value": 1.0}, data, ASOF
        )


def test_relative_clause_requires_a_candidate_direction() -> None:
    data = FakeMarketData(frames={}, signals={"long_signal": 1.0})
    clause = {
        "source": "long_signal",
        "operator": ">",
        "value": 0.0,
        "relative_to": "candidate_direction",
        "mirror": "short_signal",
    }

    with pytest.raises(ValueError, match="direction"):
        check_clause(clause, data, ASOF)


def test_relative_clause_changes_only_the_source_signal() -> None:
    data = FakeMarketData(
        frames={},
        signals={"long_signal": 1.0, "short_signal": 4.0, "threshold": 3.0},
    )
    clause = {
        "source": "long_signal",
        "operator": ">",
        "value_signal": "threshold",
        "relative_to": "candidate_direction",
        "mirror": "short_signal",
    }

    assert check_clause(clause, data, ASOF, direction="long") is False
    assert check_clause(clause, data, ASOF, direction="short") is True


def test_gate_and_veto_roles_drive_whole_set_and_candidate_verdicts() -> None:
    rules = RuleSet(
        version="test",
        rules=[
            {
                "id": "failing-gate",
                "role": "gate",
                "source": "gate",
                "operator": ">",
                "value": 0.0,
            },
            {
                "id": "firing-veto",
                "role": "veto",
                "source": "veto",
                "operator": ">",
                "value": 0.0,
            },
        ],
    )
    data = FakeMarketData(frames={}, signals={"gate": 0.0, "veto": 1.0})

    evaluated = rules.evaluate(data, ASOF)

    assert isinstance(evaluated, RuleEval)
    assert evaluated.gates_pass is False
    assert evaluated.vetoed is True
    candidate = evaluated.for_candidate("algo-a", "long")
    assert candidate.gates_pass is False
    assert evaluated.for_candidate("algo-b", "short").gates_pass is False
    assert candidate.vetoed is True
    assert candidate.vetoed_rule_id == "firing-veto"


def test_no_gate_rules_default_to_passing() -> None:
    rules = RuleSet(
        version="test",
        rules=[
            {
                "id": "confirmation",
                "source": "signal",
                "operator": ">",
                "value": 0.0,
            },
            {
                "id": "inactive-veto",
                "role": "veto",
                "source": "veto",
                "operator": ">",
                "value": 0.0,
            },
        ],
    )
    data = FakeMarketData(frames={}, signals={"signal": 1.0, "veto": 0.0})

    evaluated = rules.evaluate(data, ASOF)
    candidate = evaluated.for_candidate("algo-a", "long")

    assert evaluated.gates_pass is True
    assert candidate.gates_pass is True
    assert candidate.vetoed is False
    assert candidate.vetoed_rule_id is None


def test_direction_rule_is_attributed_only_to_its_declared_candidate_side() -> None:
    rules = RuleSet(
        version="test",
        rules=[
            {
                "id": "go-long",
                "role": "direction",
                "direction": "long",
                "source": "trend",
                "operator": ">",
                "value": 0.0,
            }
        ],
    )
    data = FakeMarketData(frames={}, signals={"trend": 1.0})

    evaluated = rules.evaluate(data, ASOF)
    long_candidate = evaluated.for_candidate("algo", "long")
    short_candidate = evaluated.for_candidate("algo", "short")

    assert evaluated.direction_votes == {"long": ["go-long"], "short": []}
    assert long_candidate.direction_votes == ["go-long"]
    assert long_candidate.rules_fired == ["go-long"]
    assert short_candidate.direction_votes == []
    assert short_candidate.rules_fired == []


def test_confirmation_count_includes_explicit_and_default_roles_in_file_order() -> None:
    rules = RuleSet(
        version="test",
        rules=[
            {
                "id": "explicit",
                "role": "confirmation",
                "source": "first",
                "operator": ">",
                "value": 0.0,
            },
            {
                "id": "default",
                "source": "second",
                "operator": ">",
                "value": 0.0,
            },
            {
                "id": "miss",
                "source": "third",
                "operator": ">",
                "value": 0.0,
            },
        ],
    )
    data = FakeMarketData(
        frames={}, signals={"first": 1.0, "second": 1.0, "third": 0.0}
    )

    candidate = rules.evaluate(data, ASOF).for_candidate("algo", "long")

    assert isinstance(candidate, CandidateEval)
    assert candidate.n_confirmations == 2
    assert candidate.rules_fired == ["explicit", "default"]


def test_applies_to_scope_removes_every_role_from_other_algos() -> None:
    rules = RuleSet(
        version="test",
        known_algo_ids={"algo-a", "algo-b"},
        rules=[
            {
                "id": "scoped-gate",
                "role": "gate",
                "applies_to": ["algo-a"],
                "source": "false",
                "operator": ">",
                "value": 0.0,
            },
            {
                "id": "scoped-veto",
                "role": "veto",
                "applies_to": ["algo-a"],
                "source": "true",
                "operator": ">",
                "value": 0.0,
            },
            {
                "id": "scoped-direction",
                "role": "direction",
                "direction": "long",
                "applies_to": ["algo-a"],
                "source": "true",
                "operator": ">",
                "value": 0.0,
            },
            {
                "id": "scoped-confirmation",
                "applies_to": ["algo-a"],
                "source": "true",
                "operator": ">",
                "value": 0.0,
            },
        ],
    )
    data = FakeMarketData(frames={}, signals={"false": 0.0, "true": 1.0})

    evaluated = rules.evaluate(data, ASOF)
    included = evaluated.for_candidate("algo-a", "long")
    excluded = evaluated.for_candidate("algo-b", "long")

    assert evaluated.applies("scoped-gate", "algo-a") is True
    assert evaluated.applies("scoped-gate", "algo-b") is False
    assert included.gates_pass is False
    assert included.vetoed is True
    assert included.direction_votes == ["scoped-direction"]
    assert included.n_confirmations == 1
    assert included.rules_fired == [
        "scoped-veto",
        "scoped-direction",
        "scoped-confirmation",
    ]
    assert excluded.gates_pass is True
    assert excluded.vetoed is False
    assert excluded.direction_votes == []
    assert excluded.n_confirmations == 0
    assert excluded.rules_fired == []


@pytest.mark.parametrize("known_algo_ids", [{"algo-a", "algo-b"}, None])
def test_except_scope_excludes_named_algos_with_or_without_a_closed_roster(
    known_algo_ids: set[str] | None,
) -> None:
    rules = RuleSet(
        version="test",
        known_algo_ids=known_algo_ids,
        rules=[
            {
                "id": "not-for-b",
                "except": ["algo-b"],
                "source": "signal",
                "operator": ">",
                "value": 0.0,
            }
        ],
    )
    data = FakeMarketData(frames={}, signals={"signal": 1.0})

    evaluated = rules.evaluate(data, ASOF)

    assert evaluated.for_candidate("algo-a", "long").n_confirmations == 1
    assert evaluated.for_candidate("algo-b", "long").n_confirmations == 0


@pytest.mark.parametrize("scope_key", ["applies_to", "except"])
def test_unknown_scoped_algo_id_is_rejected(scope_key: str) -> None:
    rule = {
        "id": "scoped",
        scope_key: ["unknown"],
        "source": "signal",
        "operator": ">",
        "value": 0.0,
    }

    with pytest.raises(ValueError, match="unknown"):
        RuleSet(version="test", rules=[rule], known_algo_ids={"algo-a"})


@pytest.mark.parametrize("scope_key", ["applies_to", "except"])
def test_empty_scope_is_rejected(scope_key: str) -> None:
    rule = {
        "id": "empty-scope",
        scope_key: [],
        "source": "signal",
        "operator": ">",
        "value": 0.0,
    }

    with pytest.raises(ValueError, match="non-empty"):
        RuleSet(version="test", rules=[rule])


def test_direction_relative_rule_fires_differently_for_each_candidate_side() -> None:
    rules = RuleSet(
        version="test",
        rules=[
            {
                "id": "directional-confirmation",
                "source": "peer_above_x",
                "mirror": "peer_below_x",
                "relative_to": "candidate_direction",
                "operator": ">",
                "value": 2.0,
            }
        ],
    )
    data = FakeMarketData(
        frames={}, signals={"peer_above_x": 1.0, "peer_below_x": 4.0}
    )

    evaluated = rules.evaluate(data, ASOF)

    assert rules.directional is True
    assert evaluated.fired == {"directional-confirmation": False}
    assert evaluated.fired_in("long") == {"directional-confirmation": False}
    assert evaluated.fired_in("short") == {"directional-confirmation": True}
    assert evaluated.for_candidate("algo", "long").n_confirmations == 0
    assert evaluated.for_candidate("algo", "short").n_confirmations == 1


def test_non_directional_rules_share_one_fired_mapping_for_both_sides() -> None:
    rules = RuleSet(
        version="test",
        rules=[
            {
                "id": "plain",
                "source": "signal",
                "operator": ">",
                "value": 0.0,
            }
        ],
    )
    data = FakeMarketData(frames={}, signals={"signal": 1.0})

    evaluated = rules.evaluate(data, ASOF)

    assert rules.directional is False
    assert evaluated.fired_in("long") is evaluated.fired
    assert evaluated.fired_in("short") is evaluated.fired


def test_relative_clause_without_nonempty_mirror_is_rejected() -> None:
    rule = {
        "id": "directional",
        "source": "signal",
        "operator": ">",
        "value": 0.0,
        "relative_to": "candidate_direction",
    }

    with pytest.raises(ValueError, match="mirror"):
        RuleSet(version="test", rules=[rule])


def test_unknown_relative_frame_is_rejected() -> None:
    rule = {
        "id": "directional",
        "source": "signal",
        "mirror": "other",
        "operator": ">",
        "value": 0.0,
        "relative_to": "position_direction",
    }

    with pytest.raises(ValueError, match="candidate_direction"):
        RuleSet(version="test", rules=[rule])


def test_unknown_nested_operator_is_rejected_at_construction() -> None:
    rule = {
        "id": "bad-operator",
        "all": [
            {"source": "signal", "operator": "!=", "value": 0.0},
        ],
    }

    with pytest.raises(ValueError, match="operator"):
        RuleSet(version="test", rules=[rule])


@pytest.mark.parametrize(
    "clause_fields",
    [
        {},
        {"value": 1.0, "value_signal": "threshold"},
    ],
)
def test_clause_requires_exactly_one_value_form(clause_fields: dict) -> None:
    rule = {
        "id": "bad-value",
        "source": "signal",
        "operator": ">",
        **clause_fields,
    }

    with pytest.raises(ValueError, match="exactly one"):
        RuleSet(version="test", rules=[rule])


def test_value_scale_requires_value_signal() -> None:
    rule = {
        "id": "bad-scale",
        "source": "signal",
        "operator": ">",
        "value": 1.0,
        "value_scale": 2.0,
    }

    with pytest.raises(ValueError, match="value_scale"):
        RuleSet(version="test", rules=[rule])


def test_duplicate_rule_id_is_rejected() -> None:
    rules = [
        {"id": "duplicate", "source": "one", "operator": ">", "value": 0.0},
        {"id": "duplicate", "source": "two", "operator": ">", "value": 0.0},
    ]

    with pytest.raises(ValueError, match="duplicate"):
        RuleSet(version="test", rules=rules)


@pytest.mark.parametrize(
    "direction_fields",
    [{}, {"direction": "up"}],
)
def test_direction_role_requires_long_or_short(direction_fields: dict) -> None:
    rule = {
        "id": "direction",
        "role": "direction",
        "source": "signal",
        "operator": ">",
        "value": 0.0,
        **direction_fields,
    }

    with pytest.raises(ValueError, match="direction"):
        RuleSet(version="test", rules=[rule])


def test_rule_cannot_have_allow_and_deny_scopes() -> None:
    rule = {
        "id": "bad-scope",
        "applies_to": ["algo-a"],
        "except": ["algo-b"],
        "source": "signal",
        "operator": ">",
        "value": 0.0,
    }

    with pytest.raises(ValueError, match="applies_to.*except|except.*applies_to"):
        RuleSet(
            version="test",
            rules=[rule],
            known_algo_ids={"algo-a", "algo-b"},
        )


def test_rule_text_is_inert_documentation() -> None:
    rules = RuleSet(
        version="test",
        rules=[
            {
                "id": "documented",
                "text": "No interpolation of {value} or {shared_constant}.",
                "source": "signal",
                "operator": ">",
                "value": 0.0,
            }
        ],
    )
    data = FakeMarketData(frames={}, signals={"signal": 1.0})

    assert rules.evaluate(data, ASOF).fired == {"documented": True}
