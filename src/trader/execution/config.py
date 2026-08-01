"""Typed execution configuration loading."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
from pathlib import Path
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

import yaml

import trader
from trader.contracts import AlgoSpec, ContractViolation


class ConfigError(ContractViolation):
    """A versioned configuration file does not match its schema."""


@dataclass(frozen=True)
class SessionTimes:
    premarket_from: str
    rth_open: str
    rth_close: str


@dataclass(frozen=True)
class LiveConfig:
    cycle_minutes: int


@dataclass(frozen=True)
class TraderConfig:
    data_root: str
    timezone: str
    primary_symbol: str
    instrument_map: dict[str, str]
    symbols: list[str]
    session: SessionTimes
    live: LiveConfig


@dataclass(frozen=True)
class RelayConfig:
    claude_bin: str
    tool: str


@dataclass(frozen=True)
class GranularitiesConfig:
    minute_history_days: int
    daily_history: str


@dataclass(frozen=True)
class ValidationConfig:
    etf_leverage_factor: float
    etf_tolerance: float
    bad_tick_neighbor_fraction: float


@dataclass(frozen=True)
class ProviderConfig:
    vendor: str
    relay: RelayConfig
    granularities: GranularitiesConfig
    validation: ValidationConfig


@dataclass(frozen=True)
class AlgosConfig:
    rules_version: str
    gates: object
    roster: list[AlgoSpec]


@dataclass(frozen=True)
class AccountConfig:
    equity: float
    capital_fraction: float
    day_slots: int


@dataclass(frozen=True)
class RailsConfig:
    max_entries_per_day: int
    one_position_at_a_time: bool
    no_hedge: bool
    mute_after_consecutive_stops: int
    mute_after_cumulative_day_r: float
    reversal_cooldown_minutes: int


@dataclass(frozen=True)
class DrawdownStopConfig:
    max_session_drawdown_r: float | None


@dataclass(frozen=True)
class RiskConfig:
    account: AccountConfig
    rails: RailsConfig
    drawdown_stop: DrawdownStopConfig


@dataclass(frozen=True)
class FillsConfig:
    entry: str
    stop_wins_ties: bool
    commission: float


@dataclass(frozen=True)
class ExecutionConfig:
    broker: str
    live_orders: bool
    fills: FillsConfig
    slippage_bps: dict[str, dict[str, float]]


@dataclass(frozen=True)
class ConsoleConfig:
    host: str
    port: int


@dataclass(frozen=True)
class ResolvedConfig:
    trader: TraderConfig
    provider: ProviderConfig
    algos: AlgosConfig
    risk: RiskConfig
    execution: ExecutionConfig
    console: ConsoleConfig
    config_sha256: str
    package_version: str


_CONFIG_SCHEMAS = frozenset(
    {
        SessionTimes,
        LiveConfig,
        TraderConfig,
        RelayConfig,
        GranularitiesConfig,
        ValidationConfig,
        ProviderConfig,
        AlgosConfig,
        AccountConfig,
        RailsConfig,
        DrawdownStopConfig,
        RiskConfig,
        FillsConfig,
        ExecutionConfig,
        ConsoleConfig,
        AlgoSpec,
    }
)
_ConfigType = TypeVar("_ConfigType")


def _build(
    cls: type[_ConfigType],
    data: object,
    where: str,
) -> _ConfigType:
    """Build a schema dataclass while rejecting unknown and missing fields."""
    if not isinstance(data, Mapping):
        raise ConfigError(f"{where}: expected a mapping")

    schema_fields = {field.name: field for field in fields(cls)}
    unknown_keys = sorted(
        (key for key in data if key not in schema_fields),
        key=repr,
    )
    if unknown_keys:
        raise ConfigError(f"{where}: unknown key {unknown_keys[0]!r}")

    for name in schema_fields:
        if name not in data:
            raise ConfigError(f"{where}: missing required key {name!r}")

    type_hints = get_type_hints(cls)
    values: dict[str, Any] = {}
    for name in schema_fields:
        annotation = type_hints[name]
        raw_value = data[name]
        child_location = f"{where}: {name}"

        if annotation in _CONFIG_SCHEMAS:
            values[name] = _build(annotation, raw_value, child_location)
            continue

        args = get_args(annotation)
        if get_origin(annotation) is list and args and args[0] in _CONFIG_SCHEMAS:
            if not isinstance(raw_value, list):
                raise ConfigError(f"{child_location}: expected a list")
            values[name] = [
                _build(args[0], item, f"{child_location}[{index}]")
                for index, item in enumerate(raw_value)
            ]
            continue

        values[name] = raw_value

    return cls(**values)


def _read_yaml(config_dir: Path, filename: str) -> object:
    path = config_dir / filename
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except OSError as exc:
        raise ConfigError(f"{filename}: could not read file: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{filename}: invalid YAML: {exc}") from exc


def _normalize_slippage(config: ExecutionConfig) -> ExecutionConfig:
    slippage = config.slippage_bps
    if not isinstance(slippage, Mapping):
        raise ConfigError("execution.yaml: slippage_bps: expected a mapping")

    normalized: dict[str, dict[str, float]] = {}
    for symbol, monthly_values in slippage.items():
        if not isinstance(monthly_values, Mapping):
            raise ConfigError(
                f"execution.yaml: slippage_bps: {symbol}: expected a mapping"
            )
        normalized[symbol] = {
            str(month): value for month, value in monthly_values.items()
        }
    return replace(config, slippage_bps=normalized)


def _config_hash(
    trader_config: TraderConfig,
    provider: ProviderConfig,
    algos: AlgosConfig,
    risk: RiskConfig,
    execution: ExecutionConfig,
    console: ConsoleConfig,
) -> str:
    bundle = {
        "trader": asdict(trader_config),
        "provider": asdict(provider),
        "algos": asdict(algos),
        "risk": asdict(risk),
        "execution": asdict(execution),
        "console": asdict(console),
    }
    canonical_json = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def load_config(config_dir: Path = Path("config")) -> ResolvedConfig:
    """Load, validate, and hash all versioned execution configuration."""
    trader_config = _build(
        TraderConfig,
        _read_yaml(config_dir, "trader.yaml"),
        "trader.yaml",
    )
    provider = _build(
        ProviderConfig,
        _read_yaml(config_dir, "provider.yaml"),
        "provider.yaml",
    )
    algos = _build(
        AlgosConfig,
        _read_yaml(config_dir, "algos.yaml"),
        "algos.yaml",
    )
    risk = _build(
        RiskConfig,
        _read_yaml(config_dir, "risk.yaml"),
        "risk.yaml",
    )
    execution = _normalize_slippage(
        _build(
            ExecutionConfig,
            _read_yaml(config_dir, "execution.yaml"),
            "execution.yaml",
        )
    )
    console = _build(
        ConsoleConfig,
        _read_yaml(config_dir, "console.yaml"),
        "console.yaml",
    )

    return ResolvedConfig(
        trader=trader_config,
        provider=provider,
        algos=algos,
        risk=risk,
        execution=execution,
        console=console,
        config_sha256=_config_hash(
            trader_config,
            provider,
            algos,
            risk,
            execution,
            console,
        ),
        package_version=trader.__version__,
    )
