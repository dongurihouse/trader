"""Shared CSS for self-contained console HTML pages."""

from __future__ import annotations


BASE_CSS = """
    :root {
      color-scheme: dark;
      --bg: #0b1015;
      --panel: #121a22;
      --panel-edge: #263441;
      --text: #e4edf4;
      --muted: #8fa3b2;
      --accent: #61d6a9;
      --danger: #ff7b79;
      --warning: #f0c36c;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas,
        "Liberation Mono", "Courier New", monospace;
    }

    main {
      width: min(1500px, 100%);
      margin: 0 auto;
      padding: 24px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 14px;
    }

    h1, h2 { margin: 0; }
    h1 { font-size: 22px; letter-spacing: 0.02em; }
    h2 { margin-bottom: 12px; font-size: 15px; color: var(--accent); }

    .console-nav {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 0 0 18px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--panel-edge);
    }

    .console-nav-link {
      padding: 6px 10px;
      border: 1px solid transparent;
      border-radius: 8px;
      color: var(--muted);
      text-decoration: none;
    }

    .console-nav-link:hover,
    .console-nav-link:focus {
      border-color: var(--panel-edge);
      color: var(--text);
      outline: none;
    }

    .console-nav-link[aria-current="page"],
    .console-nav-link.is-current {
      border-color: var(--accent);
      color: var(--accent);
      background: rgba(97, 214, 169, 0.08);
    }

    .panel {
      min-width: 0;
      padding: 16px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      background: var(--panel);
    }

    .wide { grid-column: 1 / -1; }
    .table-wrap { overflow-x: auto; }

    table {
      width: 100%;
      border-collapse: collapse;
      font-variant-numeric: tabular-nums;
    }

    th, td {
      padding: 8px 10px;
      border-bottom: 1px solid var(--panel-edge);
      text-align: right;
      white-space: nowrap;
    }

    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {
      text-align: left;
    }

    th { color: var(--muted); font-size: 12px; font-weight: 600; }

    .empty { color: var(--muted); }
"""
