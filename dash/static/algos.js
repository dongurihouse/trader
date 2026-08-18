const $ = (selector) => document.querySelector(selector);
const CHART_TICKER = "SNDK";

const numberFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const pacificDateTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});
const pacificDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
  year: "numeric",
});
const pacificDateKeyFormat = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
let algorithms = [];
let selectedAlgorithmName = new URLSearchParams(window.location.search).get("algo");
let revealSelectedAlgorithm = Boolean(selectedAlgorithmName);

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

function pacificDateKey(epoch) {
  const parts = pacificDateKeyFormat.formatToParts(dateFromEpoch(epoch));
  const value = (type) => parts.find((part) => part.type === type)?.value || "";
  return `${value("year")}-${value("month")}-${value("day")}`;
}

function chartUrl(trade) {
  const parameters = new URLSearchParams({
    ticker: CHART_TICKER,
    date: pacificDateKey(trade.entry_ts),
  });
  return `/?${parameters}`;
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
  const average = (field) => {
    const values = algos
      .map((algo) => algo.stats[field])
      .filter((value) => value !== null && value !== undefined && value !== "")
      .map(Number)
      .filter(Number.isFinite);
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  };
  const averageReturn = average("realized_return_pct");
  const averageWinRate = average("win_rate");
  $("#algo-return").textContent = returnPoints(averageReturn);
  $("#algo-return").className = valueClass(averageReturn);
  $("#algo-win-rate").textContent = rate(averageWinRate);
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
    const row = createElement("a", "algo-trade-row");
    row.href = chartUrl(trade);
    row.title = `Show all ${CHART_TICKER} trades on ${pacificDate.format(dateFromEpoch(trade.entry_ts))}`;
    const identity = createElement("div", "algo-trade-identity");
    identity.append(
      createElement("strong", "", trade.ticker),
      createElement("span", Number(trade.direction) < 0 ? "short" : "long", Number(trade.direction) < 0 ? "Short" : "Long"),
    );
    const times = createElement("div", "algo-trade-times");
    times.append(
      createElement("span", "", pacificDateTime.format(dateFromEpoch(trade.entry_ts))),
      createElement("i", "", "→"),
      createElement("span", "", pacificDateTime.format(dateFromEpoch(trade.exit_ts))),
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
      createElement("span", "algo-trade-times", pacificDateTime.format(dateFromEpoch(position.entry_ts))),
      createElement("span", "algo-trade-prices", `${price(position.entry_price)} → ${price(position.mark_price)}`),
      createElement("span", "algo-trade-hold", "Marked"),
      createElement("strong", `algo-trade-return ${valueClass(position.return_pct)}`, unitReturn(position.return_pct)),
    );
    list.append(row);
  });
  section.append(list);
  return section;
}

function renderAlgoMetrics(algo) {
  const metrics = createElement("span", "algo-tab-metric-grid");
  metrics.append(
    metric("Realized return", returnPoints(algo.stats.realized_return_pct), valueClass(algo.stats.realized_return_pct)),
    metric("Win rate", rate(algo.stats.win_rate)),
    metric("Profit factor", factor(algo.stats)),
    metric("Max drawdown", returnPoints(algo.stats.max_drawdown_pct), valueClass(algo.stats.max_drawdown_pct)),
    metric("Average hold", duration(algo.stats.average_hold_minutes)),
    metric("Closed units", numberFormat.format(algo.stats.closed_units)),
  );
  return metrics;
}

function renderAlgo(algo, tabId) {
  const card = createElement("article", "algo-card surface");
  card.id = "algo-panel";
  card.setAttribute("role", "tabpanel");
  card.setAttribute("aria-labelledby", tabId);
  card.append(renderDefinition(algo), renderTickerTable(algo));
  const open = renderOpenPositions(algo);
  if (open) card.append(open);
  card.append(renderRecentTrades(algo));
  return card;
}

