"""Versioned YAML-backed market calendar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


@dataclass(frozen=True)
class MarketCalendarImpl:
    timezone: ZoneInfo
    open_time: time
    close_time: time
    early_close_time: time
    start_date: date
    end_date: date
    holidays: frozenset[date]
    early_closes: frozenset[date]

    def is_session(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def session_close(self, day: date) -> datetime:
        if not self.is_session(day):
            raise ValueError(f"{day.isoformat()} is not a market session")

        close_time = (
            self.early_close_time if day in self.early_closes else self.close_time
        )
        local_close = datetime.combine(day, close_time, tzinfo=self.timezone)
        return local_close.astimezone(timezone.utc)

    def prev_session(self, day: date) -> date | None:
        if day <= self.start_date:
            return None

        candidate = min(day - timedelta(days=1), self.end_date)
        while candidate >= self.start_date:
            if self.is_session(candidate):
                return candidate
            if candidate == self.start_date:
                break
            candidate -= timedelta(days=1)
        return None


def _parse_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_calendar(path: Path) -> MarketCalendarImpl:
    """Load a market calendar from versioned YAML configuration."""
    with Path(path).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)

    return MarketCalendarImpl(
        timezone=ZoneInfo(config["timezone"]),
        open_time=time.fromisoformat(config["open_time"]),
        close_time=time.fromisoformat(config["close_time"]),
        early_close_time=time.fromisoformat(config["early_close_time"]),
        start_date=_parse_date(config["start_date"]),
        end_date=_parse_date(config["end_date"]),
        holidays=frozenset(_parse_date(day) for day in config["holidays"]),
        early_closes=frozenset(
            _parse_date(day) for day in config["early_closes"]
        ),
    )


__all__ = ["MarketCalendarImpl", "load_calendar"]
