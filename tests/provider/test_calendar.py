from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from trader.provider.calendar import load_calendar
from trader.provider.market import ProviderMarketData


FIXTURE_CALENDAR_PATH = Path(__file__).parent / "fixtures" / "calendar.yaml"
REPO_CALENDAR_PATH = Path(__file__).resolve().parents[2] / "config" / "calendar.yaml"


def test_committed_calendar_classifies_known_sessions() -> None:
    calendar = load_calendar(REPO_CALENDAR_PATH)

    assert not calendar.is_session(date(2026, 7, 3))
    assert not calendar.is_session(date(2026, 7, 4))
    assert calendar.is_session(date(2026, 7, 2))
    assert calendar.is_session(date(2026, 11, 27))


def test_session_close_converts_regular_and_early_closes_to_utc() -> None:
    calendar = load_calendar(REPO_CALENDAR_PATH)

    assert calendar.session_close(date(2026, 7, 2)) == datetime(
        2026, 7, 2, 20, 0, tzinfo=timezone.utc
    )
    assert calendar.session_close(date(2026, 11, 27)) == datetime(
        2026, 11, 27, 18, 0, tzinfo=timezone.utc
    )


def test_session_close_rejects_non_session_day() -> None:
    calendar = load_calendar(REPO_CALENDAR_PATH)

    with pytest.raises(ValueError, match="2026-01-19"):
        calendar.session_close(date(2026, 1, 19))


def test_prev_session_skips_adjacent_holiday_and_weekend() -> None:
    calendar = load_calendar(REPO_CALENDAR_PATH)

    assert calendar.prev_session(date(2026, 1, 20)) == date(2026, 1, 16)


def test_prev_session_stops_at_known_calendar_start() -> None:
    calendar = load_calendar(REPO_CALENDAR_PATH)

    assert calendar.prev_session(date(2025, 1, 1)) is None
    assert calendar.prev_session(date(2024, 12, 31)) is None


def test_provider_market_data_uses_configured_calendar(tmp_path: Path) -> None:
    calendar = ProviderMarketData(
        tmp_path, calendar_path=FIXTURE_CALENDAR_PATH
    ).calendar()

    assert calendar.is_session(date(2026, 1, 20))


def test_provider_market_data_without_calendar_is_deferred(tmp_path: Path) -> None:
    with pytest.raises(
        NotImplementedError, match="^wired by a later provider task$"
    ):
        ProviderMarketData(tmp_path).calendar()
