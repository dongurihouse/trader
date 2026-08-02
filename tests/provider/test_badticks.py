from __future__ import annotations

from datetime import date

import pandas as pd

from trader.provider.badticks import classify_bad_ticks


FRACTION = 0.05
MAX_BAD_RUN_BARS = 3
PERMISSIVE_ABORT_FRACTION = 1.0
SYMBOL = "SNDK"
DAY = date(2026, 7, 16)


def _frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range(
        "2026-07-16T13:30:00Z",
        periods=len(closes),
        freq="min",
        name="t",
    )
    return pd.DataFrame(
        {
            "o": [float(value) for value in closes],
            "h": [float(value) + 0.1 for value in closes],
            "l": [float(value) - 0.1 for value in closes],
            "c": [float(value) for value in closes],
            "v": [100.0 for _ in closes],
        },
        index=index,
        dtype="float64",
    )


def _ohlcv_frame(
    rows: list[tuple[str, float, float, float, float, int]]
) -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [pd.Timestamp(timestamp) for timestamp, *_ in rows],
        name="t",
    )
    return pd.DataFrame(
        {
            "o": [row[1] for row in rows],
            "h": [row[2] for row in rows],
            "l": [row[3] for row in rows],
            "c": [row[4] for row in rows],
            "v": [float(row[5]) for row in rows],
        },
        index=index,
        dtype="float64",
    )


def _classify(
    frame: pd.DataFrame,
    *,
    abort_fraction: float = PERMISSIVE_ABORT_FRACTION,
) -> tuple[list[pd.Timestamp], list[dict]]:
    return classify_bad_ticks(
        frame,
        bad_tick_neighbor_fraction=FRACTION,
        max_bad_run_bars=MAX_BAD_RUN_BARS,
        quarantine_abort_fraction=abort_fraction,
        symbol=SYMBOL,
        day=DAY,
    )


def _assert_one_labeled_error(errors: list[dict]) -> None:
    assert len(errors) == 1
    assert errors[0]["symbol"] == SYMBOL
    assert errors[0]["day"] == DAY
    assert errors[0]["reason"]


def test_isolated_single_bad_tick_interior_quarantines_one_bar() -> None:
    frame = _frame([100.0, 100.2, 180.0, 100.1, 99.9])

    quarantined, errors = _classify(frame)

    assert quarantined == [frame.index[2]]
    assert errors == []


def test_interior_bad_run_of_two_quarantines_both_bars() -> None:
    frame = _frame([100.0, 100.2, 180.0, 180.2, 100.1, 99.9])

    quarantined, errors = _classify(frame)

    assert quarantined == [frame.index[2], frame.index[3]]
    assert errors == []


def test_sndk_2026_07_16_1627_high_and_close_corruption_quarantines_body_departure() -> None:
    frame = _ohlcv_frame(
        [
            (
                "2026-07-16T16:24:00Z",
                1433.979980,
                1437.000000,
                1432.160156,
                1436.800049,
                30073,
            ),
            (
                "2026-07-16T16:25:00Z",
                1436.650024,
                1436.979980,
                1433.170044,
                1436.297485,
                24480,
            ),
            (
                "2026-07-16T16:26:00Z",
                1435.872192,
                1442.000000,
                1435.872192,
                1438.000000,
                43320,
            ),
            (
                "2026-07-16T16:27:00Z",
                1439.000000,
                1615.000000,
                1439.000000,
                1615.000000,
                36505,
            ),
            (
                "2026-07-16T16:28:00Z",
                1441.905029,
                1442.489990,
                1439.380005,
                1440.930054,
                17000,
            ),
            (
                "2026-07-16T16:29:00Z",
                1441.880005,
                1442.439941,
                1436.239990,
                1437.560059,
                22696,
            ),
            (
                "2026-07-16T16:30:00Z",
                1437.123779,
                1442.849976,
                1437.123779,
                1440.189941,
                22332,
            ),
        ]
    )
    corrupt_timestamp = pd.Timestamp("2026-07-16T16:27:00Z")

    quarantined, errors = _classify(frame)

    assert quarantined == [corrupt_timestamp]
    assert errors == []
    assert [timestamp for timestamp in frame.index if timestamp not in quarantined] == [
        pd.Timestamp("2026-07-16T16:24:00Z"),
        pd.Timestamp("2026-07-16T16:25:00Z"),
        pd.Timestamp("2026-07-16T16:26:00Z"),
        pd.Timestamp("2026-07-16T16:28:00Z"),
        pd.Timestamp("2026-07-16T16:29:00Z"),
        pd.Timestamp("2026-07-16T16:30:00Z"),
    ]


