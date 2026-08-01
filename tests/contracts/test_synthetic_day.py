"""Behavioral tests for deterministic synthetic minute-bar fixtures."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from trader.contracts.testing import FakeMarketData, synthetic_day


def test_synthetic_day_is_exactly_reproducible_with_expected_session_shape() -> None:
    session_day = date(2026, 7, 1)

    first = synthetic_day("SNDK", session_day, seed=20260701)
    second = synthetic_day("SNDK", session_day, seed=20260701)

    pd.testing.assert_frame_equal(first, second, check_exact=True)
    assert len(first) == 720
    assert list(first.columns) == ["o", "h", "l", "c", "v"]
    assert first.index.name == "ts"
    assert first.index.tz is timezone.utc
    assert first.index[0] == datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
    assert first.index[-1] == datetime(2026, 7, 1, 19, 59, tzinfo=timezone.utc)


def test_synthetic_day_produces_continuous_positive_valid_ohlcv() -> None:
    frame = synthetic_day("SNDK", date(2026, 7, 1), seed=11)

    assert np.array_equal(
        frame["o"].iloc[1:].to_numpy(),
        frame["c"].iloc[:-1].to_numpy(),
    )
    assert frame["h"].ge(frame[["o", "c"]].max(axis=1)).all()
    assert frame["l"].le(frame[["o", "c"]].min(axis=1)).all()
    assert frame[["o", "h", "l", "c"]].gt(0).all().all()
    assert frame["v"].gt(0).all()


def test_synthetic_day_is_directly_queryable_by_fake_market_data() -> None:
    frame = synthetic_day("SNDK", date(2026, 7, 1), seed=7)
    data = FakeMarketData({"SNDK": frame})

    result = data.bars_1m(
        "SNDK",
        asof=frame.index[0].to_pydatetime() + timedelta(minutes=1),
        lookback_minutes=1,
    )

    pd.testing.assert_frame_equal(result, frame.iloc[:1], check_exact=True)
