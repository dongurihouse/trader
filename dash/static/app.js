const $ = (selector) => document.querySelector(selector);

const state = {
  overview: null,
  ticker: null,
  range: "1D",
  bars: null,
  chartRequest: 0,
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

function dateFromEpoch(epoch) {
  return new Date(Number(epoch) * 1000);
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

    this.bounds = { margin, plotWidth, priceBottom, x, y, width, height };

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
    const labelEvery = Math.max(1, Math.ceil(sessionStarts.length / Math.max(3, width / 110)));
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
      if (index % labelEvery === 0) {
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
    await loadBars({ quiet });
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
  renderTickers(overview.quotes);
  renderQuote();
}

function renderTickers(quotes) {
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
  $("#ticker-rail").replaceChildren(fragment);
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
  await loadBars();
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
    chart.setData(payload);
    $("#chart-empty").hidden = payload.bars.length > 0;
    $("#stat-density").textContent = payload.source_count === payload.bars.length
      ? `${integerFormat.format(payload.source_count)} × 1 min`
      : `${integerFormat.format(payload.source_count)} min → ${integerFormat.format(payload.bars.length)} points`;
  } catch (error) {
    if (request === state.chartRequest) showToast(error.message);
  } finally {
    if (request === state.chartRequest) $("#chart-loading").hidden = true;
  }
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

loadOverview();
setInterval(() => loadOverview({ quiet: true }), 30_000);
