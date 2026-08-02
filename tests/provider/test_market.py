from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from trader.contracts.errors import LookaheadError
from trader.provider.market import ProviderMarketData
from trader.provider.store import write_1d, write_1m_day


BAR_COLUMNS = ["o", "h", "l", "c", "v"]
CALENDAR_PATH = Path(__file__).resolve().parents[2] / "config" / "calendar.yaml"


def _frame(timestamps: list[str], base: float = 10.0) -> pd.DataFrame:
    rows = len(timestamps)
    values = [base + position for position in range(rows)]
    index = pd.DatetimeIndex(timestamps, name="t")
    return pd.DataFrame(
        {
            "o": values,
            "h": [value + 1.0 for value in values],
            "l": [value - 1.0 for value in values],
            "c": [value + 0.5 for value in values],
            "v": [100.0 + position for position in range(rows)],
        },
        index=index,
        dtype="float64",
    )


def _assert_empty_bar_frame(frame: pd.DataFrame) -> None:
    assert frame.empty
    assert list(frame.columns) == BAR_COLUMNS
    assert all(dtype == "float64" for dtype in frame.dtypes.astype(str))
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert str(frame.index.tz) == "UTC"
    assert frame.index.name == "t"


def test_bars_1m_hides_bar_at_open_and_reveals_it_at_completion(tmp_path) -> None:
    timestamp = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    source = _frame([timestamp.isoformat()])
    write_1m_day(tmp_path, "SNDK", timestamp.date(), source)
    market = ProviderMarketData(tmp_path)

    at_open = market.bars_1m("SNDK", asof=timestamp)
    at_completion = market.bars_1m(
        "SNDK", asof=timestamp + timedelta(minutes=1)
    )

    _assert_empty_bar_frame(at_open)
    assert_frame_equal(at_completion, source)


def test_bars_1m_raises_when_asof_is_past_last_completion(tmp_path) -> None:
    timestamp = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    write_1m_day(tmp_path, "SNDK", timestamp.date(), _frame([timestamp.isoformat()]))

    with pytest.raises(LookaheadError):
        ProviderMarketData(tmp_path).bars_1m(
            "SNDK", asof=timestamp + timedelta(minutes=2)
        )


def test_bars_1m_returns_canonical_empty_frame_for_unknown_symbol(tmp_path) -> None:
    result = ProviderMarketData(tmp_path).bars_1m(
        "MISSING",
        asof=datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
    )

    _assert_empty_bar_frame(result)


def test_bars_1m_applies_lookback_minutes_after_pit_filter(tmp_path) -> None:
    source = _frame(
        [
            "2026-07-01T13:30:00Z",
            "2026-07-01T13:31:00Z",
            "2026-07-01T13:32:00Z",
        ]
    )
    write_1m_day(tmp_path, "SNDK", date(2026, 7, 1), source)

    result = ProviderMarketData(tmp_path).bars_1m(
        "SNDK",
        asof=datetime(2026, 7, 1, 13, 33, tzinfo=timezone.utc),
        lookback_minutes=2,
    )

    assert_frame_equal(result, source.tail(2))


def test_bars_1m_stitches_prior_day_for_early_session_lookback(tmp_path) -> None:
    prior_day = _frame(
        ["2026-07-01T19:58:00Z", "2026-07-01T19:59:00Z"], base=10.0
    )
    current_day = _frame(["2026-07-02T13:30:00Z"], base=20.0)
    write_1m_day(tmp_path, "SNDK", date(2026, 7, 1), prior_day)
    write_1m_day(tmp_path, "SNDK", date(2026, 7, 2), current_day)

    result = ProviderMarketData(tmp_path).bars_1m(
        "SNDK",
        asof=datetime(2026, 7, 2, 13, 31, tzinfo=timezone.utc),
        lookback_minutes=2,
    )

    expected = pd.concat([prior_day.tail(1), current_day])
    assert_frame_equal(result, expected)


def test_bars_1d_excludes_asof_day_and_applies_lookback_days(tmp_path) -> None:
    source = _frame(
        ["2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"], base=30.0
    )
    write_1d(tmp_path, "SNDK", source)
    market = ProviderMarketData(tmp_path)

    on_second_day = market.bars_1d(
        "SNDK", asof=date(2026, 7, 2), lookback_days=5
    )
    after_second_day = market.bars_1d(
        "SNDK", asof=date(2026, 7, 3), lookback_days=1
    )

    assert_frame_equal(on_second_day, source.iloc[[0]])
    assert_frame_equal(after_second_day, source.iloc[[1]])


def test_bars_1d_allows_weekend_gap_with_configured_calendar(tmp_path) -> None:
    source = _frame(
        ["2026-07-09T00:00:00Z", "2026-07-10T00:00:00Z"], base=30.0
    )
    write_1d(tmp_path, "SNDK", source)
    market = ProviderMarketData(tmp_path, calendar_path=CALENDAR_PATH)

    result = market.bars_1d(
        "SNDK", asof=date(2026, 7, 13), lookback_days=15
    )

    assert_frame_equal(result, source)


def test_bars_1d_raises_when_asof_is_past_last_known_day(tmp_path) -> None:
    source = _frame(["2026-07-02T00:00:00Z"])
    write_1d(tmp_path, "SNDK", source)

    with pytest.raises(LookaheadError):
        ProviderMarketData(tmp_path).bars_1d(
            "SNDK", asof=date(2026, 7, 4), lookback_days=1
        )


def test_bars_1d_returns_canonical_empty_frame_for_unknown_symbol(tmp_path) -> None:
    result = ProviderMarketData(tmp_path).bars_1d(
        "MISSING", asof=date(2026, 7, 1), lookback_days=5
    )

    _assert_empty_bar_frame(result)


def test_calendar_without_configuration_is_explicitly_deferred(tmp_path) -> None:
    with pytest.raises(NotImplementedError, match="^wired by a later provider task$"):
        ProviderMarketData(tmp_path).calendar()
