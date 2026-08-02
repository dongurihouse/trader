"""Provider-owned ``trader`` CLI subcommands."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path
import sys

from trader.provider import ingest as provider_ingest
from trader.provider import relay, store
from trader.provider.calendar import load_calendar


def _symbols(value: str) -> list[str]:
    return [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]


def _date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def handle_fetch(args: argparse.Namespace) -> int:
    """Fetch one session through the headless MCP relay and save its raw dump."""
    symbols = _symbols(args.symbols)
    day = _date(args.day)
    try:
        calendar = load_calendar(Path(args.calendar_path))
        start_iso, end_iso = relay.session_range_iso(calendar, day)
        payload = relay.run_relay(
            symbols,
            start_iso=start_iso,
            end_iso=end_iso,
            interval=args.interval,
            bounds=args.bounds,
            tool=args.tool,
            claude_bin=args.claude_bin,
            model=args.model,
        )
        relay.validate_payload(payload, symbols, start_iso, end_iso)
        path = relay.save_raw(
            payload,
            Path(args.data_root) / "raw" / "robinhood",
            day,
            args.tag or day.isoformat(),
        )
    except relay.RelayError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(path)
    return 0


def handle_ingest(args: argparse.Namespace) -> int:
    """Ingest all raw Robinhood dumps through the existing A2 pipeline."""
    summary = provider_ingest.ingest_all(
        Path(args.raw_root),
        Path(args.data_root),
        etf_leverage_factor=args.etf_leverage_factor,
        etf_tolerance=args.etf_tolerance,
        bad_tick_neighbor_fraction=args.bad_tick_neighbor_fraction,
    )
    for symbol in sorted(summary.keys() - {"quarantined", "etf_warnings"}):
        symbol_summary = summary[symbol]
        print(
            f"{symbol}: bars={symbol_summary['bars']} "
            f"days={symbol_summary['days']} "
            f"min_date={symbol_summary['min_date']} "
            f"max_date={symbol_summary['max_date']}"
        )
    print(f"quarantined={len(summary['quarantined'])}")
    print(f"etf_warnings={len(summary['etf_warnings'])}")
    return 0


def _days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def handle_validate(args: argparse.Namespace) -> int:
    """Read and health-check already committed minute-bar files."""
    data_root = Path(args.data_root)
    symbols = _symbols(args.symbols)
    found = False

    for day in _days(_date(args.start), _date(args.end)):
        frames = {
            symbol: store.read_1m_day(data_root, symbol, day)
            for symbol in symbols
        }
        for symbol, frame in frames.items():
            if frame is None or frame.empty:
                continue
            ordered = frame.sort_index()
            for position in range(1, len(ordered) - 1):
                field = provider_ingest.is_bad_tick(
                    ordered,
                    position,
                    args.bad_tick_neighbor_fraction,
                )
                if field is None:
                    continue
                column = "h" if field == "high" else "l"
                timestamp = ordered.index[position]
                value = float(ordered.iloc[position][column])
                print(
                    f"{day.isoformat()} {symbol} bad_tick "
                    f"timestamp={timestamp.isoformat()} "
                    f"field={field} value={value}"
                )
                found = True

        underlying = frames.get(args.underlying_symbol)
        if underlying is None and args.underlying_symbol not in frames:
            underlying = store.read_1m_day(
                data_root,
                args.underlying_symbol,
                day,
            )
        for symbol, frame in frames.items():
            warning = provider_ingest.etf_leverage_warning(
                symbol,
                day,
                underlying,
                frame,
                etf_leverage_factor=args.etf_leverage_factor,
                etf_tolerance=args.etf_tolerance,
            )
            if warning is None:
                continue
            print(
                f"{day.isoformat()} {symbol} leverage_warning "
                f"expected_range={warning['expected_range']} "
                f"actual_ratio={warning['actual_ratio']}"
            )
            found = True

    return 1 if found else 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the provider component's top-level CLI commands."""
    fetch = subparsers.add_parser("fetch", help="fetch raw provider bars")
    fetch.add_argument("--symbols", required=True)
    fetch.add_argument("--day", required=True)
    fetch.add_argument("--data-root", default="data")
    fetch.add_argument("--calendar-path", default="config/calendar.yaml")
    fetch.add_argument("--claude-bin", default=relay.DEFAULT_CLAUDE_BIN)
    fetch.add_argument("--tool", default=relay.DEFAULT_TOOL)
    fetch.add_argument("--model", default=relay.DEFAULT_MODEL)
    fetch.add_argument("--tag", default=None)
    fetch.add_argument("--interval", default="minute")
    fetch.add_argument("--bounds", default="regular")
    fetch.set_defaults(func=handle_fetch)

    ingest = subparsers.add_parser("ingest", help="ingest raw provider bars")
    ingest.add_argument("--data-root", default="data")
    ingest.add_argument("--raw-root", default="data/raw/robinhood")
    ingest.add_argument("--etf-leverage-factor", type=float, default=2.0)
    ingest.add_argument("--etf-tolerance", type=float, default=0.25)
    ingest.add_argument(
        "--bad-tick-neighbor-fraction",
        type=float,
        default=0.05,
    )
    ingest.set_defaults(func=handle_ingest)

    validate = subparsers.add_parser(
        "validate",
        help="validate stored provider bars",
    )
    validate.add_argument("--data-root", default="data")
    validate.add_argument("--symbols", required=True)
    validate.add_argument("--start", required=True)
    validate.add_argument("--end", required=True)
    validate.add_argument("--etf-leverage-factor", type=float, default=2.0)
    validate.add_argument("--etf-tolerance", type=float, default=0.25)
    validate.add_argument(
        "--bad-tick-neighbor-fraction",
        type=float,
        default=0.05,
    )
    validate.add_argument("--underlying-symbol", default="SNDK")
    validate.set_defaults(func=handle_validate)


__all__ = ["handle_fetch", "handle_ingest", "handle_validate", "register"]
