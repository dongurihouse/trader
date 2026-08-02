"""Run a validated, data-described setup through the shared Algo protocol."""

from __future__ import annotations

import re
from datetime import date, datetime

from trader.algos.bracket import UNDERLYING, translate
from trader.algos.rules import RuleSet, check_group
from trader.contracts import AlgoStatus, Intent, MarketData, Side


PHASES = ("closed", "open", "morning", "lunch", "late")
STOP_FORMULAS = ("structural_or", "atr_frac", "extreme_offset", "vwap_offset")
TARGET_FORMULAS = ("r_multiple",)

_STATUSES = ("emitting", "probe", "disabled")
_NAME_REF = re.compile(r"\{([a-z0-9_]+)\}")
_REQUIRED_PARAM_KEYS = {
    "values",
    "one_shot",
    "trigger",
    "sides",
    "bracket",
    "entry_cutoff_minutes_before_close",
    "rules_version",
    "gates",
}
_OPTIONAL_PARAM_KEYS = {
    "phases",
    "phases_except",
    "minute_window",
    "known_algo_ids",
}
_CLAUSE_KEYS = {
    "source",
    "operator",
    "value",
    "value_signal",
    "value_scale",
    "value_param",
    "value_scale_param",
    "relative_to",
    "mirror",
}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def phase_at(
    data: MarketData,
    asof: datetime,
    entry_cutoff_minutes: float,
) -> str:
    """Return the session phase derived from point-in-time minute signals."""
    m = data.signal("minutes_since_open", asof=asof)
    remaining = data.signal("minutes_to_close", asof=asof)
    total = m + remaining
    if m < 0 or m >= total - entry_cutoff_minutes:
        return "closed"
    if m < 60:
        return "open"
    if m < 120:
        return "morning"
    late_start = max(120, total - 120)
    return "late" if m >= late_start else "lunch"


def heuristic_confidence(n_direction_votes: int, n_confirmations: int) -> float:
    """Return dt's deliberately uncalibrated placeholder confidence."""
    return min(
        0.85,
        0.55 + 0.05 * n_direction_votes + 0.05 * n_confirmations,
    )


def _window_name(value: object) -> str:
    _require(_is_number(value), f"signal-name parameter must be numeric, got {value!r}")
    whole = int(value)
    _require(
        whole == value,
        f"signal-name parameter must be a whole number, got {value!r}",
    )
    return str(whole)


def _render_name(template: object, values: dict) -> str:
    _require(
        isinstance(template, str) and bool(template),
        f"signal name must be a non-empty string, got {template!r}",
    )

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        _require(
            name in values,
            f"signal name {template!r} references unknown values key {name!r}",
        )
        return _window_name(values[name])

    rendered = _NAME_REF.sub(replace, template)
    _require(
        "{" not in rendered and "}" not in rendered,
        f"signal name {template!r} contains an invalid parameter placeholder",
    )
    return rendered


def _param_ref(reference: object, values: dict) -> float:
    _require(
        isinstance(reference, str) and bool(reference.strip()),
        f"parameter reference must be a non-empty string, got {reference!r}",
    )
    name = reference.strip()
    if name.startswith("-"):
        return -_param_ref(name[1:], values)
    _require(name in values, f"clause references unknown values key {name!r}")
    return float(values[name])


def _materialize_clause(clause: object, values: dict) -> dict:
    _require(isinstance(clause, dict), f"clause must be a dict, got {clause!r}")
    unknown = set(clause) - _CLAUSE_KEYS
    _require(not unknown, f"clause has unknown keys {sorted(unknown)}: {clause!r}")

    forms = [
        key for key in ("value", "value_signal", "value_param") if key in clause
    ]
    _require(
        len(forms) == 1,
        "clause must contain exactly one of 'value', 'value_signal', or "
        f"'value_param': {clause!r}",
    )
    scale_forms = [
        key for key in ("value_scale", "value_scale_param") if key in clause
    ]
    _require(
        len(scale_forms) <= 1,
        f"clause cannot contain both value_scale forms: {clause!r}",
    )
    if scale_forms:
        _require(
            "value_signal" in clause,
            f"clause value_scale requires value_signal: {clause!r}",
        )

    materialized = {
        key: value
        for key, value in clause.items()
        if key not in ("value_param", "value_scale_param")
    }
    materialized["source"] = _render_name(clause.get("source"), values)
    if "value_signal" in clause:
        materialized["value_signal"] = _render_name(
            clause["value_signal"], values
        )
    if "value_param" in clause:
        materialized["value"] = _param_ref(clause["value_param"], values)
    if "value_scale_param" in clause:
        materialized["value_scale"] = _param_ref(
            clause["value_scale_param"], values
        )
    return materialized


