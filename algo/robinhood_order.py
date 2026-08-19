#!/usr/bin/env python3
"""Place one Robinhood equity order read from standard input."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Mapping


TRADER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRADER_ROOT))
sys.path.insert(0, str(TRADER_ROOT / "bars"))

from bar_provider import RobinhoodClient  # noqa: E402
from bars_service import load_settings  # noqa: E402


async def _place(config_path: Path, request: Mapping[str, Any]) -> dict:
    settings = load_settings(config_path)
    client = RobinhoodClient(settings.provider)
    async with client.session() as active:
        return await active.call_tool(
            "place_equity_order",
            dict(request),
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
