const $ = (selector) => document.querySelector(selector);

const numberFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const easternDateTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});
const easternDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  year: "numeric",
});

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toast.hidden = true;
  }, 4200);
}

function dateFromEpoch(epoch) {
  return new Date(Number(epoch) * 1000);
}

function humanize(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/^./, (letter) => letter.toUpperCase());
}

function signed(value, digits = 2, suffix = "") {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number >= 0 ? "+" : ""}${number.toFixed(digits)}${suffix}`;
}

function returnPoints(value) {
  return signed(value, 2, " pts");
}

function unitReturn(value) {
  return signed(value, 2, "%");
}

function rate(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(1)}%` : "—";
}

function factor(stats) {
  if (stats.profit_factor_unbounded) return "∞";
  if (stats.profit_factor === null || stats.profit_factor === undefined) return "—";
  const value = Number(stats.profit_factor);
  return Number.isFinite(value) ? value.toFixed(2) : "—";
}

function duration(minutes) {
  if (minutes === null || minutes === undefined || minutes === "") return "—";
  const value = Number(minutes);
  if (!Number.isFinite(value)) return "—";
  if (value < 60) return `${Math.round(value)}m`;
  const hours = Math.floor(value / 60);
  const remainder = Math.round(value % 60);
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

function valueClass(value) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "";
  return number > 0 ? "positive" : "negative";
}

function price(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return `$${Number(value).toFixed(2)}`;
}

function metric(label, value, className = "") {
  const item = createElement("span", "algo-metric");
  item.append(createElement("small", "", label), createElement("strong", className, value));
  return item;
}

function renderRollup(payload) {
  const algos = payload.algorithms || [];
  const totals = algos.reduce(
    (result, algo) => {
      result.closed += Number(algo.stats.closed_units) || 0;
      result.open += Number(algo.stats.open_units) || 0;
      result.wins += Number(algo.stats.wins) || 0;
      result.return += Number(algo.stats.realized_return_pct) || 0;
      return result;
    },
    { closed: 0, open: 0, wins: 0, return: 0 },
  );
  $("#algo-count").textContent = numberFormat.format(algos.length);
  $("#algo-closed").textContent = numberFormat.format(totals.closed);
  $("#algo-open").textContent = numberFormat.format(totals.open);
  $("#algo-return").textContent = returnPoints(totals.return);
  $("#algo-return").className = valueClass(totals.return);
  $("#algo-win-rate").textContent = totals.closed ? rate((totals.wins / totals.closed) * 100) : "—";
  $("#return-basis").textContent = payload.return_basis;
}

function renderDefinition(algo) {
  const section = createElement("div", "algo-definition");
  const inputs = createElement("div", "algo-inputs");
  inputs.append(createElement("small", "", "Inputs"));
  (algo.inputs || []).forEach((input) => inputs.append(createElement("span", "algo-chip", humanize(input))));
  if (!(algo.inputs || []).length) inputs.append(createElement("span", "algo-muted", "None recorded"));

  const parameters = createElement("dl", "algo-params");
  Object.entries(algo.params || {}).forEach(([name, value]) => {
    const parameter = document.createElement("div");
    parameter.append(
      createElement("dt", "", humanize(name)),
      createElement("dd", "", Array.isArray(value) ? value.join(", ") : String(value)),
    );
    parameters.append(parameter);
  });
  section.append(inputs);
  if (parameters.children.length) section.append(parameters);
  return section;
}

function renderTickerTable(algo) {
  const section = createElement("section", "algo-section");
  section.append(createElement("h3", "", "Instrument results"));
  if (!(algo.tickers || []).length) {
    section.append(createElement("p", "algo-muted", "No stored trade outcomes."));
    return section;
  }
  const wrap = createElement("div", "algo-table-wrap");
  const table = createElement("table", "algo-table");
  const head = document.createElement("thead");
  const header = document.createElement("tr");
  ["Ticker", "Closed", "Win rate", "Return", "Average", "PF", "Best", "Worst", "Open"].forEach(
    (label) => header.append(createElement("th", "", label)),
  );
  head.append(header);
  const body = document.createElement("tbody");
  algo.tickers.forEach((ticker) => {
    const row = document.createElement("tr");
    row.append(createElement("th", "algo-ticker", ticker.ticker));
    row.append(createElement("td", "", numberFormat.format(ticker.closed_units)));
    row.append(createElement("td", "", rate(ticker.win_rate)));
    row.append(
      createElement("td", valueClass(ticker.realized_return_pct), returnPoints(ticker.realized_return_pct)),
    );
    row.append(createElement("td", valueClass(ticker.average_return_pct), unitReturn(ticker.average_return_pct)));
    row.append(createElement("td", "", factor(ticker)));
    row.append(createElement("td", valueClass(ticker.best_return_pct), unitReturn(ticker.best_return_pct)));
    row.append(createElement("td", valueClass(ticker.worst_return_pct), unitReturn(ticker.worst_return_pct)));
    row.append(createElement("td", "", numberFormat.format(ticker.open_units)));
    body.append(row);
  });
  table.append(head, body);
  wrap.append(table);
  section.append(wrap);
  return section;
}

