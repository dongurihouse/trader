const $ = (selector) => document.querySelector(selector);

const state = {
  overview: null,
  ticker: null,
  range: "1D",
  algoFilter: "ALL",
  bars: null,
  chartRequest: 0,
  drawerTrigger: null,
};

const easternDateTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

const easternTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});

const localUpdated = new Intl.DateTimeFormat("en-US", {
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
});

const integerFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const compactFormat = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
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

function formatPrice(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatSigned(value, suffix = "") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const number = Number(value);
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}${suffix}`;
}

function formatCount(value) {
  if (value === null || value === undefined) return "—";
  return compactFormat.format(Number(value));
}

function formatAgo(seconds) {
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function dateFromEpoch(epoch) {
  return new Date(Number(epoch) * 1000);
}

function summarize(value, length = 84) {
  if (value === null || value === undefined) return "Waiting for output";
  let text;
  if (typeof value === "string") text = value;
  else text = JSON.stringify(value);
  if (text.length > length) return `${text.slice(0, length - 1)}…`;
  return text;
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

class PriceChart {
  constructor(canvas, tooltip) {
    this.canvas = canvas;
    this.tooltip = tooltip;
    this.context = canvas.getContext("2d");
    this.payload = null;
    this.hoverIndex = null;
    this.bounds = null;
    this.resizeObserver = new ResizeObserver(() => this.draw());
    this.resizeObserver.observe(canvas.parentElement);
    canvas.addEventListener("pointermove", (event) => this.onPointer(event));
    canvas.addEventListener("pointerdown", (event) => this.onPointer(event));
    canvas.addEventListener("pointerleave", () => this.clearPointer());
  }

  setData(payload) {
    this.payload = payload;
    this.hoverIndex = null;
    this.tooltip.hidden = true;
    this.canvas.setAttribute(
      "aria-label",
      payload?.bars?.length
        ? `${payload.ticker} price chart, ${payload.source_count.toLocaleString()} minute bars in the ${payload.range} range`
        : `${payload?.ticker || "Ticker"} price chart with no available bars`,
    );
    this.draw();
  }

  size() {
    const rectangle = this.canvas.getBoundingClientRect();
    const width = Math.max(280, Math.round(rectangle.width));
    const height = Math.max(260, Math.round(rectangle.height));
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (this.canvas.width !== width * ratio || this.canvas.height !== height * ratio) {
      this.canvas.width = width * ratio;
      this.canvas.height = height * ratio;
    }
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { width, height };
  }

  draw() {
    const { width, height } = this.size();
    const ctx = this.context;
    ctx.clearRect(0, 0, width, height);
    const bars = this.payload?.bars || [];
    if (!bars.length) return;

    const margin = { top: 14, right: width < 500 ? 49 : 62, bottom: 32, left: 4 };
    const volumeHeight = 42;
    const priceBottom = height - margin.bottom - volumeHeight - 18;
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = priceBottom - margin.top;
    const lows = bars.map((bar) => Number(bar.low));
    const highs = bars.map((bar) => Number(bar.high));
    let minimum = Math.min(...lows);
    let maximum = Math.max(...highs);
    const priceSpan = maximum - minimum || Math.max(maximum * 0.01, 1);
    minimum -= priceSpan * 0.06;
    maximum += priceSpan * 0.06;
    const adjustedSpan = maximum - minimum;
    const step = bars.length > 1 ? plotWidth / (bars.length - 1) : plotWidth;
    const x = (index) => margin.left + index * step;
    const y = (price) => margin.top + ((maximum - Number(price)) / adjustedSpan) * plotHeight;

    this.bounds = { margin, plotWidth, plotHeight, priceBottom, x, y, width, height };

    ctx.font = "10px ui-monospace, SFMono-Regular, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let index = 0; index <= 4; index += 1) {
      const lineY = margin.top + (plotHeight / 4) * index;
      const price = maximum - (adjustedSpan / 4) * index;
      ctx.beginPath();
      ctx.strokeStyle = "rgba(227, 235, 240, 0.08)";
      ctx.lineWidth = 1;
      ctx.moveTo(margin.left, Math.round(lineY) + 0.5);
      ctx.lineTo(width - margin.right, Math.round(lineY) + 0.5);
      ctx.stroke();
      ctx.fillStyle = "#5f6c77";
      ctx.fillText(formatPrice(price), width - 2, lineY);
    }

    const sessionStarts = [];
    let lastSession = null;
    bars.forEach((bar, index) => {
      const parts = easternDateTime.formatToParts(dateFromEpoch(bar.ts));
      const session = `${parts.find((part) => part.type === "month")?.value}-${parts.find((part) => part.type === "day")?.value}`;
      if (session !== lastSession) sessionStarts.push({ index, label: session });
      lastSession = session;
    });
    const sessionLabelEvery = Math.max(1, Math.ceil(sessionStarts.length / Math.max(3, width / 110)));
    sessionStarts.forEach((item, index) => {
      if (item.index === 0) return;
      const lineX = x(item.index);
      ctx.beginPath();
      ctx.strokeStyle = "rgba(227, 235, 240, 0.07)";
      ctx.setLineDash([3, 5]);
      ctx.moveTo(lineX, margin.top);
      ctx.lineTo(lineX, height - margin.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      if (index % sessionLabelEvery === 0) {
        ctx.textAlign = "left";
        ctx.fillStyle = "#5f6c77";
        ctx.fillText(item.label, Math.min(lineX + 4, width - margin.right - 30), height - 10);
      }
    });

    const maximumVolume = Math.max(...bars.map((bar) => Number(bar.volume)), 1);
    bars.forEach((bar, index) => {
      const barHeight = (Number(bar.volume) / maximumVolume) * volumeHeight;
      ctx.fillStyle = "rgba(112, 216, 220, 0.12)";
      ctx.fillRect(x(index), height - margin.bottom - barHeight, Math.max(1, Math.min(step, 3)), barHeight);
    });

    const rising = Number(bars.at(-1).close) >= Number(bars[0].open);
    const color = rising ? "#d8ff72" : "#ff7b73";
    const fill = ctx.createLinearGradient(0, margin.top, 0, priceBottom);
    fill.addColorStop(0, rising ? "rgba(216,255,114,0.17)" : "rgba(255,123,115,0.15)");
    fill.addColorStop(1, "rgba(16,23,33,0)");

    ctx.beginPath();
    bars.forEach((bar, index) => {
      const pointX = x(index);
      const pointY = y(bar.close);
      if (index === 0) ctx.moveTo(pointX, pointY);
      else ctx.lineTo(pointX, pointY);
    });
    ctx.lineTo(x(bars.length - 1), priceBottom);
    ctx.lineTo(x(0), priceBottom);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();

    if (step >= 3.2) {
      const candleWidth = Math.max(1.5, Math.min(6, step * 0.6));
      bars.forEach((bar, index) => {
        const pointX = x(index);
        const up = Number(bar.close) >= Number(bar.open);
        ctx.strokeStyle = up ? "#d8ff72" : "#ff7b73";
        ctx.fillStyle = up ? "#d8ff72" : "#ff7b73";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pointX, y(bar.high));
        ctx.lineTo(pointX, y(bar.low));
        ctx.stroke();
        const top = Math.min(y(bar.open), y(bar.close));
        const bodyHeight = Math.max(1.2, Math.abs(y(bar.open) - y(bar.close)));
        ctx.fillRect(pointX - candleWidth / 2, top, candleWidth, bodyHeight);
      });
    } else {
      ctx.beginPath();
      bars.forEach((bar, index) => {
        if (index === 0) ctx.moveTo(x(index), y(bar.close));
        else ctx.lineTo(x(index), y(bar.close));
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.lineJoin = "round";
      ctx.stroke();
    }

    this.drawEvents(ctx, bars, x, margin.top, priceBottom);
    this.drawTrades(ctx, bars, x, y);

    if (this.hoverIndex !== null) {
      const index = Math.max(0, Math.min(bars.length - 1, this.hoverIndex));
      const pointX = x(index);
      const pointY = y(bars[index].close);
      ctx.beginPath();
      ctx.strokeStyle = "rgba(237, 241, 239, 0.36)";
      ctx.setLineDash([3, 4]);
      ctx.moveTo(pointX, margin.top);
      ctx.lineTo(pointX, height - margin.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.fillStyle = "#edf1ef";
      ctx.arc(pointX, pointY, 3.2, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  nearestBarIndex(bars, timestamp) {
    let low = 0;
    let high = bars.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (Number(bars[middle].ts) < Number(timestamp)) low = middle + 1;
      else high = middle;
    }
    if (low > 0 && Math.abs(bars[low - 1].ts - timestamp) < Math.abs(bars[low].ts - timestamp)) return low - 1;
    return low;
  }

  drawTrades(ctx, bars, x, y) {
    (this.payload.trades || []).forEach((trade) => {
      const index = this.nearestBarIndex(bars, Number(trade.ts));
      const pointX = x(index);
      const entry = trade.action === "entry";
      const pointY = y(entry ? bars[index].low : bars[index].high) + (entry ? 10 : -10);
      ctx.beginPath();
      if (entry) {
        ctx.moveTo(pointX, pointY - 5);
        ctx.lineTo(pointX + 5, pointY + 4);
        ctx.lineTo(pointX - 5, pointY + 4);
        ctx.fillStyle = "#70d8dc";
      } else {
        ctx.moveTo(pointX, pointY + 5);
        ctx.lineTo(pointX + 5, pointY - 4);
        ctx.lineTo(pointX - 5, pointY - 4);
        ctx.fillStyle = "#ff7b73";
      }
      ctx.closePath();
      ctx.fill();
    });
  }

  drawEvents(ctx, bars, x, top, bottom) {
    (this.payload.events || []).forEach((event) => {
      const index = this.nearestBarIndex(bars, Number(event.event_ts));
      const pointX = x(index);
      ctx.beginPath();
      ctx.strokeStyle = "rgba(244, 197, 106, 0.54)";
      ctx.setLineDash([2, 4]);
      ctx.moveTo(pointX, top);
      ctx.lineTo(pointX, bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.fillStyle = "#f4c56a";
      ctx.arc(pointX, top + 7, 3, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  onPointer(event) {
    if (!this.payload?.bars?.length || !this.bounds) return;
    const rectangle = this.canvas.getBoundingClientRect();
    const localX = event.clientX - rectangle.left;
    const ratio = (localX - this.bounds.margin.left) / this.bounds.plotWidth;
    this.hoverIndex = Math.round(ratio * (this.payload.bars.length - 1));
    this.hoverIndex = Math.max(0, Math.min(this.payload.bars.length - 1, this.hoverIndex));
    const bar = this.payload.bars[this.hoverIndex];
    this.tooltip.replaceChildren(
      createElement("strong", "", easternDateTime.format(dateFromEpoch(bar.ts))),
      createElement("span", "", `O ${formatPrice(bar.open)} · H ${formatPrice(bar.high)}`),
      createElement("span", "", `L ${formatPrice(bar.low)} · C ${formatPrice(bar.close)}`),
      createElement("span", "", `Vol ${compactFormat.format(bar.volume)}`),
    );
    this.tooltip.hidden = false;
    const tooltipWidth = this.tooltip.offsetWidth || 150;
    const left = Math.max(6, Math.min(rectangle.width - tooltipWidth - 6, localX + 14));
    const top = Math.max(6, Math.min(rectangle.height - 94, event.clientY - rectangle.top - 42));
    this.tooltip.style.left = `${left}px`;
    this.tooltip.style.top = `${top}px`;
    this.draw();
  }

  clearPointer() {
    this.hoverIndex = null;
    this.tooltip.hidden = true;
    this.draw();
  }
}

const chart = new PriceChart($("#price-chart"), $("#chart-tooltip"));

let detailSeries = [];

function numericDetailSeries(values) {
  if (!values.length) return [];
  const latest = values.at(-1).output;
  if (Array.isArray(latest) && latest.length > 2 && latest.every((value) => Number.isFinite(Number(value)))) {
    return latest.map(Number);
  }
  if (latest && typeof latest === "object" && !Array.isArray(latest)) {
    const preferredKeys = ["series", "values", "path", "forecast", "points"];
    const key = preferredKeys.find(
      (name) => Array.isArray(latest[name]) && latest[name].length > 2 && latest[name].every((value) => Number.isFinite(Number(value))),
    );
    if (key) return latest[key].map(Number);
  }
  if (values.length > 2 && values.every((item) => Number.isFinite(Number(item.output)))) {
    return values.map((item) => Number(item.output));
  }
  return [];
}

function drawDetailChart() {
  const canvas = $("#detail-chart");
  const visual = $("#detail-visual");
  if (visual.hidden || detailSeries.length < 2) return;
  const rectangle = canvas.getBoundingClientRect();
  const width = Math.max(200, Math.round(rectangle.width));
  const height = Math.max(100, Math.round(rectangle.height));
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const minimum = Math.min(...detailSeries);
  const maximum = Math.max(...detailSeries);
  const span = maximum - minimum || 1;
  const x = (index) => 8 + (index / (detailSeries.length - 1)) * (width - 16);
  const y = (value) => 8 + ((maximum - value) / span) * (height - 24);
  for (let index = 0; index < 3; index += 1) {
    const lineY = 8 + ((height - 24) / 2) * index;
    ctx.beginPath();
    ctx.strokeStyle = "rgba(227,235,240,0.08)";
    ctx.moveTo(8, lineY);
    ctx.lineTo(width - 8, lineY);
    ctx.stroke();
  }
  const gradient = ctx.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, "rgba(216,255,114,0.18)");
  gradient.addColorStop(1, "rgba(216,255,114,0)");
  ctx.beginPath();
  detailSeries.forEach((value, index) => {
    if (index === 0) ctx.moveTo(x(index), y(value));
    else ctx.lineTo(x(index), y(value));
  });
  ctx.lineTo(x(detailSeries.length - 1), height - 8);
  ctx.lineTo(x(0), height - 8);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();
  ctx.beginPath();
  detailSeries.forEach((value, index) => {
    if (index === 0) ctx.moveTo(x(index), y(value));
    else ctx.lineTo(x(index), y(value));
  });
  ctx.strokeStyle = "#d8ff72";
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();
  canvas.setAttribute(
    "aria-label",
    `Numeric output preview with ${detailSeries.length} points, from ${minimum.toFixed(2)} to ${maximum.toFixed(2)}`,
  );
}

new ResizeObserver(drawDetailChart).observe($("#detail-visual"));

async function loadOverview({ quiet = false } = {}) {
  const refresh = $("#refresh-button");
  if (!quiet) refresh.classList.add("is-spinning");
  try {
    const overview = await api("/api/overview");
    state.overview = overview;
    if (!state.ticker || !overview.quotes.some((quote) => quote.ticker === state.ticker)) {
      const preferred = overview.quotes.find((quote) => quote.ticker === "SNDK" && quote.available);
      state.ticker = preferred?.ticker || overview.quotes.find((quote) => quote.available)?.ticker || overview.config.tickers[0];
    }
    renderOverview();
    await Promise.all([loadBars({ quiet }), loadPerformance()]);
  } catch (error) {
    showToast(error.message);
    if (!quiet) $("#updated-at").textContent = "Connection failed";
  } finally {
    refresh.classList.remove("is-spinning");
  }
}

function renderOverview() {
  const overview = state.overview;
  $("#updated-at").textContent = `Updated ${localUpdated.format(dateFromEpoch(overview.generated_at))}`;
  $("#market-label").textContent = overview.market.label;
  $("#market-time").textContent = easternTime.format(dateFromEpoch(overview.market.eastern_time));
  $("#market-dot").classList.toggle("live", overview.market.state === "live");

  $("#bar-count").textContent = formatCount(overview.counts.bars);
  $("#output-count").textContent = formatCount(overview.counts.outputs);
  $("#trade-count").textContent = formatCount(overview.counts.trades);
  $("#config-version").textContent = overview.config.version ?? "—";
  $("#bar-caption").textContent = `across ${overview.quotes.length} tracked tickers`;
  $("#output-caption").textContent = overview.counts.outputs ? "cached signal and algo values" : "waiting for algo service";
  $("#config-caption").textContent = overview.config.evaluation_days
    ? `${overview.config.evaluation_days}-day evaluation window`
    : "source of truth";

  renderTickers(overview.quotes);
  renderQuote();
  renderAlgoFilter(overview.nodes);
  renderNodes(overview.nodes);
  renderServices(overview.services);
  renderTimeline($("#problem-list"), overview.problems, "No current problems", "Warnings and errors will appear here.");
  renderTimeline($("#history-list"), overview.history, "No run history yet", "Service summaries will appear here.");
  $("#problem-count").textContent = String(overview.problems.length);
}

function renderTickers(quotes) {
  const rail = $("#ticker-rail");
  const fragment = document.createDocumentFragment();
  quotes.forEach((quote) => {
    const button = createElement("button", `ticker-card${quote.ticker === state.ticker ? " active" : ""}`);
    button.type = "button";
    button.dataset.ticker = quote.ticker;
    button.setAttribute("aria-pressed", String(quote.ticker === state.ticker));
    const left = createElement("span");
    left.append(createElement("b", "", quote.ticker));
    left.append(createElement("span", "ticker-price", quote.available ? `$${formatPrice(quote.price)}` : "No data"));
    const change = createElement(
      "span",
      `ticker-change${Number(quote.change_pct) < 0 ? " down" : ""}`,
      quote.available ? formatSigned(quote.change_pct, "%") : "—",
    );
    button.append(left, change);
    button.addEventListener("click", () => selectTicker(quote.ticker));
    fragment.append(button);
  });
  rail.replaceChildren(fragment);
}

function renderQuote() {
  const quote = state.overview?.quotes.find((item) => item.ticker === state.ticker);
  $("#chart-title").textContent = state.ticker || "—";
  $("#quote-price").textContent = quote?.available ? `$${formatPrice(quote.price)}` : "—";
  const change = $("#quote-change");
  change.textContent = quote?.available
    ? `${formatSigned(quote.change)}  ${formatSigned(quote.change_pct, "%")}`
    : "No bars";
  change.classList.toggle("down", Number(quote?.change) < 0);
  $("#stat-open").textContent = quote?.available ? formatPrice(quote.open) : "—";
  $("#stat-high").textContent = quote?.available ? formatPrice(quote.high) : "—";
  $("#stat-low").textContent = quote?.available ? formatPrice(quote.low) : "—";
  $("#stat-volume").textContent = quote?.available ? compactFormat.format(quote.volume) : "—";
}

async function selectTicker(ticker) {
  if (ticker === state.ticker) return;
  state.ticker = ticker;
  renderTickers(state.overview.quotes);
  renderQuote();
  renderNodes(state.overview.nodes);
  await Promise.all([loadBars(), loadPerformance()]);
}

async function loadBars({ quiet = false } = {}) {
  if (!state.ticker) return;
  const request = ++state.chartRequest;
  if (!quiet || !state.bars) $("#chart-loading").hidden = false;
  $("#chart-empty").hidden = true;
  try {
    const payload = await api(`/api/bars?ticker=${encodeURIComponent(state.ticker)}&range=${state.range}`);
    if (request !== state.chartRequest) return;
    state.bars = payload;
    renderAlgoFilter(state.overview?.nodes || []);
    applyTradeFilter();
    $("#chart-empty").hidden = payload.bars.length > 0;
    const density = payload.source_count === payload.bars.length
      ? `${integerFormat.format(payload.source_count)} × 1 min`
      : `${integerFormat.format(payload.source_count)} min → ${integerFormat.format(payload.bars.length)} points`;
    $("#stat-density").textContent = density;
  } catch (error) {
    if (request === state.chartRequest) showToast(error.message);
  } finally {
    if (request === state.chartRequest) $("#chart-loading").hidden = true;
  }
}

function renderNodes(nodes) {
  const list = $("#node-list");
  $("#node-count").textContent = `${nodes.length} ${nodes.length === 1 ? "node" : "nodes"}`;
  if (!nodes.length) {
    const empty = createElement("div", "empty-state");
    empty.append(
      createElement("span", "empty-symbol", "⌁"),
      createElement("strong", "", "No signals or algorithms configured"),
      createElement("p", "", "Add them to config and bump its version. They will appear here as outputs land."),
    );
    list.replaceChildren(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  nodes.forEach((node) => {
    const latest = node.latest_by_ticker?.[state.ticker] || null;
    const row = createElement("button", "node-row");
    row.type = "button";
    const icon = createElement("span", `node-icon${node.node_type === "algo" ? " algo" : ""}`, node.node_type === "algo" ? "A" : "S");
    const name = createElement("span", "node-name");
    name.append(createElement("strong", "", node.name), createElement("small", "", node.node_type));
    const output = createElement("span", "node-output");
    output.append(createElement("strong", "", summarize(latest?.output)), createElement("small", "", latest?.ts ? easternDateTime.format(dateFromEpoch(latest.ts)) : "not computed"));
    const version = createElement("span", "node-meta", latest?.version ? `v${latest.version}  →` : "DETAIL  →");
    row.append(icon, name, output, version);
    row.addEventListener("click", () => openDetail(node, latest, row));
    fragment.append(row);
  });
  list.replaceChildren(fragment);
}

function renderAlgoFilter(nodes) {
  const select = $("#algo-filter");
  const algos = nodes.filter((node) => node.node_type === "algo").map((node) => node.name);
  const tradeAlgos = (state.bars?.trades || []).map((trade) => trade.algo);
  const names = [...new Set([...algos, ...tradeAlgos])].sort();
  if (state.algoFilter !== "ALL" && !names.includes(state.algoFilter)) state.algoFilter = "ALL";
  const fragment = document.createDocumentFragment();
  const all = createElement("option", "", "All algorithms");
  all.value = "ALL";
  fragment.append(all);
  names.forEach((name) => {
    const option = createElement("option", "", name);
    option.value = name;
    fragment.append(option);
  });
  select.replaceChildren(fragment);
  select.value = state.algoFilter;
}

function applyTradeFilter() {
  if (!state.bars) return;
  chart.setData({
    ...state.bars,
    trades: state.algoFilter === "ALL"
      ? state.bars.trades
      : state.bars.trades.filter((trade) => trade.algo === state.algoFilter),
  });
}

function renderServices(services) {
  const list = $("#service-list");
  const active = services.filter((service) => service.state === "active").length;
  $("#service-count").textContent = `${active} active`;
  if (!services.length) {
    const empty = createElement("div", "empty-state compact");
    empty.append(
      createElement("span", "empty-symbol", "·"),
      createElement("strong", "", "No service heartbeats yet"),
      createElement("p", "", "The latest row from each service will appear here."),
    );
    list.replaceChildren(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  services.forEach((service) => {
    const row = createElement("div", "service-row");
    row.append(createElement("span", `service-state ${service.state}`));
    row.append(createElement("strong", "service-name", service.service));
    const message = createElement("span", "service-message");
    message.append(createElement("span", "", service.message), createElement("small", "", `${service.state} · ${formatAgo(service.age_seconds)}`));
    row.append(message);
    fragment.append(row);
  });
  list.replaceChildren(fragment);
}

function renderTimeline(container, rows, emptyTitle, emptyCopy) {
  if (!rows.length) {
    const empty = createElement("div", "empty-state compact");
    empty.append(
      createElement("span", "empty-symbol", "✓"),
      createElement("strong", "", emptyTitle),
      createElement("p", "", emptyCopy),
    );
    container.replaceChildren(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  rows.forEach((row) => {
    const item = createElement("div", `timeline-item ${row.level}`);
    item.append(createElement("time", "timeline-time", easternDateTime.format(dateFromEpoch(row.ts))));
    const copy = createElement("span", "timeline-copy");
    copy.append(createElement("strong", "", `${row.service} · ${row.level}`), createElement("span", "", row.message));
    item.append(copy);
    fragment.append(item);
  });
  container.replaceChildren(fragment);
}

async function loadPerformance() {
  if (!state.ticker) return;
  $("#performance-context").textContent = state.ticker;
  try {
    const payload = await api(`/api/performance?ticker=${encodeURIComponent(state.ticker)}`);
    renderPerformance(payload.rows);
  } catch (error) {
    showToast(error.message);
  }
}

function renderPerformance(rows) {
  const body = $("#performance-body");
  const empty = $("#performance-empty");
  body.replaceChildren();
  empty.hidden = rows.length > 0;
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const values = [
      row.algo,
      row.version,
      integerFormat.format(row.entries),
      integerFormat.format(row.closed_units),
      integerFormat.format(row.open_units),
      row.win_rate === null ? "—" : `${row.win_rate.toFixed(1)}%`,
      formatSigned(row.total_pct, "%"),
    ];
    values.forEach((value, index) => {
      const cell = createElement("td", "", value);
      if (index === 6) cell.className = Number(row.total_pct) >= 0 ? "positive" : "negative";
      tr.append(cell);
    });
    body.append(tr);
  });
}

async function openDetail(node, latest, trigger) {
  state.drawerTrigger = trigger;
  const drawer = $("#detail-drawer");
  const backdrop = $("#drawer-backdrop");
  backdrop.hidden = false;
  drawer.setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => drawer.classList.add("open"));
  $("#detail-title").textContent = node.name;
  $("#detail-type").textContent = `${node.node_type} detail`;
  $("#detail-ticker").textContent = state.ticker;
  $("#detail-version").textContent = latest?.version ?? state.overview.config.version ?? "—";
  $("#detail-count").textContent = "Loading";
  $("#detail-definition").textContent = JSON.stringify(node.definition || {}, null, 2);
  $("#detail-values").replaceChildren();
  detailSeries = [];
  $("#detail-visual").hidden = true;
  $("#drawer-close").focus();
  try {
    const version = latest?.version ? `&version=${encodeURIComponent(latest.version)}` : "";
    const detail = await api(`/api/detail?ticker=${encodeURIComponent(state.ticker)}&kind=${encodeURIComponent(node.name)}${version}`);
    $("#detail-type").textContent = `${detail.node_type} detail`;
    $("#detail-version").textContent = detail.version || "—";
    $("#detail-count").textContent = integerFormat.format(detail.values.length);
    $("#detail-definition").textContent = JSON.stringify(detail.definition || {}, null, 2);
    detailSeries = numericDetailSeries(detail.values);
    $("#detail-visual").hidden = detailSeries.length < 2;
    requestAnimationFrame(drawDetailChart);
    const fragment = document.createDocumentFragment();
    if (!detail.values.length) {
      const empty = createElement("div", "empty-state compact");
      empty.append(
        createElement("span", "empty-symbol", "⌁"),
        createElement("strong", "", "No values for this version"),
        createElement("p", "", "The definition is ready. Values will appear as the algo service computes them."),
      );
      fragment.append(empty);
    } else {
      [...detail.values].reverse().forEach((value) => {
        const row = createElement("div", "value-row");
        row.append(createElement("time", "", easternDateTime.format(dateFromEpoch(value.ts))));
        row.append(createElement("code", "", summarize(value.output, 220)));
        fragment.append(row);
      });
    }
    $("#detail-values").replaceChildren(fragment);
  } catch (error) {
    showToast(error.message);
    $("#detail-count").textContent = "Error";
  }
}

function closeDetail() {
  const drawer = $("#detail-drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  setTimeout(() => {
    $("#drawer-backdrop").hidden = true;
    state.drawerTrigger?.focus();
  }, 220);
}

$("#range-control").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-range]");
  if (!button || button.dataset.range === state.range) return;
  state.range = button.dataset.range;
  document.querySelectorAll("#range-control button").forEach((candidate) => {
    candidate.classList.toggle("active", candidate === button);
  });
  loadBars();
});

$("#refresh-button").addEventListener("click", () => loadOverview());
$("#algo-filter").addEventListener("change", (event) => {
  state.algoFilter = event.target.value;
  applyTradeFilter();
});
$("#drawer-close").addEventListener("click", closeDetail);
$("#drawer-backdrop").addEventListener("click", closeDetail);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("#detail-drawer").classList.contains("open")) closeDetail();
});

loadOverview();
setInterval(() => loadOverview({ quiet: true }), 30_000);
