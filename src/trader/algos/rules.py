"""Pure evaluator for the trader rule grammar."""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from trader.contracts import MarketData


OPERATORS: tuple[str, ...] = (
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

_OPERATOR_FUNCTIONS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "abs>": lambda lhs, rhs: abs(lhs) > rhs,
    "abs<": lambda lhs, rhs: abs(lhs) < rhs,
    "abs>=": lambda lhs, rhs: abs(lhs) >= rhs,
    "abs<=": lambda lhs, rhs: abs(lhs) <= rhs,
}


def check_clause(
    clause: dict,
    data: MarketData,
    asof: datetime,
    direction: str | None = None,
) -> bool:
    """Evaluate one source/operator/value clause against live signal values."""
    operator_name = clause["operator"]
    if operator_name not in OPERATORS:
        raise ValueError(f"unknown rule operator {operator_name!r}")

    source = clause["source"]
    if "relative_to" in clause:
        if direction not in ("long", "short"):
            raise ValueError(
                "a clause relative to candidate_direction requires direction "
                "to be 'long' or 'short'"
            )
        if direction == "short":
            source = clause["mirror"]

    lhs = data.signal(source, asof=asof)
    if "value_signal" in clause:
        rhs = data.signal(clause["value_signal"], asof=asof)
        if "value_scale" in clause:
            rhs = clause["value_scale"] * rhs
    else:
        rhs = clause["value"]

    return _OPERATOR_FUNCTIONS[operator_name](lhs, rhs)


def check_group(
    group: dict,
    data: MarketData,
    asof: datetime,
    direction: str | None = None,
) -> bool:
    """Evaluate an all/any group or a bare clause."""
    if "all" in group:
        return all(
            [
                check_clause(clause, data, asof, direction)
                for clause in group["all"]
            ]
        )
    if "any" in group:
        return any(
            [
                check_clause(clause, data, asof, direction)
                for clause in group["any"]
            ]
        )
    return check_clause(group, data, asof, direction)


def _clauses(rule: dict) -> list[dict]:
    for key in ("all", "any"):
        if key in rule:
            return list(rule[key])
    return [rule]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_clause(clause: dict) -> None:
    source = clause.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError(f"clause source must be a non-empty string: {clause!r}")

    operator_name = clause.get("operator")
    if operator_name not in OPERATORS:
        raise ValueError(f"unknown rule operator {operator_name!r}: {clause!r}")

    if "value_ref" in clause:
        raise ValueError("value_ref is not supported; use value or value_signal")
    value_keys = [key for key in ("value", "value_signal") if key in clause]
    if len(value_keys) != 1:
        raise ValueError(
            "clause must contain exactly one of 'value' or 'value_signal': "
            f"{clause!r}"
        )

    if "value" in clause and not _is_number(clause["value"]):
        raise ValueError(f"clause value must be a number: {clause!r}")
    if "value_signal" in clause:
        value_signal = clause["value_signal"]
        if not isinstance(value_signal, str) or not value_signal:
            raise ValueError(
                f"clause value_signal must be a non-empty string: {clause!r}"
            )
    if "value_scale" in clause:
        if "value_signal" not in clause:
            raise ValueError("value_scale is only allowed with value_signal")
        if not _is_number(clause["value_scale"]):
            raise ValueError(f"clause value_scale must be a number: {clause!r}")

    if "relative_to" in clause:
        if clause["relative_to"] != "candidate_direction":
            raise ValueError(
                "clause relative_to must be exactly 'candidate_direction': "
                f"{clause!r}"
            )
        mirror = clause.get("mirror")
        if not isinstance(mirror, str) or not mirror:
            raise ValueError(
                "a candidate_direction clause requires a non-empty mirror signal"
            )


def _copy_and_validate_rule(rule: dict) -> dict:
    group_keys = [key for key in ("all", "any") if key in rule]
    if len(group_keys) > 1:
        raise ValueError("a rule cannot contain both 'all' and 'any'")

    copied = dict(rule)
    if group_keys:
        group_key = group_keys[0]
        clauses = rule[group_key]
        if not isinstance(clauses, list) or not all(
            isinstance(clause, dict) for clause in clauses
        ):
            raise ValueError(f"rule {group_key} must be a list of clauses")
        copied[group_key] = [dict(clause) for clause in clauses]

    for clause in _clauses(copied):
        _validate_clause(clause)
    return copied


@dataclass(frozen=True)
class _ExcludedScope:
    algo_ids: frozenset[str]


_Scope = frozenset[str] | _ExcludedScope | None


def _scope_of(
    rule: dict, known_algo_ids: set[str] | None
) -> _Scope:
    scope_keys = [key for key in ("applies_to", "except") if key in rule]
    if len(scope_keys) > 1:
        raise ValueError(
            f"rule {rule.get('id')!r} cannot contain both applies_to and except"
        )
    if not scope_keys:
        return None

    key = scope_keys[0]
    names = rule[key]
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) for name in names)
    ):
        raise ValueError(
            f"rule {rule.get('id')!r}: {key} must be a non-empty list of strings"
        )

    named_ids = frozenset(names)
    if known_algo_ids is not None:
        unknown_ids = sorted(named_ids - known_algo_ids)
        if unknown_ids:
            raise ValueError(
                f"rule {rule.get('id')!r}: {key} names unknown algo ids "
                f"{unknown_ids}"
            )
        if key == "except":
            return frozenset(known_algo_ids) - named_ids
    if key == "applies_to":
        return named_ids
    return _ExcludedScope(named_ids)


