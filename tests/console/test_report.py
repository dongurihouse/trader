"""Tests for deterministic post-session Markdown reports."""

from __future__ import annotations

from pathlib import Path

from trader.contracts import read_jsonl
from trader.console.dashboard import SHADOW_CAVEAT_TEXT


REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_TELEMETRY = REPO_ROOT / "tests" / "fixtures" / "telemetry.sample.jsonl"
GOLDEN_REPORT = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "report.fixture-session-001.golden.md"
)


def test_build_report_markdown_contains_fixture_session_facts() -> None:
    from trader.console.report import build_report_markdown

    events = list(read_jsonl(SAMPLE_TELEMETRY))

    markdown = build_report_markdown("fixture-session-001", events)

    for expected in (
        "fixture-session-001",
        "paper",
        "fixture-config-sha256",
        "0.1.0",
        "720",
        "100337.5",
        "breakout",
        "mean-reversion",
        "confidence_floor",
        "0.42 is below the required 0.50",
        "fixture signal unavailable",
        SHADOW_CAVEAT_TEXT,
    ):
        assert expected in markdown

    assert "| n_real | n_shadow | wins |" in markdown
    assert "| breakout | real | emitting | 1 | 0 | 1 |" in markdown
    assert "1.500" in markdown
    assert markdown.count(SHADOW_CAVEAT_TEXT) == 1
    assert markdown.index(SHADOW_CAVEAT_TEXT) < markdown.index("## Rejections by rule")


def test_build_report_uses_latest_metrics_and_marks_absent_data() -> None:
    from trader.console.report import build_report_markdown

    events = [
        {
            "ev": "session_start",
            "ts": "2026-07-01T13:29:00Z",
            "session": "incomplete-session",
            "mode": "paper",
            "config_sha256": "config-sha",
            "package_version": "0.1.0",
            "symbols": ["SNDK"],
            "roster": [
                {"id": "alpha", "status": "emitting"},
                {"id": "beta", "status": "disabled"},
            ],
        },
        {
            "ev": "metrics",
            "ts": "2026-07-01T14:00:00Z",
            "session": "incomplete-session",
            "book": "real",
            "algo_id": "alpha",
            "status": "emitting",
            "n_real": 1,
            "n_shadow": 0,
            "wins": 0,
            "win_rate": 0.0,
            "mean_r": -1.0,
            "expectancy_r": -1.0,
            "profit_factor": None,
            "max_drawdown_r": 1.0,
            "cum_r": -1.0,
            "updated_ts": "2026-07-01T14:00:00Z",
        },
        {
            "ev": "metrics",
            "ts": "2026-07-01T15:00:00Z",
            "session": "incomplete-session",
            "book": "real",
            "algo_id": "alpha",
            "status": "emitting",
            "n_real": 7,
            "n_shadow": 2,
            "wins": 6,
            "win_rate": 0.75,
            "mean_r": 2.25,
            "expectancy_r": 1.25,
            "profit_factor": 9.25,
            "max_drawdown_r": 0.5,
            "cum_r": 8.75,
            "updated_ts": "2026-07-01T15:00:00Z",
        },
    ]

    markdown = build_report_markdown("incomplete-session", events)

    assert "Session end was not recorded" in markdown
    assert "| alpha | real | emitting | 7 | 2 | 6 | 0.750 | 2.250 | 1.250 | 9.250 | 0.500 | 8.750 |" in markdown
    assert "| beta | — | disabled | — | — | — | — | — | — | — | — | — |" in markdown
    assert "No rejections were recorded." in markdown
    assert "No algo errors were recorded." in markdown


def test_write_report_reads_telemetry_and_writes_exact_markdown(tmp_path: Path) -> None:
    from trader.console.report import build_report_markdown, write_report

    session_dir = tmp_path / "fixture-session-001"
    session_dir.mkdir()
    (session_dir / "telemetry.jsonl").write_bytes(SAMPLE_TELEMETRY.read_bytes())
    events = list(read_jsonl(session_dir / "telemetry.jsonl"))

    returned_markdown = write_report(session_dir)
    expected_markdown = build_report_markdown("fixture-session-001", events)

    assert returned_markdown == expected_markdown
    assert (session_dir / "report.md").read_text(encoding="utf-8") == expected_markdown


def test_build_report_is_deterministic_and_matches_golden() -> None:
    from trader.console.report import build_report_markdown

    events = list(read_jsonl(SAMPLE_TELEMETRY))

    first = build_report_markdown("fixture-session-001", events)
    second = build_report_markdown("fixture-session-001", events)

    assert first == second
    assert second == GOLDEN_REPORT.read_text(encoding="utf-8")
