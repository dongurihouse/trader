from datetime import datetime, timezone

import pandas as pd
import pytest

from trader.algos.bracket import (
    etf_price,
    instrument_for,
    leverage_for,
    previous_close,
    translate,
)
from trader.contracts.testing import FakeMarketData


ASOF = datetime(2026, 7, 3, 14, 30, tzinfo=timezone.utc)


def _daily_frame(closes: list[float], *, start: str = "2026-07-01") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "o": closes,
            "h": [close + 1.0 for close in closes],
            "l": [close - 1.0 for close in closes],
            "c": closes,
            "v": [1_000 for _ in closes],
        },
        index=index,
    )


@pytest.mark.parametrize(
    ("direction", "expected"),
    [("long", "SNXX"), ("short", "SNDQ")],
)
def test_instrument_for_resolves_each_candidate_direction(
    direction: str, expected: str
) -> None:
    assert instrument_for(direction) == expected


def test_instrument_for_rejects_an_unknown_direction() -> None:
    with pytest.raises(ValueError, match="sideways"):
        instrument_for("sideways")


def test_leverage_for_returns_each_etfs_signed_daily_leverage() -> None:
    assert leverage_for("SNXX") == 2.0
    assert leverage_for("SNDQ") == -2.0


def test_leverage_for_names_an_unknown_instrument() -> None:
    with pytest.raises(ValueError, match="UNKNOWN"):
        leverage_for("UNKNOWN")


@pytest.mark.parametrize(
    ("leverage", "expected"),
    [(2.0, 60.0), (-2.0, 40.0)],
)
def test_etf_price_applies_signed_leverage_to_the_underlying_return(
    leverage: float, expected: float
) -> None:
    # SNDK rising from 100 to 110 is a 10% return. At a prior ETF close of
    # 50, the hand-computed +20%/-20% outcomes are 60 and 40 respectively.
    assert etf_price(110.0, 100.0, 50.0, leverage) == pytest.approx(expected)


def test_previous_close_returns_the_last_complete_session_before_asof() -> None:
    data = FakeMarketData({"SNDK": _daily_frame([99.0, 101.25, 103.0])})

    assert previous_close("SNDK", data, ASOF) == 101.25


def test_previous_close_returns_none_on_the_frames_first_day() -> None:
    data = FakeMarketData(
        {"SNDK": _daily_frame([103.0, 104.0], start="2026-07-03")}
    )

    assert previous_close("SNDK", data, ASOF) is None


def test_translate_long_returns_a_rounded_snxx_bracket_in_long_share_order() -> None:
    data = FakeMarketData(
        {
            "SNDK": _daily_frame([98.0, 100.0, 104.0]),
            "SNXX": _daily_frame([48.0, 50.0, 52.0]),
        }
    )

    result = translate(
        "long", 101.237, 99.123, 103.456, "SNDK", data, ASOF
    )

    assert result == ("SNXX", 51.24, 49.12, 53.46)
    instrument, entry, stop, target = result
    assert instrument == "SNXX"
    assert stop < entry < target


def test_translate_short_flips_sndk_order_via_negative_leverage() -> None:
    data = FakeMarketData(
        {
            "SNDK": _daily_frame([98.0, 100.0, 104.0]),
            "SNDQ": _daily_frame([51.0, 50.0, 47.0]),
        }
    )
    stop_sndk, entry_sndk, target_sndk = 102.0, 100.0, 95.0

    result = translate(
        "short",
        entry_sndk,
        stop_sndk,
        target_sndk,
        "SNDK",
        data,
        ASOF,
    )

    assert stop_sndk > entry_sndk > target_sndk
    assert result == ("SNDQ", 50.0, 48.0, 55.0)
    instrument, entry, stop, target = result
    assert instrument == "SNDQ"
    assert stop < entry < target


@pytest.mark.parametrize("missing_symbol", ["SNDK", "SNXX"])
def test_translate_returns_none_when_either_previous_session_is_missing(
    missing_symbol: str,
) -> None:
    frames = {
        "SNDK": _daily_frame([98.0, 100.0, 104.0]),
        "SNXX": _daily_frame([48.0, 50.0, 52.0]),
    }
    frames[missing_symbol] = _daily_frame([100.0, 101.0], start="2026-07-03")
    data = FakeMarketData(frames)

    assert translate("long", 101.0, 99.0, 103.0, "SNDK", data, ASOF) is None


def test_translate_returns_none_for_a_non_positive_sndk_previous_close() -> None:
    data = FakeMarketData(
        {
            "SNDK": _daily_frame([98.0, 0.0, 104.0]),
            "SNXX": _daily_frame([48.0, 50.0, 52.0]),
        }
    )

    assert translate("long", 101.0, 99.0, 103.0, "SNDK", data, ASOF) is None
