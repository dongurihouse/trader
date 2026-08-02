"""Tests for execution-owned CLI commands."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import shutil

import pytest
import yaml


REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CONFIG_FILENAMES = (
    "trader.yaml",
    "provider.yaml",
    "algos.yaml",
    "risk.yaml",
    "execution.yaml",
    "console.yaml",
)


def _execution_cli():
    return importlib.import_module("trader.execution.cli")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader")
    subparsers = parser.add_subparsers(dest="command")
    _execution_cli().register(subparsers)
    return parser


@pytest.fixture
def configured_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for filename in CONFIG_FILENAMES:
        shutil.copy2(REPO_CONFIG_DIR / filename, config_dir / filename)

    trader_path = config_dir / "trader.yaml"
    with trader_path.open(encoding="utf-8") as stream:
        trader_config = yaml.safe_load(stream)
    data_root = tmp_path / "state"
    trader_config["data_root"] = str(data_root)
    with trader_path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(trader_config, stream, sort_keys=False)

    monkeypatch.chdir(tmp_path)
    return data_root


def test_register_parses_fills_record_arguments_and_attaches_handler() -> None:
    args = _parser().parse_args(
        [
            "fills",
            "record",
            "--session",
            "s1",
            "--ticket",
            "t1",
            "--price",
            "10.5",
            "--shares",
            "5",
            "--kind",
            "entry",
        ]
    )

    assert callable(args.func)
    assert args.session == "s1"
    assert args.ticket_id == "t1"
    assert args.price == 10.5
    assert args.shares == 5
    assert args.kind == "entry"
    assert args.ts is None


def test_fills_record_handler_loads_default_config_and_appends_fill(
    configured_working_directory: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _parser().parse_args(
        [
            "fills",
            "record",
            "--session",
            "s1",
            "--ticket",
            "t1",
            "--price",
            "10.5",
            "--shares",
            "5",
            "--kind",
            "entry",
            "--ts",
            "2026-08-01T19:15:00Z",
        ]
    )

    result = args.func(args)

    assert result is None
    fill_path = (
        configured_working_directory
        / "sessions"
        / "s1"
        / "manual_fills.jsonl"
    )
    records = [json.loads(line) for line in fill_path.read_text().splitlines()]
    assert records == [
        {
            "ticket_id": "t1",
            "price": 10.5,
            "shares": 5,
            "kind": "entry",
            "ts": "2026-08-01T19:15:00Z",
        }
    ]
    assert "recorded" in capsys.readouterr().out.lower()


def test_fills_without_record_prints_usage_and_returns_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _parser().parse_args(["fills"])

    result = args.func(args)

    assert result == 1
    assert "usage: trader fills" in capsys.readouterr().out
