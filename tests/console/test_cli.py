"""Tests for console-owned CLI subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest


SAMPLE_TELEMETRY = (
    Path(__file__).resolve().parents[1] / "fixtures" / "telemetry.sample.jsonl"
)


@pytest.fixture
def report_cli_fixture(tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "console.yaml").write_text(
        "host: 127.0.0.1\nport: 0\n", encoding="utf-8"
    )
    (config_dir / "trader.yaml").write_text("data_root: data\n", encoding="utf-8")

    session_dir = tmp_path / "data" / "sessions" / "fixture-session-001"
    session_dir.mkdir(parents=True)
    (session_dir / "telemetry.jsonl").write_bytes(SAMPLE_TELEMETRY.read_bytes())
    return config_dir, session_dir


def _build_component_parser() -> argparse.ArgumentParser:
    from trader.console.cli import register

    parser = argparse.ArgumentParser(prog="trader")
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)
    return parser


def test_report_command_writes_report_and_prints_markdown(
    report_cli_fixture: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir, session_dir = report_cli_fixture
    parser = _build_component_parser()
    args = parser.parse_args(
        ["report", "fixture-session-001", "--config-dir", str(config_dir)]
    )

    result = args.func(args)

    captured = capsys.readouterr()
    assert result in (0, None)
    assert "fixture-session-001" in captured.out
    assert captured.err == ""
    assert (session_dir / "report.md").read_text(encoding="utf-8") == captured.out


def test_report_command_returns_one_for_missing_session(
    report_cli_fixture: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir, _ = report_cli_fixture
    parser = _build_component_parser()
    args = parser.parse_args(
        ["report", "does-not-exist", "--config-dir", str(config_dir)]
    )

    result = args.func(args)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "does-not-exist" in captured.err
