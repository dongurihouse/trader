"""Configuration for the local telemetry console."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ConsoleConfig:
    """Settings needed by the local console server."""

    host: str
    port: int
    data_root: Path


def load_console_config(
    config_dir: Path, base_dir: Path | None = None
) -> ConsoleConfig:
    """Load console and data-root settings from the project YAML files."""
    console_path = config_dir / "console.yaml"
    trader_path = config_dir / "trader.yaml"
    console_values = yaml.safe_load(console_path.read_text(encoding="utf-8")) or {}
    trader_values = yaml.safe_load(trader_path.read_text(encoding="utf-8")) or {}

    host = _required(console_values, "host", console_path)
    port = _required(console_values, "port", console_path)
    data_root_value = _required(trader_values, "data_root", trader_path)

    data_root = Path(data_root_value)
    if not data_root.is_absolute():
        data_root = (Path.cwd() if base_dir is None else base_dir) / data_root

    return ConsoleConfig(
        host=str(host),
        port=int(port),
        data_root=data_root.resolve(),
    )


def _required(values: object, key: str, source: Path) -> object:
    if not isinstance(values, dict) or key not in values:
        raise ValueError(f"missing required key {key!r} in {source}")
    return values[key]
