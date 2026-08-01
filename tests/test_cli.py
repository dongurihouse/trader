"""Tests for the top-level CLI scaffold."""

import argparse
import importlib

import pytest


def _cli_module():
    return importlib.import_module("trader.cli")


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _cli_module().main(["--help"])

    assert exc_info.value.code == 0


def test_no_subcommand_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    result = _cli_module().main([])

    assert result == 0
    assert "usage:" in capsys.readouterr().out


def test_parser_builds_without_component_packages() -> None:
    parser = _cli_module()._build_parser()

    assert isinstance(parser, argparse.ArgumentParser)
