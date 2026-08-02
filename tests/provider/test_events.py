from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from trader.provider.calendar import load_calendar
from trader.provider.events import (
    capture_metrics,
    earnings_proximity,
    implied_move_iv,
    implied_move_straddle,
    is_reaction_day,
    load_captures,
    load_earnings_calendar,
)
from trader.provider.market import ProviderMarketData


REPO_ROOT = Path(__file__).resolve().parents[2]
CALENDAR_PATH = REPO_ROOT / "config" / "calendar.yaml"
FIXTURE_ROOT = Path(__file__).parent / "fixtures"
OPTIONS_FIXTURE = (
    FIXTURE_ROOT / "events" / "options" / "2026-08-01-sndk.json"
)


def _asof(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 16, tzinfo=timezone.utc)


def _market(
    data_root: Path,
    *,
    with_calendar: bool = True,
    primary_symbol: str = "SNDK",
) -> ProviderMarketData:
    return ProviderMarketData(
        data_root,
        calendar_path=CALENDAR_PATH if with_calendar else None,
        primary_symbol=primary_symbol,
    )


def _write_earnings(root: Path, payload: object) -> Path:
    path = root / "events" / "earnings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _capture() -> dict:
    return json.loads(OPTIONS_FIXTURE.read_text(encoding="utf-8"))


def test_implied_move_estimators_and_capture_metrics_match_hand_values() -> None:
    assert implied_move_straddle(100.0, 6.0, 4.0) == pytest.approx(0.1)
    assert implied_move_iv(0.55, 7.0) == pytest.approx(0.0607722532214491)
    assert capture_metrics(_capture()) == pytest.approx(
        {
            "straddle": 10.0,
            "straddle_pct": 0.1,
            "expected_move_pct": 0.085,
            "iv_mean": 0.55,
            "iv_expected_move_pct": 0.0607722532214491,
            "days_to_expiry": 7,
            "method_spread_pct": 0.0242277467785509,
        }
    )


