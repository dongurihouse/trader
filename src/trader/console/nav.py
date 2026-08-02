"""Shared navigation for console-owned HTML pages."""

from __future__ import annotations

from html import escape


NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("Live", "/"),
    ("Provider", "/workbench/provider"),
    ("Algos", "/workbench/algos"),
    ("Execution", "/workbench/execution"),
    ("Results", "/results"),
)


def render_nav_html(active_href: str) -> str:
    """Return a console navigation bar with the active page marked."""
    links: list[str] = []
    for label, href in NAV_ITEMS:
        classes = "console-nav-link"
        attributes = [f'href="{escape(href, quote=True)}"']
        if href == active_href:
            classes += " is-current"
            attributes.append('aria-current="page"')
        attributes.append(f'class="{classes}"')
        links.append(f'<a {" ".join(attributes)}>{escape(label)}</a>')

    return (
        '<nav class="console-nav" aria-label="Console sections">'
        + "".join(links)
        + "</nav>"
    )