@dataclass
class CandidateEval:
    algo_id: str
    direction: str
    gates_pass: bool
    vetoed: bool
    direction_votes: list[str]
    n_confirmations: int
    rules_fired: list[str]
    vetoed_rule_id: str | None = None


@dataclass
class RuleEval:
    fired: dict[str, bool]
    gates_pass: bool
    vetoed: bool
    direction_votes: dict[str, list[str]]
    _rules: list[dict] = field(default_factory=list, repr=False)
    _scopes: dict[str, _Scope] = field(default_factory=dict, repr=False)
    _by_direction: dict[str, dict[str, bool]] = field(
        default_factory=dict, repr=False
    )

    def applies(self, rule_id: str, algo_id: str) -> bool:
        """Return whether a rule speaks for an algo's candidates."""
        scope = self._scopes[rule_id]
        if scope is None:
            return True
        if isinstance(scope, _ExcludedScope):
            return algo_id not in scope.algo_ids
        return algo_id in scope

    def fired_in(self, direction: str) -> dict[str, bool]:
        """Return rule hits evaluated in one candidate direction's sense."""
        return self._by_direction[direction]

    def for_candidate(self, algo_id: str, direction: str) -> CandidateEval:
        """Build the scoped and direction-specific evaluation for a candidate."""
        fired = self.fired_in(direction)
        gates: list[bool] = []
        vetoed = False
        vetoed_rule_id: str | None = None
        direction_votes: list[str] = []
        n_confirmations = 0
        rules_fired: list[str] = []

        for rule in self._rules:
            rule_id = rule["id"]
            if not self.applies(rule_id, algo_id):
                continue

            hit = fired[rule_id]
            role = rule.get("role", "confirmation")
            if role == "gate":
                gates.append(hit)
            elif role == "veto":
                vetoed = vetoed or hit
                if hit and vetoed_rule_id is None:
                    vetoed_rule_id = rule_id
            elif role == "direction":
                if hit and rule["direction"] != direction:
                    continue
                if hit:
                    direction_votes.append(rule_id)
            elif role == "confirmation" and hit:
                n_confirmations += 1

            if hit:
                rules_fired.append(rule_id)

        return CandidateEval(
            algo_id=algo_id,
            direction=direction,
            gates_pass=all(gates) if gates else True,
            vetoed=vetoed,
            vetoed_rule_id=vetoed_rule_id,
            direction_votes=direction_votes,
            n_confirmations=n_confirmations,
            rules_fired=rules_fired,
        )


@dataclass
class RuleSet:
    version: str
    rules: list[dict]
    known_algo_ids: set[str] | None = None
    directional: bool = field(init=False)
    _scopes: dict[str, _Scope] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        resolved_rules: list[dict] = []
        scopes: dict[str, _Scope] = {}
        seen_ids: set[str] = set()

        for raw_rule in self.rules:
            if not isinstance(raw_rule, dict):
                raise ValueError(f"each rule must be a dict, got {raw_rule!r}")
            rule_id = raw_rule.get("id")
            if not isinstance(rule_id, str) or not rule_id:
                raise ValueError(f"each rule needs a non-empty string id: {raw_rule!r}")
            if rule_id in seen_ids:
                raise ValueError(f"duplicate rule id {rule_id!r}")
            seen_ids.add(rule_id)

            rule = _copy_and_validate_rule(raw_rule)
            if rule.get("role", "confirmation") == "direction" and rule.get(
                "direction"
            ) not in ("long", "short"):
                raise ValueError(
                    f"direction rule {rule_id!r} must declare direction "
                    "'long' or 'short'"
                )

            resolved_rules.append(rule)
            scopes[rule_id] = _scope_of(rule, self.known_algo_ids)

        self.rules = resolved_rules
        self._scopes = scopes
        self.directional = any(
            "relative_to" in clause
            for rule in self.rules
            for clause in _clauses(rule)
        )

    def evaluate(self, data: MarketData, asof: datetime) -> RuleEval:
        """Evaluate the full rule set and prepare candidate-specific views."""
        fired: dict[str, bool] = {}
        gates: list[bool] = []
        vetoed = False
        direction_votes: dict[str, list[str]] = {"long": [], "short": []}

        for rule in self.rules:
            rule_id = rule["id"]
            hit = check_group(rule, data, asof, direction="long")
            fired[rule_id] = hit
            role = rule.get("role", "confirmation")
            if role == "gate":
                gates.append(hit)
            elif role == "veto" and hit:
                vetoed = True
            elif role == "direction" and hit:
                direction_votes[rule["direction"]].append(rule_id)

        by_direction = {
            "long": fired,
            "short": (
                {
                    rule["id"]: check_group(
                        rule, data, asof, direction="short"
                    )
                    for rule in self.rules
                }
                if self.directional
                else fired
            ),
        }
        return RuleEval(
            fired=fired,
            gates_pass=all(gates) if gates else True,
            vetoed=vetoed,
            direction_votes=direction_votes,
            _rules=self.rules,
            _scopes=self._scopes,
            _by_direction=by_direction,
        )
