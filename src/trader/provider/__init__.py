"""Parquet-backed market-data provider."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from trader.contracts import MarketData
from trader.provider.market import ProviderMarketData

if TYPE_CHECKING:
    from trader.execution.config import ResolvedConfig


def build_market_data(config: ResolvedConfig) -> MarketData:
    """Build the provider-owned market-data service from resolved config."""
    return ProviderMarketData(
        data_root=Path(config.trader.data_root),
        calendar_path=Path("config/calendar.yaml"),
        primary_symbol=config.trader.primary_symbol,
    )


__all__ = ["ProviderMarketData", "build_market_data"]