def test_rule_2a_departing_high_with_in_level_body_is_retained_and_named_error() -> None:
    frame = _ohlcv_frame(
        [
            ("2026-07-16T13:30:00Z", 100.0, 100.2, 99.9, 100.0, 100),
            ("2026-07-16T13:31:00Z", 100.1, 100.3, 99.9, 100.1, 100),
            ("2026-07-16T13:32:00Z", 100.2, 140.0, 100.0, 100.2, 100),
            ("2026-07-16T13:33:00Z", 100.0, 100.2, 99.8, 100.0, 100),
            ("2026-07-16T13:34:00Z", 99.9, 100.1, 99.7, 99.9, 100),
        ]
    )
    wick_timestamp = pd.Timestamp("2026-07-16T13:32:00Z")

    quarantined, errors = _classify(frame)

    assert wick_timestamp not in quarantined
    assert len(errors) == 1
    assert errors[0]["rule"] == "rule 2a"
    assert errors[0]["span_start"] == wick_timestamp
    assert errors[0]["span_end"] == wick_timestamp
    assert errors[0]["bar_count"] == 1
    assert str(wick_timestamp) in errors[0]["reason"]
    assert "high" in errors[0]["reason"]


def test_first_bar_edge_bad_run_of_two_quarantines_when_reference_is_longer() -> None:
    frame = _frame([180.0, 180.2, 100.0, 100.1, 99.9, 100.2])

    quarantined, errors = _classify(frame)

    assert quarantined == [frame.index[0], frame.index[1]]
    assert errors == []


def test_last_bar_edge_bad_run_of_two_quarantines_when_reference_is_longer() -> None:
    frame = _frame([100.0, 100.1, 99.9, 100.2, 180.0, 180.2])

    quarantined, errors = _classify(frame)

    assert quarantined == [frame.index[4], frame.index[5]]
    assert errors == []


def test_three_bar_majority_bad_run_keeps_everything_with_insufficient_context_error() -> None:
    frame = _frame([100.0, 180.0, 180.2])

    quarantined, errors = _classify(frame)

    assert quarantined == []
    _assert_one_labeled_error(errors)
    assert "insufficient context" in errors[0]["reason"]


def test_eight_bar_majority_interior_bad_run_escalates_without_quarantine() -> None:
    frame = _frame([100.0, 180.0, 180.1, 180.2, 180.3, 180.4, 180.5, 100.1])

    quarantined, errors = _classify(frame)

    assert quarantined == []
    _assert_one_labeled_error(errors)
    assert "rule 4" in errors[0]["reason"]


def test_forty_bar_majority_interior_bad_run_escalates_without_quarantine() -> None:
    frame = _frame([100.0] * 5 + [180.0] * 30 + [100.1] * 5)

    quarantined, errors = _classify(frame)

    assert quarantined == []
    _assert_one_labeled_error(errors)
    assert "rule 4" in errors[0]["reason"]


def test_full_day_majority_edge_bad_runs_escalate_without_quarantine() -> None:
    for bad_count, good_count in [(200, 190), (273, 117)]:
        frame = _frame([180.0] * bad_count + [100.0] * good_count)

        quarantined, errors = _classify(frame)

        assert quarantined == []
        _assert_one_labeled_error(errors)
        assert "rule 4" in errors[0]["reason"]


def test_self_consistent_gap_up_day_has_no_quarantines_or_errors() -> None:
    frame = _frame(
        [
            140.0,
            140.6,
            141.1,
            140.8,
            141.4,
            142.0,
            141.7,
            142.3,
            142.6,
            143.1,
        ]
    )

    quarantined, errors = _classify(frame)

    assert quarantined == []
    assert errors == []


def test_halt_and_reopen_equal_length_tie_retains_with_one_rule_six_error() -> None:
    frame = _frame([100.0] * 5 + [130.0] * 5)

    quarantined, errors = _classify(frame)

    assert quarantined == []
    _assert_one_labeled_error(errors)
    assert "rule 6" in errors[0]["reason"]
    assert "tie" in errors[0]["reason"]


def test_halt_and_reopen_unequal_long_segments_retains_with_one_rule_four_error() -> None:
    frame = _frame([100.0] * 4 + [130.0] * 6)

    quarantined, errors = _classify(frame)

    assert quarantined == []
    _assert_one_labeled_error(errors)
    assert "rule 4" in errors[0]["reason"]


def test_frames_too_short_for_reference_without_split_have_no_errors() -> None:
    one_bar = _frame([100.0])
    two_agreeing_bars = _frame([100.0, 100.1])

    assert _classify(one_bar) == ([], [])
    assert _classify(two_agreeing_bars) == ([], [])


def test_abort_valve_reverts_three_isolated_bad_ticks_at_real_default() -> None:
    closes = [100.0] * 40
    closes[8] = 160.0
    closes[20] = 180.0
    closes[32] = 210.0
    frame = _frame(closes)

    quarantined, errors = _classify(frame, abort_fraction=0.05)

    assert quarantined == []
    _assert_one_labeled_error(errors)
    assert "rule 5" in errors[0]["reason"]
    assert "3 of 40" in errors[0]["reason"]


def test_abort_valve_allows_single_isolated_bad_tick_below_real_default() -> None:
    closes = [100.0] * 40
    closes[20] = 180.0
    frame = _frame(closes)

    quarantined, errors = _classify(frame, abort_fraction=0.05)

    assert quarantined == [frame.index[20]]
    assert errors == []
