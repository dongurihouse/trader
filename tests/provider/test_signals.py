from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from trader.contracts.errors import LookaheadError
from trader.provider.market import ProviderMarketData
from trader.provider.signals import REGISTRY, render_markdown, write_doc
from trader.provider.store import write_1d, write_1m_day


CALENDAR_PATH = Path(__file__).resolve().parents[2] / "config" / "calendar.yaml"
DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "signals.md"
SIGNAL_IDS = (
    "minutes_since_open",
    "minutes_to_close",
    "price",
    "vwap",
    "vwap_sigma",
    "vwap_z",
    "vwap_dist_pct",
    "vwap_side_run_minutes",
    "or5_high",
    "or5_low",
    "or5_range",
    "or15_high",
    "or15_low",
    "or15_range",
    "or60_high",
    "or60_low",
    "or60_range",
    "day_high",
    "day_low",
    "new_day_high",
    "new_day_low",
    "day_range_atr",
    "gap_pct",
    "open_vs_prior_high",
    "open_vs_prior_low",
    "price_vs_prior_high",
    "price_vs_prior_low",
    "first_bar_ret",
    "ret_from_open",
    "day_ret_from_prev_close",
    "first30_ret",
    "rvol_open_30m",
    "rvol_open_30m_true",
    "rvol_slot",
    "atr_day",
    "atr_px",
    "peer_above_vwap_count",
    "peer_below_vwap_count",
    "comparable_ret_spread",
    "sector_ret_spread",
    "market_ret_spread",
    "idio_strength",
    "comparable_agrees",
)
EXCLUDED_SIGNAL_IDS = (
    "sector_bias",
    "market_bias",
    "bias_confidence",
    "catalyst_present",
    "catalyst_grade",
    "news_volume_z",
    "news_tone_mean",
    "news_tone_dispersion",
    "news_tone_shift",
    "nbbo_spread_bps",
    "l2_imbalance",
    "days_to_earnings",
    "peer_earnings_reaction",
    "implied_move_pct",
)


def _minute_frame(
    day: date,
    count: int,
    *,
    start_price: float = 100.0,
    end_price: float | None = None,
    volume: float = 100.0,
    positions: list[int] | None = None,
) -> pd.DataFrame:
    if positions is None:
        positions = list(range(count))
    assert len(positions) == count
    session_open = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    index = pd.DatetimeIndex(
        [session_open + timedelta(minutes=position) for position in positions],
        name="t",
    )
    if end_price is None:
        end_price = start_price + count / 10
    if count == 1:
        closes = [end_price]
    else:
        step = (end_price - start_price) / (count - 1)
        closes = [start_price + step * position for position in range(count)]
    opens = [start_price, *closes[:-1]]
    return pd.DataFrame(
        {
            "o": opens,
            "h": [max(open_px, close) + 0.25 for open_px, close in zip(opens, closes)],
            "l": [min(open_px, close) - 0.25 for open_px, close in zip(opens, closes)],
            "c": closes,
            "v": [volume] * count,
        },
        index=index,
        dtype="float64",
    )


def _write_primary_history(
    root: Path,
    *,
    current_day: date,
    current: pd.DataFrame,
    prior: pd.DataFrame | None = None,
) -> None:
    if prior is None:
        prior_day = current_day - timedelta(days=1)
        prior = _minute_frame(prior_day, 60, start_price=95.0, volume=100.0)
    write_1m_day(root, "SNDK", prior.index[0].date(), prior)
    write_1m_day(root, "SNDK", current_day, current)


def _market(root: Path, *, primary_symbol: str = "SNDK") -> ProviderMarketData:
    return ProviderMarketData(
        root,
        calendar_path=CALENDAR_PATH,
        primary_symbol=primary_symbol,
    )


@pytest.mark.parametrize("completed_bars", [5, 15, 60])
def test_pit_signals_equal_the_same_computation_on_manually_truncated_bars(
    tmp_path: Path, completed_bars: int
) -> None:
    day = date(2026, 7, 2)
    prior = _minute_frame(date(2026, 7, 1), 60, start_price=95.0, volume=80.0)
    full_current = _minute_frame(day, 60, start_price=101.0, volume=120.0)
    full_root = tmp_path / "full"
    truncated_root = tmp_path / f"truncated-{completed_bars}"
    _write_primary_history(
        full_root, current_day=day, current=full_current, prior=prior
    )
    _write_primary_history(
        truncated_root,
        current_day=day,
        current=full_current.head(completed_bars),
        prior=prior,
    )
    asof = full_current.index[completed_bars - 1].to_pydatetime() + timedelta(
        minutes=1
    )

    full_market = _market(full_root)
    truncated_market = _market(truncated_root)

    for name in ("vwap", "or5_high", "rvol_slot"):
        assert full_market.signal(name, asof=asof) == pytest.approx(
            truncated_market.signal(name, asof=asof)
        )


