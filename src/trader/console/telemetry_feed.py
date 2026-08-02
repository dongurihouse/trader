"""Incremental reading of JSONL telemetry files."""

from __future__ import annotations

import json
from pathlib import Path


def read_new_lines(path: Path, offset: int) -> tuple[int, list[dict]]:
    """Parse complete JSONL records after *offset* without consuming a partial line."""
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read()
    except FileNotFoundError:
        return offset, []

    lines = data.split(b"\n")
    complete_lines = lines[:-1]
    consumed_bytes = sum(len(line) + 1 for line in complete_lines)
    records: list[dict] = []
    for line in complete_lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            # Telemetry is append-only from one trusted producer, so malformed
            # complete lines are ignorable noise (for example, a torn write).
            continue
    return offset + consumed_bytes, records
