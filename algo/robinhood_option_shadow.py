#!/usr/bin/env python3
"""Quote one conservative option shadow entry or exit from standard input."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


TRADER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRADER_ROOT))
sys.path.insert(0, str(TRADER_ROOT / "bars"))

from bar_provider import RobinhoodClient  # noqa: E402
from bars_service import load_settings  # noqa: E402


EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MAX_INSTRUMENT_PAGES = 50
QUOTE_BATCH_SIZE = 20


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("%s is not a decimal" % name) from exc
    if not result.is_finite():
        raise ValueError("%s is not finite" % name)
    return result


def _data(payload: Mapping[str, Any], key: str) -> Any:
    data = payload.get("data", {})
    return data.get(key) if isinstance(data, dict) else None


def _cursor(next_url: Any) -> str:
    if not isinstance(next_url, str) or not next_url:
        return ""
    values = parse_qs(urlparse(next_url).query).get("cursor", [])
    return values[0] if values else ""


def _quote_map(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    results = _data(payload, "results")
    if not isinstance(results, list):
        return {}
    quotes: dict[str, Mapping[str, Any]] = {}
    for result in results:
        quote = result.get("quote", {}) if isinstance(result, dict) else {}
        if isinstance(quote, dict) and quote.get("instrument_id"):
            quotes[str(quote["instrument_id"])] = quote
    return quotes


async def _expiration_dates(active: RobinhoodClient, ticker: str) -> list[str]:
    payload = await active.call_tool(
        "get_option_chains",
        {"underlying_symbol": ticker},
        "%s option chains" % ticker,
    )
    chains = _data(payload, "chains")
    today = datetime.now(tz=EASTERN).date().isoformat()
    dates = {
        str(expiration)
        for chain in chains if isinstance(chain, dict)
        for expiration in chain.get("expiration_dates", [])
        if isinstance(expiration, str) and expiration >= today
    } if isinstance(chains, list) else set()
    return sorted(dates)


async def _nearest_otm_instrument(
    active: RobinhoodClient,
    ticker: str,
    expiration_date: str,
    option_type: str,
    underlying_price: Decimal,
) -> Mapping[str, Any] | None:
    request: dict[str, Any] = {
        "chain_symbol": ticker,
        "expiration_dates": expiration_date,
        "type": option_type,
    }
    candidates: list[Mapping[str, Any]] = []
    for _page in range(MAX_INSTRUMENT_PAGES):
        payload = await active.call_tool(
            "get_option_instruments",
            request,
            "%s %s instruments" % (ticker, option_type),
        )
        rows = _data(payload, "instruments")
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if (
                isinstance(row, dict)
                and row.get("state") == "active"
                and row.get("tradability") == "tradable"
                and row.get("type") == option_type
            ):
                candidates.append(row)
        strikes = [
            _decimal(row.get("strike_price"), "strike_price")
            for row in candidates
        ]
        crossed_spot = any(
            strike > underlying_price for strike in strikes
        )
        if crossed_spot or not _cursor(
            payload.get("data", {}).get("next")
            if isinstance(payload.get("data"), dict)
            else None
        ):
            break
        request["cursor"] = _cursor(payload["data"]["next"])

    if option_type == "call":
        eligible = [
            row
            for row in candidates
            if _decimal(row.get("strike_price"), "strike_price") > underlying_price
        ]
        key = lambda row: _decimal(row.get("strike_price"), "strike_price")
        return min(eligible, key=key) if eligible else None
    eligible = [
        row
        for row in candidates
        if _decimal(row.get("strike_price"), "strike_price") < underlying_price
    ]
    key = lambda row: _decimal(row.get("strike_price"), "strike_price")
    return max(eligible, key=key) if eligible else None


def _before_sellout(instrument: Mapping[str, Any]) -> bool:
    raw = instrument.get("sellout_datetime")
    if not isinstance(raw, str) or not raw:
        return True
    sellout = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.now(tz=UTC) < sellout


async def _entry(
    active: RobinhoodClient, request: Mapping[str, Any]
) -> dict[str, Any]:
    ticker = str(request.get("ticker", "")).strip().upper()
    direction = int(request.get("direction", 0))
    if not ticker or direction not in (-1, 1):
        raise ValueError("entry ticker or direction is invalid")
    underlying_price = _decimal(request.get("underlying_price"), "underlying_price")
    if underlying_price <= 0:
        raise ValueError("underlying_price must be positive")
    option_type = "call" if direction == 1 else "put"
    expirations = await _expiration_dates(active, ticker)
    if not expirations:
        raise ValueError("%s has no active option expiration" % ticker)

    instrument = None
    for expiration in expirations:
        candidate = await _nearest_otm_instrument(
            active,
            ticker,
            expiration,
            option_type,
            underlying_price,
        )
        if candidate is not None and _before_sellout(candidate):
            instrument = candidate
            break
    if instrument is None:
        raise ValueError("%s has no tradable near-OTM %s" % (ticker, option_type))

    option_id = str(instrument["id"])
    quote_payload = await active.call_tool(
        "get_option_quotes",
        {"instrument_ids": [option_id]},
        "%s option entry quote" % ticker,
    )
    quote = _quote_map(quote_payload).get(option_id)
    if quote is None:
        raise ValueError("selected option has no quote")
    ask = _decimal(quote.get("ask_price"), "entry ask")
    if ask <= 0:
        raise ValueError("selected option has no positive ask")
    return {
        "status": "open",
        "option_id": option_id,
        "option_type": option_type,
        "expiration_date": str(instrument["expiration_date"]),
        "strike_price": float(_decimal(instrument["strike_price"], "strike_price")),
        "underlying_price": float(underlying_price),
        "entry_ask": float(ask),
        "entry_quote_ts": quote.get("updated_at"),
    }


async def _exit(
    active: RobinhoodClient, positions: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    quotes: dict[str, Mapping[str, Any]] = {}
    option_ids = [str(position["option_id"]) for position in positions]
    for offset in range(0, len(option_ids), QUOTE_BATCH_SIZE):
        batch = option_ids[offset : offset + QUOTE_BATCH_SIZE]
        payload = await active.call_tool(
            "get_option_quotes",
            {"instrument_ids": batch},
            "option shadow exit quotes",
        )
        quotes.update(_quote_map(payload))

    results = []
    for position in positions:
        option_id = str(position["option_id"])
        base = {"entry_ts": int(position["entry_ts"]), "option_id": option_id}
        quote = quotes.get(option_id)
        if quote is None:
            results.append(
                {**base, "status": "exit_error", "error": "option exit quote unavailable"}
            )
            continue
        try:
            entry_ask = _decimal(position.get("entry_ask"), "entry ask")
            exit_bid = _decimal(quote.get("bid_price"), "exit bid")
            if entry_ask <= 0 or exit_bid < 0:
                raise ValueError("option shadow prices are invalid")
            return_pct = ((exit_bid / entry_ask) - Decimal("1")) * Decimal("100")
            pnl_dollars = (exit_bid - entry_ask) * Decimal("100")
            results.append(
                {
                    **base,
                    "status": "closed",
                    "exit_bid": float(exit_bid),
                    "exit_quote_ts": quote.get("updated_at"),
                    "return_pct": round(float(return_pct), 4),
                    "pnl_dollars": round(float(pnl_dollars), 4),
                }
            )
        except ValueError as exc:
            results.append({**base, "status": "exit_error", "error": str(exc)})
    return results


async def _run(config_path: Path, request: Mapping[str, Any]) -> dict[str, Any]:
    settings = load_settings(config_path)
    client = RobinhoodClient(settings.provider)
    async with client.session() as active:
        action = request.get("action")
        if action == "entry":
            return {"result": await _entry(active, request)}
        if action == "exit":
            positions = request.get("positions")
            if not isinstance(positions, list) or not positions:
                raise ValueError("exit positions must be a non-empty list")
            return {"results": await _exit(active, positions)}
        raise ValueError("action must be entry or exit")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: robinhood_option_shadow.py CONFIG", file=sys.stderr)
        return 2
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        response = asyncio.run(_run(Path(sys.argv[1]).resolve(), request))
    except BaseException as exc:
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print(json.dumps(response, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
