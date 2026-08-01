"""In-memory telemetry test fake."""

from __future__ import annotations


class CollectingTelemetry:
    """Capture emitted telemetry records in call order."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def emit(self, record: dict) -> None:
        self.records.append(record)


__all__ = ["CollectingTelemetry"]