function renderRecentTrades(algo) {
  const section = createElement("section", "algo-section");
  section.append(createElement("h3", "", "Recent closed units"));
  if (!(algo.recent_trades || []).length) {
    section.append(createElement("p", "algo-muted", "No closed units yet."));
    return section;
  }
  const list = createElement("div", "algo-trade-list");
  algo.recent_trades.forEach((trade) => {
    const row = createElement("article", "algo-trade-row");
    const identity = createElement("div", "algo-trade-identity");
    identity.append(
      createElement("strong", "", trade.ticker),
      createElement("span", Number(trade.direction) < 0 ? "short" : "long", Number(trade.direction) < 0 ? "Short" : "Long"),
    );
    const times = createElement("div", "algo-trade-times");
    times.append(
      createElement("span", "", easternDateTime.format(dateFromEpoch(trade.entry_ts))),
      createElement("i", "", "→"),
      createElement("span", "", easternDateTime.format(dateFromEpoch(trade.exit_ts))),
    );
    const prices = createElement(
      "span",
      "algo-trade-prices",
      `${price(trade.entry_price)} → ${price(trade.exit_price)}`,
    );
    row.append(
      identity,
      times,
      prices,
      createElement("span", "algo-trade-hold", duration(trade.hold_minutes)),
      createElement("strong", `algo-trade-return ${valueClass(trade.return_pct)}`, unitReturn(trade.return_pct)),
    );
    list.append(row);
  });
  section.append(list);
  return section;
}

function renderOpenPositions(algo) {
  if (!(algo.open_positions || []).length) return null;
  const section = createElement("section", "algo-section");
  section.append(createElement("h3", "", "Open units"));
  const list = createElement("div", "algo-trade-list");
  algo.open_positions.forEach((position) => {
    const row = createElement("article", "algo-trade-row open");
    const identity = createElement("div", "algo-trade-identity");
    identity.append(
      createElement("strong", "", position.ticker),
      createElement("span", Number(position.direction) < 0 ? "short" : "long", Number(position.direction) < 0 ? "Short" : "Long"),
    );
    row.append(
      identity,
      createElement("span", "algo-trade-times", easternDateTime.format(dateFromEpoch(position.entry_ts))),
      createElement("span", "algo-trade-prices", `${price(position.entry_price)} → ${price(position.mark_price)}`),
      createElement("span", "algo-trade-hold", "Marked"),
      createElement("strong", `algo-trade-return ${valueClass(position.return_pct)}`, unitReturn(position.return_pct)),
    );
    list.append(row);
  });
  section.append(list);
  return section;
}

function renderAlgo(algo) {
  const card = createElement("article", "algo-card surface");
  const header = createElement("header", "algo-card-header");
  const heading = createElement("div", "algo-heading");
  const titleRow = createElement("div", "algo-title-row");
  titleRow.append(createElement("h2", "", algo.name.toUpperCase()));
  const status = algo.configured && algo.trades_enabled ? "Active" : algo.configured ? "Configured" : "Historical";
  titleRow.append(createElement("span", `algo-status ${status.toLowerCase()}`, status));
  heading.append(
    titleRow,
    createElement(
      "p",
      "",
      `${humanize(algo.function)} · ${algo.configured ? "current" : "historical"} definition · config ${algo.version || "unknown"}`,
    ),
  );
  const coverage = createElement("div", "algo-coverage");
  coverage.append(
    createElement("small", "", "Stored activity"),
    createElement(
      "strong",
      "",
      algo.stats.first_action
        ? `${easternDate.format(dateFromEpoch(algo.stats.first_action))}–${easternDate.format(dateFromEpoch(algo.stats.last_action))}`
        : "No actions",
    ),
    createElement("span", "", `${numberFormat.format(algo.stats.session_count)} sessions · ${numberFormat.format(algo.stats.ticker_count)} tickers`),
  );
  header.append(heading, coverage);

  const metrics = createElement("div", "algo-metric-grid");
  metrics.append(
    metric("Realized return", returnPoints(algo.stats.realized_return_pct), valueClass(algo.stats.realized_return_pct)),
    metric("Win rate", rate(algo.stats.win_rate)),
    metric("Profit factor", factor(algo.stats)),
    metric("Max drawdown", returnPoints(algo.stats.max_drawdown_pct), valueClass(algo.stats.max_drawdown_pct)),
    metric("Average unit", unitReturn(algo.stats.average_return_pct), valueClass(algo.stats.average_return_pct)),
    metric("Average hold", duration(algo.stats.average_hold_minutes)),
    metric("Closed units", numberFormat.format(algo.stats.closed_units)),
    metric("Open units", numberFormat.format(algo.stats.open_units)),
  );

  card.append(header, metrics, renderDefinition(algo), renderTickerTable(algo));
  const open = renderOpenPositions(algo);
  if (open) card.append(open);
  card.append(renderRecentTrades(algo));
  return card;
}

function render(payload) {
  renderRollup(payload);
  $("#market-status").setAttribute("aria-label", payload.market.label);
  $("#market-status").title = payload.market.label;
  $("#market-dot").classList.toggle("live", payload.market.state === "live");
  const book = $("#algo-book");
  if (!(payload.algorithms || []).length) {
    const empty = createElement("div", "algo-empty surface");
    empty.append(
      createElement("strong", "", "No algorithms configured"),
      createElement("span", "", "Algorithm summaries will appear when config or stored trades provide one."),
    );
    book.replaceChildren(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  payload.algorithms.forEach((algo) => fragment.append(renderAlgo(algo)));
  book.replaceChildren(fragment);
}

async function loadAlgorithms({ quiet = false } = {}) {
  try {
    render(await api("/api/algorithms"));
  } catch (error) {
    showToast(error.message);
    if (!quiet) {
      const errorState = createElement("div", "algo-empty surface");
      errorState.append(
        createElement("strong", "", "Algorithm results unavailable"),
        createElement("span", "", error.message),
      );
      $("#algo-book").replaceChildren(errorState);
    }
  }
}

loadAlgorithms();
setInterval(() => loadAlgorithms({ quiet: true }), 30_000);
