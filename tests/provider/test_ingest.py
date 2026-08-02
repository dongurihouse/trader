from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from trader.provider.ingest import ingest_all, ingest_file, parse_result_block
from trader.provider.store import read_1d, read_1m_day, write_1m_day


BAR_COLUMNS = ["o", "h", "l", "c", "v"]
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "raw"


def _payload(results: list[dict]) -> dict:
    return {"data": {"results": results}}


def _result(symbol: str, bars: list[dict]) -> dict:
    return {
        "symbol": symbol,
        "interval": "minute",
        "bounds": "regular",
        "bars": bars,
    }


def _bar(
    timestamp: str,
    *,
    open_price: float,
    close_price: float,
    high_price: float | None = None,
    low_price: float | None = None,
    volume: int = 100,
) -> dict:
    high = max(open_price, close_price) if high_price is None else high_price
    low = min(open_price, close_price) if low_price is None else low_price
    return {
        "begins_at": timestamp,
        "open_price": f"{open_price:.2f}",
        "close_price": f"{close_price:.2f}",
        "high_price": f"{high:.2f}",
        "low_price": f"{low:.2f}",
        "volume": volume,
        "session": "reg",
    }


def _write_dump(path: Path, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_payload(results)))


def _fixture_results(name: str) -> list[dict]:
    payload = json.loads((FIXTURE_ROOT / name).read_text())
    return payload["data"]["results"]


def test_parse_result_block_drops_interpolated_bars_and_uses_float64() -> None:
    frame, dropped = parse_result_block(_fixture_results("sample_dump.json")[0])

    expected = pd.DataFrame(
        {
            "o": [100.0, 100.0, 101.0],
            "h": [101.0, 160.0, 103.0],
            "l": [99.0, 99.5, 100.5],
            "c": [100.0, 101.0, 102.0],
            "v": [1000.0, 1100.0, 1200.0],
        },
        index=pd.DatetimeIndex(
            [
                "2026-07-01T13:30:00Z",
                "2026-07-01T13:32:00Z",
                "2026-07-01T13:33:00Z",
            ],
            name="t",
        ),
    ).astype("float64")

    assert dropped == 1
    assert_frame_equal(frame, expected)


def test_parse_result_block_returns_typed_empty_frame_when_all_bars_drop() -> None:
    interpolated = _bar(
        "2026-07-01T13:30:00Z", open_price=10, close_price=10
    )
    interpolated["interpolated"] = True

    frame, dropped = parse_result_block(_result("SNDK", [interpolated]))

    assert dropped == 1
    assert frame.empty
    assert list(frame.columns) == BAR_COLUMNS
    assert frame.index.name == "t"
    assert str(frame.index.dtype) == "datetime64[ns, UTC]"
    assert all(dtype == "float64" for dtype in frame.dtypes)


def test_ingest_file_quarantines_bad_tick_and_accepts_matching_etf(tmp_path) -> None:
    summary = ingest_file(FIXTURE_ROOT / "sample_dump.json", tmp_path)

    assert summary["SNDK"] == {
        "bars": 3,
        "interpolated_dropped": 1,
        "days": [date(2026, 7, 1)],
    }
    assert summary["SNXX"] == {
        "bars": 3,
        "interpolated_dropped": 0,
        "days": [date(2026, 7, 1)],
    }
    assert summary["etf_warnings"] == []
    assert summary["quarantined"] == [
        {
            "timestamp": pd.Timestamp("2026-07-01T13:32:00Z"),
            "symbol": "SNDK",
            "day": date(2026, 7, 1),
            "field": "high",
            "value": 160.0,
        }
    ]

    stored = read_1m_day(tmp_path, "SNDK", date(2026, 7, 1))
    assert stored is not None
    assert list(stored.index) == [
        pd.Timestamp("2026-07-01T13:30:00Z"),
        pd.Timestamp("2026-07-01T13:33:00Z"),
    ]
    assert pd.Timestamp("2026-07-01T13:31:00Z") not in stored.index
    assert pd.Timestamp("2026-07-01T13:32:00Z") not in stored.index