function renderAlgorithmSwitcher() {
  const switcher = $("#algo-switcher");
  const strip = $("#algo-strip");
  const tabs = $("#algo-tabs");
  const previousScroll = strip.scrollLeft;
  const fragment = document.createDocumentFragment();
  algorithms.forEach((algo, index) => {
    const selected = algo.name === selectedAlgorithmName;
    const button = createElement("button", `algo-tab${selected ? " active" : ""}`);
    button.type = "button";
    button.id = `algo-tab-${index}`;
    button.dataset.algoName = algo.name;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", "algo-panel");
    button.setAttribute("aria-selected", String(selected));
    button.setAttribute("aria-label", algo.name);
    button.tabIndex = selected ? 0 : -1;
    const summary = createElement("span", "algo-tab-summary");
    summary.append(createElement("strong", "algo-tab-title", algo.name.toUpperCase()));
    button.append(summary, renderAlgoMetrics(algo));
    fragment.append(button);
  });
  tabs.replaceChildren(fragment);
  strip.scrollLeft = previousScroll;
  switcher.hidden = algorithms.length === 0;
}

function renderSelectedAlgorithm() {
  const index = algorithms.findIndex((algo) => algo.name === selectedAlgorithmName);
  const algo = algorithms[index];
  if (!algo) return;
  $("#algo-book").replaceChildren(renderAlgo(algo, `algo-tab-${index}`));
}

function selectAlgorithm(name, { focus = false } = {}) {
  if (!algorithms.some((algo) => algo.name === name)) return;
  selectedAlgorithmName = name;
  $("#algo-strip").querySelectorAll("button[data-algo-name]").forEach((button) => {
    const selected = button.dataset.algoName === name;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  renderSelectedAlgorithm();
  const selectedButton = [...$("#algo-strip").querySelectorAll("button[data-algo-name]")]
    .find((button) => button.dataset.algoName === name);
  selectedButton?.scrollIntoView({ block: "nearest", inline: "center" });
  if (focus) selectedButton?.focus();
}

function render(payload) {
  renderRollup(payload);
  $("#market-status").setAttribute("aria-label", payload.market.label);
  $("#market-dot").classList.toggle("live", payload.market.state === "live");
  const book = $("#algo-book");
  algorithms = payload.algorithms || [];
  if (!algorithms.length) {
    selectedAlgorithmName = null;
    renderAlgorithmSwitcher();
    const empty = createElement("div", "algo-empty surface");
    empty.append(
      createElement("strong", "", "No algorithms configured"),
      createElement("span", "", "Algorithm summaries will appear when config or stored trades provide one."),
    );
    book.replaceChildren(empty);
    return;
  }
  if (!algorithms.some((algo) => algo.name === selectedAlgorithmName)) {
    selectedAlgorithmName = algorithms[0].name;
  }
  renderAlgorithmSwitcher();
  renderSelectedAlgorithm();
  if (revealSelectedAlgorithm) {
    revealSelectedAlgorithm = false;
    requestAnimationFrame(() => {
      $("#algo-strip .algo-tab.active")?.scrollIntoView({ block: "nearest", inline: "center" });
    });
  }
}

$("#algo-strip").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-algo-name]");
  if (!button) return;
  selectAlgorithm(button.dataset.algoName);
});

$("#algo-strip").addEventListener("wheel", (event) => {
  const strip = event.currentTarget;
  if (strip.scrollWidth <= strip.clientWidth) return;
  if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
  const deltaScale = event.deltaMode === WheelEvent.DOM_DELTA_LINE
    ? 16
    : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
      ? strip.clientWidth
      : 1;
  const delta = event.deltaY * deltaScale;
  const maxScroll = strip.scrollWidth - strip.clientWidth;
  const canScroll = (delta < 0 && strip.scrollLeft > 0) || (delta > 0 && strip.scrollLeft < maxScroll);
  if (!canScroll) return;
  strip.scrollLeft += delta;
  event.preventDefault();
}, { passive: false });

$("#algo-strip").addEventListener("keydown", (event) => {
  const button = event.target.closest("button[data-algo-name]");
  if (!button || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const buttons = [...event.currentTarget.querySelectorAll("button[data-algo-name]")];
  let index = buttons.indexOf(button);
  if (event.key === "Home") index = 0;
  if (event.key === "End") index = buttons.length - 1;
  if (event.key === "ArrowLeft") index = Math.max(0, index - 1);
  if (event.key === "ArrowRight") index = Math.min(buttons.length - 1, index + 1);
  selectAlgorithm(buttons[index].dataset.algoName, { focus: true });
  event.preventDefault();
});

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
