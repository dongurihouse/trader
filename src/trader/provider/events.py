"""Point-in-time readers for earnings calendars and options captures."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

from trader.contracts.market import MarketCalendar
from trader.provider.signals import SignalSpec, register


STRADDLE_EXPECTED_MOVE_FACTOR = 0.85
DAYS_PER_YEAR = 365.0
EXPECTED_ABS_MOVE_FACTOR = math.sqrt(2 / math.pi)

REQUIRED_CAPTURE_KEYS = (
    "captured_at",
    "underlying",
    "spot",
    "expiration",
    "strike",
    "call",
    "put",
)
REQUIRED_LEG_KEYS = ("mark", "iv")
LEGS = ("call", "put")

REQUIRED_EARNINGS_EVENT_KEYS = ("symbol", "date", "timing")
EARNINGS_TIMINGS = frozenset({"am", "pm", "unknown"})


for _spec in (
    SignalSpec(
        id="days_to_earnings",
        units="calendar days (999.0 = no future event known)",
        description=(
            "calendar-day distance to the primary symbol's next known earnings "
            "report; day-constant, with 999.0 meaning no future event is known"
        ),
    ),
    SignalSpec(
        id="peer_earnings_reaction",
        units="boolean (0/1)",
        description=(
            "1.0 on the first tradeable reaction session for a peer earnings "
            "report: same-session for an AM report on a session, otherwise the "
            "next session; day-constant"
        ),
    ),
    SignalSpec(
        id="implied_move_pct",
        units="fraction of spot",
        description=(
            "the 0.85x-haircut expected move to the most recent eligible options "
            "capture's expiration; day-constant, with 0.0 meaning no capture is "
            "available for this session, never no expected move"
        ),
    ),
):
    register(_spec)


def implied_move_straddle(
    spot: float,
    call_mark: float,
    put_mark: float,
) -> float:
    """Return the raw ATM straddle as a fraction of spot."""
    if spot <= 0:
        raise ValueError(f"implied move: spot must be > 0, got {spot!r}")
    if call_mark < 0:
        raise ValueError(
            f"implied move: call mark must be >= 0, got {call_mark!r}"
        )
    if put_mark < 0:
        raise ValueError(
            f"implied move: put mark must be >= 0, got {put_mark!r}"
        )
    return (call_mark + put_mark) / spot


def implied_move_iv(iv: float, days_to_expiry: float) -> float:
    """Return the expected absolute move implied by annualized IV."""
    if iv <= 0:
        raise ValueError(f"implied move: iv must be > 0, got {iv!r}")
    if days_to_expiry <= 0:
        raise ValueError(
            "implied move: days_to_expiry must be > 0, got "
            f"{days_to_expiry!r}"
        )
    return (
        iv
        * math.sqrt(days_to_expiry / DAYS_PER_YEAR)
        * EXPECTED_ABS_MOVE_FACTOR
    )


def _parse_capture_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(
            f"options capture: {field} must be an ISO string, got {value!r}"
        )
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"options capture: {field} is not a parseable date ({value!r})"
        ) from None


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"options capture: {field} must be a number, got {value!r}"
        )
    return float(value)


def validate_capture(capture: dict) -> dict:
    """Validate one hand-taken capture and return its parsed metric inputs."""
    if not isinstance(capture, dict):
        raise ValueError(
            "options capture: expected an object, got "
            f"{type(capture).__name__}"
        )
    for key in REQUIRED_CAPTURE_KEYS:
        if key not in capture:
            raise ValueError(
                f"options capture: missing required field {key!r}"
            )
    for leg in LEGS:
        if not isinstance(capture[leg], dict):
            raise ValueError(
                f"options capture: {leg} must be an object with "
                f"{list(REQUIRED_LEG_KEYS)}, got {capture[leg]!r}"
            )
        for key in REQUIRED_LEG_KEYS:
            if key not in capture[leg]:
                raise ValueError(
                    "options capture: missing required field "
                    f"'{leg}.{key}'"
                )

    spot = _number(capture["spot"], "spot")
    if spot <= 0:
        raise ValueError(f"options capture: spot must be > 0, got {spot!r}")

    marks: dict[str, float] = {}
    ivs: dict[str, float] = {}
    for leg in LEGS:
        mark = _number(capture[leg]["mark"], f"{leg}.mark")
        if mark <= 0:
            raise ValueError(
                f"options capture: {leg}.mark must be > 0, got {mark!r}"
            )
        iv = _number(capture[leg]["iv"], f"{leg}.iv")
        if iv <= 0:
            raise ValueError(
                f"options capture: {leg}.iv must be > 0, got {iv!r}"
            )
        marks[leg] = mark
        ivs[leg] = iv

    captured_date = _parse_capture_date(capture["captured_at"], "captured_at")
    expiration_date = _parse_capture_date(capture["expiration"], "expiration")
    if expiration_date < captured_date:
        raise ValueError(
            f"options capture: expiration {expiration_date} is before the "
            f"captured_at date {captured_date} -- the contract had already "
            "expired when the quote was taken"
        )
    return {
        "captured_date": captured_date,
        "expiration_date": expiration_date,
        "spot": spot,
        "call_mark": marks["call"],
        "put_mark": marks["put"],
        "call_iv": ivs["call"],
        "put_iv": ivs["put"],
    }


def capture_metrics(capture: dict) -> dict:
    """Return both price- and IV-derived metrics for a validated capture."""
    validated = validate_capture(capture)
    straddle = validated["call_mark"] + validated["put_mark"]
    straddle_pct = implied_move_straddle(
        validated["spot"],
        validated["call_mark"],
        validated["put_mark"],
    )
    expected_move_pct = straddle_pct * STRADDLE_EXPECTED_MOVE_FACTOR
    iv_mean = (validated["call_iv"] + validated["put_iv"]) / 2
    days = (
        validated["expiration_date"] - validated["captured_date"]
    ).days
    iv_expected_move_pct = implied_move_iv(iv_mean, days) if days > 0 else 0.0
    return {
        "straddle": straddle,
        "straddle_pct": straddle_pct,
        "expected_move_pct": expected_move_pct,
        "iv_mean": iv_mean,
        "iv_expected_move_pct": iv_expected_move_pct,
        "days_to_expiry": days,
        "method_spread_pct": abs(expected_move_pct - iv_expected_move_pct),
    }


def load_captures(data_root: Path) -> list[dict]:
    """Load validated captures, returning an empty list for a missing store."""
    directory = Path(data_root) / "events" / "options"
    if not directory.exists():
        return []

    captures = []
    for path in sorted(directory.glob("*.json")):
        try:
            capture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(
                f"options capture file {path.name} is not valid JSON: {error}"
            ) from None
        try:
            validate_capture(capture)
        except ValueError as error:
            raise ValueError(
                f"options capture file {path.name}: {error}"
            ) from None
        captures.append(capture)
    return sorted(captures, key=lambda capture: capture["captured_at"])


def latest_for_session(day: date, captures: list[dict]) -> dict | None:
    """Return the latest capture taken by and unexpired on ``day``."""
    eligible = []
    for capture in captures:
        validated = validate_capture(capture)
        if (
            validated["captured_date"]
            <= day
            <= validated["expiration_date"]
        ):
            eligible.append((validated["captured_date"], capture))
    if not eligible:
        return None
    return max(eligible, key=lambda pair: pair[0])[1]


def implied_move(data_root: Path, *, asof: datetime) -> dict | None:
    """Return metrics from the latest options capture eligible on as-of day."""
    capture = latest_for_session(asof.date(), load_captures(data_root))
    return capture_metrics(capture) if capture is not None else None


def _validate_earnings_calendar(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError(
            "earnings calendar: expected an object, got "
            f"{type(data).__name__}"
        )
    if "events" not in data:
        raise ValueError("earnings calendar: missing required field 'events'")
    if not isinstance(data["events"], list):
        raise ValueError("earnings calendar: events must be a list")

    for position, event in enumerate(data["events"]):
        if not isinstance(event, dict):
            raise ValueError(
                f"earnings calendar: event {position} must be an object"
            )
        for key in REQUIRED_EARNINGS_EVENT_KEYS:
            if key not in event:
                raise ValueError(
                    f"earnings calendar: event {position} missing required "
                    f"field {key!r}"
                )
        try:
            date.fromisoformat(event["date"])
        except (TypeError, ValueError):
            raise ValueError(
                f"earnings calendar: event {position} date is not a parseable "
                f"ISO date ({event['date']!r})"
            ) from None
        if event["timing"] not in EARNINGS_TIMINGS:
            raise ValueError(
                f"earnings calendar: event {position} timing must be one of "
                f"{sorted(EARNINGS_TIMINGS)}, got {event['timing']!r}"
            )
    return data


def load_earnings_calendar(data_root: Path) -> dict | None:
    """Load the known earnings calendar, or ``None`` when it is absent."""
    path = Path(data_root) / "events" / "earnings.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"earnings calendar file {path.name} is not valid JSON: {error}"
        ) from None
    try:
        return _validate_earnings_calendar(data)
    except ValueError as error:
        raise ValueError(f"earnings calendar file {path.name}: {error}") from None


def _next_session_after(
    calendar: MarketCalendar,
    day: date,
    *,
    max_days: int = 10,
) -> date | None:
    """Return the first market session after ``day`` within ``max_days``."""
    for offset in range(1, max_days + 1):
        candidate = day + timedelta(days=offset)
        if calendar.is_session(candidate):
            return candidate
    return None


def is_reaction_day(
    events: dict,
    day: date,
    calendar: MarketCalendar,
    *,
    exclude_symbol: str,
) -> bool:
    """Whether ``day`` is the first tradeable reaction day for any peer."""
    for event in events["events"]:
        if event["symbol"] == exclude_symbol:
            continue
        report_day = date.fromisoformat(event["date"])
        if event["timing"] == "am" and calendar.is_session(report_day):
            reaction_day = report_day
        else:
            reaction_day = _next_session_after(calendar, report_day)
        if reaction_day == day:
            return True
    return False


def earnings_proximity(
    data_root: Path,
    calendar: MarketCalendar,
    primary_symbol: str,
    *,
    asof: datetime,
) -> dict | None:
    """Return primary earnings distance and peer reaction state for the day."""
    data = load_earnings_calendar(data_root)
    if data is None:
        return None

    day = asof.date()
    future_events = [
        event
        for event in data["events"]
        if event["symbol"] == primary_symbol
        and date.fromisoformat(event["date"]) >= day
    ]
    next_event = min(
        future_events,
        key=lambda event: date.fromisoformat(event["date"]),
        default=None,
    )
    next_date = next_event["date"] if next_event is not None else None
    return {
        "primary_symbol": primary_symbol,
        "next_date": next_date,
        "timing": next_event["timing"] if next_event is not None else None,
        "days_to_earnings": (
            float((date.fromisoformat(next_date) - day).days)
            if next_date is not None
            else None
        ),
        "peer_reaction_today": is_reaction_day(
            data,
            day,
            calendar,
            exclude_symbol=primary_symbol,
        ),
    }


__all__ = [
    "DAYS_PER_YEAR",
    "EXPECTED_ABS_MOVE_FACTOR",
    "STRADDLE_EXPECTED_MOVE_FACTOR",
    "capture_metrics",
    "earnings_proximity",
    "implied_move",
    "implied_move_iv",
    "implied_move_straddle",
    "is_reaction_day",
    "latest_for_session",
    "load_captures",
    "load_earnings_calendar",
    "validate_capture",
]
