"""Command-line entry points owned by the execution component."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from trader.execution.config import load_config


_FILL_KINDS = ("entry", "stop", "target", "reversal", "eod")


def _parse_timestamp(value: str) -> datetime:
    """Parse ISO-8601 timestamps, including the codebase's trailing-Z form."""
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    return datetime.fromisoformat(value)


def _handle_fills(args) -> int:
    """Reject an incomplete ``fills`` command with its specific usage."""
    args._fills_parser.print_usage()
    return 1


def _handle_fills_record(args) -> None:
    """Append a human-confirmed broker fill to the selected session."""
    # Import lazily so parser registration stays independent of broker startup.
    from trader.execution.broker.manual import record_fill

    resolved = load_config()
    state_dir = Path(resolved.trader.data_root) / "sessions" / args.session
    ts = _parse_timestamp(args.ts) if args.ts is not None else None
    record_fill(
        state_dir,
        ticket_id=args.ticket_id,
        price=args.price,
        shares=args.shares,
        kind=args.kind,
        ts=ts,
    )
    print(
        f"Recorded {args.kind} fill for ticket {args.ticket_id} "
        f"in session {args.session}."
    )


def _register_fills(subparsers) -> None:
    """Register the ``fills`` command family."""
    fills_parser = subparsers.add_parser("fills")
    fills_parser.set_defaults(func=_handle_fills, _fills_parser=fills_parser)
    fills_subparsers = fills_parser.add_subparsers(dest="fills_command")

    record_parser = fills_subparsers.add_parser("record")
    record_parser.add_argument("--session", required=True)
    record_parser.add_argument("--ticket", required=True, dest="ticket_id")
    record_parser.add_argument("--price", required=True, type=float)
    record_parser.add_argument("--shares", required=True, type=int)
    record_parser.add_argument("--kind", required=True, choices=_FILL_KINDS)
    record_parser.add_argument("--ts", default=None)
    record_parser.set_defaults(func=_handle_fills_record)


def register(subparsers) -> None:
    """Register all execution component commands with the top-level parser."""
    _register_fills(subparsers)
