from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from trader.contracts.testing import FakeMarketData


ASOF = datetime(2026, 7, 3, 14, 30, tzinfo=timezone.utc)
EXPECTED_IDS = [
    "orb5",
    "gap_play",
    "lateday_momentum",
    "opening_momentum",
    "day_extreme_reversal",
    "first_pullback",
    "prior_level_breakout",
    "range_compression",
]
BASE_SIGNALS = {
    "rvol_open_30m": 1.5,
    "gap_pct": 0.0,
    "open_vs_prior_high": -1.0,
    "open_vs_prior_low": 1.0,
    "peer_above_vwap_count": 2.0,
    "peer_below_vwap_count": 2.0,
    "vwap_side_run_minutes": 10.0,
}


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("could not locate repository root from test file")


@pytest.fixture(scope="module")
def roster_config() -> tuple[str, dict]:
    raw_text = (_repo_root() / "config" / "algos.yaml").read_text()
    return raw_text, yaml.safe_load(raw_text)


def _construct_algo(entry: dict):
    module_name, class_name = entry["factory"].split(":", maxsplit=1)
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    return cls(entry["id"], entry["status"], entry["params"])


def _daily_frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-07-01", periods=3, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "o": closes,
            "h": [close + 1.0 for close in closes],
            "l": [close - 1.0 for close in closes],
            "c": closes,
            "v": [1_000, 1_000, 1_000],
        },
        index=index,
    )


@pytest.fixture(scope="module")
def daily_frames() -> dict[str, pd.DataFrame]:
    return {
        "SNDK": _daily_frame([98.0, 100.0, 102.0]),
        "SNXX": _daily_frame([48.0, 50.0, 52.0]),
        "SNDQ": _daily_frame([51.0, 50.0, 48.0]),
    }


def test_roster_config_has_no_port_markers_and_constructs_all_setups(
    roster_config: tuple[str, dict],
) -> None:
    raw_text, doc = roster_config
    roster = doc["roster"]

    assert "PORT" not in raw_text
    assert [entry["id"] for entry in roster] == EXPECTED_IDS
    assert [entry["status"] for entry in roster] == [
        "emitting",
        "emitting",
        "emitting",
        "probe",
        "probe",
        "probe",
        "probe",
        "probe",
    ]
    assert len([_construct_algo(entry) for entry in roster]) == 8


@pytest.mark.parametrize(
    ("algo_id", "m", "overrides"),
    [
        (
            "orb5",
            10.0,
            {"price": 106.0, "or5_high": 105.0, "or5_low": 100.0},
        ),
        (
            "gap_play",
            20.0,
            {
                "gap_pct": 3.0,
                "open_vs_prior_high": 1.0,
                "price": 110.0,
                "or15_high": 105.0,
                "atr_px": 8.0,
            },
        ),
        (
            "lateday_momentum",
            300.0,
            {"first30_ret": 2.0, "price": 110.0, "atr_px": 8.0},
        ),
        (
            "opening_momentum",
            20.0,
            {"ret_from_open": 2.0, "price": 110.0, "atr_px": 8.0},
        ),
        (
            "day_extreme_reversal",
            90.0,
            {
                "day_range_atr": 1.0,
                "new_day_low": 1.0,
                "new_day_high": 0.0,
                "price": 100.0,
                "atr_px": 8.0,
                "day_low": 97.0,
                "day_high": 110.0,
            },
        ),
        (
            "first_pullback",
            90.0,
            {
                "vwap_side_run_minutes": 25.0,
                "vwap_dist_pct": 0.1,
                "price": 105.0,
                "atr_day": 0.02,
                "vwap": 104.0,
            },
        ),
        (
            "prior_level_breakout",
            20.0,
            {
                "open_vs_prior_high": -1.0,
                "price_vs_prior_high": 1.0,
                "price": 110.0,
                "atr_px": 8.0,
            },
        ),
        (
            "range_compression",
            150.0,
            {
                "or60_range": 2.0,
                "atr_px": 8.0,
                "price": 107.0,
                "or60_high": 105.0,
                "or60_low": 100.0,
            },
        ),
    ],
)
def test_each_roster_setup_fires_on_its_synthetic_bar(
    algo_id: str,
    m: float,
    overrides: dict[str, float],
    roster_config: tuple[str, dict],
    daily_frames: dict[str, pd.DataFrame],
) -> None:
    _, doc = roster_config
    entry = next(entry for entry in doc["roster"] if entry["id"] == algo_id)
    signals = {
        **BASE_SIGNALS,
        "minutes_since_open": m,
        "minutes_to_close": 390.0 - m,
        **overrides,
    }
    data = FakeMarketData(frames=daily_frames, signals=signals)

    intents = _construct_algo(entry).on_bar(ASOF, data)

    assert len(intents) == 1
    assert intents[0].algo_id == algo_id
    assert intents[0].action == "open"
