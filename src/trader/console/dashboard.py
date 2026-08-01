"""Self-contained HTML dashboard for live console telemetry."""

from __future__ import annotations


SHADOW_CAVEAT_TEXT = (
    "Shadow metrics simulate every probe-algo intent and every risk-rejected "
    "intent from emitting algos. The predecessor program measured that shadow "
    "leaderboards overstate the real edge: the apparent edge concentrated in "
    "candidates the rules refused (recorded examples: 29 shadow candidates to 2 "
    "emitted trades; 13 to 1). Treat shadow numbers as a look at what got "
    "refused, never as a forecast of promoted performance."
)


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>trader.console</title>
  <style>
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
      margin-bottom: 18px;
    }

    h1, h2 { margin: 0; }
    h1 { font-size: 22px; letter-spacing: 0.02em; }
    h2 { margin-bottom: 12px; font-size: 15px; color: var(--accent); }

    #mode-badge {
      min-width: 52px;
      padding: 4px 9px;
      border: 1px solid var(--accent);
      border-radius: 999px;
      color: var(--accent);
      text-align: center;
      text-transform: uppercase;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
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

    #shadow-caveat {
      margin: 12px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    #cum-r-sparkline {
      display: block;
      width: 100%;
      max-width: 600px;
      height: 120px;
      border: 1px solid var(--panel-edge);
      background: #0d141a;
    }

    .empty { color: var(--muted); }
    .feed-line, .position-line { padding: 7px 0; border-bottom: 1px solid var(--panel-edge); }
    .rejection, .error-line { color: var(--danger); }
    .position-line strong { color: var(--warning); }

    @media (max-width: 840px) {
      main { padding: 14px; }
      .grid { grid-template-columns: 1fr; }
      .wide { grid-column: auto; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>trader.console</h1>
      <span id="mode-badge"></span>
    </header>

    <div class="grid">
      <section class="panel wide" aria-labelledby="leaderboard-heading">
        <h2 id="leaderboard-heading">Leaderboard</h2>
        <div class="table-wrap">
          <table id="leaderboard">
            <thead>
              <tr>
                <th>algo_id</th>
                <th>book</th>
                <th>status</th>
                <th>n_real</th>
                <th>n_shadow</th>
                <th>win_rate</th>
                <th>expectancy_r</th>
                <th>profit_factor</th>
                <th>max_drawdown_r</th>
                <th>cum_r</th>
              </tr>
            </thead>
            <tbody id="leaderboard-body"></tbody>
          </table>
        </div>
        <p id="shadow-caveat">__SHADOW_CAVEAT_TEXT__</p>
      </section>

      <section class="panel" aria-labelledby="cum-r-heading">
        <h2 id="cum-r-heading">Real-book cumulative R</h2>
        <canvas id="cum-r-sparkline" width="600" height="120"></canvas>
      </section>

      <section class="panel" aria-labelledby="positions-heading">
        <h2 id="positions-heading">Open positions</h2>
        <div id="positions"><p class="empty">No open positions.</p></div>
      </section>

      <section class="panel" aria-labelledby="intents-heading">
        <h2 id="intents-heading">Intents and rejections</h2>
        <div id="intents"><p class="empty">Waiting for intents.</p></div>
      </section>

      <section class="panel" aria-labelledby="errors-heading">
        <h2 id="errors-heading">Algo errors</h2>
        <div id="errors"><p class="empty">No algo errors.</p></div>
      </section>
    </div>
  </main>

  <script>
    (() => {
      "use strict";

      const modeBadge = document.getElementById("mode-badge");
      const leaderboardBody = document.getElementById("leaderboard-body");
      const positionsPanel = document.getElementById("positions");
      const intentsPanel = document.getElementById("intents");
      const errorsPanel = document.getElementById("errors");
      const sparkline = document.getElementById("cum-r-sparkline");

      const roster = new Map();
      const metrics = new Map();
      const tickets = new Map();
      const openPositions = new Map();
      const realCumR = [];

      function escapeHtml(value) {
        const node = document.createElement("span");
        node.textContent = value == null ? "—" : String(value);
        return node.innerHTML;
      }

      function formatMetric(value) {
        if (value == null) return "—";
        return typeof value === "number" ? value.toFixed(3) : String(value);
      }

      function metricCells(record) {
        return [
          record.algo_id,
          record.book,
          record.status || roster.get(record.algo_id) || "—",
          record.n_real,
          record.n_shadow,
          record.win_rate,
          record.expectancy_r,
          record.profit_factor,
          record.max_drawdown_r,
          record.cum_r,
        ];
      }

      function renderLeaderboard() {
        const records = Array.from(metrics.values()).sort((left, right) =>
          left.algo_id.localeCompare(right.algo_id) || left.book.localeCompare(right.book)
        );
        const algosWithMetrics = new Set(records.map((record) => record.algo_id));
        const rows = [];

        for (const [algoId, status] of Array.from(roster.entries()).sort()) {
          if (!algosWithMetrics.has(algoId)) {
            rows.push([algoId, "—", status, null, null, null, null, null, null, null]);
          }
        }
        for (const record of records) rows.push(metricCells(record));

        leaderboardBody.innerHTML = rows.length
          ? rows.map((cells) => `<tr>${cells.map((cell) =>
              `<td>${escapeHtml(formatMetric(cell))}</td>`).join("")}</tr>`).join("")
          : '<tr><td class="empty" colspan="10">Waiting for metrics.</td></tr>';
      }

      function renderPositions() {
        const positions = Array.from(openPositions.values());
        positionsPanel.innerHTML = positions.length
          ? positions.map((position) =>
              `<div class="position-line"><strong>${escapeHtml(position.instrument)}</strong> ` +
              `${escapeHtml(position.side)} ${escapeHtml(position.shares)} shares · ` +
              `${escapeHtml(position.algo_id)} · entry ${escapeHtml(position.price)} · ` +
              `stop ${escapeHtml(position.stop)} · target ${escapeHtml(position.target)} · ` +
              `${escapeHtml(position.book)} · ${escapeHtml(position.ts)}</div>`
            ).join("")
          : '<p class="empty">No open positions.</p>';
      }

      function prependFeedLine(panel, text, className) {
        const placeholder = panel.querySelector(".empty");
        if (placeholder) placeholder.remove();
        const line = document.createElement("div");
        line.className = className;
        line.textContent = text;
        panel.prepend(line);
        while (panel.children.length > 50) panel.lastElementChild.remove();
      }

      function renderIntent(record) {
        let text = `${record.algo_id} · ${record.side ?? "—"} · ` +
          `${record.instrument} · ${record.reason}`;
        if (record.ev === "rejection") {
          text += ` · ${record.rule}: ${record.detail}`;
        }
        prependFeedLine(
          intentsPanel,
          text,
          record.ev === "rejection" ? "feed-line rejection" : "feed-line"
        );
      }

      function drawSparkline() {
        const context = sparkline.getContext("2d");
        if (!context) return;

        const width = sparkline.width;
        const height = sparkline.height;
        const padding = 10;
        const points = [0, ...realCumR];
        const minimum = Math.min(0, ...points);
        const maximum = Math.max(0, ...points);
        const range = maximum - minimum || 1;

        context.clearRect(0, 0, width, height);
        context.beginPath();
        context.strokeStyle = "#61d6a9";
        context.lineWidth = 2;
        points.forEach((value, index) => {
          const x = padding + (index / Math.max(1, points.length - 1)) *
            (width - padding * 2);
          const y = height - padding - ((value - minimum) / range) *
            (height - padding * 2);
          if (index === 0) context.moveTo(x, y);
          else context.lineTo(x, y);
        });
        context.stroke();
      }

      function handleEvent(record) {
        switch (record.ev) {
          case "session_start":
            modeBadge.textContent = record.mode;
            roster.clear();
            metrics.clear();
            tickets.clear();
            openPositions.clear();
            realCumR.length = 0;
            intentsPanel.innerHTML = '<p class="empty">Waiting for intents.</p>';
            errorsPanel.innerHTML = '<p class="empty">No algo errors.</p>';
            for (const algo of record.roster || []) roster.set(algo.id, algo.status);
            renderLeaderboard();
            renderPositions();
            drawSparkline();
            break;
          case "metrics":
            metrics.set(`${record.algo_id}\u0000${record.book}`, record);
            renderLeaderboard();
            break;
          case "ticket":
            tickets.set(record.ticket_id, {
              instrument: record.instrument,
              side: record.side,
              shares: record.shares,
              algo_id: record.algo_id,
              stop: record.stop,
              target: record.target,
            });
            break;
          case "fill":
            if (record.kind === "entry") {
              const ticket = tickets.get(record.ticket_id) || {};
              openPositions.set(record.ticket_id, {
                ...ticket,
                ticket_id: record.ticket_id,
                shares: ticket.shares ?? record.shares,
                price: record.price,
                ts: record.ts,
                book: record.book,
              });
            } else {
              openPositions.delete(record.ticket_id);
            }
            renderPositions();
            break;
          case "intent":
          case "rejection":
            renderIntent(record);
            break;
          case "algo_error":
            prependFeedLine(
              errorsPanel,
              `${record.algo_id} · ${record.error}`,
              "feed-line error-line"
            );
            break;
          case "position_closed":
            if (record.book === "real") {
              const previous = realCumR.length ? realCumR[realCumR.length - 1] : 0;
              realCumR.push(previous + record.r_multiple);
              drawSparkline();
            }
            break;
          default:
            break;
        }
      }

      function showNoSessions(message) {
        leaderboardBody.innerHTML =
          `<tr><td class="empty" colspan="10">${escapeHtml(message)}</td></tr>`;
        positionsPanel.innerHTML = `<p class="empty">${escapeHtml(message)}</p>`;
      }

      async function followSession() {
        try {
          const response = await fetch('/sessions');
          if (!response.ok) throw new Error(`sessions request failed: ${response.status}`);
          const payload = await response.json();
          const sessions = payload.sessions || [];

          if (sessions.length === 0) {
            showNoSessions("no sessions yet");
            return;
          }

          const searchParams = new URLSearchParams(window.location.search);
          const sessionId = searchParams.has("session")
            ? searchParams.get("session")
            : sessions[sessions.length - 1];
          const source = new EventSource(
            '/events?session=' + encodeURIComponent(sessionId)
          );
          source.onmessage = (event) => {
            try {
              handleEvent(JSON.parse(event.data));
            } catch (error) {
              console.error("invalid telemetry event", error);
            }
          };
        } catch (error) {
          showNoSessions("unable to load sessions");
          console.error(error);
        }
      }

      renderLeaderboard();
      drawSparkline();
      followSession();
    })();
  </script>
</body>
</html>
"""


def render_dashboard_html() -> str:
    """Return the complete dashboard document ready for an HTTP response."""
    return _DASHBOARD_HTML.replace("__SHADOW_CAVEAT_TEXT__", SHADOW_CAVEAT_TEXT)
