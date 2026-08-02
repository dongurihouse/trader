"""Composition coverage across the shipped algo and execution components."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest
import yaml

from trader.cli import main
from trader.contracts.testing import CollectingTelemetry, FakeMarketData
from trader.execution.book import RealBook, ShadowBook
from trader.execution.broker import SimBroker
from trader.execution.config import load_config
from trader.execution.driver import compose_algos_from_roster
from trader.execution.risk import RiskRails
from trader.execution.session import SessionRunner


REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
SESSION_DAY = date(2026, 7, 1)
FIRST_BAR_TS = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
EXPECTED_STATUSES = {
    "orb5": "emitting",
    "gap_play": "emitting",
    "lateday_momentum": "emitting",
    "opening_momentum": "probe",
    "day_extreme_reversal": "probe",
    "first_pullback": "probe",
    "prior_level_breakout": "probe",
    "range_compression": "probe",
}


def _synthetic_frame(
    previous_close: float,
    current_closes: list[float],
) -> pd.DataFrame:
    timestamps = [
        FIRST_BAR_TS - timedelta(days=1),
        *[
            FIRST_BAR_TS + timedelta(minutes=offset)
            for offset in range(len(current_closes))
        ],
    ]
    closes = [previous_close, *current_closes]
    opens = [previous_close, previous_close, *current_closes[:-1]]
    return pd.DataFrame(
        [
            {
                "o": open_price,
                "h": max(open_price, close) + 0.25,
                "l": min(open_price, close) - 0.25,
                "c": close,
                "v": 1_000.0,
            }
            for open_price, close in zip(opens, closes, strict=True)
        ],
        index=pd.DatetimeIndex(timestamps, name="ts"),
    )


def test_shipped_roster_composes_and_runs_synthetic_session() -> None:
    resolved = load_config(REPO_ROOT / "config")

    roster = compose_algos_from_roster(resolved)

    assert {entry.algo.id: entry.status for entry in roster} == EXPECTED_STATUSES
    assert len([entry for entry in roster if entry.status == "emitting"]) == 3
    assert len([entry for entry in roster if entry.status == "probe"]) == 5
    for spec in resolved.algos.roster:
        roster_entry = next(entry for entry in roster if entry.algo.id == spec.id)
        assert roster_entry.status == spec.status
        assert getattr(roster_entry.algo, "status") == spec.status

    current_closes = {
        "SNDK": [101.0, 101.1, 101.2, 101.3],
        "SNXX": [50.9, 51.0, 51.1, 51.2],
        "SNDQ": [49.1, 49.0, 48.9, 48.8],
    }
    market_data = FakeMarketData(
        frames={
            "SNDK": _synthetic_frame(100.0, current_closes["SNDK"]),
            "SNXX": _synthetic_frame(50.0, current_closes["SNXX"]),
            "SNDQ": _synthetic_frame(50.0, current_closes["SNDQ"]),
        },
        signals={
            "minutes_since_open": 10.0,
            "minutes_to_close": 380.0,
            "price": 101.0,
            "or5_high": 100.5,
            "or5_low": 99.0,
            "ret_from_open": 0.0,
            "rvol_open_30m": 1.5,
            "gap_pct": 0.0,
            "open_vs_prior_high": -1.0,
            "open_vs_prior_low": 1.0,
            "peer_above_vwap_count": 2.0,
            "peer_below_vwap_count": 2.0,
            "vwap_side_run_minutes": 10.0,
        },
    )
    telemetry = CollectingTelemetry()
    runner = SessionRunner(
        session_id="backtest-composition-fixture",
        mode="backtest",
        market_data=market_data,
        broker=SimBroker(
            resolved.execution,
            timezone=resolved.trader.timezone,
        ),
        risk=RiskRails(
            resolved.risk,
            resolved.execution,
            timezone=resolved.trader.timezone,
        ),
        real_book=RealBook(
            resolved.risk,
            resolved.execution,
            timezone=resolved.trader.timezone,
        ),
        shadow_book=ShadowBook(
            resolved.execution,
            timezone=resolved.trader.timezone,
        ),
        telemetry=telemetry,
        roster=roster,
        symbols=resolved.trader.symbols,
        config_sha256=resolved.config_sha256,
        package_version=resolved.package_version,
        execution_config=resolved.execution,
        traded_instruments=list(resolved.trader.instrument_map.values()),
        timezone=resolved.trader.timezone,
    )
    bar_asofs = [
        FIRST_BAR_TS + timedelta(minutes=offset + 1)
        for offset in range(len(current_closes["SNDK"]))
    ]

    runner.start_session(bar_asofs[0])
    runner.start_day(SESSION_DAY)
    for asof in bar_asofs:
        runner.process_bar(asof)
    summary = runner.end_session(bar_asofs[-1])

    assert summary.bars_processed == len(bar_asofs)
    assert not [record for record in telemetry.records if record["ev"] == "algo_error"]
    assert any(record["ev"] == "intent" for record in telemetry.records)
    assert telemetry.records[-1]["ev"] == "session_end"


@pytest.mark.skip(
    reason="Wave 2: requires trader.provider + trader.algos, not yet built"
)
def test_run_backtest_writes_complete_session_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config_dir)
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "bars").symlink_to(
        REPO_ROOT / "data" / "bars",
        target_is_directory=True,
    )

    trader_config_path = config_dir / "trader.yaml"
    trader_config = yaml.safe_load(trader_config_path.read_text())
    trader_config["data_root"] = str(data_root)
    trader_config_path.write_text(
        yaml.safe_dump(trader_config, sort_keys=False)
    )
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "run",
            "--mode",
            "backtest",
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-01",
        ]
    )

    assert exit_code == 0
    session_dirs = list((data_root / "sessions").glob("backtest-20260701-*"))
    assert len(session_dirs) == 1
    telemetry_path = session_dirs[0] / "telemetry.jsonl"
    assert telemetry_path.is_file()
    records = [json.loads(line) for line in telemetry_path.read_text().splitlines()]
    assert records[0]["ev"] == "session_start"
    assert records[-1]["ev"] == "session_end"
