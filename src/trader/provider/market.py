"""Point-in-time market data backed by the provider parquet store."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from trader.contracts.errors import LookaheadError
from trader.contracts.market import MarketCalendar
from trader.provider.calendar import load_calendar
from trader.provider.signals import SignalEngine
from trader.provider.store import read_1d, read_1m_day


_BAR_COLUMNS = ["o", "h", "l", "c", "v"]
_ONE_MINUTE = timedelta(minutes=1)

# This is a safety bound for calendar-free file walking, not a market-history
# limit. A later provider task will use the calendar to traverse real gaps.
MAX_1M_WALKBACK_DAYS = 10


def _empty_bar_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex([], dtype="datetime64[ns, UTC]", name="t")
    return pd.DataFrame(
        {column: pd.Series(dtype="float64") for column in _BAR_COLUMNS},
        index=index,
    )


class ProviderMarketData:
    def __init__(
        self,
        data_root: Path,
        *,
        calendar_path: Path | None = None,
        primary_symbol: str = "SNDK",
    ) -> None:
        self._data_root = Path(data_root)
        self._calendar = (
            load_calendar(calendar_path) if calendar_path is not None else None
        )
        self._primary_symbol = primary_symbol
        self._signals = SignalEngine(self)

    def _latest_1m_completion(self, symbol: str) -> datetime | None:
        symbol_dir = self._data_root / "bars" / "1m" / symbol
        for path in sorted(symbol_dir.glob("*.parquet"), reverse=True):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            frame = read_1m_day(self._data_root, symbol, day)
            if frame is not None and not frame.empty:
                return frame.index.max() + _ONE_MINUTE
        return None

    def bars_1m(
        self,
        symbol: str,
        *,
        asof: datetime,
        lookback_minutes: int | None = None,
    ) -> pd.DataFrame:
        frames = []
        for offset in range(MAX_1M_WALKBACK_DAYS):
            day = asof.date() - timedelta(days=offset)
            frame = read_1m_day(self._data_root, symbol, day)
            if frame is not None and not frame.empty:
                frames.append(frame)

        last_completion = self._latest_1m_completion(symbol)
        if last_completion is None:
            return _empty_bar_frame()
        if asof > last_completion:
            raise LookaheadError(
                f"{symbol} minute data is known only through {last_completion}"
            )

        if not frames:
            return _empty_bar_frame()

        combined = pd.concat(frames).sort_index()
        visible = combined.loc[combined.index + _ONE_MINUTE <= asof]
        if lookback_minutes is not None:
            visible = visible.tail(lookback_minutes)
        return visible.copy(deep=True)

    def bars_1d(
        self,
        symbol: str,
        *,
        asof: date,
        lookback_days: int,
    ) -> pd.DataFrame:
        frame = read_1d(self._data_root, symbol)
        if frame is None or frame.empty:
            return _empty_bar_frame()

        frame = frame.sort_index()
        frame_days = frame.index.date
        last_known_day = max(frame_days)
        if asof > last_known_day + timedelta(days=1):
            raise LookaheadError(
                f"{symbol} daily data is known only through {last_known_day}"
            )

        eligible = frame.loc[[day < asof for day in frame_days]]
        return eligible.tail(lookback_days).copy(deep=True)

    def calendar(self) -> MarketCalendar:
        if self._calendar is None:
            raise NotImplementedError("wired by a later provider task")
        return self._calendar

    def signal(
        self,
        name: str,
        *,
        asof: datetime,
        params: Mapping[str, object] | None = None,
    ) -> float:
        if self._calendar is None:
            raise RuntimeError(
                "signal() requires a calendar; provide calendar_path at construction"
            )
        return self._signals.get(
            name,
            symbol=self._primary_symbol,
            asof=asof,
            params=params,
        )

    def event(self, kind: str, *, asof: datetime) -> dict | None:
        raise NotImplementedError("wired by a later provider task")


__all__ = ["ProviderMarketData"]