@pytest.mark.parametrize(
    ("call", "problem"),
    [
        (lambda: implied_move_straddle(0.0, 1.0, 1.0), "spot"),
        (lambda: implied_move_straddle(100.0, -1.0, 1.0), "call mark"),
        (lambda: implied_move_straddle(100.0, 1.0, -1.0), "put mark"),
        (lambda: implied_move_iv(0.0, 7.0), "iv"),
        (lambda: implied_move_iv(0.5, 0.0), "days_to_expiry"),
    ],
)
def test_implied_move_estimators_reject_undefined_inputs(call, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        call()


def test_options_event_obeys_capture_date_pit_and_returns_full_metrics() -> None:
    market = _market(FIXTURE_ROOT, with_calendar=False)

    assert market.event("implied_move_pct", asof=_asof(date(2026, 7, 31))) is None
    assert market.event(
        "implied_move_pct", asof=_asof(date(2026, 8, 1))
    ) == pytest.approx(
        {
            "straddle": 10.0,
            "straddle_pct": 0.1,
            "expected_move_pct": 0.085,
            "iv_mean": 0.55,
            "iv_expected_move_pct": 0.0607722532214491,
            "days_to_expiry": 7,
            "method_spread_pct": 0.0242277467785509,
        }
    )
    assert market.event("implied_move_pct", asof=_asof(date(2026, 8, 9))) is None


def test_options_event_without_store_is_none_and_needs_no_calendar(
    tmp_path: Path,
) -> None:
    assert _market(tmp_path, with_calendar=False).event(
        "implied_move_pct", asof=_asof(date(2026, 8, 1))
    ) is None


def test_load_captures_sorts_by_captured_at_after_filename_walk(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "events" / "options"
    directory.mkdir(parents=True)
    later = _capture()
    later["captured_at"] = "2026-08-02T15:00:00Z"
    later["expiration"] = "2026-08-09"
    earlier = _capture()
    (directory / "a-later.json").write_text(json.dumps(later), encoding="utf-8")
    (directory / "z-earlier.json").write_text(
        json.dumps(earlier), encoding="utf-8"
    )

    assert [capture["captured_at"] for capture in load_captures(tmp_path)] == [
        "2026-08-01T15:00:00Z",
        "2026-08-02T15:00:00Z",
    ]


def test_malformed_capture_names_file_and_field(tmp_path: Path) -> None:
    directory = tmp_path / "events" / "options"
    directory.mkdir(parents=True)
    malformed = _capture()
    del malformed["call"]["mark"]
    (directory / "bad-capture.json").write_text(
        json.dumps(malformed), encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"bad-capture\.json.*call\.mark"):
        _market(tmp_path, with_calendar=False).event(
            "implied_move_pct", asof=_asof(date(2026, 8, 1))
        )


def test_missing_earnings_calendar_is_none(tmp_path: Path) -> None:
    assert _market(tmp_path).event(
        "earnings_proximity", asof=_asof(date(2026, 7, 6))
    ) is None
    assert load_earnings_calendar(tmp_path) is None


def test_earnings_proximity_returns_future_primary_and_am_holiday_reaction() -> None:
    result = _market(FIXTURE_ROOT).event(
        "earnings_proximity", asof=_asof(date(2026, 7, 6))
    )

    assert result == {
        "primary_symbol": "SNDK",
        "next_date": "2026-08-05",
        "timing": "pm",
        "days_to_earnings": 30.0,
        "peer_reaction_today": True,
    }


def test_am_report_on_holiday_rolls_to_next_real_session() -> None:
    events = load_earnings_calendar(FIXTURE_ROOT)
    assert events is not None
    calendar = load_calendar(CALENDAR_PATH)

    assert not is_reaction_day(
        events, date(2026, 7, 3), calendar, exclude_symbol="SNDK"
    )
    assert is_reaction_day(
        events, date(2026, 7, 6), calendar, exclude_symbol="SNDK"
    )


def test_pm_report_reacts_on_next_session_after_weekend() -> None:
    events = load_earnings_calendar(FIXTURE_ROOT)
    assert events is not None
    calendar = load_calendar(CALENDAR_PATH)

    assert not is_reaction_day(
        events, date(2026, 7, 10), calendar, exclude_symbol="SNDK"
    )
    assert is_reaction_day(
        events, date(2026, 7, 13), calendar, exclude_symbol="SNDK"
    )


def test_primary_symbol_is_excluded_from_peer_reaction(tmp_path: Path) -> None:
    _write_earnings(
        tmp_path,
        {
            "events": [
                {"symbol": "SNDK", "date": "2026-07-10", "timing": "pm"}
            ]
        },
    )

    assert _market(tmp_path).event(
        "earnings_proximity", asof=_asof(date(2026, 7, 13))
    ) == {
        "primary_symbol": "SNDK",
        "next_date": None,
        "timing": None,
        "days_to_earnings": None,
        "peer_reaction_today": False,
    }


@pytest.mark.parametrize(
    ("event", "problem"),
    [
        ({"date": "2026-08-05", "timing": "pm"}, "symbol"),
        (
            {"symbol": "SNDK", "date": "not-a-date", "timing": "pm"},
            "date",
        ),
        (
            {"symbol": "SNDK", "date": "2026-08-05", "timing": "midday"},
            "timing",
        ),
    ],
)
def test_malformed_earnings_calendar_names_file_and_problem(
    tmp_path: Path, event: dict, problem: str
) -> None:
    _write_earnings(tmp_path, {"events": [event]})

    with pytest.raises(ValueError, match=rf"earnings\.json.*{problem}"):
        earnings_proximity(
            tmp_path,
            load_calendar(CALENDAR_PATH),
            "SNDK",
            asof=_asof(date(2026, 8, 1)),
        )


def test_event_rejects_unknown_kind_and_earnings_without_calendar(
    tmp_path: Path,
) -> None:
    market = _market(tmp_path, with_calendar=False)

    with pytest.raises(ValueError, match="unknown_event"):
        market.event("unknown_event", asof=_asof(date(2026, 8, 1)))
    with pytest.raises(
        RuntimeError,
        match=r'event\(\) requires a calendar for "earnings_proximity"; '
        r"provide calendar_path at construction",
    ):
        market.event("earnings_proximity", asof=_asof(date(2026, 8, 1)))


def test_event_signals_map_absence_and_present_values() -> None:
    without_events = _market(FIXTURE_ROOT / "missing")
    with_events = _market(FIXTURE_ROOT)

    assert without_events.signal(
        "implied_move_pct", asof=_asof(date(2026, 8, 1))
    ) == 0.0
    assert without_events.signal(
        "days_to_earnings", asof=_asof(date(2026, 7, 6))
    ) == 999.0
    assert without_events.signal(
        "peer_earnings_reaction", asof=_asof(date(2026, 7, 6))
    ) == 0.0

    assert with_events.signal(
        "implied_move_pct", asof=_asof(date(2026, 8, 1))
    ) == pytest.approx(0.085)
    assert with_events.signal(
        "days_to_earnings", asof=_asof(date(2026, 7, 6))
    ) == 30.0
    assert with_events.signal(
        "peer_earnings_reaction", asof=_asof(date(2026, 7, 10))
    ) == 0.0
    assert with_events.signal(
        "peer_earnings_reaction", asof=_asof(date(2026, 7, 13))
    ) == 1.0


def test_implied_move_signal_does_not_require_calendar() -> None:
    market = _market(FIXTURE_ROOT, with_calendar=False)

    assert market.signal(
        "implied_move_pct", asof=_asof(date(2026, 8, 1))
    ) == pytest.approx(0.085)