def test_ingest_file_writes_rth_daily_bars_for_every_touched_symbol(
    tmp_path,
) -> None:
    path = tmp_path / "daily-bars.json"
    data_root = tmp_path / "store"
    _write_dump(
        path,
        [
            _result(
                "SNDK",
                [
                    _bar(
                        "2026-07-09T13:00:00Z",
                        open_price=90,
                        close_price=100,
                        high_price=101,
                        low_price=89,
                        volume=1_000,
                    ),
                    _bar(
                        "2026-07-09T13:30:00Z",
                        open_price=100,
                        close_price=101,
                        high_price=102,
                        low_price=99,
                        volume=10,
                    ),
                    _bar(
                        "2026-07-09T13:31:00Z",
                        open_price=101,
                        close_price=102,
                        high_price=103,
                        low_price=100,
                        volume=20,
                    ),
                    _bar(
                        "2026-07-09T19:59:00Z",
                        open_price=102,
                        close_price=103,
                        high_price=104,
                        low_price=101,
                        volume=30,
                    ),
                    _bar(
                        "2026-07-09T20:00:00Z",
                        open_price=103,
                        close_price=103,
                        high_price=200,
                        low_price=10,
                        volume=2_000,
                    ),
                ],
            ),
            _result(
                "MU",
                [
                    _bar(
                        "2026-07-09T13:30:00Z",
                        open_price=50,
                        close_price=51,
                        high_price=52,
                        low_price=49,
                        volume=40,
                    ),
                    _bar(
                        "2026-07-09T19:59:00Z",
                        open_price=51,
                        close_price=52,
                        high_price=53,
                        low_price=50,
                        volume=60,
                    ),
                ],
            ),
        ],
    )

    ingest_file(path, data_root)

    sndk_daily = read_1d(data_root, "SNDK")
    mu_daily = read_1d(data_root, "MU")
    assert sndk_daily is not None
    assert mu_daily is not None
    expected_timestamp = pd.Timestamp("2026-07-09T00:00:00Z")
    assert list(sndk_daily.index) == [expected_timestamp]
    assert sndk_daily.iloc[0].to_dict() == {
        "o": 100.0,
        "h": 104.0,
        "l": 99.0,
        "c": 103.0,
        "v": 60.0,
    }
    assert list(mu_daily.index) == [expected_timestamp]
    assert mu_daily.iloc[0].to_dict() == {
        "o": 50.0,
        "h": 53.0,
        "l": 49.0,
        "c": 52.0,
        "v": 100.0,
    }


def test_reingesting_same_file_upserts_daily_bar_without_duplicate(tmp_path) -> None:
    path = tmp_path / "repeat-daily-bar.json"
    data_root = tmp_path / "store"
    _write_dump(
        path,
        [
            _result(
                "SNDK",
                [
                    _bar(
                        "2026-07-10T13:30:00Z",
                        open_price=100,
                        close_price=101,
                        high_price=102,
                        low_price=99,
                        volume=10,
                    ),
                    _bar(
                        "2026-07-10T19:59:00Z",
                        open_price=101,
                        close_price=103,
                        high_price=104,
                        low_price=100,
                        volume=20,
                    ),
                ],
            )
        ],
    )

    ingest_file(path, data_root)
    first = read_1d(data_root, "SNDK")
    ingest_file(path, data_root)
    second = read_1d(data_root, "SNDK")

    assert first is not None
    assert second is not None
    assert len(second) == 1
    assert_frame_equal(second, first)
    assert second.iloc[0].to_dict() == {
        "o": 100.0,
        "h": 104.0,
        "l": 99.0,
        "c": 103.0,
        "v": 30.0,
    }