def _materialize_group(
    group: object,
    values: dict,
    *,
    context: str,
) -> dict | None:
    if group is None:
        return None
    _require(isinstance(group, dict), f"{context} must be a dict or None")
    group_keys = [key for key in ("all", "any") if key in group]
    _require(len(group_keys) <= 1, f"{context} cannot contain both all and any")
    if group_keys:
        key = group_keys[0]
        _require(set(group) == {key}, f"{context} group has unknown keys")
        clauses = group[key]
        _require(isinstance(clauses, list), f"{context} {key} must be a list")
        materialized = {
            key: [_materialize_clause(clause, values) for clause in clauses]
        }
    else:
        materialized = _materialize_clause(group, values)

    validation_rule = dict(materialized)
    validation_rule["id"] = f"__{context.replace(' ', '_')}__"
    try:
        RuleSet(version="local-validation", rules=[validation_rule])
    except ValueError as exc:
        raise ValueError(f"invalid {context}: {exc}") from exc
    return materialized


def _groups(params: dict) -> list[dict | None]:
    groups: list[dict | None] = [params["trigger"]]
    groups.extend(side.get("when") for side in params["sides"])
    return groups


def _clauses(group: dict | None) -> list[dict]:
    if group is None:
        return []
    for key in ("all", "any"):
        if key in group:
            return list(group[key])
    return [group]


def _params_used(params: dict) -> set[str]:
    used = set((params.get("minute_window") or {}).values())
    for group in _groups(params):
        for clause in _clauses(group):
            for key in ("value_param", "value_scale_param"):
                if key in clause and isinstance(clause[key], str):
                    used.add(clause[key].lstrip("-").strip())
            for key in ("source", "value_signal"):
                if key in clause and isinstance(clause[key], str):
                    used.update(_NAME_REF.findall(clause[key]))
    bracket = params["bracket"]
    if isinstance(bracket, dict) and isinstance(bracket.get("entry"), str):
        used.update(_NAME_REF.findall(bracket["entry"]))
    stop = bracket.get("stop", {}) if isinstance(bracket, dict) else {}
    target = bracket.get("target", {}) if isinstance(bracket, dict) else {}
    for key in ("window_param", "frac_param"):
        if key in stop:
            used.add(stop[key])
    if "r_param" in target:
        used.add(target["r_param"])
    return used


def _structural_risk(entry: float, stop: float, direction: str) -> float:
    if direction == "long":
        return entry - stop
    if direction == "short":
        return stop - entry
    raise ValueError(f"unknown direction {direction!r}")


def _stop_and_risk(
    spec: dict,
    values: dict,
    direction: str,
    entry_sndk: float,
    data: MarketData,
    asof: datetime,
) -> tuple[float, float]:
    """Compute one named SNDK-space stop and its signed risk distance."""
    formula = spec["formula"]
    if formula == "structural_or":
        window = _window_name(values[spec["window_param"]])
        signal = f"or{window}_{'low' if direction == 'long' else 'high'}"
        level = data.signal(signal, asof=asof)
        return level, _structural_risk(entry_sndk, level, direction)
    if formula == "atr_frac":
        dist = data.signal("atr_px", asof=asof) * values[spec["frac_param"]]
        stop = entry_sndk - dist if direction == "long" else entry_sndk + dist
        return stop, dist
    if formula == "extreme_offset":
        offset = values[spec["frac_param"]] * data.signal("atr_px", asof=asof)
        if direction == "long":
            stop = data.signal("day_low", asof=asof) - offset
        else:
            stop = data.signal("day_high", asof=asof) + offset
        return stop, _structural_risk(entry_sndk, stop, direction)
    if formula == "vwap_offset":
        dist = (
            values[spec["frac_param"]]
            * data.signal("atr_day", asof=asof)
            * data.signal("price", asof=asof)
        )
        vwap = data.signal("vwap", asof=asof)
        stop = vwap - dist if direction == "long" else vwap + dist
        return stop, _structural_risk(entry_sndk, stop, direction)
    raise ValueError(f"unknown stop formula {formula!r}")


