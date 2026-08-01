"""Behavioral tests for the point-in-time market-data fake."""

from datetime import date, datetime, timedelta, timezone
import importlib
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trader.contracts.errors import LookaheadError


def _fake_market_data_type():
    return importlib.import_module("trader.contracts.testing").FakeMarketData


def _intraday_frame() -> pd.DataFrame:
    index = pd.date_range(
        "2026-07-01 13:30:00+00:00",
        periods=121,
        freq="min",
        name="ts",
    )
    offsets = pd.Series(range(len(index)), dtype="float64").to_numpy() / 100
    opens = 42.0 + offsets
    return pd.DataFrame(
        {
            "o": opens,
            "h": opens + 0.20,
            "l": opens - 0.15,
            "c": opens + 0.05,
            "v": pd.Series(range(1_000, 1_000 + len(index)), dtype="float64").to_numpy(),
        },
        index=index,
    )


def _multi_day_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [
            "2026-01-05 14:30:00+00:00",
            "2026-01-05 14:31:00+00:00",
            "2026-07-01 13:30:00+00:00",
            "2026-07-01 13:31:00+00:00",
            "2026-07-02 13:30:00+00:00",
            "2026-07-02 13:31:00+00:00",
        ],
        name="ts",
    )
    return pd.DataFrame(
        [
            {"o": 10.0, "h": 12.0, "l": 9.0, "c": 11.0, "v": 100.0},
            {"o": 11.0, "h": 13.0, "l": 10.0, "c": 12.0, "v": 150.0},
            {"o": 20.0, "h": 21.0, "l": 19.0, "c": 20.5, "v": 200.0},
            {"o": 20.5, "h": 22.0, "l": 20.0, "c": 21.5, "v": 250.0},
            {"o": 30.0, "h": 31.0, "l": 29.0, "c": 30.5, "v": 300.0},
            {"o": 30.5, "h": 32.0, "l": 30.0, "c": 31.5, "v": 350.0},
        ],
        index=index,
    )


def test_package_reexports_all_testing_helpers() -> None:
    package = importlib.import_module("trader.contracts.testing")

    assert package.__all__ == [
        "CollectingTelemetry",
        "FakeBroker",
        "FakeClock",
        "FakeMarketData",
        "synthetic_day",
    ]
    assert package.FakeMarketData.__name__ == "FakeMarketData"


def test_bars_1m_returns_only_bars_complete_at_asof() -> None:
    frame = _intraday_frame()
    data = _fake_market_data_type()({"SNDK": frame})
    asof = datetime(2026, 7, 1, 13, 33, 30, tzinfo=timezone.utc)

    result = data.bars_1m("SNDK", asof=asof)

    pd.testing.assert_frame_equal(result, frame.iloc[:3])
    assert all(ts + timedelta(minutes=1) <= asof for ts in result.index)
    assert frame.index[3] + timedelta(minutes=1) > asof


def test_bars_1m_before_first_completion_returns_typed_empty_frame() -> None:
    frame = _intraday_frame()
    data = _fake_market_data_type()({"SNDK": frame})

    result = data.bars_1m(
        "SNDK",
        asof=datetime(2026, 7, 1, 13, 30, 30, tzinfo=timezone.utc),
    )

    pd.testing.assert_frame_equal(result, frame.iloc[:0])
    assert list(result.columns) == ["o", "h", "l", "c", "v"]


def test_bars_1m_lookback_returns_last_visible_rows() -> None:
    frame = _intraday_frame()
    data = _fake_market_data_type()({"SNDK": frame})
    asof = datetime(2026, 7, 1, 13, 35, tzinfo=timezone.utc)

    last_two = data.bars_1m("SNDK", asof=asof, lookback_minutes=2)
    oversized = data.bars_1m("SNDK", asof=asof, lookback_minutes=20)

    pd.testing.assert_frame_equal(last_two, frame.iloc[3:5])
    pd.testing.assert_frame_equal(oversized, frame.iloc[:5])


def test_bars_1m_rejects_asof_beyond_known_frame() -> None:
    frame = _intraday_frame()
    data = _fake_market_data_type()({"SNDK": frame})
    known_through = frame.index.max().to_pydatetime() + timedelta(minutes=1)

    with pytest.raises(LookaheadError):
        data.bars_1m("SNDK", asof=known_through + timedelta(microseconds=1))


def test_bars_1m_accepts_exact_last_bar_completion_boundary() -> None:
    frame = _intraday_frame()
    data = _fake_market_data_type()({"SNDK": frame})
    known_through = frame.index.max().to_pydatetime() + timedelta(minutes=1)

    result = data.bars_1m("SNDK", asof=known_through)

    pd.testing.assert_frame_equal(result, frame)


def test_bars_1m_unknown_symbol_raises_plain_key_error() -> None:
    data = _fake_market_data_type()({"SNDK": _intraday_frame()})

    with pytest.raises(KeyError) as error:
        data.bars_1m(
            "NVDA",
            asof=datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        )

    assert error.value.args == ("NVDA",)