def test_ingest_file_warns_when_etf_return_misses_leverage_range(tmp_path) -> None:
    summary = ingest_file(FIXTURE_ROOT / "warning_dump.json", tmp_path)

    assert summary["quarantined"] == []
    assert summary["etf_warnings"] == [
        {
            "symbol": "SNXX",
            "day": date(2026, 7, 2),
            "expected_range": (1.5, 2.5),
            "actual_ratio": pytest.approx(0.5),
        }
    ]
    stored = read_1m_day(tmp_path, "SNXX", date(2026, 7, 2))
    assert stored is not None
    assert stored.iloc[-1]["c"] == 50.5


def test_ingest_file_revised_rows_win_duplicate_timestamps(tmp_path) -> None:
    ingest_file(FIXTURE_ROOT / "sample_dump.json", tmp_path)
    revised_path = tmp_path / "revised.json"
    _write_dump(
        revised_path,
        [
            _result(
                "SNDK",
                [
                    _bar(
                        "2026-07-01T13:33:00Z",
                        open_price=101,
                        close_price=103,
                        high_price=104,
                        low_price=100.5,
                        volume=9999,
                    )
                ],
            )
        ],
    )

    summary = ingest_file(revised_path, tmp_path)

    assert summary["SNDK"]["bars"] == 1
    stored = read_1m_day(tmp_path, "SNDK", date(2026, 7, 1))
    assert stored is not None
    assert stored.loc[pd.Timestamp("2026-07-01T13:33:00Z")].to_dict() == {
        "o": 101.0,
        "h": 104.0,
        "l": 100.5,
        "c": 103.0,
        "v": 9999.0,
    }


def test_quarantine_uses_stored_neighbors_and_preserves_prior_revision(
    tmp_path,
) -> None:
    day = date(2026, 7, 3)
    index = pd.DatetimeIndex(
        ["2026-07-03T13:30:00Z", "2026-07-03T13:31:00Z", "2026-07-03T13:32:00Z"],
        name="t",
    )
    existing = pd.DataFrame(
        {
            "o": [100.0, 100.0, 100.0],
            "h": [101.0, 101.0, 102.0],
            "l": [99.0, 99.0, 99.0],
            "c": [100.0, 100.5, 101.0],
            "v": [100.0, 100.0, 100.0],
        },
        index=index,
    )
    write_1m_day(tmp_path, "SNDK", day, existing)
    path = tmp_path / "bad-revision.json"
    _write_dump(
        path,
        [
            _result(
                "SNDK",
                [
                    _bar(
                        "2026-07-03T13:31:00Z",
                        open_price=100,
                        close_price=100.5,
                        high_price=180,
                        low_price=99,
                    )
                ],
            )
        ],
    )

    summary = ingest_file(path, tmp_path)

    assert summary["SNDK"]["bars"] == 1
    assert summary["quarantined"][0]["timestamp"] == pd.Timestamp(
        "2026-07-03T13:31:00Z"
    )
    stored = read_1m_day(tmp_path, "SNDK", day)
    assert stored is not None
    assert_frame_equal(stored, existing.astype("float64"))


def test_ingest_file_does_not_requarantine_preexisting_bad_tick(tmp_path) -> None:
    day = date(2026, 7, 4)
    existing = pd.DataFrame(
        {
            "o": [100.0, 100.0, 100.0],
            "h": [101.0, 180.0, 102.0],
            "l": [99.0, 99.0, 99.0],
            "c": [100.0, 100.5, 101.0],
            "v": [100.0, 100.0, 100.0],
        },
        index=pd.DatetimeIndex(
            [
                "2026-07-04T13:30:00Z",
                "2026-07-04T13:31:00Z",
                "2026-07-04T13:32:00Z",
            ],
            name="t",
        ),
    )
    write_1m_day(tmp_path, "SNDK", day, existing)
    path = tmp_path / "new-edge.json"
    _write_dump(
        path,
        [
            _result(
                "SNDK",
                [
                    _bar(
                        "2026-07-04T13:33:00Z",
                        open_price=101,
                        close_price=101.5,
                    )
                ],
            )
        ],
    )

    summary = ingest_file(path, tmp_path)

    assert summary["quarantined"] == []
    stored = read_1m_day(tmp_path, "SNDK", day)
    assert stored is not None
    assert stored.loc[pd.Timestamp("2026-07-04T13:31:00Z"), "h"] == 180.0


