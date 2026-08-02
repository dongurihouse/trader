from __future__ import annotations

from datetime import date

import pandas as pd
from pandas.testing import assert_frame_equal

from trader.contracts.testing import synthetic_day
from trader.provider.store import read_1d, read_1m_day, write_1d, write_1m_day


BAR_COLUMNS = ["o", "h", "l", "c", "v"]


def _canonical(frame: pd.DataFrame) -> pd.DataFrame:
    expected = frame.loc[:, BAR_COLUMNS].astype("float64").copy(deep=True)
    expected.index = pd.DatetimeIndex(expected.index).tz_convert("UTC")
    expected.index.freq = None
    expected.index.name = "t"
    return expected


def test_write_then_read_1m_day_uses_canonical_layout(tmp_path) -> None:
    day = date(2026, 7, 1)
    source = synthetic_day("SNDK", day, seed=1).head(3)

    write_1m_day(tmp_path, "SNDK", day, source)

    stored = read_1m_day(tmp_path, "SNDK", day)
    assert stored is not None
    assert_frame_equal(stored, _canonical(source))
    assert (tmp_path / "bars" / "1m" / "SNDK" / "2026-07-01.parquet").is_file()


def test_write_then_read_1d_uses_canonical_layout(tmp_path) -> None:
    index = pd.DatetimeIndex(
        ["2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"],
        name="day",
    )
    source = pd.DataFrame(
        {
            "o": [10, 11],
            "h": [12, 13],
            "l": [9, 10],
            "c": [11, 12],
            "v": [100, 200],
        },
        index=index,
    )

    write_1d(tmp_path, "SNDK", source)

    stored = read_1d(tmp_path, "SNDK")
    assert stored is not None
    assert_frame_equal(stored, _canonical(source))
    assert (tmp_path / "bars" / "1d" / "SNDK.parquet").is_file()


def test_missing_store_files_return_none(tmp_path) -> None:
    assert read_1m_day(tmp_path, "MISSING", date(2026, 7, 1)) is None
    assert read_1d(tmp_path, "MISSING") is None
