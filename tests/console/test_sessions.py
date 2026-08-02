"""Tests for console session selection helpers."""

from __future__ import annotations

from pathlib import Path

from trader.console.sessions import default_results_session_id


def test_default_results_session_id_prefers_newest_backtest_over_newer_other_mode(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    for session_id in (
        "backtest-20260730-090000",
        "paper-20260801-090000",
        "backtest-20260731-153000",
        "cross-ticker-20260802-090000",
    ):
        (sessions_dir / session_id).mkdir()

    assert default_results_session_id(tmp_path) == "backtest-20260731-153000"


def test_default_results_session_id_falls_back_to_newest_session_without_backtest(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    for session_id in (
        "paper-20260730-090000",
        "cross-ticker-20260801-093000",
        "paper-20260801-153000",
    ):
        (sessions_dir / session_id).mkdir()

    assert default_results_session_id(tmp_path) == "paper-20260801-153000"


def test_default_results_session_id_returns_none_when_no_sessions_exist(
    tmp_path: Path,
) -> None:
    assert default_results_session_id(tmp_path) is None
