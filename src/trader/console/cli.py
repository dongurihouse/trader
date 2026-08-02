"""Command-line registration for console-owned commands."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from trader.console.config import load_console_config
from trader.console.report import write_report
from trader.console.server import ConsolePortInUseError, run_server
from trader.console.sessions import resolve_session_dir


def register(subparsers) -> None:
    """Register console component commands with the top-level parser."""
    _register_report(subparsers)
    _register_console(subparsers)


def _register_report(subparsers) -> None:
    parser = subparsers.add_parser(
        "report", help="write a post-session Markdown report"
    )
    parser.add_argument("session", help="session id to report")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="configuration directory (default: config)",
    )
    parser.set_defaults(func=_handle_report)


def _register_console(subparsers) -> None:
    parser = subparsers.add_parser(
        "console", help="serve a live session dashboard"
    )
    parser.add_argument(
        "session",
        nargs="?",
        default=None,
        help="session id to serve (default: newest)",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="configuration directory (default: config)",
    )
    parser.set_defaults(func=_handle_console)


def _handle_report(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir)
    config = load_console_config(
        config_dir,
        base_dir=config_dir.resolve().parent,
    )
    try:
        session_dir = resolve_session_dir(config.data_root, args.session)
    except FileNotFoundError as exc:
        print(f"Unable to generate report: {exc}", file=sys.stderr)
        return 1

    markdown = write_report(session_dir)
    print(markdown, end="")
    return 0


def _handle_console(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir)
    config = load_console_config(
        config_dir,
        base_dir=config_dir.resolve().parent,
    )
    try:
        session_dir = resolve_session_dir(config.data_root, args.session)
    except FileNotFoundError as exc:
        if args.session is None:
            print(
                f"Unable to start console: no sessions exist ({exc})",
                file=sys.stderr,
            )
        else:
            print(f"Unable to serve session {args.session!r}: {exc}", file=sys.stderr)
        return 1

    session_id = session_dir.name
    try:
        with run_server(config) as server:
            host, port = server.server_address
            print(
                f"Serving session {session_id!r} at "
                f"http://{host}:{port}/?session={session_id}",
                flush=True,
            )
            _wait_until_interrupted()
    except ConsolePortInUseError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def _wait_until_interrupted(poll_interval: float = 0.2) -> None:
    try:
        while True:
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        return
