#!/usr/bin/env python3
"""Place one Robinhood equity order read from standard input."""

from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


TRADER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRADER_ROOT))
sys.path.insert(0, str(TRADER_ROOT / "bars"))

from bar_provider import RobinhoodClient  # noqa: E402
from bars_service import load_settings  # noqa: E402


def _filled_quantity(order: Mapping[str, Any]) -> str:
    raw = order.get("cumulative_quantity", "0")
    try:
        quantity = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError("entry order has invalid filled quantity: %r" % raw) from exc
    if not quantity.is_finite() or quantity < 0:
        raise ValueError("entry order has invalid filled quantity: %r" % raw)
    return format(quantity, "f")


async def _place(config_path: Path, raw_request: Mapping[str, Any]) -> dict:
    settings = load_settings(config_path)
    client = RobinhoodClient(settings.provider)
    request = dict(raw_request)
    entry_order_ids = request.pop("entry_order_ids", None)
    async with client.session() as active:
        if entry_order_ids is not None:
            if not isinstance(entry_order_ids, list) or not entry_order_ids:
                raise ValueError("entry_order_ids must be a non-empty list")
            filled = Decimal("0")
            states = []
            for entry_order_id in entry_order_ids:
                lookup = await active.call_tool(
                    "get_equity_orders",
                    {
                        "account_number": request["account_number"],
                        "order_id": entry_order_id,
                    },
                    "get entry equity order",
                )
                data = lookup.get("data", {}) if isinstance(lookup, dict) else {}
                orders = data.get("orders", []) if isinstance(data, dict) else []
                order = orders[0] if isinstance(orders, list) and orders else None
                if not isinstance(order, dict):
                    raise ValueError(
                        "entry order does not exist: %s" % entry_order_id
                    )
                if (
                    order.get("side") != "buy"
                    or order.get("symbol") != request["symbol"]
                ):
                    raise ValueError(
                        "entry order does not match close: %s %s"
                        % (order.get("side"), order.get("symbol"))
                    )
                filled += Decimal(_filled_quantity(order))
                states.append(str(order.get("state", "unknown")))
            if filled == 0:
                return {
                    "skipped": "accepted entries have no fills (states=%s)"
                    % ",".join(states)
                }
            request["quantity"] = format(filled, "f")
        return await active.call_tool(
            "place_equity_order",
            request,
            "place equity order",
        )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: robinhood_order.py CONFIG", file=sys.stderr)
        return 2
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("order request must be a JSON object")
        response = asyncio.run(_place(Path(sys.argv[1]).resolve(), request))
    except BaseException as exc:
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print(json.dumps(response, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
