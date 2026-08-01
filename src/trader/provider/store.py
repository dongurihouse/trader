"""Parquet storage primitives for provider bar data."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd


_BAR_COLUMNS = ["o", "h", "l", "c", "v"]


def _canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    stored = frame.loc[:, _BAR_COLUMNS].astype("float64").copy(deep=True)
    stored.index = pd.DatetimeIndex(pd.to_datetime(stored.index, utc=True), name="t")
    return stored


def _path_1m_day(data_root: Path, symbol: str, day: date) -> Path:
    return Path(data_root) / "bars" / "1m" / symbol / f"{day.isoformat()}.parquet"


def _path_1d(data_root: Path, symbol: str) -> Path:
    return Path(data_root) / "bars" / "1d" / f"{symbol}.parquet"


def read_1m_day(
    data_root: Path, symbol: str, day: date
) -> pd.DataFrame | None:
    path = _path_1m_day(data_root, symbol, day)
    try:
        return pd.read_parquet(path)
    except FileNotFoundError:
        return None


def write_1m_day(
    data_root: Path,
    symbol: str,
    day: date,
    frame: pd.DataFrame,
) -> None:
    path = _path_1m_day(data_root, symbol, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    _canonical_frame(frame).to_parquet(path)


def read_1d(data_root: Path, symbol: str) -> pd.DataFrame | None:
    path = _path_1d(data_root, symbol)
    try:
        return pd.read_parquet(path)
    except FileNotFoundError:
        return None


def write_1d(data_root: Path, symbol: str, frame: pd.DataFrame) -> None:
    path = _path_1d(data_root, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    _canonical_frame(frame).to_parquet(path)


__all__ = ["read_1d", "read_1m_day", "write_1d", "write_1m_day"]
