"""Tests for shared console navigation and base styles."""

from __future__ import annotations

from trader.console.nav import NAV_ITEMS, render_nav_html
from trader.console.styles import BASE_CSS


def test_render_nav_html_marks_active_link_and_contains_all_hrefs() -> None:
    assert NAV_ITEMS == (
        ("Live", "/"),
        ("Provider", "/workbench/provider"),
        ("Algos", "/workbench/algos"),
        ("Execution", "/workbench/execution"),
        ("Results", "/results"),
    )

    html = render_nav_html("/workbench/algos")

    for _label, href in NAV_ITEMS:
        assert f'href="{href}"' in html

    assert 'href="/workbench/algos" aria-current="page"' in html
    assert html.count('aria-current="page"') == 1


def test_base_css_contains_shared_dark_theme_and_nav_rules() -> None:
    for token in (
        "--bg",
        "--panel",
        "--panel-edge",
        "--text",
        "--muted",
        "--accent",
        "--danger",
        "--warning",
    ):
        assert token in BASE_CSS

    assert ".console-nav" in BASE_CSS
    assert "[aria-current=\"page\"]" in BASE_CSS
