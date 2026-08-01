"""Tests for typed execution configuration loading."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import shutil

import pytest
import yaml

import trader
from trader.contracts import AlgoSpec
from trader.contracts.errors import ContractViolation
from trader.execution.config import (
    AccountConfig,
    AlgosConfig,
    ConfigError,
    ConsoleConfig,
    DrawdownStopConfig,
    ExecutionConfig,
    FillsConfig,
    GranularitiesConfig,
    LiveConfig,
    ProviderConfig,
    RailsConfig,
    RelayConfig,
    ResolvedConfig,
    RiskConfig,
    SessionTimes,
    TraderConfig,
    ValidationConfig,
    load_config,
)


REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CONFIG_FILENAMES = (
    "trader.yaml",
    "provider.yaml",
    "algos.yaml",
    "risk.yaml",
    "execution.yaml",
    "console.yaml",
)
CONFIG_DATACLASSES = (
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
    ResolvedConfig,
)


@pytest.fixture
def copied_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for filename in CONFIG_FILENAMES:
        shutil.copy2(REPO_CONFIG_DIR / filename, config_dir / filename)
    return config_dir


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _write_yaml(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(data, stream, sort_keys=False)


def test_loads_real_config_into_expected_typed_shape() -> None:
    resolved = load_config(REPO_CONFIG_DIR)

    assert isinstance(resolved, ResolvedConfig)
    assert isinstance(resolved.trader, TraderConfig)
    assert resolved.trader.primary_symbol == "SNDK"
    assert resolved.trader.instrument_map == {"long": "SNXX", "short": "SNDQ"}
    assert resolved.risk.account.equity == 10000
    assert resolved.risk.account.day_slots == 2
    assert resolved.execution.broker == "sim"
    assert resolved.execution.slippage_bps["SNXX"]["2026-07"] == 5
    assert resolved.risk.drawdown_stop.max_session_drawdown_r is None
    assert resolved.console.port == 8777
    assert len(resolved.algos.roster) == 8
    assert all(isinstance(spec, AlgoSpec) for spec in resolved.algos.roster)
    assert all(spec.params == "PORT" for spec in resolved.algos.roster)
    assert resolved.package_version == trader.__version__


def test_all_execution_config_dataclasses_are_frozen() -> None:
    assert all(cls.__dataclass_params__.frozen for cls in CONFIG_DATACLASSES)

    resolved = load_config(REPO_CONFIG_DIR)
    with pytest.raises(FrozenInstanceError):
        resolved.console.port = 9000


def test_config_sha256_is_deterministic_and_changes_with_config(
    copied_config: Path,
) -> None:
    first_hash = load_config(REPO_CONFIG_DIR).config_sha256
    second_hash = load_config(REPO_CONFIG_DIR).config_sha256

    risk_path = copied_config / "risk.yaml"
    risk_data = _load_yaml(risk_path)
    risk_data["account"]["day_slots"] = 3
    _write_yaml(risk_path, risk_data)

    changed_hash = load_config(copied_config).config_sha256

    assert first_hash == second_hash
    assert changed_hash != first_hash
    assert len(first_hash) == 64


@pytest.mark.parametrize(
    ("filename", "add_bogus_key", "expected_location"),
    [
        (
            "console.yaml",
            lambda data: data.update({"bogus_top_level": True}),
            "console.yaml",
        ),
        (
            "risk.yaml",
            lambda data: data["rails"].update({"bogus_nested": True}),
            "risk.yaml: rails",
        ),
    ],
)
def test_rejects_unknown_keys_at_every_schema_level(
    copied_config: Path,
    filename: str,
    add_bogus_key,
    expected_location: str,
) -> None:
    config_path = copied_config / filename
    data = _load_yaml(config_path)
    add_bogus_key(data)
    _write_yaml(config_path, data)

    bad_key = "bogus_top_level" if filename == "console.yaml" else "bogus_nested"
    with pytest.raises(ConfigError) as exc_info:
        load_config(copied_config)

    message = str(exc_info.value)
    assert expected_location in message
    assert bad_key in message


def test_rejects_missing_required_key(copied_config: Path) -> None:
    risk_path = copied_config / "risk.yaml"
    risk_data = _load_yaml(risk_path)
    del risk_data["account"]["equity"]
    _write_yaml(risk_path, risk_data)

    with pytest.raises(
        ConfigError,
        match=r"risk\.yaml: account.*equity",
    ):
        load_config(copied_config)


def test_config_error_is_a_contract_violation() -> None:
    assert isinstance(ConfigError("invalid config"), ContractViolation)