def _target(
    spec: dict,
    values: dict,
    direction: str,
    entry_sndk: float,
    risk: float,
) -> float:
    if spec["formula"] != "r_multiple":
        raise ValueError(f"unknown target formula {spec['formula']!r}")
    multiple = values[spec["r_param"]]
    if direction == "long":
        return entry_sndk + multiple * risk
    return entry_sndk - multiple * risk


class DeclarativeAlgo:
    """Execute one already-parsed declarative setup as an ``Algo``."""

    def __init__(self, id: str, status: AlgoStatus, params: dict):
        _require(isinstance(id, str) and bool(id), "algo id must be a non-empty string")
        _require(status in _STATUSES, f"{id}: unknown algo status {status!r}")
        _require(isinstance(params, dict), f"{id}: params must be a dict")

        missing = _REQUIRED_PARAM_KEYS - set(params)
        _require(not missing, f"{id}: missing required params keys {sorted(missing)}")
        unknown = set(params) - _REQUIRED_PARAM_KEYS - _OPTIONAL_PARAM_KEYS
        _require(not unknown, f"{id}: unknown params keys {sorted(unknown)}")

        phase_keys = [key for key in ("phases", "phases_except") if key in params]
        _require(
            len(phase_keys) == 1,
            f"{id}: state exactly one of phases / phases_except",
        )
        selected_phases = params[phase_keys[0]]
        _require(
            isinstance(selected_phases, list)
            and all(isinstance(phase, str) for phase in selected_phases),
            f"{id}: {phase_keys[0]} must be a list of phase names",
        )
        for phase in selected_phases:
            _require(phase in PHASES, f"{id}: unknown phase {phase!r}")

        raw_values = params["values"]
        _require(isinstance(raw_values, dict), f"{id}: values must be a dict")
        _require(
            all(isinstance(name, str) and bool(name) for name in raw_values),
            f"{id}: values keys must be non-empty strings",
        )
        for name, value in raw_values.items():
            _require(
                _is_number(value),
                f"{id}: values parameter {name!r} must be numeric, got {value!r}",
            )
        values = dict(raw_values)

        _require(
            isinstance(params["one_shot"], bool),
            f"{id}: one_shot must be a bool",
        )
        cutoff = params["entry_cutoff_minutes_before_close"]
        _require(
            _is_number(cutoff),
            f"{id}: entry_cutoff_minutes_before_close must be numeric",
        )
        rules_version = params["rules_version"]
        _require(
            isinstance(rules_version, str) and bool(rules_version),
            f"{id}: rules_version must be a non-empty string",
        )

        minute_window = params.get("minute_window") or {}
        _require(isinstance(minute_window, dict), f"{id}: minute_window must be a dict")
        unknown_window_keys = set(minute_window) - {"min_param", "max_param"}
        _require(
            not unknown_window_keys,
            f"{id}: minute_window keys must be min_param/max_param, got "
            f"{sorted(unknown_window_keys)}",
        )
        for key, parameter_name in minute_window.items():
            _require(
                isinstance(parameter_name, str) and parameter_name in values,
                f"{id}: minute_window {key} names unknown values parameter "
                f"{parameter_name!r}",
            )

        raw_sides = params["sides"]
        _require(
            isinstance(raw_sides, list) and bool(raw_sides),
            f"{id}: sides must be a non-empty list",
        )
        sides: list[tuple[Side | dict[str, str], dict | None]] = []
        for index, raw_side in enumerate(raw_sides):
            _require(isinstance(raw_side, dict), f"{id}: side {index} must be a dict")
            unknown_side_keys = set(raw_side) - {"direction", "when"}
            _require(
                not unknown_side_keys and "direction" in raw_side,
                f"{id}: side {index} needs direction and optional when",
            )
            direction = raw_side["direction"]
            if isinstance(direction, dict):
                _require(
                    set(direction) == {"sign_of"}
                    and isinstance(direction["sign_of"], str)
                    and bool(direction["sign_of"]),
                    f"{id}: side direction mapping must be {{'sign_of': '<signal>'}}",
                )
                _require(
                    raw_side.get("when") is None,
                    f"{id}: a sign_of side must omit when",
                )
                resolved_direction: Side | dict[str, str] = dict(direction)
            else:
                _require(
                    direction in ("long", "short"),
                    f"{id}: side direction must be long/short/sign_of, got "
                    f"{direction!r}",
                )
                resolved_direction = direction
            when = _materialize_group(
                raw_side.get("when"), values, context=f"side {index} when"
            )
            sides.append((resolved_direction, when))

        bracket = params["bracket"]
        _require(isinstance(bracket, dict), f"{id}: bracket must be a dict")
        _require(
            set(bracket) == {"entry", "stop", "target"},
            f"{id}: bracket needs exactly entry/stop/target",
        )
        entry_name = _render_name(bracket["entry"], values)

        stop = bracket["stop"]
        _require(isinstance(stop, dict), f"{id}: bracket stop must be a dict")
        stop_formula = stop.get("formula")
        _require(
            stop_formula in STOP_FORMULAS,
            f"{id}: unknown stop formula {stop_formula!r}",
        )
        stop_parameter_key = (
            "window_param" if stop_formula == "structural_or" else "frac_param"
        )
        _require(
            set(stop) == {"formula", stop_parameter_key},
            f"{id}: stop formula {stop_formula!r} needs exactly "
            f"{stop_parameter_key!r}",
        )
        stop_parameter = stop[stop_parameter_key]
        _require(
            isinstance(stop_parameter, str) and stop_parameter in values,
            f"{id}: stop {stop_parameter_key} names unknown values parameter "
            f"{stop_parameter!r}",
        )
        if stop_formula == "structural_or":
            _window_name(values[stop_parameter])

        target = bracket["target"]
        _require(isinstance(target, dict), f"{id}: bracket target must be a dict")
        target_formula = target.get("formula")
        _require(
            target_formula in TARGET_FORMULAS,
            f"{id}: target formula must be 'r_multiple', got {target_formula!r}",
        )
        _require(
            set(target) == {"formula", "r_param"},
            f"{id}: r_multiple target needs exactly r_param",
        )
        r_parameter = target["r_param"]
        _require(
            isinstance(r_parameter, str) and r_parameter in values,
            f"{id}: target r_param names unknown values parameter {r_parameter!r}",
        )

        trigger = _materialize_group(params["trigger"], values, context="trigger")

        used = _params_used(params)
        unused = set(values) - used
        _require(
            not unused,
            f"{id}: values parameters are unused structurally: {sorted(unused)}",
        )

        gates = params["gates"]
        _require(isinstance(gates, list), f"{id}: gates must be a list")
        known_algo_ids = params.get("known_algo_ids")
        if known_algo_ids is not None:
            _require(
                isinstance(known_algo_ids, list)
                and all(isinstance(name, str) for name in known_algo_ids),
                f"{id}: known_algo_ids must be a list of strings",
            )
            known_algo_ids = set(known_algo_ids)

        self._id = id
        self.status = status
        self.values = values
        self.one_shot = params["one_shot"]
        self.entry_cutoff_minutes_before_close = float(cutoff)
        self._phases = tuple(params["phases"]) if "phases" in params else None
        self._phases_except = (
            tuple(params["phases_except"])
            if "phases_except" in params
            else None
        )
        self._min_minutes = (
            values[minute_window["min_param"]]
            if "min_param" in minute_window
            else None
        )
        self._max_minutes = (
            values[minute_window["max_param"]]
            if "max_param" in minute_window
            else None
        )
        self._trigger = trigger
        self._sides = sides
        self._entry_name = entry_name
        self._stop = dict(stop)
        self._target_spec = dict(target)
        self._rules_version = rules_version
        self._ruleset = RuleSet(
            version=rules_version,
            rules=gates,
            known_algo_ids=known_algo_ids,
        )
        self._done = False

    @property
    def id(self) -> str:
        return self._id

    def warmup(self, day: date, data: MarketData) -> None:
        """Reset the instance's daily one-shot state."""
        self._done = False

    def _phase_allowed(self, phase: str) -> bool:
        if self._phases is not None:
            return phase in self._phases
        return phase not in self._phases_except

    def on_bar(self, asof: datetime, data: MarketData) -> list[Intent]:
        if self._done and self.one_shot:
            return []

        phase = phase_at(
            data,
            asof,
            self.entry_cutoff_minutes_before_close,
        )
        if not self._phase_allowed(phase):
            return []

        if self._min_minutes is not None or self._max_minutes is not None:
            minutes = data.signal("minutes_since_open", asof=asof)
            if self._min_minutes is not None and minutes < self._min_minutes:
                return []
            if self._max_minutes is not None and minutes > self._max_minutes:
                return []

        if self._trigger is not None and not check_group(self._trigger, data, asof):
            return []

        for direction_spec, when in self._sides:
            if when is not None and not check_group(when, data, asof):
                continue
            if isinstance(direction_spec, str):
                direction: Side = direction_spec
            else:
                direction = (
                    "long"
                    if data.signal(direction_spec["sign_of"], asof=asof) > 0
                    else "short"
                )

            entry_sndk = data.signal(self._entry_name, asof=asof)
            stop_sndk, risk = _stop_and_risk(
                self._stop,
                self.values,
                direction,
                entry_sndk,
                data,
                asof,
            )
            if risk <= 0:
                continue

            target_sndk = _target(
                self._target_spec,
                self.values,
                direction,
                entry_sndk,
                risk,
            )

            self._done = True
            rule_eval = self._ruleset.evaluate(data, asof)
            candidate_eval = rule_eval.for_candidate(self.id, direction)

            translated = translate(
                direction,
                entry_sndk,
                stop_sndk,
                target_sndk,
                UNDERLYING,
                data,
                asof,
            )
            if translated is None:
                return []
            instrument, _translated_entry, stop, target = translated
            confidence = round(
                heuristic_confidence(
                    len(candidate_eval.direction_votes),
                    candidate_eval.n_confirmations,
                ),
                2,
            )
            fired_text = ", ".join(sorted(candidate_eval.rules_fired)) or "none"
            reason = (
                f"{self.id} {direction} candidate fired; named rules fired: "
                f"{fired_text}; confirmations: "
                f"{candidate_eval.n_confirmations}."
            )
            meta = {
                "setup_id": self.id,
                "rules_version": self._rules_version,
                "rules_fired": list(candidate_eval.rules_fired),
                "direction_votes": list(candidate_eval.direction_votes),
                "gates_pass": (
                    candidate_eval.gates_pass and not candidate_eval.vetoed
                ),
                "uncalibrated": True,
            }
            if candidate_eval.vetoed_rule_id is not None:
                meta["vetoed"] = candidate_eval.vetoed_rule_id
            return [
                Intent(
                    algo_id=self.id,
                    ts=asof,
                    action="open",
                    side=direction,
                    signal_symbol=UNDERLYING,
                    instrument=instrument,
                    entry="market_next_open",
                    stop=stop,
                    target=target,
                    confidence=confidence,
                    reason=reason,
                    meta=meta,
                )
            ]
        return []


__all__ = ["DeclarativeAlgo", "PHASES", "heuristic_confidence", "phase_at"]
