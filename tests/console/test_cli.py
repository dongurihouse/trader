"""Tests for console-owned CLI subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import socket

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


def _assert_printed_server_is_stopped(stdout: str) -> None:
    match = re.search(
        r"http://127\.0\.0\.1:(\d+)/\?session=fixture-session-001", stdout
    )
    assert match is not None

    with pytest.raises(ConnectionRefusedError):
        with socket.create_connection(
            ("127.0.0.1", int(match.group(1))), timeout=0.1
        ):
            pass


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


def test_console_command_serves_newest_session_and_shuts_down(
    report_cli_fixture: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trader.console import cli as console_cli

    config_dir, _ = report_cli_fixture
    parser = _build_component_parser()
    args = parser.parse_args(["console", "--config-dir", str(config_dir)])
    monkeypatch.setattr(console_cli, "_wait_until_interrupted", lambda: None)

    result = args.func(args)

    captured = capsys.readouterr()
    assert result in (0, None)
    assert "fixture-session-001" in captured.out
    assert "http://127.0.0.1" in captured.out
    assert captured.err == ""
    _assert_printed_server_is_stopped(captured.out)


def test_console_command_serves_named_session_and_shuts_down(
    report_cli_fixture: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trader.console import cli as console_cli

    config_dir, _ = report_cli_fixture
    parser = _build_component_parser()
    args = parser.parse_args(
        ["console", "fixture-session-001", "--config-dir", str(config_dir)]
    )
    monkeypatch.setattr(console_cli, "_wait_until_interrupted", lambda: None)

    result = args.func(args)

    captured = capsys.readouterr()
    assert result in (0, None)
    assert "fixture-session-001" in captured.out
    assert "http://127.0.0.1" in captured.out
    assert captured.err == ""
    _assert_printed_server_is_stopped(captured.out)


def test_console_command_returns_one_when_no_sessions_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trader.console import cli as console_cli

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "console.yaml").write_text(
        "host: 127.0.0.1\nport: 0\n", encoding="utf-8"
    )
    (config_dir / "trader.yaml").write_text("data_root: data\n", encoding="utf-8")
    (tmp_path / "data" / "sessions").mkdir(parents=True)

    def fail_if_server_starts(_config: object) -> None:
        raise AssertionError("server must not start without a session")

    monkeypatch.setattr(console_cli, "run_server", fail_if_server_starts, raising=False)
    parser = _build_component_parser()
    args = parser.parse_args(["console", "--config-dir", str(config_dir)])

    result = args.func(args)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "no sessions" in captured.err.lower()


def test_wait_until_interrupted_returns_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trader.console import cli as console_cli

    sleep_intervals: list[float] = []

    def interrupt(interval: float) -> None:
        sleep_intervals.append(interval)
        raise KeyboardInterrupt

    monkeypatch.setattr(console_cli.time, "sleep", interrupt)

    console_cli._wait_until_interrupted()

    assert sleep_intervals == [0.2]