def test_signal_propagates_lookahead_error_from_primary_minute_bars(
    tmp_path: Path,
) -> None:
    day = date(2026, 7, 2)
    current = _minute_frame(day, 10)
    _write_primary_history(tmp_path, current_day=day, current=current)
    after_stored_data = current.index[-1].to_pydatetime() + timedelta(minutes=2)

    with pytest.raises(LookaheadError, match="SNDK minute data"):
        _market(tmp_path).signal("price", asof=after_stored_data)


def test_unregistered_signal_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="not_registered"):
        _market(tmp_path).signal(
            "not_registered",
            asof=datetime(2026, 7, 2, 13, 31, tzinfo=timezone.utc),
        )


def test_two_signal_reads_at_one_asof_share_the_memoized_computation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = date(2026, 7, 2)
    current = _minute_frame(day, 10)
    _write_primary_history(tmp_path, current_day=day, current=current)
    market = _market(tmp_path)
    original = market.bars_1m
    calls = 0

    def counted_bars_1m(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(market, "bars_1m", counted_bars_1m)
    asof = current.index[-1].to_pydatetime() + timedelta(minutes=1)

    market.signal("price", asof=asof)
    calls_after_first_signal = calls
    market.signal("vwap", asof=asof, params={"unused": 123})

    assert calls_after_first_signal > 0
    assert calls == calls_after_first_signal


def test_signal_cache_is_invalidated_when_the_symbol_moves_to_a_new_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day_0 = date(2026, 6, 30)
    day_1 = date(2026, 7, 1)
    day_2 = date(2026, 7, 2)
    write_1m_day(tmp_path, "SNDK", day_0, _minute_frame(day_0, 10, start_price=90.0))
    first = _minute_frame(day_1, 10, start_price=100.0)
    second = _minute_frame(day_2, 10, start_price=200.0)
    write_1m_day(tmp_path, "SNDK", day_1, first)
    write_1m_day(tmp_path, "SNDK", day_2, second)
    market = _market(tmp_path)
    original = market.bars_1m
    calls = 0

    def counted_bars_1m(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(market, "bars_1m", counted_bars_1m)
    asof_1 = first.index[-1].to_pydatetime() + timedelta(minutes=1)
    asof_2 = second.index[-1].to_pydatetime() + timedelta(minutes=1)

    first_price = market.signal("price", asof=asof_1)
    calls_after_day_1 = calls
    second_price = market.signal("price", asof=asof_2)
    calls_after_day_2 = calls
    market.signal("price", asof=asof_1)

    assert first_price == pytest.approx(101.0)
    assert second_price == pytest.approx(201.0)
    assert calls_after_day_2 > calls_after_day_1
    assert calls > calls_after_day_2


def test_true_opening_rvol_is_neutral_until_all_30_bars_exist(
    tmp_path: Path,
) -> None:
    day = date(2026, 7, 2)
    prior = _minute_frame(date(2026, 7, 1), 60, volume=100.0)
    current = _minute_frame(day, 30, volume=200.0)
    _write_primary_history(tmp_path, current_day=day, current=current, prior=prior)
    market = _market(tmp_path)
    before_window = current.index[28].to_pydatetime() + timedelta(minutes=1)
    at_window = current.index[29].to_pydatetime() + timedelta(minutes=1)

    assert market.signal("rvol_open_30m_true", asof=before_window) == 0.0
    assert market.signal("rvol_open_30m", asof=at_window) == pytest.approx(2.0)
    assert market.signal("rvol_open_30m_true", asof=at_window) == pytest.approx(2.0)


def test_daily_atr_price_and_session_range_use_known_true_range_sequence(
    tmp_path: Path,
) -> None:
    day = date(2026, 7, 2)
    prior = _minute_frame(date(2026, 7, 1), 10, start_price=99.0)
    current = _minute_frame(day, 2, start_price=120.0, end_price=123.0)
    _write_primary_history(tmp_path, current_day=day, current=current, prior=prior)
    daily = pd.DataFrame(
        {
            "o": [98.0, 101.0, 106.0],
            "h": [108.0, 112.0, 120.0],
            "l": [94.0, 95.0, 100.0],
            "c": [100.0, 105.0, 110.0],
            "v": [1_000.0, 1_100.0, 1_200.0],
        },
        index=pd.DatetimeIndex(
            ["2026-06-29T00:00:00Z", "2026-06-30T00:00:00Z", "2026-07-01T00:00:00Z"],
            name="t",
        ),
    )
    write_1d(tmp_path, "SNDK", daily)
    market = _market(tmp_path)
    asof = current.index[-1].to_pydatetime() + timedelta(minutes=1)
    expected_atr = (0.17 + 20.0 / 105.0) / 2.0
    expected_atr_px = expected_atr * 123.0
    expected_range = (123.25 - 119.75) / expected_atr_px

    assert market.signal("atr_day", asof=asof) == pytest.approx(expected_atr)
    assert market.signal("atr_px", asof=asof) == pytest.approx(expected_atr_px)
    assert market.signal("day_range_atr", asof=asof) == pytest.approx(expected_range)


def test_missing_immediately_previous_session_data_names_symbol_and_day(
    tmp_path: Path,
) -> None:
    day = date(2026, 7, 2)
    current = _minute_frame(day, 1)
    write_1m_day(tmp_path, "SNDK", day, current)
    asof = current.index[-1].to_pydatetime() + timedelta(minutes=1)

    with pytest.raises(ValueError, match=r"SNDK.*2026-07-02.*previous-session"):
        _market(tmp_path).signal("gap_pct", asof=asof)


def test_unobserved_peers_are_excluded_instead_of_zero_filled(
    tmp_path: Path,
) -> None:
    day = date(2026, 7, 2)
    primary = _minute_frame(day, 6, start_price=100.0, end_price=106.0)
    prior = _minute_frame(date(2026, 7, 1), 10, start_price=95.0)
    _write_primary_history(tmp_path, current_day=day, current=primary, prior=prior)
    write_1m_day(
        tmp_path,
        "SOXX",
        day,
        _minute_frame(day, 6, start_price=100.0, end_price=103.0),
    )
    write_1m_day(
        tmp_path,
        "SPY",
        day,
        _minute_frame(day, 6, start_price=100.0, end_price=102.0),
    )
    write_1m_day(
        tmp_path,
        "QQQ",
        day,
        _minute_frame(
            day,
            5,
            start_price=100.0,
            end_price=150.0,
            positions=[0, 1, 2, 3, 5],
        ),
    )
    market = _market(tmp_path)
    asof = primary.index[-1].to_pydatetime() + timedelta(minutes=1)

    assert market.signal("comparable_ret_spread", asof=asof) == 0.0
    assert market.signal("comparable_agrees", asof=asof) == 0.0
    assert market.signal("sector_ret_spread", asof=asof) == pytest.approx(3.0)
    assert market.signal("market_ret_spread", asof=asof) == pytest.approx(4.0)
    assert market.signal("idio_strength", asof=asof) == pytest.approx(3.5)
    assert market.signal("peer_above_vwap_count", asof=asof) == 2.0


def test_registry_and_generated_markdown_cover_only_this_tasks_catalog(
    tmp_path: Path,
) -> None:
    rendered = render_markdown()
    generated_path = tmp_path / "signals.md"

    assert tuple(REGISTRY) == SIGNAL_IDS
    for signal_id in SIGNAL_IDS:
        assert f"| `{signal_id}` |" in rendered
    for signal_id in EXCLUDED_SIGNAL_IDS:
        assert f"| `{signal_id}` |" not in rendered
    assert write_doc(generated_path) == str(generated_path)
    assert generated_path.read_text(encoding="utf-8") == rendered
    assert DOC_PATH.read_text(encoding="utf-8") == rendered


def test_signal_requires_calendar_configuration(tmp_path: Path) -> None:
    market = ProviderMarketData(tmp_path)

    with pytest.raises(RuntimeError, match=r"signal\(\) requires a calendar.*calendar_path"):
        market.signal(
            "price",
            asof=datetime(2026, 7, 2, 13, 31, tzinfo=timezone.utc),
        )


def test_provider_signal_uses_the_configured_primary_symbol(tmp_path: Path) -> None:
    day = date(2026, 7, 2)
    current = _minute_frame(day, 1, start_price=250.0, end_price=251.0)
    prior = _minute_frame(date(2026, 7, 1), 1, start_price=245.0)
    write_1m_day(tmp_path, "ALT", prior.index[0].date(), prior)
    write_1m_day(tmp_path, "ALT", day, current)
    asof = current.index[-1].to_pydatetime() + timedelta(minutes=1)

    assert _market(tmp_path, primary_symbol="ALT").signal(
        "price", asof=asof
    ) == pytest.approx(251.0)
