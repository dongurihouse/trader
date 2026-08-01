"""Pure strategy boundary and roster specification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from .intents import Intent
from .market import MarketData
from .types import AlgoStatus


class Algo(Protocol):
    @property
    def id(self) -> str: ...

    def warmup(self, day: date, data: MarketData) -> None: ...

    def on_bar(self, asof: datetime, data: MarketData) -> list[Intent]: ...


@dataclass
class AlgoSpec:
    id: str
    factory: str
    params: dict
    status: AlgoStatus


__all__ = ["Algo", "AlgoSpec"]
