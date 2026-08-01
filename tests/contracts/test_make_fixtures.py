"""Regression tests for the committed contract fixtures and their generator."""

from datetime import date
from pathlib import Path

import pandas as pd

from trader.contracts.intents import Intent
from trader.contracts.serde import read_jsonl, record_from_json, record_to_json
from trader.contracts.telemetry import EVENT_TYPES
import trader.contracts.testing.make_fixtures as make_fixtures
from trader.contracts.testing import synthetic_day


FIXTURE_SEED = 20260701
EXPECTED_FILES = {
    Path("bars/SNDK/2026-07-01.parquet"),
    Path("intents.sample.jsonl"),
    Path("telemetry.sample.jsonl"),
}


def _generated_files(output_dir: Path) -> set[Path]:
    return {
        path.relative_to(output_dir)
        for path in output_dir.rglob("*")
        if path.is_file()
    }


def test_generate_fixtures_writes_exact_expected_files(tmp_path: Path) -> None:
    make_fixtures.generate_fixtures(tmp_path)

    assert _generated_files(tmp_path) == EXPECTED_FILES


def test_generated_fixtures_round_trip_with_complete_representative_content(
    tmp_path: Path,
) -> None:
    make_fixtures.generate_fixtures(tmp_path)

    bars = pd.read_parquet(tmp_path / "bars/SNDK/2026-07-01.parquet")
    expected_bars = synthetic_day(
        "SNDK",
        date(2026, 7, 1),
        seed=FIXTURE_SEED,
    )
    assert len(bars) == 720
    pd.testing.assert_frame_equal(
        bars,
        expected_bars,
        check_exact=True,
        check_freq=False,
    )

    intents_json = list(read_jsonl(tmp_path / "intents.sample.jsonl"))
    intents = [record_from_json(record) for record in intents_json]
    assert all(type(intent) is Intent for intent in intents)
    assert [record_to_json(intent) for intent in intents] == intents_json
    assert any(
        intent.action == "open" and intent.side == "long" for intent in intents
    )
    assert any(
        intent.action == "open" and intent.side == "short" for intent in intents
    )
    assert any(intent.action == "close" and intent.side is None for intent in intents)
    assert [intent.ts for intent in intents] == sorted(intent.ts for intent in intents)

    telemetry_json = list(read_jsonl(tmp_path / "telemetry.sample.jsonl"))
    telemetry = [record_from_json(record) for record in telemetry_json]
    assert [record_to_json(event) for event in telemetry] == telemetry_json
    assert all(type(event) is EVENT_TYPES[event.ev] for event in telemetry)
    assert set(EVENT_TYPES).issubset({event.ev for event in telemetry})
    assert {event.session for event in telemetry} == {"fixture-session-001"}
    assert [event.ts for event in telemetry] == sorted(event.ts for event in telemetry)


def test_generate_fixtures_is_deterministic_across_directories(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    make_fixtures.generate_fixtures(first_dir)
    make_fixtures.generate_fixtures(second_dir)

    pd.testing.assert_frame_equal(
        pd.read_parquet(first_dir / "bars/SNDK/2026-07-01.parquet"),
        pd.read_parquet(second_dir / "bars/SNDK/2026-07-01.parquet"),
        check_exact=True,
    )
    for relative_path in (
        Path("intents.sample.jsonl"),
        Path("telemetry.sample.jsonl"),
    ):
        assert (first_dir / relative_path).read_text(encoding="utf-8") == (
            second_dir / relative_path
        ).read_text(encoding="utf-8")


def test_committed_fixtures_match_fresh_generation(tmp_path: Path) -> None:
    make_fixtures.generate_fixtures(tmp_path)
    repository_root = Path(make_fixtures.__file__).resolve().parents[4]
    committed_dir = repository_root / "tests" / "fixtures"

    pd.testing.assert_frame_equal(
        pd.read_parquet(committed_dir / "bars/SNDK/2026-07-01.parquet"),
        pd.read_parquet(tmp_path / "bars/SNDK/2026-07-01.parquet"),
        check_exact=True,
    )
    for relative_path in (
        Path("intents.sample.jsonl"),
        Path("telemetry.sample.jsonl"),
    ):
        assert (committed_dir / relative_path).read_bytes() == (
            tmp_path / relative_path
        ).read_bytes()