def test_ingest_file_quarantines_low_but_never_checks_day_edges(tmp_path) -> None:
    path = tmp_path / "low-and-edges.json"
    _write_dump(
        path,
        [
            _result(
                "SNDK",
                [
                    _bar(
                        "2026-07-07T13:30:00Z",
                        open_price=100,
                        close_price=100,
                        high_price=180,
                        low_price=99,
                    ),
                    _bar(
                        "2026-07-07T13:31:00Z",
                        open_price=100,
                        close_price=100.5,
                        high_price=101,
                        low_price=40,
                    ),
                    _bar(
                        "2026-07-07T13:32:00Z",
                        open_price=100.5,
                        close_price=101,
                        high_price=102,
                        low_price=40,
                    ),
                ],
            )
        ],
    )

    summary = ingest_file(path, tmp_path)

    assert summary["quarantined"] == [
        {
            "timestamp": pd.Timestamp("2026-07-07T13:31:00Z"),
            "symbol": "SNDK",
            "day": date(2026, 7, 7),
            "field": "low",
            "value": 40.0,
        }
    ]
    stored = read_1m_day(tmp_path, "SNDK", date(2026, 7, 7))
    assert stored is not None
    assert list(stored.index) == [
        pd.Timestamp("2026-07-07T13:30:00Z"),
        pd.Timestamp("2026-07-07T13:32:00Z"),
    ]


def test_ingest_file_counts_duplicate_vendor_rows_while_later_row_wins(
    tmp_path,
) -> None:
    path = tmp_path / "same-file-revision.json"
    timestamp = "2026-07-08T13:30:00Z"
    _write_dump(
        path,
        [
            _result(
                "SNDK",
                [_bar(timestamp, open_price=100, close_price=100.5)],
            ),
            _result(
                "SNDK",
                [_bar(timestamp, open_price=100, close_price=101.5)],
            ),
        ],
    )

    summary = ingest_file(path, tmp_path)

    assert summary["SNDK"]["bars"] == 2
    stored = read_1m_day(tmp_path, "SNDK", date(2026, 7, 8))
    assert stored is not None
    assert stored.loc[pd.Timestamp(timestamp), "c"] == 101.5


def test_ingest_all_walks_nested_files_in_sorted_order_and_aggregates(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    first = raw_root / "a" / "first.json"
    second = raw_root / "z" / "nested" / "second.json"
    _write_dump(
        first,
        [
            _result(
                "SNDK",
                [
                    _bar(
                        "2026-07-05T13:30:00Z",
                        open_price=100,
                        close_price=100.5,
                    )
                ],
            )
        ],
    )
    _write_dump(
        second,
        [
            _result(
                "SNDK",
                [
                    _bar(
                        "2026-07-05T13:30:00Z",
                        open_price=100,
                        close_price=101.5,
                    ),
                    _bar(
                        "2026-07-06T13:30:00Z",
                        open_price=102,
                        close_price=102.5,
                    ),
                ],
            )
        ],
    )

    summary = ingest_all(raw_root, tmp_path / "store")

    assert summary["SNDK"] == {
        "bars": 3,
        "interpolated_dropped": 0,
        "days": 2,
        "min_date": date(2026, 7, 5),
        "max_date": date(2026, 7, 6),
    }
    assert summary["quarantined"] == []
    assert summary["etf_warnings"] == []
    stored = read_1m_day(tmp_path / "store", "SNDK", date(2026, 7, 5))
    assert stored is not None
    assert stored.loc[pd.Timestamp("2026-07-05T13:30:00Z"), "c"] == 101.5
