"""Top-level CLI dispatcher for independently owned component commands.

Each component may expose ``trader.<component>.cli.register(subparsers)``.
``register`` adds its own argparse subparser(s) and assigns each command handler with
``subparser.set_defaults(func=handler)``. Handlers accept the parsed namespace and
return an integer exit code or ``None`` for success.
"""

from __future__ import annotations

import argparse
import importlib
import sys


_COMPONENTS = ("provider", "runtime", "creators", "execution", "console")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader")
    subparsers = parser.add_subparsers(dest="command")

    for component in _COMPONENTS:
        module_name = f"trader.{component}.cli"
        try:
            component_cli = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name not in {f"trader.{component}", module_name}:
                raise
            continue

        register = getattr(component_cli, "register", None)
        if callable(register):
            register(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch to a component-owned handler."""
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Registered component commands attach their handler with set_defaults(func=...).
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return 0

    result = handler(args)
    return 0 if result is None else int(result)
