"""Self-contained HTML page for post-session console results."""

from __future__ import annotations

from trader.console.dashboard import SHADOW_CAVEAT_TEXT
from trader.console.nav import render_nav_html
from trader.console.styles import BASE_CSS


def render_results_html() -> str:
    """Return the post-session results page as one self-contained document."""
    return (
        _RESULTS_HTML.replace("__BASE_CSS__", BASE_CSS)
        .replace("__NAV_HTML__", render_nav_html("/results"))
        .replace("__SHADOW_CAVEAT_TEXT__", SHADOW_CAVEAT_TEXT)
    )


_RESULTS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>trader.console results</title>
  <style>
__BASE_CSS__

    .top-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 0.95fr);
      gap: 16px;
      align-items: start;
      margin-bottom: 16px;
    }

    .session-line {
      margin: 0;
      color: var(--muted);
      text-align: right;
    }

    .session-control {
      display: grid;
      gap: 6px;
      width: min(440px, 100%);
    }

    .session-control label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
    }

    #session-picker {
      width: 100%;
      min-height: 36px;
      padding: 7px 9px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      background: #0d141a;
      color: var(--text);
      font: inherit;
    }

    #session-picker:focus {
      border-color: var(--accent);
      outline: none;
    }

    #session-picker:disabled {
      color: var(--muted);
      cursor: not-allowed;
      opacity: 0.75;
    }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
      gap: 8px;
    }

    .kpi-chip {
      min-width: 0;
      padding: 9px 10px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      background: #0d141a;
    }

    .kpi-label {
      color: var(--muted);
      font-size: 11px;
    }

    .kpi-value {
      margin-top: 3px;
      color: var(--text);
      font-size: 15px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }

    .algo-filter {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }

    .algo-pill {
      min-height: 32px;
      padding: 6px 10px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      background: #0d141a;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
    }

    .algo-pill:hover,
    .algo-pill:focus {
      border-color: var(--accent);
      color: var(--text);
      outline: none;
    }

    .algo-pill.is-active {
      border-color: var(--accent);
      color: var(--accent);
      background: rgba(97, 214, 169, 0.10);
    }

    #shadow-caveat {
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    .data-thin-section {
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid var(--panel-edge);
    }

    .data-thin-section h3 {
      margin: 0 0 8px;
      color: var(--text);
      font-size: 13px;
    }

    .results-board {
      display: grid;
      grid-template-columns: 280px minmax(0, 1.55fr) minmax(310px, 0.95fr);
      gap: 16px;
      align-items: start;
    }

    #days-list {
      display: grid;
      gap: 8px;
      max-height: 680px;
      overflow-y: auto;
    }

    .day-card {
      width: 100%;
      min-height: 66px;
      padding: 9px 10px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      background: #0d141a;
      color: var(--text);
      cursor: pointer;
      font: inherit;
      text-align: left;
    }

    .day-card:hover,
    .day-card:focus {
      border-color: var(--accent);
      outline: none;
    }

    .day-card.is-selected {
      border-color: var(--accent);
      background: rgba(97, 214, 169, 0.08);
    }

    .day-card.is-skipped {
      border-color: var(--warning);
      color: #f7dfac;
      background: rgba(240, 195, 108, 0.06);
    }

    .day-card-date {
      font-weight: 700;
    }

    .day-card-stats,
    .day-card-reason {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
    }

    .chart-wrap {
      display: grid;
      gap: 12px;
    }

    #day-chart {
      display: block;
      width: 100%;
      height: 320px;
      border: 1px solid var(--panel-edge);
      background: #0d141a;
    }

    #day-trades-table tr.is-selected td {
      color: var(--accent);
      background: rgba(97, 214, 169, 0.06);
    }

    #day-trades-table tbody tr {
      cursor: pointer;
    }

    .detail-stack {
      display: grid;
      gap: 10px;
    }

    .detail-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 8px 12px;
    }

    .detail-label {
      color: var(--muted);
      font-size: 11px;
    }

    .detail-value {
      color: var(--text);
      overflow-wrap: anywhere;
    }

    .price-note,
    .trace-block {
      padding: 10px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      background: #0d141a;
      color: var(--muted);
    }

    .trace-block {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .basis-provenance-note {
      margin: 0;
      font-size: 12px;
      line-height: 1.45;
    }

    .price-basis {
      color: var(--text);
      font-size: 12px;
    }

    .price-basis-unrecorded {
      color: #f7dfac;
      font-weight: 700;
    }

    #empty-state {
      margin-top: 16px;
    }

    @media (max-width: 1120px) {
      .top-row,
      .results-board {
        grid-template-columns: 1fr;
      }

      #days-list {
        max-height: 280px;
      }
    }

    @media (max-width: 760px) {
      main { padding: 14px; }
      header { align-items: flex-start; flex-direction: column; }
      .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .detail-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>trader.console results</h1>
      <div class="session-control">
        <label for="session-picker">
          Session
          <select id="session-picker" disabled></select>
        </label>
        <div id="session-label" class="session-line" aria-live="polite">loading...</div>
      </div>
    </header>

    __NAV_HTML__

    <section id="empty-state" class="panel" hidden>
      <h2>No results</h2>
      <p class="empty">No backtest session was found.</p>
    </section>

    <div id="results-content">
      <div class="top-row">
        <section class="panel" aria-labelledby="exec-heading">
          <h2 id="exec-heading">Executive Summary</h2>
          <div id="exec-summary" class="kpi-grid"></div>
          <section id="data-thin-warnings" class="data-thin-section" aria-labelledby="data-thin-heading">
            <h3 id="data-thin-heading">Data thinness</h3>
            <div id="data-thin-warnings-body"></div>
          </section>
        </section>

        <section class="panel" aria-labelledby="algo-heading">
          <h2 id="algo-heading">Algo Scope</h2>
          <div id="algo-filter" class="algo-filter" role="group" aria-label="Algo filter">
            <button class="algo-pill" type="button" data-algo="">All</button>
          </div>
          <div class="table-wrap">
            <table id="per-algo-metrics"></table>
          </div>
          <p id="shadow-caveat">__SHADOW_CAVEAT_TEXT__</p>
        </section>
      </div>

      <div class="results-board">
        <section class="panel" aria-labelledby="days-heading">
          <h2 id="days-heading">Days</h2>
          <div id="days-list">
            <div class="day-card" hidden></div>
          </div>
        </section>

        <section class="panel chart-wrap" aria-labelledby="day-heading">
          <h2 id="day-heading">Day</h2>
          <canvas id="day-chart" width="760" height="320"></canvas>
          <p id="price-basis-note" class="price-note basis-provenance-note">
            Fills without a recorded basis predate A12's producer-side basis telemetry.
            Historical ETF fills in this program were derived from SNDK through the
            leverage relation, so unrecorded ETF basis means legacy synthetic-derived
            provenance.
          </p>
          <div class="table-wrap">
            <table id="day-trades-table"></table>
          </div>
        </section>

        <section class="panel" aria-labelledby="trade-heading">
          <h2 id="trade-heading">Trade Detail</h2>
          <div id="trade-detail" class="detail-stack">
            <p class="empty">No trade selected.</p>
          </div>
        </section>
      </div>
    </div>
  </main>

  <script>
    (() => {
      "use strict";

      const sessionLabel = document.getElementById("session-label");
      const sessionPicker = document.getElementById("session-picker");
      const emptyState = document.getElementById("empty-state");
      const resultsContent = document.getElementById("results-content");
      const execSummary = document.getElementById("exec-summary");
      const dataThinWarnings = document.getElementById("data-thin-warnings-body");
      const algoFilter = document.getElementById("algo-filter");
      const perAlgoMetrics = document.getElementById("per-algo-metrics");
      const daysList = document.getElementById("days-list");
      const chart = document.getElementById("day-chart");
      const tradesTable = document.getElementById("day-trades-table");
      const tradeDetail = document.getElementById("trade-detail");
      const searchParams = new URLSearchParams(window.location.search);
      const initialSession = searchParams.has("session")
        ? searchParams.get("session")
        : null;

      const DASH = "\\u2014";
      const state = {
        payload: null,
        selectedAlgo: null,
        selectedDay: null,
        selectedTradeId: null,
        candlesByDay: new Map(),
        markerHits: [],
        knownSessions: [],
        selectedSession: initialSession,
      };

      function escapeHtml(value) {
        const node = document.createElement("span");
        node.textContent = value == null ? DASH : String(value);
        return node.innerHTML;
      }

      function finiteNumber(value) {
        return typeof value === "number" && Number.isFinite(value);
      }

      function trimNumber(value, digits = 3) {
        if (!finiteNumber(value)) return DASH;
        return Number.isInteger(value)
          ? String(value)
          : value.toFixed(digits).replace(/0+$/, "").replace(/\\.$/, "");
      }

      function formatValue(value) {
        if (value == null) return DASH;
        if (finiteNumber(value)) return trimNumber(value);
        if (Array.isArray(value)) return value.length ? value.join(", ") : DASH;
        return String(value);
      }

      function formatPriceBasisValue(value) {
        return value == null ? "not recorded" : String(value);
      }

      function renderPriceBasisValue(value) {
        const classes = value == null
          ? "price-basis-value price-basis-unrecorded"
          : "price-basis-value";
        return `<span class="${classes}">${escapeHtml(formatPriceBasisValue(value))}</span>`;
      }

      function renderPriceBasisPair(trade) {
        return '<span class="price-basis">entry: ' +
          `${renderPriceBasisValue(trade.entry_price_basis)} / exit: ` +
          `${renderPriceBasisValue(trade.exit_price_basis)}</span>`;
      }

      function formatPercent(value) {
        return finiteNumber(value) ? `${trimNumber(value * 100, 1)}%` : DASH;
      }

      function apiPath(path, extras = {}) {
        const params = new URLSearchParams();
        if (state.selectedSession !== null) params.set("session", state.selectedSession);
        for (const [key, value] of Object.entries(extras)) {
          params.set(key, value);
        }
        const query = params.toString();
        return query ? `${path}?${query}` : path;
      }

      function sessionMode(sessionId) {
        const value = String(sessionId);
        const separator = value.indexOf("-");
        return separator === -1 ? value : value.slice(0, separator);
      }

      function sessionsNewestFirst() {
        return [...state.knownSessions].reverse();
      }

      function defaultSelectedSession() {
        const sessions = sessionsNewestFirst();
        return sessions.find((sessionId) => sessionId.startsWith("backtest-")) ||
          sessions[0] || null;
      }

      function syncSessionPicker() {
        if (state.selectedSession === null) {
          sessionPicker.selectedIndex = -1;
          return;
        }
        sessionPicker.value = state.selectedSession;
      }

      function renderSessionPicker() {
        const sessions = sessionsNewestFirst();
        if (state.selectedSession === null) {
          state.selectedSession = defaultSelectedSession();
        }
        sessionPicker.textContent = "";
        for (const sessionId of sessions) {
          const option = document.createElement("option");
          option.value = sessionId;
          option.textContent = `${sessionId} (${sessionMode(sessionId)})`;
          sessionPicker.appendChild(option);
        }
        sessionPicker.disabled = sessions.length === 0;
        syncSessionPicker();
      }

      function replaceSessionUrl(sessionId) {
        const url = new URL(window.location.href);
        url.search = "";
        url.searchParams.set("session", sessionId);
        history.replaceState(null, "", url);
      }

      function resetSessionViewState() {
        state.payload = null;
        state.selectedAlgo = null;
        state.selectedDay = null;
        state.selectedTradeId = null;
        state.candlesByDay.clear();
        state.markerHits = [];
      }

      function sortedDays() {
        return [...((state.payload && state.payload.days) || [])].sort((left, right) =>
          right.day.localeCompare(left.day)
        );
      }

      function filteredTrades(day = null) {
        const trades = (state.payload && state.payload.trades) || [];
        return trades.filter((trade) =>
          (day == null || trade.day === day) &&
          (state.selectedAlgo == null || trade.algo_id === state.selectedAlgo)
        );
      }

      function allTradesForDay(day) {
        return ((state.payload && state.payload.trades) || []).filter(
          (trade) => trade.day === day
        );
      }

      function sumR(trades) {
        return trades.reduce((total, trade) =>
          total + (finiteNumber(trade.r_multiple) ? trade.r_multiple : 0), 0);
      }

      function renderExecSummary() {
        const summary = (state.payload && state.payload.executive_summary) || {};
        const fields = [
          ["Window start", "window_start", formatValue],
          ["Window end", "window_end", formatValue],
          ["Days", "days_count", formatValue],
          ["Trades", "trades", formatValue],
          ["Win rate", "win_rate", formatPercent],
          ["Mean R", "mean_r", formatValue],
          ["Profit factor", "profit_factor", formatValue],
          ["Cum R", "cum_r", formatValue],
          ["Max DD R", "max_drawdown_r", formatValue],
          ["Final equity", "final_equity", formatValue],
        ];
        execSummary.innerHTML = fields.map(([label, key, formatter]) =>
          `<div class="kpi-chip"><div class="kpi-label">${escapeHtml(label)}</div>` +
          `<div class="kpi-value">${escapeHtml(formatter(summary[key]))}</div></div>`
        ).join("");
      }

      function renderDataThinWarnings() {
        const warnings = (state.payload && state.payload.data_thin_warnings) || [];
        if (!warnings.length) {
          dataThinWarnings.innerHTML =
            "<p class=\"empty\">No data-thinness warnings were recorded in this session's telemetry.</p>";
          return;
        }
        const columns = ["symbol", "day", "count", "ts"];
        dataThinWarnings.innerHTML = '<div class="table-wrap"><table>' +
          "<thead><tr>" + columns.map((column) =>
            `<th>${escapeHtml(column)}</th>`).join("") + "</tr></thead><tbody>" +
          warnings.map((warning) =>
            "<tr>" + columns.map((column) =>
              `<td>${escapeHtml(formatValue(warning[column]))}</td>`).join("") + "</tr>"
          ).join("") + "</tbody></table></div>";
      }

      function renderAlgoFilter() {
        const emitting = ((state.payload && state.payload.algos) || [])
          .filter((algo) => algo.status === "emitting");
        if (state.selectedAlgo != null &&
            !emitting.some((algo) => algo.id === state.selectedAlgo)) {
          state.selectedAlgo = null;
        }
        const pills = [{id: null, label: "All"}, ...emitting.map((algo) => ({
          id: algo.id,
          label: algo.id,
        }))];
        algoFilter.innerHTML = pills.map((pill) => {
          const active = pill.id === state.selectedAlgo;
          const dataAlgo = pill.id == null ? "" : pill.id;
          return `<button class="algo-pill${active ? " is-active" : ""}" ` +
            `type="button" data-algo="${escapeHtml(dataAlgo)}">` +
            `${escapeHtml(pill.label)}</button>`;
        }).join("");
        for (const button of algoFilter.querySelectorAll(".algo-pill")) {
          button.addEventListener("click", () => {
            state.selectedAlgo = button.dataset.algo || null;
            state.selectedTradeId = null;
            renderColumns();
          });
        }
      }

      function renderPerAlgoMetrics() {
        const rows = [...((state.payload && state.payload.per_algo_metrics) || [])]
          .sort((left, right) =>
            String(left.algo_id).localeCompare(String(right.algo_id)) ||
            String(left.book || "").localeCompare(String(right.book || ""))
          );
        const columns = [
          "algo_id",
          "book",
          "status",
          "n_real",
          "n_shadow",
          "win_rate",
          "mean_r",
          "expectancy_r",
          "profit_factor",
          "max_drawdown_r",
          "cum_r",
        ];
        perAlgoMetrics.innerHTML = "<thead><tr>" + columns.map((column) =>
          `<th>${escapeHtml(column)}</th>`).join("") + "</tr></thead><tbody>" +
          (rows.length ? rows.map((row) =>
            "<tr>" + columns.map((column) =>
              `<td>${escapeHtml(formatValue(row[column]))}</td>`).join("") + "</tr>"
          ).join("") : '<tr><td class="empty" colspan="11">No metrics.</td></tr>') +
          "</tbody>";
      }

      function chooseInitialDay() {
        const days = sortedDays();
        if (!days.length) return null;
        const withTrade = days.find((day) => allTradesForDay(day.day).length > 0);
        if (withTrade) return withTrade.day;
        const processed = days.find((day) => day.status === "processed");
        return (processed || days[0]).day;
      }

      function renderDaysList() {
        const days = sortedDays();
        if (!days.length) {
          daysList.innerHTML = '<p class="empty">No days recorded.</p>';
          return;
        }
        daysList.innerHTML = days.map((day) => {
          const trades = filteredTrades(day.day);
          const classes = [
            "day-card",
            day.day === state.selectedDay ? "is-selected" : "",
            day.status === "skipped" ? "is-skipped" : "",
          ].filter(Boolean).join(" ");
          const reason = day.status === "skipped" && day.reason
            ? `<div class="day-card-reason">${escapeHtml(day.reason)}</div>`
            : "";
          return `<button class="${classes}" type="button" data-day="${escapeHtml(day.day)}">` +
            `<div class="day-card-date">${escapeHtml(day.day)}</div>` +
            `<div class="day-card-stats">${trades.length} trades · ` +
            `${escapeHtml(trimNumber(sumR(trades)))} R</div>${reason}</button>`;
        }).join("");
        for (const card of daysList.querySelectorAll(".day-card")) {
          card.addEventListener("click", () => selectDay(card.dataset.day));
        }
      }

      async function selectDay(day) {
        if (!day) return;
        state.selectedDay = day;
        const dayTrades = filteredTrades(day);
        if (!dayTrades.some((trade) => trade.id === state.selectedTradeId)) {
          state.selectedTradeId = dayTrades.length ? dayTrades[0].id : null;
        }
        renderColumns();
        await fetchCandles(day);
        renderColumns();
      }

      async function fetchCandles(day) {
        if (state.candlesByDay.has(day)) return;
        state.candlesByDay.set(day, {candles: [], loading: true, error: null});
        drawChart();
        try {
          const response = await fetch(apiPath("/api/results/day", {day}));
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
          state.candlesByDay.set(day, {
            candles: payload.candles || [],
            loading: false,
            error: null,
          });
        } catch (error) {
          state.candlesByDay.set(day, {
            candles: [],
            loading: false,
            error: String(error),
          });
        }
      }

      function renderTradeTable() {
        const trades = state.selectedDay ? filteredTrades(state.selectedDay) : [];
        const columns = [
          "algo_id",
          "side",
          "instrument",
          "basis",
          "entry_ts",
          "entry_price",
          "exit_ts",
          "exit_price",
          "exit_kind",
          "r_multiple",
        ];
        tradesTable.innerHTML = "<thead><tr>" + columns.map((column) =>
          `<th>${escapeHtml(column)}</th>`).join("") + "</tr></thead><tbody>" +
          (trades.length ? trades.map((trade) => {
            const selected = trade.id === state.selectedTradeId ? ' class="is-selected"' : "";
            return `<tr data-trade="${escapeHtml(trade.id)}"${selected}>` +
              columns.map((column) => {
                if (column === "basis") return `<td>${renderPriceBasisPair(trade)}</td>`;
                return `<td>${escapeHtml(formatValue(trade[column]))}</td>`;
              }).join("") +
              "</tr>";
          }).join("") : `<tr><td class="empty" colspan="${columns.length}">No trades for this day.</td></tr>`) +
          "</tbody>";
        for (const row of tradesTable.querySelectorAll("tbody tr[data-trade]")) {
          row.addEventListener("click", () => {
            state.selectedTradeId = row.dataset.trade;
            renderTradeTable();
            renderTradeDetail();
            drawChart();
          });
        }
      }

      function selectedTrade() {
        if (state.selectedTradeId == null) return null;
        return ((state.payload && state.payload.trades) || []).find(
          (trade) => trade.id === state.selectedTradeId
        ) || null;
      }

      function renderTradeDetail() {
        const trade = selectedTrade();
        if (trade == null) {
          tradeDetail.innerHTML = '<p class="empty">No trade selected.</p>';
          return;
        }
        const risk = finiteNumber(trade.entry_price) && finiteNumber(trade.stop)
          ? Math.abs(trade.entry_price - trade.stop)
          : null;
        const plannedReward = finiteNumber(trade.target) && finiteNumber(trade.entry_price)
          ? Math.abs(trade.target - trade.entry_price)
          : null;
        const trace = trade.rule_trace;
        const traceHtml = trace ? [
          ["setup_id", trace.setup_id],
          ["rules_version", trace.rules_version],
          ["rules_fired", trace.rules_fired || []],
          ["direction_votes", trace.direction_votes || []],
          ["gates_pass", trace.gates_pass],
          ["vetoed", trace.vetoed],
          ["uncalibrated", trace.uncalibrated],
          ["confidence", trade.confidence],
          ["reason", trade.reason],
        ].map(([key, value]) => `${key}: ${formatValue(value)}`).join("\\n")
          : "no rule trace available";

        const detailRows = [
          {label: "Algo", value: trade.algo_id},
          {label: "Side", value: trade.side},
          {label: "Instrument", value: trade.instrument},
          {label: "Entry", value: trade.entry_price},
          {
            label: "Entry price basis:",
            html: `<span class="price-basis">${renderPriceBasisValue(trade.entry_price_basis)}</span>`,
          },
          {label: "Stop", value: trade.stop},
          {label: "Target", value: trade.target},
          {label: "Exit", value: trade.exit_price},
          {
            label: "Exit price basis:",
            html: `<span class="price-basis">${renderPriceBasisValue(trade.exit_price_basis)}</span>`,
          },
          {label: "Exit kind", value: trade.exit_kind},
          {label: "Risk", value: `|entry - stop| = ${trimNumber(risk)}`},
          {label: "Planned reward", value: `|target - entry| = ${trimNumber(plannedReward)}`},
          {label: "Realized R", value: trade.r_multiple},
          {label: "Entry ts", value: trade.entry_ts},
          {label: "Exit ts", value: trade.exit_ts},
        ];
        tradeDetail.innerHTML = '<div class="detail-grid">' + detailRows.map((row) =>
          `<div><div class="detail-label">${escapeHtml(row.label)}</div>` +
          `<div class="detail-value">${row.html || escapeHtml(formatValue(row.value))}</div></div>`
        ).join("") + '</div><div class="price-note">' +
          "Entry, stop, target, and exit are the traded instrument's own prices " +
          "(SNXX/SNDQ when used), not SNDK; the candle chart is SNDK." +
          '</div><div class="trace-block">' + escapeHtml(traceHtml) + '</div>';
      }

      function resizeCanvas() {
        const width = Math.max(320, Math.floor(chart.clientWidth || chart.width));
        if (chart.width !== width) chart.width = width;
        if (chart.height !== 320) chart.height = 320;
      }

      function drawChartMessage(context, message) {
        context.clearRect(0, 0, chart.width, chart.height);
        context.fillStyle = getComputedStyle(document.documentElement)
          .getPropertyValue("--muted").trim() || "#8fa3b2";
        context.fillText(message, 18, 32);
      }

      function drawChart() {
        resizeCanvas();
        const context = chart.getContext("2d");
        if (!context) return;
        state.markerHits = [];
        const selectedDay = state.selectedDay;
        if (!selectedDay) {
          drawChartMessage(context, "No day selected.");
          return;
        }
        const cached = state.candlesByDay.get(selectedDay);
        if (!cached || cached.loading) {
          drawChartMessage(context, "Loading candles...");
          return;
        }
        if (cached.error) {
          drawChartMessage(context, cached.error);
          return;
        }
        const candles = cached.candles || [];
        if (!candles.length) {
          drawChartMessage(context, "No candles for selected day.");
          return;
        }

        const styles = getComputedStyle(document.documentElement);
        const accent = styles.getPropertyValue("--accent").trim() || "#61d6a9";
        const danger = styles.getPropertyValue("--danger").trim() || "#ff7b79";
        const muted = styles.getPropertyValue("--muted").trim() || "#8fa3b2";
        const text = styles.getPropertyValue("--text").trim() || "#e4edf4";
        const width = chart.width;
        const height = chart.height;
        const left = 38;
        const right = 12;
        const top = 14;
        const bottom = 24;
        const plotWidth = width - left - right;
        const plotHeight = height - top - bottom;
        const lows = candles.map((candle) => candle.l).filter(finiteNumber);
        const highs = candles.map((candle) => candle.h).filter(finiteNumber);
        const minPrice = Math.min(...lows);
        const maxPrice = Math.max(...highs);
        const range = maxPrice - minPrice || 1;
        const paddedMin = minPrice - range * 0.05;
        const paddedMax = maxPrice + range * 0.05;
        const paddedRange = paddedMax - paddedMin || 1;
        const xForIndex = (index) => left + (
          candles.length === 1 ? plotWidth / 2 : (index / (candles.length - 1)) * plotWidth
        );
        const yForPrice = (price) => top + ((paddedMax - price) / paddedRange) * plotHeight;

        context.clearRect(0, 0, width, height);
        context.strokeStyle = "rgba(143, 163, 178, 0.22)";
        context.lineWidth = 1;
        context.beginPath();
        for (let line = 0; line <= 4; line += 1) {
          const y = top + (plotHeight / 4) * line;
          context.moveTo(left, y);
          context.lineTo(width - right, y);
        }
        context.stroke();
        context.fillStyle = muted;
        context.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
        context.fillText(trimNumber(paddedMax, 2), 4, top + 8);
        context.fillText(trimNumber(paddedMin, 2), 4, top + plotHeight);

        const step = candles.length > 1 ? plotWidth / (candles.length - 1) : plotWidth;
        const bodyWidth = Math.max(2, Math.min(8, step * 0.58));
        candles.forEach((candle, index) => {
          const x = xForIndex(index);
          const up = candle.c >= candle.o;
          context.strokeStyle = up ? accent : danger;
          context.fillStyle = up ? accent : danger;
          context.beginPath();
          context.moveTo(x, yForPrice(candle.l));
          context.lineTo(x, yForPrice(candle.h));
          context.stroke();
          const yOpen = yForPrice(candle.o);
          const yClose = yForPrice(candle.c);
          const y = Math.min(yOpen, yClose);
          const bodyHeight = Math.max(1, Math.abs(yOpen - yClose));
          context.fillRect(x - bodyWidth / 2, y, bodyWidth, bodyHeight);
        });

        for (const trade of filteredTrades(selectedDay)) {
          drawMarker(context, candles, xForIndex, yForPrice, trade, "entry", accent, text);
          drawMarker(context, candles, xForIndex, yForPrice, trade, "exit", danger, text);
        }
      }

      function nearestCandleIndex(candles, timestamp) {
        const target = Date.parse(timestamp || "");
        if (!Number.isFinite(target)) return 0;
        let bestIndex = 0;
        let bestDistance = Infinity;
        candles.forEach((candle, index) => {
          const distance = Math.abs(Date.parse(candle.ts) - target);
          if (distance < bestDistance) {
            bestDistance = distance;
            bestIndex = index;
          }
        });
        return bestIndex;
      }

      function drawMarker(context, candles, xForIndex, yForPrice, trade, kind, color, text) {
        const timestamp = kind === "entry" ? trade.entry_ts : trade.exit_ts;
        if (!timestamp) return;
        const index = nearestCandleIndex(candles, timestamp);
        const candle = candles[index];
        const x = xForIndex(index);
        const y = kind === "entry"
          ? Math.max(12, yForPrice(candle.l) - 9)
          : Math.min(chart.height - 12, yForPrice(candle.h) + 9);
        const selected = trade.id === state.selectedTradeId;
        context.beginPath();
        context.arc(x, y, selected ? 6 : 4, 0, Math.PI * 2);
        context.fillStyle = color;
        context.fill();
        context.fillStyle = text;
        context.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
        context.fillText(kind === "entry" ? "E" : "X", x + 6, y + 3);
        state.markerHits.push({tradeId: trade.id, x, y, radius: selected ? 9 : 7});
      }

      function renderColumns() {
        renderDaysList();
        renderTradeTable();
        renderTradeDetail();
        drawChart();
      }

      function showEmpty() {
        sessionLabel.textContent = "no sessions yet";
        resultsContent.hidden = true;
        emptyState.hidden = false;
      }

      async function loadSessionList() {
        try {
          const response = await fetch("/sessions");
          if (response.ok) {
            const payload = await response.json();
            state.knownSessions = payload.sessions || [];
          } else {
            state.knownSessions = [];
          }
        } catch (error) {
          state.knownSessions = [];
        }
        renderSessionPicker();
      }

      async function loadResults() {
        sessionLabel.textContent = "loading...";
        resultsContent.hidden = false;
        emptyState.hidden = true;
        try {
          const response = await fetch(apiPath("/api/results"));
          const payload = await response.json();
          if (response.status === 404) {
            showEmpty();
            return;
          }
          if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
          state.payload = payload;
          state.selectedAlgo = null;
          state.selectedDay = chooseInitialDay();
          state.selectedTradeId = null;
          const session = payload.session || {};
          if (session.id != null) {
            state.selectedSession = String(session.id);
            syncSessionPicker();
          }
          sessionLabel.textContent = `${formatValue(session.id)} · ${formatValue(session.mode)}`;
          renderExecSummary();
          renderDataThinWarnings();
          renderAlgoFilter();
          renderPerAlgoMetrics();
          renderColumns();
          if (state.selectedDay) await selectDay(state.selectedDay);
        } catch (error) {
          sessionLabel.textContent = "unable to load results";
          resultsContent.hidden = true;
          emptyState.hidden = false;
          emptyState.querySelector(".empty").textContent = String(error);
        }
      }

      chart.addEventListener("click", (event) => {
        const rect = chart.getBoundingClientRect();
        const x = (event.clientX - rect.left) * (chart.width / rect.width);
        const y = (event.clientY - rect.top) * (chart.height / rect.height);
        const hit = state.markerHits.find((marker) =>
          Math.hypot(marker.x - x, marker.y - y) <= marker.radius
        );
        if (!hit) return;
        state.selectedTradeId = hit.tradeId;
        renderTradeTable();
        renderTradeDetail();
        drawChart();
      });
      sessionPicker.addEventListener("change", async () => {
        if (!sessionPicker.value || sessionPicker.value === state.selectedSession) return;
        state.selectedSession = sessionPicker.value;
        resetSessionViewState();
        replaceSessionUrl(state.selectedSession);
        await loadResults();
      });
      window.addEventListener("resize", drawChart);

      (async () => {
        await loadSessionList();
        await loadResults();
      })();
    })();
  </script>
</body>
</html>
"""


__all__ = ["render_results_html"]
