"""Contract tests for core aliases, records, and errors."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import importlib
from typing import get_args, get_type_hints

import pytest


def _types_module():
    return importlib.import_module("trader.contracts.types")


def _errors_module():
    return importlib.import_module("trader.contracts.errors")


@pytest.mark.parametrize(
    ("alias_name", "literal_values"),
    [
        ("Mode", ("backtest", "paper", "live")),
        ("Side", ("long", "short")),
        ("AlgoStatus", ("emitting", "probe", "disabled")),
    ],
)
def test_type_alias_has_exact_literal_values(
    alias_name: str, literal_values: tuple[str, ...]
) -> None:
    alias = getattr(_types_module(), alias_name)

    assert get_args(alias) == literal_values


def test_bar_constructs_with_exact_fields_and_values() -> None:
    bar_type = _types_module().Bar
    ts = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)

    bar = bar_type(
        symbol="SNDK",
        ts=ts,
        open=42.0,
        high=43.5,
        low=41.5,
        close=43.0,
        volume=12_345.0,
    )

    assert vars(bar) == {
        "symbol": "SNDK",
        "ts": ts,
        "open": 42.0,
        "high": 43.5,
        "low": 41.5,
        "close": 43.0,
        "volume": 12_345.0,
    }
    assert get_type_hints(bar_type) == {
        "symbol": str,
        "ts": datetime,
        "open": float,
        "high": float,
        "low": float,
        "close": float,
        "volume": float,
    }


def test_bar_is_frozen() -> None:
    bar_type = _types_module().Bar
    bar = bar_type(
        symbol="SNDK",
        ts=datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
        open=42.0,
        high=43.5,
        low=41.5,
        close=43.0,
        volume=12_345.0,
    )

    with pytest.raises(FrozenInstanceError):
        bar.close = 44.0


@pytest.mark.parametrize(
    "error_name",
    ["LookaheadError", "ContractViolation", "BrokerNotConfigured"],
)
def test_contract_error_constructs_as_exception(error_name: str) -> None:
    error_type = getattr(_errors_module(), error_name)

    error = error_type("contract failed")

    assert isinstance(error, Exception)
    assert str(error) == "contract failed"
