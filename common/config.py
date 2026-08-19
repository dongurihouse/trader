"""Shared config parsing and market-session rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from common.validation import normalize_symbol, require_clock, require_int


EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc


@dataclass(frozen=True)
class MarketSchedule:
    live_start: time
    regular_open: time
    regular_close: time
    early_close: time
    early_close_days: frozenset[date]
    after_close_minutes: int

    def close_for(self, day: date) -> time:
        return (
            self.early_close
            if day in self.early_close_days
            else self.regular_close
        )

    def session_window(self, day: date) -> tuple[int, int]:
        start = datetime.combine(day, self.regular_open, tzinfo=EASTERN)
        end = datetime.combine(day, self.close_for(day), tzinfo=EASTERN)
        return int(start.timestamp()), int(end.timestamp())

    def collection_window(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, self.live_start, tzinfo=EASTERN)
        end = datetime.combine(day, self.close_for(day), tzinfo=EASTERN) + timedelta(
            minutes=self.after_close_minutes
        )
        return start.astimezone(UTC), end.astimezone(UTC)


def read_config(path: Path, error: type[Exception]) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise error("config file does not exist: %s" % path) from exc
    except json.JSONDecodeError as exc:
        raise error("invalid JSON in %s: %s" % (path, exc)) from exc
    if not isinstance(document, dict):
        raise error("config must be a JSON object")
    return document


def configured_tickers(
    document: Mapping[str, Any], error: type[Exception]
) -> tuple[str, ...]:
    values = document.get("tickers")
    if not isinstance(values, list) or not values:
        raise error("tickers must be a non-empty list")
    result: list[str] = []
    for value in values:
        ticker = normalize_symbol(value, error=error)
        if ticker not in result:
            result.append(ticker)
    return tuple(result)


def resolve_config_path(
    document: Mapping[str, Any],
    name: str,
    config_path: Path,
    default: str,
    error: type[Exception],
) -> Path:
    value = document.get(name, default)
    if not isinstance(value, str) or not value:
        raise error("%s must be a path string" % name)
    path = Path(value).expanduser()
    return (
        path.resolve()
        if path.is_absolute()
        else (config_path.parent / path).resolve()
    )


def market_schedule(
    document: Mapping[str, Any], error: type[Exception]
) -> MarketSchedule:
    polling = document.get("live_polling", {})
    if not isinstance(polling, dict):
        raise error("live_polling must be an object")
    early_closes = document.get("early_closes", [])
    if not isinstance(early_closes, list):
        raise error("early_closes must be a list")
    try:
        early_close_days = frozenset(
            date.fromisoformat(value) for value in early_closes
        )
    except (TypeError, ValueError) as exc:
        raise error("early_closes must contain YYYY-MM-DD strings") from exc
    return MarketSchedule(
        live_start=require_clock(
            polling.get("start", "04:00"), "live_polling.start", error=error
        ),
        regular_open=require_clock(
            polling.get("regular_open", "09:30"),
            "live_polling.regular_open",
            error=error,
        ),
        regular_close=require_clock(
            polling.get("regular_close", "16:00"),
            "live_polling.regular_close",
            error=error,
        ),
        early_close=require_clock(
            polling.get("early_close", "13:00"),
            "live_polling.early_close",
            error=error,
        ),
        early_close_days=early_close_days,
        after_close_minutes=require_int(
            polling.get("after_close_minutes", 240),
            "live_polling.after_close_minutes",
            minimum=0,
            error=error,
        ),
    )
