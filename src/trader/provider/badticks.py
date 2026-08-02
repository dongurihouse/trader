"""Bad-tick classification for one provider minute-bar day."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class _Segment:
    start: int
    stop: int
    level: float

    @property
    def length(self) -> int:
        return self.stop - self.start


def _agree(value: float, reference: float, fraction: float) -> bool:
    if reference == 0.0:
        return value == 0.0
    return abs(value - reference) / abs(reference) <= fraction


def _deviates(value: float, reference: float, fraction: float) -> bool:
    if reference == 0.0:
        return value != 0.0
    return abs(value - reference) / abs(reference) > fraction


def _segments(frame: pd.DataFrame, fraction: float) -> list[_Segment]:
    if frame.empty:
        return []

    closes = [float(value) for value in frame["c"]]
    segments: list[_Segment] = []
    start = 0
    for position in range(1, len(closes)):
        if _agree(closes[position], closes[position - 1], fraction):
            continue
        level = float(median(closes[start:position]))
        segments.append(_Segment(start=start, stop=position, level=level))
        start = position

    segments.append(
        _Segment(start=start, stop=len(closes), level=float(median(closes[start:])))
    )
    return segments


def _span_error(
    frame: pd.DataFrame,
    *,
    start: int,
    stop: int,
    rule: str,
    detail: str,
    symbol: str | None,
    day: Any | None,
) -> dict[str, Any]:
    span_start = frame.index[start]
    span_end = frame.index[stop - 1]
    bar_count = stop - start
    return {
        "symbol": symbol,
        "day": day,
        "rule": rule,
        "span_start": span_start,
        "span_end": span_end,
        "bar_count": bar_count,
        "reason": (
            f"{rule}: {detail}; span {span_start} to {span_end} "
            f"({bar_count} bars)"
        ),
    }


def _segment_error(
    frame: pd.DataFrame,
    segment: _Segment,
    *,
    rule: str,
    detail: str,
    symbol: str | None,
    day: Any | None,
) -> dict[str, Any]:
    return _span_error(
        frame,
        start=segment.start,
        stop=segment.stop,
        rule=rule,
        detail=detail,
        symbol=symbol,
        day=day,
    )


def _run_deviates(
    frame: pd.DataFrame,
    segment: _Segment,
    *,
    reference_level: float,
    fraction: float,
) -> bool:
    run = frame.iloc[segment.start : segment.stop]
    return bool(
        (
            run["o"].map(
                lambda value: _deviates(float(value), reference_level, fraction)
            )
            | run["c"].map(
                lambda value: _deviates(float(value), reference_level, fraction)
            )
        ).any()
    )


def _wick_positions_and_errors(
    frame: pd.DataFrame,
    segments: list[_Segment],
    *,
    fraction: float,
    symbol: str | None,
    day: Any | None,
) -> tuple[set[int], list[dict[str, Any]]]:
    wick_positions: set[int] = set()
    errors: list[dict[str, Any]] = []
    for segment in segments:
        for position in range(segment.start, segment.stop):
            row = frame.iloc[position]
            high_departs = _deviates(float(row["h"]), segment.level, fraction)
            low_departs = _deviates(float(row["l"]), segment.level, fraction)
            if not (high_departs or low_departs):
                continue

            body_agrees = _agree(
                float(row["o"]),
                segment.level,
                fraction,
            ) and _agree(float(row["c"]), segment.level, fraction)
            if not body_agrees:
                continue

            departed = []
            if high_departs:
                departed.append("high")
            if low_departs:
                departed.append("low")
            timestamp = frame.index[position]
            wick_positions.add(position)
            errors.append(
                _span_error(
                    frame,
                    start=position,
                    stop=position + 1,
                    rule="rule 2a",
                    detail=(
                        f"wick retained at {timestamp}: "
                        f"{' and '.join(departed)} departed while open and close "
                        "stayed within the local level"
                    ),
                    symbol=symbol,
                    day=day,
                )
            )

    return wick_positions, errors


def _classify_shorter_against_longer(
    frame: pd.DataFrame,
    *,
    candidate: _Segment,
    reference: _Segment,
    bad_tick_neighbor_fraction: float,
    max_bad_run_bars: int,
    symbol: str | None,
    day: Any | None,
) -> tuple[list[int], list[dict[str, Any]]]:
    if reference.length <= max_bad_run_bars:
        return (
            [],
            [
                _segment_error(
                    frame,
                    candidate,
                    rule="rule 6",
                    detail=(
                        "insufficient context: reference segment has "
                        f"{reference.length} bars and does not exceed "
                        f"max_bad_run_bars={max_bad_run_bars}"
                    ),
                    symbol=symbol,
                    day=day,
                )
            ],
        )

    if candidate.length > max_bad_run_bars:
        return (
            [],
            [
                _segment_error(
                    frame,
                    candidate,
                    rule="rule 4",
                    detail=(
                        "candidate run exceeds "
                        f"max_bad_run_bars={max_bad_run_bars}"
                    ),
                    symbol=symbol,
                    day=day,
                )
            ],
        )

    if _run_deviates(
        frame,
        candidate,
        reference_level=reference.level,
        fraction=bad_tick_neighbor_fraction,
    ):
        return (list(range(candidate.start, candidate.stop)), [])

    return ([], [])


def _classify_edge_against_neighbor(
    frame: pd.DataFrame,
    *,
    edge: _Segment,
    neighbor: _Segment,
    bad_tick_neighbor_fraction: float,
    max_bad_run_bars: int,
    symbol: str | None,
    day: Any | None,
) -> tuple[list[int], list[dict[str, Any]]]:
    if edge.length == neighbor.length:
        return (
            [],
            [
                _span_error(
                    frame,
                    start=min(edge.start, neighbor.start),
                    stop=max(edge.stop, neighbor.stop),
                    rule="rule 6",
                    detail="tie between edge run and its only reference",
                    symbol=symbol,
                    day=day,
                )
            ],
        )

    if neighbor.length <= edge.length:
        return (
            [],
            [
                _segment_error(
                    frame,
                    edge,
                    rule="rule 6",
                    detail=(
                        "insufficient context: available reference side is "
                        "not strictly longer than the edge run"
                    ),
                    symbol=symbol,
                    day=day,
                )
            ],
        )

    return _classify_shorter_against_longer(
        frame,
        candidate=edge,
        reference=neighbor,
        bad_tick_neighbor_fraction=bad_tick_neighbor_fraction,
        max_bad_run_bars=max_bad_run_bars,
        symbol=symbol,
        day=day,
    )


def classify_bad_ticks(
    frame: pd.DataFrame,
    *,
    bad_tick_neighbor_fraction: float,
    max_bad_run_bars: int,
    quarantine_abort_fraction: float,
    symbol: str | None = None,
    day: Any | None = None,
) -> tuple[list[pd.Timestamp], list[dict[str, Any]]]:
    """Return timestamps to quarantine and validation errors for one symbol/day."""
    original_size = len(frame)
    segments = _segments(frame, bad_tick_neighbor_fraction)
    wick_positions, wick_errors = _wick_positions_and_errors(
        frame,
        segments,
        fraction=bad_tick_neighbor_fraction,
        symbol=symbol,
        day=day,
    )
    if len(segments) <= 1:
        return ([], wick_errors)

    quarantined_positions: set[int] = set()
    validation_errors: list[dict[str, Any]] = list(wick_errors)

    if len(segments) == 2:
        first, second = segments
        if first.length == second.length:
            validation_errors.append(
                _span_error(
                    frame,
                    start=first.start,
                    stop=second.stop,
                    rule="rule 6",
                    detail="tie between two split levels with no observed return",
                    symbol=symbol,
                    day=day,
                )
            )
        else:
            shorter, longer = (
                (first, second) if first.length < second.length else (second, first)
            )
            positions, errors = _classify_shorter_against_longer(
                frame,
                candidate=shorter,
                reference=longer,
                bad_tick_neighbor_fraction=bad_tick_neighbor_fraction,
                max_bad_run_bars=max_bad_run_bars,
                symbol=symbol,
                day=day,
            )
            quarantined_positions.update(positions)
            validation_errors.extend(errors)

    else:
        interior_candidates: set[int] = set()
        for segment_index in range(1, len(segments) - 1):
            left = segments[segment_index - 1]
            candidate = segments[segment_index]
            right = segments[segment_index + 1]
            if not _agree(left.level, right.level, bad_tick_neighbor_fraction):
                continue

            interior_candidates.add(segment_index)
            if candidate.length > max_bad_run_bars:
                validation_errors.append(
                    _segment_error(
                        frame,
                        candidate,
                        rule="rule 4",
                        detail=(
                            "candidate run exceeds "
                            f"max_bad_run_bars={max_bad_run_bars}"
                        ),
                        symbol=symbol,
                        day=day,
                    )
                )
                continue

            agreed_level = (left.level + right.level) / 2.0
            if _run_deviates(
                frame,
                candidate,
                reference_level=agreed_level,
                fraction=bad_tick_neighbor_fraction,
            ):
                quarantined_positions.update(range(candidate.start, candidate.stop))

        if 1 not in interior_candidates:
            positions, errors = _classify_edge_against_neighbor(
                frame,
                edge=segments[0],
                neighbor=segments[1],
                bad_tick_neighbor_fraction=bad_tick_neighbor_fraction,
                max_bad_run_bars=max_bad_run_bars,
                symbol=symbol,
                day=day,
            )
            quarantined_positions.update(positions)
            validation_errors.extend(errors)

        last_neighbor_index = len(segments) - 2
        if last_neighbor_index not in interior_candidates:
            positions, errors = _classify_edge_against_neighbor(
                frame,
                edge=segments[-1],
                neighbor=segments[-2],
                bad_tick_neighbor_fraction=bad_tick_neighbor_fraction,
                max_bad_run_bars=max_bad_run_bars,
                symbol=symbol,
                day=day,
            )
            quarantined_positions.update(positions)
            validation_errors.extend(errors)

    quarantined_positions.difference_update(wick_positions)
    quarantined = [frame.index[position] for position in sorted(quarantined_positions)]
    if original_size and len(quarantined) / original_size > quarantine_abort_fraction:
        flagged_count = len(quarantined)
        flagged_fraction = flagged_count / original_size
        quarantined = []
        validation_errors.append(
            _span_error(
                frame,
                start=0,
                stop=original_size,
                rule="rule 5",
                detail=(
                    "quarantine abort: "
                    f"{flagged_count} of {original_size} bars flagged "
                    f"({flagged_fraction:.3%}), exceeding "
                    f"quarantine_abort_fraction={quarantine_abort_fraction}"
                ),
                symbol=symbol,
                day=day,
            )
        )

    return (quarantined, validation_errors)


__all__ = ["classify_bad_ticks"]
