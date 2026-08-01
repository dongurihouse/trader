"""JSON serialization for contract records.

Telemetry events retain their documented flat ``ev`` envelope. Records without an
event discriminator use the reserved ``__type__`` key for reconstruction.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from types import UnionType
from typing import Iterator, Union, get_args, get_origin, get_type_hints

from .intents import Intent
from .orders import Fill, OrderTicket, PortfolioState, PositionState, Rejection
from .telemetry import AlgoMetrics, EVENT_TYPES
from .types import Bar


_TYPE_KEY = "__type__"
_TYPE_REGISTRY: dict[str, type] = {
    record_type.__name__: record_type
    for record_type in (
        Bar,
        Intent,
        OrderTicket,
        Fill,
        Rejection,
        PositionState,
        PortfolioState,
        AlgoMetrics,
    )
}
_EVENT_RECORD_TYPES = frozenset(EVENT_TYPES.values())
_SUPPORTED_RECORD_TYPES = frozenset(_TYPE_REGISTRY.values()) | _EVENT_RECORD_TYPES


def _datetime_to_json(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("contract record datetimes must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _value_to_json(value: object) -> object:
    if isinstance(value, datetime):
        return _datetime_to_json(value)
    if type(value) in _SUPPORTED_RECORD_TYPES:
        return record_to_json(value)
    if isinstance(value, list):
        return [_value_to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _value_to_json(item) for key, item in value.items()}
    return value


def record_to_json(obj: object) -> dict:
    """Convert one supported contract dataclass into a JSON-safe dictionary."""
    record_type = type(obj)
    if record_type not in _SUPPORTED_RECORD_TYPES or not is_dataclass(obj):
        raise TypeError(f"unsupported contract record type: {record_type.__name__}")

    result = {
        field.name: _value_to_json(getattr(obj, field.name))
        for field in fields(obj)
    }
    if record_type in _TYPE_REGISTRY.values():
        result[_TYPE_KEY] = record_type.__name__
    return result


def _datetime_from_json(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _value_from_json(value: object, annotation: object) -> object:
    if value is None:
        return None
    if annotation is datetime:
        if not isinstance(value, str):
            raise TypeError("serialized datetime fields must be strings")
        return _datetime_from_json(value)

    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        non_none_types = [
            item for item in get_args(annotation) if item is not type(None)
        ]
        if len(non_none_types) == 1:
            return _value_from_json(value, non_none_types[0])
    if origin is list:
        (item_type,) = get_args(annotation)
        return [_value_from_json(item, item_type) for item in value]
    if isinstance(annotation, type) and is_dataclass(annotation):
        nested = record_from_json(value)
        if type(nested) is not annotation:
            raise TypeError(
                f"expected nested {annotation.__name__}, got {type(nested).__name__}"
            )
        return nested
    return value


def record_from_json(data: dict) -> object:
    """Reconstruct one supported contract dataclass from its JSON dictionary."""
    if not isinstance(data, dict):
        raise TypeError("serialized contract records must be dictionaries")

    payload = dict(data)
    type_name = payload.pop(_TYPE_KEY, None)
    if type_name is not None:
        record_type = _TYPE_REGISTRY.get(type_name)
    else:
        record_type = EVENT_TYPES.get(payload.get("ev"))
    if record_type is None:
        raise ValueError("serialized contract record has no recognized discriminator")

    annotations = get_type_hints(record_type)
    for field_name, annotation in annotations.items():
        if field_name in payload:
            payload[field_name] = _value_from_json(payload[field_name], annotation)
    return record_type(**payload)


def append_jsonl(path: str | Path, record: dict) -> None:
    """Append one JSON-safe record to a JSONL file and flush the line."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        json.dump(record, handle)
        handle.write("\n")
        handle.flush()


def read_jsonl(path: str | Path) -> Iterator[dict]:
    """Yield plain dictionaries parsed from a JSONL file."""
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


__all__ = ["append_jsonl", "read_jsonl", "record_from_json", "record_to_json"]