def test_frames_and_returned_bars_are_defensive_copies() -> None:
    frame = _intraday_frame()
    frames = {"SNDK": frame}
    data = _fake_market_data_type()(frames)
    asof = datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc)

    frames.clear()
    frame.iloc[0, frame.columns.get_loc("c")] = -1.0
    first_result = data.bars_1m("SNDK", asof=asof)
    first_result.iloc[0, first_result.columns.get_loc("c")] = -2.0

    second_result = data.bars_1m("SNDK", asof=asof)
    assert second_result.iloc[0]["c"] == pytest.approx(42.05)


def test_bars_1d_aggregates_only_days_strictly_before_asof() -> None:
    frame = _multi_day_frame()
    data = _fake_market_data_type()({"SNDK": frame})

    result = data.bars_1d(
        "SNDK",
        asof=date(2026, 7, 2),
        lookback_days=2,
    )

    assert list(result.index) == [date(2026, 1, 5), date(2026, 7, 1)]
    assert list(result.columns) == ["o", "h", "l", "c", "v"]
    assert result.loc[date(2026, 1, 5)].to_dict() == {
        "o": 10.0,
        "h": 13.0,
        "l": 9.0,
        "c": 12.0,
        "v": 250.0,
    }
    assert result.loc[date(2026, 7, 1)].to_dict() == {
        "o": 20.0,
        "h": 22.0,
        "l": 19.0,
        "c": 21.5,
        "v": 450.0,
    }


def test_bars_1d_lookback_returns_only_most_recent_eligible_days() -> None:
    data = _fake_market_data_type()({"SNDK": _multi_day_frame()})

    result = data.bars_1d(
        "SNDK",
        asof=date(2026, 7, 2),
        lookback_days=1,
    )

    assert list(result.index) == [date(2026, 7, 1)]


def test_bars_1d_rejects_asof_beyond_known_days() -> None:
    data = _fake_market_data_type()({"SNDK": _multi_day_frame()})

    with pytest.raises(LookaheadError):
        data.bars_1d("SNDK", asof=date(2026, 7, 4), lookback_days=2)


def test_bars_1d_accepts_exact_known_day_boundary() -> None:
    data = _fake_market_data_type()({"SNDK": _multi_day_frame()})

    result = data.bars_1d("SNDK", asof=date(2026, 7, 3), lookback_days=10)

    assert list(result.index) == [
        date(2026, 1, 5),
        date(2026, 7, 1),
        date(2026, 7, 2),
    ]


def test_bars_1d_unknown_symbol_raises_plain_key_error() -> None:
    data = _fake_market_data_type()({"SNDK": _multi_day_frame()})

    with pytest.raises(KeyError) as error:
        data.bars_1d("NVDA", asof=date(2026, 7, 2), lookback_days=2)

    assert error.value.args == ("NVDA",)


def test_signal_returns_stored_value_and_rejects_unknown_name() -> None:
    signals = {"momentum": 0.75}
    data = _fake_market_data_type()({"SNDK": _intraday_frame()}, signals)
    signals["momentum"] = -1.0
    asof = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)

    assert data.signal("momentum", asof=asof, params={"window": 12}) == 0.75
    with pytest.raises(KeyError) as error:
        data.signal("missing", asof=asof)
    assert error.value.args == ("missing",)


def test_event_always_returns_none() -> None:
    data = _fake_market_data_type()({"SNDK": _intraday_frame()})

    assert (
        data.event(
            "earnings_proximity",
            asof=datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc),
        )
        is None
    )


def test_calendar_tracks_known_sessions_and_previous_session() -> None:
    calendar = _fake_market_data_type()({"SNDK": _multi_day_frame()}).calendar()

    assert calendar.is_session(date(2026, 7, 1)) is True
    assert calendar.is_session(date(2026, 7, 3)) is False
    assert calendar.prev_session(date(2026, 7, 2)) == date(2026, 7, 1)
    assert calendar.prev_session(date(2026, 7, 1)) == date(2026, 1, 5)
    assert calendar.prev_session(date(2026, 1, 5)) is None


@pytest.mark.parametrize(
    ("session_day", "expected_utc"),
    [
        (date(2026, 1, 5), datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)),
        (date(2026, 7, 1), datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)),
    ],
)
def test_calendar_session_close_converts_eastern_close_to_utc(
    session_day: date, expected_utc: datetime
) -> None:
    calendar = _fake_market_data_type()({"SNDK": _multi_day_frame()}).calendar()

    close = calendar.session_close(session_day)

    assert close == expected_utc
    assert close.tzinfo is timezone.utc
    eastern_close = datetime.combine(
        session_day,
        datetime.min.time().replace(hour=16),
        tzinfo=ZoneInfo("America/New_York"),
    )
    assert close == eastern_close.astimezone(timezone.utc)
