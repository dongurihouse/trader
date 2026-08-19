#!/usr/bin/env python3
"""Fetch and sanitize one read-only Robinhood account snapshot."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


TRADER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRADER_ROOT))
sys.path.insert(0, str(TRADER_ROOT / "bars"))

from bar_provider import RobinhoodClient  # noqa: E402
from bars_service import load_settings  # noqa: E402


def _data(payload: Mapping[str, Any], key: str) -> Any:
    data = payload.get("data", {})
    return data.get(key) if isinstance(data, dict) else None


def _masked(account_number: str) -> str:
    return "••••%s" % account_number[-4:]


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _position_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    positions = _data(payload, "positions")
    if not isinstance(positions, list):
        return []
    rows = []
    for position in positions:
        if not isinstance(position, dict) or not position.get("symbol"):
            continue
        rows.append(
            {
                "symbol": str(position["symbol"]),
                "quantity": position.get("quantity"),
                "shares_available_for_sells": position.get("shares_available_for_sells"),
                "average_buy_price": position.get("average_buy_price"),
                "intraday_quantity": position.get("intraday_quantity"),
                "type": position.get("type", "long"),
            }
        )
    return rows


def _quote_map(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    data = payload.get("data", {})
    if not isinstance(data, dict):
        return {}
    quotes = data.get("quotes", [])
    if not isinstance(quotes, list):
        return {}
    return {
        str(quote["symbol"]): quote
        for quote in quotes
        if isinstance(quote, dict) and quote.get("symbol")
    }


def _with_quotes(
    positions: list[dict[str, Any]], quotes: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    for position in positions:
        quote = quotes.get(str(position["symbol"]), {})
        last_price = quote.get("last_trade_price") or quote.get("price")
        quantity = _decimal(position.get("quantity"))
        price = _decimal(last_price)
        average = _decimal(position.get("average_buy_price"))
        position["last_trade_price"] = last_price
        position["market_value"] = (
            format(quantity * price, "f")
            if quantity is not None and price is not None
            else None
        )
        position["unrealized_profit_loss"] = (
            format(quantity * (price - average), "f")
            if quantity is not None and price is not None and average is not None
            else None
        )
    return positions


def _order_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    orders = _data(payload, "orders")
    if not isinstance(orders, list):
        return []
    rows = []
    for order in orders[:25]:
        if not isinstance(order, dict):
            continue
        rows.append(
            {
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "type": order.get("type"),
                "state": order.get("state"),
                "quantity": order.get("quantity"),
                "dollar_based_amount": order.get("dollar_based_amount"),
                "cumulative_quantity": order.get("cumulative_quantity"),
                "average_price": order.get("average_price"),
                "fees": order.get("fees"),
                "placed_agent": order.get("placed_agent"),
                "created_at": order.get("created_at"),
                "last_transaction_at": order.get("last_transaction_at"),
            }
        )
    return rows


async def _snapshot(config_path: Path, account_number: str) -> dict[str, Any]:
    settings = load_settings(config_path)
    client = RobinhoodClient(settings.provider)
    warnings: list[str] = []
    async with client.session() as active:
        accounts_payload = await active.call_tool("get_accounts", {}, "accounts")
        accounts = _data(accounts_payload, "accounts")
        account = (
            next(
                (
                    item
                    for item in accounts
                    if isinstance(item, dict)
                    and item.get("account_number") == account_number
                ),
                None,
            )
            if isinstance(accounts, list)
            else None
        )
        if account is None:
            raise ValueError("configured Robinhood account was not found")

        portfolio_payload = await active.call_tool(
            "get_portfolio", {"account_number": account_number}, "portfolio"
        )
        positions_payload = await active.call_tool(
            "get_equity_positions",
            {"account_number": account_number},
            "equity positions",
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        orders_payload = await active.call_tool(
            "get_equity_orders",
            {
                "account_number": account_number,
                "created_at_gte": cutoff,
                "placed_agent": "agentic",
            },
            "agentic equity orders",
        )
        positions = _position_rows(positions_payload)
        quotes: dict[str, dict[str, Any]] = {}
        if positions:
            try:
                quote_payload = await active.call_tool(
                    "get_equity_quotes",
                    {"symbols": [row["symbol"] for row in positions]},
                    "equity quotes",
                )
                quotes = _quote_map(quote_payload)
            except Exception as exc:
                warnings.append("Position quotes unavailable: %s" % type(exc).__name__)

    raw_portfolio = portfolio_payload.get("data", {})
    if not isinstance(raw_portfolio, dict):
        raw_portfolio = {}
    buying_power = raw_portfolio.get("buying_power", {})
    if not isinstance(buying_power, dict):
        buying_power = {}
    return {
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
        "source": "Robinhood MCP",
        "account": {
            "masked_number": _masked(account_number),
            "nickname": account.get("nickname"),
            "brokerage_account_type": account.get("brokerage_account_type"),
            "trading_type": account.get("type"),
            "state": account.get("state"),
            "agentic_allowed": bool(account.get("agentic_allowed")),
            "unsettled_funds": account.get("unsettled_funds"),
        },
        "portfolio": {
            "total_value": raw_portfolio.get("total_value"),
            "cash": raw_portfolio.get("cash"),
            "equity_value": raw_portfolio.get("equity_value"),
            "options_value": raw_portfolio.get("options_value"),
            "pending_deposits": raw_portfolio.get("pending_deposits"),
            "currency": raw_portfolio.get("currency", "USD"),
            "buying_power": buying_power.get("buying_power"),
            "unleveraged_buying_power": buying_power.get("unleveraged_buying_power"),
        },
        "positions": _with_quotes(positions, quotes),
        "orders": _order_rows(orders_payload),
        "warnings": warnings,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: robinhood_snapshot.py CONFIG", file=sys.stderr)
        return 2
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or not isinstance(
            request.get("account_number"), str
        ):
            raise ValueError("account_number is required")
        payload = asyncio.run(
            _snapshot(Path(sys.argv[1]).resolve(), request["account_number"].strip())
        )
    except BaseException as exc:
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
