"""Point-in-time technical signals computed from provider-owned bar data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol

import pandas as pd

from trader.provider.calendar import MarketCalendarImpl


OPENING_RANGE_WINDOWS = (5, 15, 60)
RVOL_OPEN_WINDOW_BARS = 30
PEER_MIN_BARS = 5

PEER_COMPARABLE = ("MU",)
PEER_SECTOR = ("SOXX",)
PEER_MARKET = ("SPY", "QQQ")


@dataclass(frozen=True)
class SignalSpec:
    id: str
    description: str
    units: str


REGISTRY: dict[str, SignalSpec] = {}


def register(spec: SignalSpec) -> None:
    """Extend the public signal catalog with one uniquely named signal."""
    if spec.id in REGISTRY:
        raise ValueError(f"duplicate signal id: {spec.id}")
    REGISTRY[spec.id] = spec


def _feature(signal_id: str, description: str, units: str) -> SignalSpec:
    return SignalSpec(id=signal_id, description=description, units=units)


_SPECS = [
    # Clock.
    _feature(
        "minutes_since_open",
        "minutes elapsed from the regular-session open to this bar",
        "minutes",
    ),
    _feature(
        "minutes_to_close",
        "minutes remaining from this bar to the regular-session close "
        "(the actual close, so early-close days compress correctly)",
        "minutes",
    ),
    # Price and VWAP.
    _feature("price", "SNDK price at this bar's close", "USD"),
    _feature(
        "vwap",
        "session volume-weighted average price so far today, regular hours "
        "only, from typical price (high+low+close)/3",
        "USD",
    ),
    _feature(
        "vwap_sigma",
        "standard deviation of close-minus-VWAP over the session so far; "
        "reads 0.0 until more than 5 bars exist",
        "USD",
    ),
    _feature(
        "vwap_z",
        "how far the current close sits from VWAP, in session-sigma units; "
        "0.0 while vwap_sigma is 0",
        "sigma",
    ),
    _feature(
        "vwap_dist_pct",
        "unsigned distance from VWAP as a percent of VWAP -- how far 'away "
        "from fair' price is right now, ignoring which side. Reads 0.0 when "
        "VWAP itself is 0",
        "percent",
    ),
    _feature(
        "vwap_side_run_minutes",
        "how long price has stayed on one side of VWAP -- positive above, "
        "negative below. Counts BARS, so a session with missing interior "
        "minutes undercounts the true elapsed time",
        "bars",
    ),
]

for _window in OPENING_RANGE_WINDOWS:
    _SPECS.extend(
        [
            _feature(
                f"or{_window}_high",
                f"highest high of the first {_window} regular-hours bars of "
                f"the session; reads 0.0 until {_window} bars exist",
                "USD",
            ),
            _feature(
                f"or{_window}_low",
                f"lowest low of the first {_window} regular-hours bars of "
                f"the session; reads 0.0 until {_window} bars exist",
                "USD",
            ),
            _feature(
                f"or{_window}_range",
                f"high-to-low span of the first {_window} regular-hours bars; "
                f"reads 0.0 until {_window} bars exist, which is "
                "indistinguishable from a genuinely zero-width range",
                "USD",
            ),
        ]
    )

_SPECS.extend(
    [
        # Session structure.
        _feature(
            "day_high", "highest high of the regular session so far", "USD"
        ),
        _feature("day_low", "lowest low of the regular session so far", "USD"),
        _feature(
            "new_day_high",
            "1.0 when THIS bar's high is the session's running high",
            "boolean (0/1)",
        ),
        _feature(
            "new_day_low",
            "1.0 when THIS bar's low is the session's running low",
            "boolean (0/1)",
        ),
        _feature(
            "day_range_atr",
            "session high-to-low range so far, measured in daily-ATR units",
            "multiples of daily ATR",
        ),
        # Gap and prior session.
        _feature(
            "gap_pct",
            "opening gap: the regular-hours open against the prior session's "
            "close",
            "percent",
        ),
        _feature(
            "open_vs_prior_high",
            "regular-hours open minus the prior session's high; positive means "
            "the session opened above yesterday's range",
            "USD",
        ),
        _feature(
            "open_vs_prior_low",
            "regular-hours open minus the prior session's low; negative means "
            "the session opened below yesterday's range",
            "USD",
        ),
        _feature(
            "price_vs_prior_high",
            "current price minus the prior session's high",
            "USD",
        ),
        _feature(
            "price_vs_prior_low",
            "current price minus the prior session's low",
            "USD",
        ),
        _feature(
            "first_bar_ret",
            "the FIRST regular-hours bar's own return, its close against its "
            "open. Positive means the session's opening minute printed up. "
            "Exactly 0.0 is a genuinely directionless first bar, not a "
            "missing reading -- the first bar exists from the first feature "
            "computation onward. Named so 'break the opening range in the "
            "direction of the first bar' can be stated as a condition over a "
            "signal rather than arithmetic buried in one algo",
            "percent",
        ),
        _feature(
            "ret_from_open",
            "return from the regular-hours open to the current price",
            "percent",
        ),
        _feature(
            "day_ret_from_prev_close",
            "return from the prior session's close to the current price",
            "percent",
        ),
        _feature(
            "first30_ret",
            "return from the prior session's close to the 30th regular-hours "
            "bar's close; reads 0.0 until 30 bars exist",
            "percent",
        ),
        # Volume.
        _feature(
            "rvol_open_30m",
            "relative volume of the opening window: volume SO FAR this session "
            "against the prior-sessions volume baseline for the SAME elapsed "
            "window, capped at the first 30 bars. Before 30 bars have elapsed "
            "this is an elapsed-window reading (5 minutes vs the baseline's "
            "first 5 minutes), NOT a 30-minute one -- see rvol_open_30m_true "
            "for the strict version",
            "multiples of baseline volume",
        ),
        _feature(
            "rvol_open_30m_true",
            "the same opening-window relative volume measured over the FULL "
            "first 30 regular-hours bars; reads the documented neutral 0.0 "
            "until those 30 bars exist, then the true full-window value",
            "multiples of baseline volume",
        ),
        _feature(
            "rvol_slot",
            "this bar's volume against the prior-sessions baseline for the "
            "same minute-of-day slot",
            "multiples of baseline volume",
        ),
        _feature(
            "atr_day",
            "mean of up to the last 14 consecutive-session true ranges from "
            "daily bars, in return units (a fraction of price, not dollars); "
            "reads 0.0 when no true-range pair exists",
            "fraction of price",
        ),
        _feature(
            "atr_px",
            "the same daily average true range converted to dollars at the "
            "current price (atr_day x price) -- the unit every ATR-sized stop "
            "distance and range-size test is measured in",
            "USD",
        ),
        # Peers.
        _feature(
            "peer_above_vwap_count",
            "how many peer symbols (MU, SOXX, SPY, QQQ) are at or above their "
            "own session VWAP right now -- exactly at VWAP counts as above. "
            "Only peers whose bars were loaded and that have more than 5 bars "
            "so far are counted at all, so 0.0 means peers not confirming OR "
            "not observed",
            "count (0-4)",
        ),
        _feature(
            "peer_below_vwap_count",
            "how many peer symbols (MU, SOXX, SPY, QQQ) are STRICTLY below "
            "their own session VWAP right now. An unloaded or too-short peer "
            "is counted on neither side, so the two counts sum to the peers "
            "actually observed, not to 4",
            "count (0-4)",
        ),
        _feature(
            "comparable_ret_spread",
            "how far SNDK's return-since-open is running AHEAD of its "
            "comparable (MU), in percentage points. Positive means SNDK is "
            "outperforming the comparable; reads neutral 0.0 when MU is "
            "unobserved, so 0.0 means dead level OR no comparison possible",
            "percentage points",
        ),
        _feature(
            "sector_ret_spread",
            "SNDK's return-since-open minus the mean return-since-open of the "
            "sector proxy (SOXX), in percentage points. Positive means SNDK "
            "is outrunning the semiconductor sector. An unobserved sector "
            "reads neutral 0.0, not a flat sector",
            "percentage points",
        ),
        _feature(
            "market_ret_spread",
            "SNDK's return-since-open minus the mean return-since-open of the "
            "market proxies (SPY, QQQ), in percentage points. Positive means "
            "SNDK is moving on more than the broad tape. Neutral 0.0 when "
            "neither market proxy is observed",
            "percentage points",
        ),
        _feature(
            "idio_strength",
            "the part of SNDK's return-since-open that the sector and market "
            "are NOT explaining: SNDK's return minus the mean return-since-open "
            "pooled across SOXX, SPY, QQQ, equal weight per observed peer. "
            "This assumes beta = 1 and is a proxy, not a regression. Neutral "
            "0.0 when no sector or market peer is observed",
            "percentage points",
        ),
        _feature(
            "comparable_agrees",
            "1.0 when SNDK and its comparable (MU) are pointing the SAME way "
            "since the open -- both up or both down -- and 0.0 otherwise. "
            "Exactly flat agrees with nothing. Reads 0.0 when MU is "
            "unavailable, so 0.0 means not agreeing OR not observed",
            "boolean (0/1)",
        ),
    ]
)

for _spec in _SPECS:
    register(_spec)


class _SignalMarket(Protocol):
    def bars_1m(
        self,
        symbol: str,
        *,
        asof: datetime,
        lookback_minutes: int | None = None,
    ) -> pd.DataFrame: ...

    def bars_1d(
        self,
        symbol: str,
        *,
        asof: date,
        lookback_days: int,
    ) -> pd.DataFrame: ...

    def calendar(self) -> MarketCalendarImpl: ...


def _rth_slice(
    bars: pd.DataFrame,
    day: date,
    calendar: MarketCalendarImpl,
) -> pd.DataFrame:
    open_utc = datetime.combine(
        day,
        calendar.open_time,
        tzinfo=calendar.timezone,
    ).astimezone(timezone.utc)
    close_utc = calendar.session_close(day)
    return bars.loc[(bars.index >= open_utc) & (bars.index < close_utc)]


def _group_mean_return(
    symbols: tuple[str, ...], peer_returns: dict[str, float]
) -> float | None:
    observed = [peer_returns[symbol] for symbol in symbols if symbol in peer_returns]
    return sum(observed) / len(observed) if observed else None


def _daily_atr(market: _SignalMarket, symbol: str, day: date) -> float:
    daily = market.bars_1d(symbol, asof=day, lookback_days=15).sort_index()
    true_ranges: list[float] = []
    for position in range(1, len(daily)):
        previous_close = float(daily["c"].iloc[position - 1])
        high = float(daily["h"].iloc[position])
        low = float(daily["l"].iloc[position])
        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
            / previous_close
        )
    if not true_ranges:
        return 0.0
    return float(sum(true_ranges[-14:]) / len(true_ranges[-14:]))


def _slot_volume_baseline(
    visible_bars: pd.DataFrame,
    *,
    day: date,
    previous_day: date,
    previous_rth: pd.DataFrame,
    calendar: MarketCalendarImpl,
) -> pd.Series:
    sessions: dict[date, pd.DataFrame] = {previous_day: previous_rth}
    for historical_day in sorted(set(visible_bars.index.date)):
        if historical_day >= day or not calendar.is_session(historical_day):
            continue
        historical = _rth_slice(visible_bars, historical_day, calendar)
        if not historical.empty:
            sessions[historical_day] = historical
    slots = [
        session["v"].reset_index(drop=True)
        for session in sessions.values()
        if not session.empty
    ]
    return pd.concat(slots, axis=1).median(axis=1).astype("float64")


class SignalEngine:
    """Compute the complete technical feature set once for each symbol/as-of."""

    def __init__(self, market: _SignalMarket) -> None:
        self._market = market
        self._cache: dict[tuple[str, datetime], dict[str, float]] = {}
        self._session_days: dict[str, date] = {}

    def _prepare_session_cache(self, symbol: str, day: date) -> None:
        if self._session_days.get(symbol) == day:
            return
        self._cache = {
            key: value for key, value in self._cache.items() if key[0] != symbol
        }
        self._session_days[symbol] = day
        # Retaining only one date per symbol also bounds the cache to the
        # current session's distinct as-of timestamps.

    def compute_all(
        self,
        symbol: str,
        *,
        asof: datetime,
    ) -> dict[str, float]:
        day = asof.date()
        # Provider sessions and premarket both remain on one UTC calendar date
        # for America/New_York, so the contract deliberately uses asof.date().
        self._prepare_session_cache(symbol, day)
        cache_key = (symbol, asof)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        features = {signal_id: 0.0 for signal_id in REGISTRY}
        calendar = self._market.calendar()
        bars = self._market.bars_1m(symbol, asof=asof)
        rth = _rth_slice(bars, day, calendar)
        if rth.empty:
            self._cache[cache_key] = features
            return features.copy()

        previous_day = calendar.prev_session(day)
        if previous_day is None:
            raise ValueError(
                f"{symbol} {day.isoformat()}: no previous-session data is available"
            )
        previous_close_utc = calendar.session_close(previous_day)
        previous_bars = self._market.bars_1m(
            symbol,
            asof=previous_close_utc,
        )
        previous_rth = _rth_slice(previous_bars, previous_day, calendar)
        if previous_rth.empty:
            raise ValueError(
                f"{symbol} {day.isoformat()}: no previous-session data is available"
            )

        previous_close = float(previous_rth["c"].iloc[-1])
        previous_high = float(previous_rth["h"].max())
        previous_low = float(previous_rth["l"].min())
        slot_baseline = _slot_volume_baseline(
            bars,
            day=day,
            previous_day=previous_day,
            previous_rth=previous_rth,
            calendar=calendar,
        )

        timestamp = rth.index[-1]
        open_utc = datetime.combine(
            day,
            calendar.open_time,
            tzinfo=calendar.timezone,
        ).astimezone(timezone.utc)
        close_utc = calendar.session_close(day)
        minutes_since_open = int(
            (timestamp - open_utc).total_seconds() // 60
        )
        price = float(rth["c"].iloc[-1])

        features["minutes_since_open"] = float(minutes_since_open)
        features["minutes_to_close"] = float(
            (close_utc - timestamp).total_seconds() / 60
        )
        features["price"] = price

        typical_price = (rth["h"] + rth["l"] + rth["c"]) / 3
        cumulative_volume = rth["v"].cumsum()
        vwap_series = (typical_price * rth["v"]).cumsum() / cumulative_volume.replace(
            0, 1
        )
        vwap = float(vwap_series.iloc[-1])
        deviation = rth["c"] - vwap_series
        vwap_sigma = float(deviation.std(ddof=0)) if len(rth) > 5 else 0.0
        features["vwap"] = vwap
        features["vwap_sigma"] = vwap_sigma
        features["vwap_z"] = (
            float(deviation.iloc[-1] / vwap_sigma) if vwap_sigma else 0.0
        )
        features["vwap_dist_pct"] = (
            abs(price - vwap) / vwap * 100 if vwap else 0.0
        )

        side = rth["c"] >= vwap_series
        run = 0
        for above in reversed(side.tolist()):
            if above != bool(side.iloc[-1]):
                break
            run += 1
        features["vwap_side_run_minutes"] = float(
            run if bool(side.iloc[-1]) else -run
        )

        for window in OPENING_RANGE_WINDOWS:
            head = rth.iloc[:window]
            high_id = f"or{window}_high"
            low_id = f"or{window}_low"
            if len(rth) >= window:
                features[high_id] = float(head["h"].max())
                features[low_id] = float(head["l"].min())
            features[f"or{window}_range"] = (
                features[high_id] - features[low_id]
            )

        day_high = float(rth["h"].max())
        day_low = float(rth["l"].min())
        features["day_high"] = day_high
        features["day_low"] = day_low
        features["new_day_high"] = (
            1.0 if float(rth["h"].iloc[-1]) >= day_high else 0.0
        )
        features["new_day_low"] = (
            1.0 if float(rth["l"].iloc[-1]) <= day_low else 0.0
        )

        atr_day = _daily_atr(self._market, symbol, day)
        atr_px = atr_day * price
        features["atr_day"] = atr_day
        features["atr_px"] = atr_px
        features["day_range_atr"] = (
            (day_high - day_low) / atr_px if atr_px > 0 else 0.0
        )

        open_price = float(rth["o"].iloc[0])
        first_bar = rth.iloc[0]
        features["first_bar_ret"] = (
            float(first_bar["c"]) / float(first_bar["o"]) - 1
        ) * 100
        features["gap_pct"] = (open_price / previous_close - 1) * 100
        features["open_vs_prior_high"] = open_price - previous_high
        features["open_vs_prior_low"] = open_price - previous_low
        features["ret_from_open"] = (price / open_price - 1) * 100
        features["price_vs_prior_high"] = price - previous_high
        features["price_vs_prior_low"] = price - previous_low
        features["day_ret_from_prev_close"] = (
            price / previous_close - 1
        ) * 100
        if len(rth) >= RVOL_OPEN_WINDOW_BARS:
            features["first30_ret"] = (
                float(rth["c"].iloc[RVOL_OPEN_WINDOW_BARS - 1])
                / previous_close
                - 1
            ) * 100

        window = RVOL_OPEN_WINDOW_BARS
        observed_open_volume = float(rth["v"].iloc[:window].sum())
        expected_full_volume = float(
            slot_baseline.iloc[: min(window, len(slot_baseline))].sum()
        ) or 1.0
        full_window = len(rth) >= window
        if full_window:
            opening_rvol = observed_open_volume / expected_full_volume
        else:
            expected_elapsed_volume = float(
                slot_baseline.iloc[: len(rth)].sum()
            ) or 1.0
            opening_rvol = observed_open_volume / expected_elapsed_volume
        features["rvol_open_30m"] = float(opening_rvol)
        features["rvol_open_30m_true"] = (
            float(opening_rvol) if full_window else 0.0
        )
        slot = min(minutes_since_open, len(slot_baseline) - 1)
        expected_slot_volume = float(slot_baseline.iloc[slot]) or 1.0
        features["rvol_slot"] = float(
            float(rth["v"].iloc[-1]) / expected_slot_volume
        )

        above_count = 0
        below_count = 0
        peer_returns: dict[str, float] = {}
        peers = PEER_COMPARABLE + PEER_SECTOR + PEER_MARKET
        for peer_symbol in peers:
            peer_bars = self._market.bars_1m(peer_symbol, asof=asof)
            peer_bars = peer_bars.loc[peer_bars.index <= timestamp]
            peer_rth = _rth_slice(peer_bars, day, calendar)
            if len(peer_rth) <= PEER_MIN_BARS:
                continue
            peer_typical_price = (
                peer_rth["h"] + peer_rth["l"] + peer_rth["c"]
            ) / 3
            peer_vwap = (
                (peer_typical_price * peer_rth["v"]).cumsum()
                / peer_rth["v"].cumsum().replace(0, 1)
            )
            if float(peer_rth["c"].iloc[-1]) >= float(peer_vwap.iloc[-1]):
                above_count += 1
            else:
                below_count += 1
            peer_open = float(peer_rth["o"].iloc[0])
            if peer_open:
                peer_returns[peer_symbol] = (
                    float(peer_rth["c"].iloc[-1]) / peer_open - 1
                ) * 100

        features["peer_above_vwap_count"] = float(above_count)
        features["peer_below_vwap_count"] = float(below_count)
        own_return = features["ret_from_open"]
        comparable_return = _group_mean_return(PEER_COMPARABLE, peer_returns)
        sector_return = _group_mean_return(PEER_SECTOR, peer_returns)
        market_return = _group_mean_return(PEER_MARKET, peer_returns)
        residual_return = _group_mean_return(
            PEER_SECTOR + PEER_MARKET,
            peer_returns,
        )
        features["comparable_ret_spread"] = (
            own_return - comparable_return
            if comparable_return is not None
            else 0.0
        )
        features["sector_ret_spread"] = (
            own_return - sector_return if sector_return is not None else 0.0
        )
        features["market_ret_spread"] = (
            own_return - market_return if market_return is not None else 0.0
        )
        features["idio_strength"] = (
            own_return - residual_return
            if residual_return is not None
            else 0.0
        )
        features["comparable_agrees"] = (
            1.0
            if comparable_return is not None
            and (
                (own_return > 0 and comparable_return > 0)
                or (own_return < 0 and comparable_return < 0)
            )
            else 0.0
        )

        normalized = {
            signal_id: float(value) for signal_id, value in features.items()
        }
        self._cache[cache_key] = normalized
        return normalized.copy()

    def get(
        self,
        name: str,
        *,
        symbol: str,
        asof: datetime,
        params: Mapping[str, object] | None = None,
    ) -> float:
        if name not in REGISTRY:
            raise KeyError(f"unregistered signal: {name}")
        del params
        return self.compute_all(symbol, asof=asof)[name]


_DOC_HEADER = """# Signal Catalog

