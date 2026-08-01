"""Command-line registration for console-owned commands."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from trader.console.config import load_console_config
from trader.console.report import write_report
from trader.console.sessions import resolve_session_dir


def register(subparsers) -> None:
    """Register console component commands with the top-level parser."""
    _register_report(subparsers)


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
