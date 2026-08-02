"""Future end-to-end coverage once provider and algo packages are merged."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
import yaml

from trader.cli import main


REPO_ROOT = Path(__file__).resolve().parents[2]


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