Generated from `src/trader/provider/signals.py`; do not edit this file by hand.

This catalog is scoped to the technical signals supported by stored market bars under
`architecture.md` section 8. Calendar and event signals land in a later provider task
and regenerate this same file.
"""


def render_markdown() -> str:
    """Render the public signal catalog as a Markdown table."""
    lines = [
        _DOC_HEADER.rstrip(),
        "",
        "| signal | units | description |",
        "| --- | --- | --- |",
    ]
    for spec in REGISTRY.values():
        description = " ".join(spec.description.split())
        lines.append(f"| `{spec.id}` | {spec.units} | {description} |")
    lines.append("")
    return "\n".join(lines)


def _repository_root() -> Path:
    source = Path(__file__).resolve()
    for candidate in source.parents:
        if (candidate / "src" / "trader" / "provider" / "signals.py") == source:
            return candidate
    raise RuntimeError(f"could not locate repository root from {source}")


def write_doc(path: Path | None = None) -> str:
    """Write the rendered catalog to ``docs/signals.md`` or an explicit path."""
    target = Path(path) if path is not None else _repository_root() / "docs" / "signals.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(), encoding="utf-8")
    return str(target)


__all__ = [
    "OPENING_RANGE_WINDOWS",
    "PEER_COMPARABLE",
    "PEER_MARKET",
    "PEER_MIN_BARS",
    "PEER_SECTOR",
    "REGISTRY",
    "RVOL_OPEN_WINDOW_BARS",
    "SignalEngine",
    "SignalSpec",
    "register",
    "render_markdown",
    "write_doc",
]
