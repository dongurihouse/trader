const HISTORY_SESSIONS_PER_CHUNK = 5;
const HISTORY_PARALLEL_REQUESTS = 3;
const initialParameters = new URLSearchParams(window.location.search);
const requestedDate = initialParameters.get("date");
const requestedTicker = initialParameters.get("ticker");

function algorithmUrl(name) {
  const parameters = new URLSearchParams({ algo: name });
  return `/algos?${parameters}`;
}

function algorithmDisplayName(trade) {
  return trade.display_name || humanize(trade.algo);
}

function rememberChartView() {
  window.traderHeader?.rememberView("chart", {
    ticker: state.ticker,
    date: state.selectedDate,
  });
}

function tradePairKey(pair) {
  return `${pair.entry.algo}|${Number(pair.entry.ts)}`;
}

const state = {
  overview: null,
  ticker: requestedTicker?.toUpperCase() || null,
  range: "HISTORY",
  style: "line",
  indicator: "rsi",
  requestedDate: /^\d{4}-\d{2}-\d{2}$/.test(requestedDate || "") ? requestedDate : null,
  bars: null,
  chartRevision: null,
  sessions: [],
  selectedDate: null,
  chartRequest: 0,
  historyLoad: 0,
};

const pacificDateTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

const pacificInspectionTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});

const pacificClock = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  hour: "numeric",
  minute: "2-digit",
});

const pacificDateButton = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
});

const pacificWeekday = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  weekday: "short",
});

const pacificFullDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  year: "numeric",
  month: "long",
  day: "numeric",
});

const compactFormat = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

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

function formatTradeReturn(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return `${number >= 0 ? "+" : ""}${number.toFixed(Math.abs(number) >= 10 ? 0 : 1)}%`;
}

function roundedRectPath(ctx, x, y, width, height, radius) {
  const corner = Math.max(0, Math.min(radius, width / 2, height / 2));
  ctx.beginPath();
  ctx.moveTo(x + corner, y);
  ctx.arcTo(x + width, y, x + width, y + height, corner);
  ctx.arcTo(x + width, y + height, x, y + height, corner);
  ctx.arcTo(x, y + height, x, y, corner);
  ctx.arcTo(x, y, x + width, y, corner);
  ctx.closePath();
}

function formatProbability(value) {
  const probability = Number(value);
  if (!Number.isFinite(probability)) return "—";
  return `${(probability * 100).toFixed(1)}%`;
}

function sessionDateRanges(bars) {
  const sessions = [];
  bars.forEach((bar, index) => {
    const date = pacificDateKey(bar.ts);
    const previous = sessions.at(-1);
    if (!previous || previous.date !== date) {
      sessions.push({
        date,
        startIndex: index,
        endIndex: index,
        ts: Number(bar.ts),
      });
    } else {
      previous.endIndex = index;
    }
  });
  return sessions;
}

function mergeBy(items, keyFor) {
  const values = new Map();
  items.forEach((item) => values.set(keyFor(item), item));
  return [...values.values()];
}

function mergeForecasts(payloads, key) {
  const forecasts = payloads.map((payload) => payload?.[key]).filter(Boolean);
  if (!forecasts.length) return null;
  return {
    ...forecasts[0],
    snapshots: mergeBy(
      forecasts.flatMap((forecast) => forecast.snapshots || []),
      (snapshot) => Number(snapshot.ts),
    ).sort((left, right) => Number(left.ts) - Number(right.ts)),
  };
}

function mergeOverlays(payloads) {
  const overlays = new Map();
  payloads.flatMap((payload) => payload?.algo_overlays || []).forEach((overlay) => {
    const current = overlays.get(overlay.algo) || { ...overlay, ranges: [] };
    current.ranges = mergeBy(
      [...current.ranges, ...(overlay.ranges || [])],
      (range) => `${range.date}|${range.start}|${range.end}`,
    ).sort((left, right) => Number(left.start) - Number(right.start));
    overlays.set(overlay.algo, current);
  });
  return [...overlays.values()];
}

function mergeHistoryPayload(base, parts, { includeBaseInterpolation = true } = {}) {
  const payloads = [base, ...parts].filter(Boolean);
  const bars = mergeBy(
    payloads.flatMap((payload) => payload.bars || []),
    (bar) => Number(bar.ts),
  ).sort((left, right) => Number(left.ts) - Number(right.ts));
  const trades = mergeBy(
    payloads.flatMap((payload) => payload.trades || []),
    (trade) => `${trade.ticker}|${trade.algo}|${trade.ts}|${trade.action}|${trade.direction}`,
  ).sort((left, right) => Number(left.ts) - Number(right.ts));
  const events = mergeBy(
    payloads.flatMap((payload) => payload.events || []),
    (event) => `${event.event}|${event.event_ts}|${event.window_start}|${event.window_end}`,
  ).sort((left, right) => Number(left.event_ts) - Number(right.event_ts));
  const partInterpolation = parts.reduce(
    (total, payload) => total + Number(payload?.interpolated_count || 0),
    0,
  );
  return {
    ticker: base.ticker,
    range: "HISTORY",
    start: bars[0]?.ts ?? null,
    end: bars.at(-1)?.ts ?? null,
    source_count: bars.length,
    interpolated_count: partInterpolation
      + (includeBaseInterpolation ? Number(base.interpolated_count || 0) : 0),
    bars,
    trades,
    events,
    shape_forecast: mergeForecasts(payloads, "shape_forecast"),
    relative_momentum: mergeForecasts(payloads, "relative_momentum"),
    algo_overlays: mergeOverlays(payloads),
  };
}

function exponentialMovingAverage(values, period) {
  if (!values.length) return [];
  const multiplier = 2 / (period + 1);
  const result = new Array(values.length);
  result[0] = Number(values[0]);
  for (let index = 1; index < values.length; index += 1) {
    result[index] = (Number(values[index]) - result[index - 1]) * multiplier + result[index - 1];
  }
  return result;
}

function relativeStrengthIndex(values, period = 14) {
  const result = new Array(values.length).fill(null);
  if (values.length <= period) return result;
  let gains = 0;
  let losses = 0;
  for (let index = 1; index <= period; index += 1) {
    const change = Number(values[index]) - Number(values[index - 1]);
    gains += Math.max(0, change);
    losses += Math.max(0, -change);
  }
  let averageGain = gains / period;
  let averageLoss = losses / period;
  const value = () => {
    if (!averageGain && !averageLoss) return 50;
    if (!averageLoss) return 100;
    if (!averageGain) return 0;
    return 100 - 100 / (1 + averageGain / averageLoss);
  };
  result[period] = value();
  for (let index = period + 1; index < values.length; index += 1) {
    const change = Number(values[index]) - Number(values[index - 1]);
    averageGain = (averageGain * (period - 1) + Math.max(0, change)) / period;
    averageLoss = (averageLoss * (period - 1) + Math.max(0, -change)) / period;
    result[index] = value();
  }
  return result;
}

function rateOfChange(values, period = 10) {
  return values.map((price, index) => {
    if (index < period) return null;
    const reference = Number(values[index - period]);
    return reference ? (Number(price) / reference - 1) * 100 : null;
  });
}

function movingAverageConvergenceDivergence(values) {
  const fast = exponentialMovingAverage(values, 12);
  const slow = exponentialMovingAverage(values, 26);
  const macd = values.map((_, index) => fast[index] - slow[index]);
  const signal = exponentialMovingAverage(macd, 9);
  return macd.map((value, index) => ({
    value,
    signal: signal[index],
    histogram: value - signal[index],
  }));
}

function commonMomentumIndicators(bars) {
  const closes = bars.map((bar) => Number(bar.close));
  return {
    rsi: relativeStrengthIndex(closes, 14),
    macd: movingAverageConvergenceDivergence(closes),
    roc: rateOfChange(closes, 10),
  };
}

class PriceChart {
  constructor(canvas, tooltip, onViewChange, onInspect, onTradeHighlight) {
    this.canvas = canvas;
    this.tooltip = tooltip;
    this.onViewChange = onViewChange;
    this.onInspect = onInspect;
    this.onTradeHighlight = onTradeHighlight;
    this.context = canvas.getContext("2d");
    this.style = "line";
    this.indicator = "rsi";
    this.indicators = { rsi: [], macd: [], roc: [] };
    this.barIndexByTimestamp = new Map();
    this.tradePresentationCache = null;
    this.tradeMarkerHitAreas = [];
    this.tradeLineHitAreas = [];
    this.highlightedTradeKeys = new Set();
    this.payload = null;
    this.momentumByTimestamp = new Map();
    this.hoverIndex = null;
    this.focusedTimestamp = null;
    this.bounds = null;
    this.viewStart = 0;
    this.viewEnd = -1;
    this.drag = null;
    this.touchPointers = new Map();
    this.pinch = null;
    this.pinchGestureActive = false;
    this.suppressClick = false;
    this.wheelPanRemainder = 0;
    this.minimumViewPoints = 10;
    this.resizeObserver = new ResizeObserver(() => this.draw());
    this.resizeObserver.observe(canvas.parentElement);
    canvas.addEventListener("pointermove", (event) => this.onPointerMove(event));
    canvas.addEventListener("pointerdown", (event) => this.onPointerDown(event));
    canvas.addEventListener("pointerup", (event) => this.onPointerUp(event));
    canvas.addEventListener("pointercancel", (event) => this.onPointerUp(event));
    canvas.addEventListener("click", (event) => this.onClick(event));
    canvas.addEventListener("blur", () => this.clearFocus());
    canvas.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || this.focusedTimestamp === null) return;
      this.clearFocus();
      event.preventDefault();
    });
    canvas.addEventListener("pointerleave", () => {
      if (!this.drag) this.clearPointer();
    });
    canvas.addEventListener("wheel", (event) => this.onWheel(event), { passive: false });
  }

  setData(payload) {
    const previousPayload = this.payload;
    const previousBars = previousPayload?.bars || [];
    const previousViewLength = this.viewLength();
    const sameSeries = previousPayload?.ticker === payload?.ticker && previousPayload?.range === payload?.range;
    const wasZoomed = previousViewLength > 0 && previousViewLength < previousBars.length;
    const wasAtLatest = this.viewEnd >= previousBars.length - 1;
    const previousStartTime = previousBars[this.viewStart]?.ts;
    this.payload = payload;
    this.tradePresentationCache = null;
    this.highlightedTradeKeys = new Set();
    this.onTradeHighlight?.(this.highlightedTradeKeys);
    this.indicators = commonMomentumIndicators(payload?.bars || []);
    this.barIndexByTimestamp = new Map(
      (payload?.bars || []).map((bar, index) => [Number(bar.ts), index]),
    );
    this.momentumByTimestamp = new Map(
      (payload?.relative_momentum?.snapshots || []).map((snapshot) => [
        Number(snapshot.ts),
        snapshot,
      ]),
    );
    this.wheelPanRemainder = 0;
    this.hoverIndex = null;
    if (!sameSeries) this.focusedTimestamp = null;
    this.tooltip.hidden = true;
    if (sameSeries && wasZoomed && payload.bars.length) {
      let start = 0;
      if (wasAtLatest) {
        start = payload.bars.length - previousViewLength;
      } else {
        const matchingIndex = payload.bars.findIndex((bar) => Number(bar.ts) >= Number(previousStartTime));
        start = matchingIndex < 0 ? payload.bars.length - previousViewLength : matchingIndex;
      }
      this.setView(start, start + previousViewLength - 1, { notify: false, redraw: false });
    } else {
      this.resetView({ notify: false, redraw: false });
    }
    this.notifyViewChange();
    this.notifyInspection();
    this.draw();
  }

  setStyle(style) {
    if (!['line', 'candles'].includes(style)) return;
    this.style = style;
    this.updateLabel();
    this.draw();
  }

  setIndicator(indicator) {
    if (!["rsi", "macd", "roc", "none"].includes(indicator)) return;
    if (indicator === this.indicator) return;
    this.indicator = indicator;
    this.updateLabel();
    this.notifyInspection();
    this.draw();
  }

  visibleTrades(bars = this.visibleBars()) {
    if (!bars.length) return [];
    const timestamps = new Set(bars.map((bar) => Number(bar.ts)));
    return (this.payload?.trades || []).filter(
      (trade) => timestamps.has(Number(trade.ts)),
    );
  }

  tradesAt(timestamp) {
    return (this.payload?.trades || []).filter(
      (trade) => Number(trade.ts) === Number(timestamp),
    );
  }

  tradeKey(trade) {
    return `${trade.algo}|${Number(trade.ts)}`;
  }

  tradeActionKey(trade) {
    return `${this.tradeKey(trade)}|${trade.action}`;
  }

  tradePairsForAction(trade) {
    const key = this.tradeActionKey(trade);
    return this.tradePresentation().pairs.filter((pair) => (
      this.tradeActionKey(pair.entry) === key
      || (pair.exit && this.tradeActionKey(pair.exit) === key)
    ));
  }

  tradeTargetAt(event) {
    const rectangle = this.canvas.getBoundingClientRect();
    const x = event.clientX - rectangle.left;
    const y = event.clientY - rectangle.top;
    const marker = [...this.tradeMarkerHitAreas].reverse().find((area) => (
      x >= area.left && x <= area.right && y >= area.top && y <= area.bottom
    ));
    if (marker) return { algo: marker.trade.algo, pairs: marker.pairs };

    const line = [...this.tradeLineHitAreas].reverse().find((area) => {
      const dx = area.x2 - area.x1;
      const dy = area.y2 - area.y1;
      const lengthSquared = dx * dx + dy * dy;
      const position = lengthSquared
        ? Math.max(0, Math.min(1, ((x - area.x1) * dx + (y - area.y1) * dy) / lengthSquared))
        : 0;
      const nearestX = area.x1 + position * dx;
      const nearestY = area.y1 + position * dy;
      return Math.hypot(x - nearestX, y - nearestY) <= 7;
    });
    return line ? { algo: line.pair.entry.algo, pairs: [line.pair] } : null;
  }

  setTradeHighlight(pairs) {
    const next = new Set(pairs.map(tradePairKey));
    const unchanged = next.size === this.highlightedTradeKeys.size
      && [...next].every((key) => this.highlightedTradeKeys.has(key));
    if (unchanged) return;
    this.highlightedTradeKeys = next;
    this.onTradeHighlight?.(next);
    this.draw();
  }

  clearTradeHighlight() {
    this.setTradeHighlight([]);
  }

  tradePresentation() {
    if (this.tradePresentationCache) return this.tradePresentationCache;
    const barByTimestamp = new Map(
      (this.payload?.bars || []).map((bar) => [Number(bar.ts), bar]),
    );
    const trades = (this.payload?.trades || [])
      .slice()
      .sort((left, right) => (
        Number(left.ts) - Number(right.ts) || String(left.algo).localeCompare(String(right.algo))
      ));
    const openEntriesByAlgo = new Map();
    const pairs = [];
    const exitSummaries = new Map();

    trades.forEach((trade) => {
      const openEntries = openEntriesByAlgo.get(trade.algo) || [];
      openEntriesByAlgo.set(trade.algo, openEntries);
      if (trade.action === "entry") {
        const pair = { entry: trade, exit: null, returnPct: null };
        openEntries.push(pair);
        pairs.push(pair);
        return;
      }
      if (trade.action !== "exit_all") return;

      const exitBar = barByTimestamp.get(Number(trade.ts));
      const closed = openEntries.splice(0);
      const returns = [];
      closed.forEach((pair) => {
        const entryBar = barByTimestamp.get(Number(pair.entry.ts));
        const entryPrice = Number(entryBar?.close);
        const exitPrice = Number(exitBar?.close);
        const direction = Number(pair.entry.direction) < 0 ? -1 : 1;
        const returnPct = Number.isFinite(entryPrice) && entryPrice > 0 && Number.isFinite(exitPrice)
          ? ((exitPrice / entryPrice) - 1) * 100 * direction
          : null;
        if (Number.isFinite(returnPct)) returns.push(returnPct);
        pair.exit = trade;
        pair.returnPct = returnPct;
      });
      if (returns.length) {
        exitSummaries.set(this.tradeKey(trade), {
          entryCount: returns.length,
          averageReturnPct: returns.reduce((total, value) => total + value, 0) / returns.length,
        });
      }
    });

    this.tradePresentationCache = { pairs, exitSummaries };
    return this.tradePresentationCache;
  }

  shapeAt(timestamp) {
    const forecast = this.payload?.shape_forecast;
    const snapshots = forecast?.snapshots || [];
    if (!snapshots.length) return null;
    const target = Number(timestamp);
    let low = 0;
    let high = snapshots.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (Number(snapshots[middle].ts) <= target) low = middle + 1;
      else high = middle;
    }
    const snapshot = snapshots[low - 1];
    if (!snapshot) return null;
    return pacificDateKey(snapshot.ts) === pacificDateKey(timestamp) ? snapshot : null;
  }

  momentumAt(timestamp) {
    return this.momentumByTimestamp.get(Number(timestamp)) || null;
  }

  indicatorAt(timestamp) {
    if (this.indicator === "none") return null;
    const index = this.barIndexByTimestamp.get(Number(timestamp));
    if (index === undefined) return null;
    const value = this.indicators[this.indicator]?.[index];
    return value === undefined ? null : value;
  }

  focusedIndex() {
    if (this.focusedTimestamp === null) return null;
    const index = this.payload?.bars?.findIndex(
      (bar) => Number(bar.ts) === Number(this.focusedTimestamp),
    );
    return index >= 0 ? index : null;
  }

  highlightedVisibleIndex() {
    if (this.hoverIndex !== null) {
      return Math.max(0, Math.min(this.viewLength() - 1, this.hoverIndex));
    }
    const focused = this.focusedIndex();
    if (focused === null || focused < this.viewStart || focused > this.viewEnd) return null;
    return focused - this.viewStart;
  }

  changeAt(index) {
    const bars = this.payload?.bars || [];
    const bar = bars[index];
    if (!bar) return { change: null, changePct: null };
    const session = pacificDateKey(bar.ts);
    let sessionStart = index;
    while (sessionStart > 0 && pacificDateKey(bars[sessionStart - 1].ts) === session) {
      sessionStart -= 1;
    }
    const baseline = Number(
      sessionStart > 0 ? bars[sessionStart - 1].close : bars[sessionStart].open,
    );
    const price = Number(bar.close);
    const change = price - baseline;
    return {
      change,
      changePct: baseline ? (change / baseline) * 100 : 0,
    };
  }

  notifyInspection() {
    const bars = this.payload?.bars || [];
    if (!bars.length) {
      this.onInspect?.(null);
      return;
    }
    const focused = this.hoverIndex === null ? this.focusedIndex() : null;
    const index = this.hoverIndex !== null
      ? this.viewStart + Math.max(0, Math.min(this.viewLength() - 1, this.hoverIndex))
      : focused ?? bars.length - 1;
    const bar = bars[index];
    const { change, changePct } = this.changeAt(index);
    this.onInspect?.({
      bar,
      change,
      changePct,
      shape: this.shapeAt(bar.ts),
      momentum: this.momentumAt(bar.ts),
      indicator: this.indicatorAt(bar.ts),
      mode: this.hoverIndex !== null ? "hover" : focused !== null ? "focused" : "latest",
    });
  }

  updateLabel() {
    const label = this.style === "candles" ? "candlestick" : "line";
    const visible = this.visibleBars();
    const first = visible[0];
    const last = visible.at(-1);
    const visibleTrades = this.visibleTrades(visible);
    const actionCount = visibleTrades.length;
    const algoCount = new Set(visibleTrades.map((trade) => trade.algo)).size;
    const overlayLabel = actionCount
      ? `, trade overlay with ${actionCount.toLocaleString()} ${actionCount === 1 ? "action" : "actions"} from ${algoCount.toLocaleString()} ${algoCount === 1 ? "algorithm" : "algorithms"}`
      : "";
    const momentumLabel = this.payload?.relative_momentum?.snapshots?.length
      ? ", with relative momentum below volume"
      : "";
    const indicatorLabel = this.indicator === "none"
      ? ""
      : `, with ${this.indicator.toUpperCase()} below relative momentum`;
    this.canvas.setAttribute(
      "aria-label",
      visible.length
        ? `${this.payload.ticker} ${label} chart, ${visible.length.toLocaleString()} visible points from ${pacificDateTime.format(dateFromEpoch(first.ts))} to ${pacificDateTime.format(dateFromEpoch(last.ts))}${overlayLabel}${momentumLabel}${indicatorLabel}`
        : `${this.payload?.ticker || "Ticker"} ${label} chart with no available bars`,
    );
  }

  totalBars() {
    return this.payload?.bars?.length || 0;
  }

  viewLength() {
    return this.viewEnd >= this.viewStart ? this.viewEnd - this.viewStart + 1 : 0;
  }

  visibleBars() {
    return this.payload?.bars?.slice(this.viewStart, this.viewEnd + 1) || [];
  }

  setView(start, end, { notify = true, redraw = true } = {}) {
    const total = this.totalBars();
    if (!total) {
      this.viewStart = 0;
      this.viewEnd = -1;
    } else {
      const requestedLength = Math.max(1, Math.round(end - start + 1));
      const length = Math.min(total, requestedLength);
      const maximumStart = total - length;
      this.viewStart = Math.max(0, Math.min(maximumStart, Math.round(start)));
      this.viewEnd = this.viewStart + length - 1;
    }
    this.hoverIndex = null;
    this.tooltip.hidden = true;
    if (notify) this.notifyViewChange();
    if (notify || redraw) this.notifyInspection();
    if (redraw) this.draw();
  }

  resetView({ notify = true, redraw = true } = {}) {
    this.setView(0, this.totalBars() - 1, { notify, redraw });
  }

  zoom(factor, anchor = 0.5) {
    const total = this.totalBars();
    const currentLength = this.viewLength();
    if (!total || !currentLength) return;
    const minimum = Math.min(this.minimumViewPoints, total);
    const nextLength = Math.max(minimum, Math.min(total, Math.round(currentLength * factor)));
    if (nextLength === currentLength) return;
    const safeAnchor = Math.max(0, Math.min(1, anchor));
    const anchorIndex = this.viewStart + (currentLength - 1) * safeAnchor;
    const nextStart = Math.round(anchorIndex - (nextLength - 1) * safeAnchor);
    this.wheelPanRemainder = 0;
    this.setView(nextStart, nextStart + nextLength - 1);
  }

  panTo(start) {
    const length = this.viewLength();
    this.setView(start, start + length - 1);
  }

  notifyViewChange() {
    this.updateLabel();
    const visibleBars = this.visibleBars();
    this.onViewChange?.({
      visible: this.viewLength(),
      total: this.totalBars(),
      canZoomIn: this.viewLength() > Math.min(this.minimumViewPoints, this.totalBars()),
      canZoomOut: this.viewLength() < this.totalBars(),
      firstDate: visibleBars.length ? pacificDateKey(visibleBars[0].ts) : null,
      lastDate: visibleBars.length ? pacificDateKey(visibleBars.at(-1).ts) : null,
    });
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

  drawTradeMarkers(ctx, bars, x, y, margin, priceBottom) {
    const trades = this.visibleTrades(bars);
    if (!trades.length) return;
    const indexByTimestamp = new Map(bars.map((bar, index) => [Number(bar.ts), index]));
    const barByTimestamp = new Map(bars.map((bar) => [Number(bar.ts), bar]));
    const { pairs, exitSummaries } = this.tradePresentation();
    const plotRight = (this.bounds?.width || this.canvas.clientWidth) - margin.right;
    const badgeHeight = 18;
    const placedBadges = [];

    ctx.save();
    ctx.lineJoin = "round";

    const orderedPairs = pairs.slice().sort((left, right) => (
      Number(this.highlightedTradeKeys.has(tradePairKey(left)))
      - Number(this.highlightedTradeKeys.has(tradePairKey(right)))
    ));
    orderedPairs.forEach((pair) => {
      if (!pair.exit) return;
      const entryIndex = indexByTimestamp.get(Number(pair.entry.ts));
      const exitIndex = indexByTimestamp.get(Number(pair.exit.ts));
      if (entryIndex === undefined || exitIndex === undefined) return;
      const entryBar = barByTimestamp.get(Number(pair.entry.ts));
      const exitBar = barByTimestamp.get(Number(pair.exit.ts));
      const resultColor = Number(pair.returnPct) >= 0 ? "216, 255, 114" : "255, 123, 115";
      const highlighted = this.highlightedTradeKeys.has(tradePairKey(pair));
      const dimmed = this.highlightedTradeKeys.size > 0 && !highlighted;
      const x1 = x(entryIndex);
      const y1 = y(entryBar.close);
      const x2 = x(exitIndex);
      const y2 = y(exitBar.close);
      this.tradeLineHitAreas.push({ pair, x1, y1, x2, y2 });

      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.strokeStyle = `rgba(${resultColor}, ${highlighted ? 0.34 : dimmed ? 0.03 : 0.12})`;
      ctx.lineWidth = highlighted ? 9 : 5;
      ctx.setLineDash([]);
      ctx.stroke();

      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.strokeStyle = `rgba(${resultColor}, ${highlighted ? 1 : dimmed ? 0.18 : 0.58})`;
      ctx.lineWidth = highlighted ? 2.5 : 1.25;
      ctx.setLineDash([5, 4]);
      ctx.stroke();
    });
    ctx.setLineDash([]);

    trades.forEach((trade) => {
      const index = indexByTimestamp.get(Number(trade.ts));
      if (index === undefined) return;
      const bar = bars[index];
      const isEntry = trade.action === "entry";
      const isLong = Number(trade.direction) >= 0;
      const badgeBelowPrice = isEntry ? isLong : !isLong;
      const pointX = x(index);
      const priceY = y(bar.close);
      const color = isEntry ? "#70d8dc" : "#ff7b73";
      const actionText = `${isEntry ? "IN" : "OUT"} ${isLong ? "↑" : "↓"}`;
      ctx.font = "700 9px ui-monospace, SFMono-Regular, monospace";
      const actionWidth = Math.ceil(ctx.measureText(actionText).width) + 12;
      const summary = isEntry ? null : exitSummaries.get(this.tradeKey(trade));
      const formattedReturn = formatTradeReturn(summary?.averageReturnPct);
      const returnText = formattedReturn
        ? `${summary.entryCount > 1 ? "AVG " : ""}${formattedReturn}`
        : null;
      const markerPairs = this.tradePairsForAction(trade);
      const highlighted = markerPairs.some((pair) => (
        this.highlightedTradeKeys.has(tradePairKey(pair))
      ));
      const returnWidth = returnText ? Math.ceil(ctx.measureText(returnText).width) + 12 : 0;
      const badgeGap = returnText ? 4 : 0;
      const actionLeft = Math.max(
        margin.left,
        Math.min(plotRight - actionWidth, pointX - actionWidth / 2),
      );
      const returnLeft = !returnText
        ? null
        : actionLeft + actionWidth + badgeGap + returnWidth <= plotRight
          ? actionLeft + actionWidth + badgeGap
          : actionLeft - badgeGap - returnWidth;
      const groupLeft = returnLeft === null ? actionLeft : Math.min(actionLeft, returnLeft);
      const groupRight = returnLeft === null
        ? actionLeft + actionWidth
        : Math.max(actionLeft + actionWidth, returnLeft + returnWidth);
      const boundedCenterY = (value) => Math.max(
        margin.top + badgeHeight / 2,
        Math.min(priceBottom - badgeHeight / 2, value),
      );
      let badgeCenterY = boundedCenterY(priceY + (badgeBelowPrice ? 28 : -28));
      for (let attempt = 0; attempt < 4; attempt += 1) {
        const badgeTop = badgeCenterY - badgeHeight / 2;
        const overlaps = placedBadges.some((placed) => (
          groupLeft < placed.right + 3
          && groupRight > placed.left - 3
          && badgeTop < placed.bottom + 3
          && badgeTop + badgeHeight > placed.top - 3
        ));
        if (!overlaps) break;
        const shifted = boundedCenterY(
          badgeCenterY + (badgeBelowPrice ? badgeHeight + 4 : -badgeHeight - 4),
        );
        if (shifted === badgeCenterY) break;
        badgeCenterY = shifted;
      }
      const badgeTop = badgeCenterY - badgeHeight / 2;
      placedBadges.push({
        left: groupLeft,
        right: groupRight,
        top: badgeTop,
        bottom: badgeTop + badgeHeight,
      });
      this.tradeMarkerHitAreas.push({
        trade,
        pairs: markerPairs,
        left: actionLeft,
        right: actionLeft + actionWidth,
        top: badgeTop,
        bottom: badgeTop + badgeHeight,
      });
      const actionCenterX = actionLeft + actionWidth / 2;
      const leaderEndY = badgeBelowPrice ? badgeTop : badgeTop + badgeHeight;

      ctx.beginPath();
      ctx.moveTo(pointX, priceY + (badgeBelowPrice ? 4 : -4));
      ctx.lineTo(actionCenterX, leaderEndY);
      ctx.strokeStyle = isEntry
        ? `rgba(112, 216, 220, ${highlighted ? 1 : 0.82})`
        : `rgba(255, 123, 115, ${highlighted ? 1 : 0.82})`;
      ctx.lineWidth = highlighted ? 2.4 : 1.4;
      ctx.stroke();

      ctx.beginPath();
      ctx.fillStyle = color;
      ctx.arc(pointX, priceY, highlighted ? 4.5 : 3.2, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = highlighted ? "#f4fbff" : "#101721";
      ctx.lineWidth = highlighted ? 2 : 1.5;
      ctx.stroke();

      if (highlighted) {
        roundedRectPath(ctx, actionLeft - 2, badgeTop - 2, actionWidth + 4, badgeHeight + 4, isEntry ? 11 : 5);
        ctx.strokeStyle = "rgba(244, 251, 255, 0.95)";
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      roundedRectPath(ctx, actionLeft, badgeTop, actionWidth, badgeHeight, isEntry ? 9 : 3);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = "rgba(7, 16, 20, 0.9)";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = "#071014";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(actionText, actionCenterX, badgeCenterY + 0.5);

      if (returnText && returnLeft !== null) {
        const resultColor = Number(summary.averageReturnPct) >= 0 ? "#d8ff72" : "#ff7b73";
        roundedRectPath(ctx, returnLeft, badgeTop, returnWidth, badgeHeight, 3);
        ctx.fillStyle = "rgba(10, 15, 21, 0.96)";
        ctx.fill();
        ctx.strokeStyle = resultColor;
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.fillStyle = resultColor;
        ctx.fillText(returnText, returnLeft + returnWidth / 2, badgeCenterY + 0.5);
      }
    });
    ctx.restore();
  }

  drawMomentumPanel(ctx, bars, x, margin, plotWidth, momentumTop, momentumBottom) {
    const panelHeight = momentumBottom - momentumTop;
    const momentumY = (value) => (
      momentumTop + ((100 - Math.max(-100, Math.min(100, Number(value)))) / 200) * panelHeight
    );
    const configuredThreshold = Number(this.payload?.relative_momentum?.strong_threshold);
    const threshold = Number.isFinite(configuredThreshold)
      ? Math.max(0, Math.min(100, configuredThreshold))
      : 80;
    const positiveThresholdY = momentumY(threshold);
    const negativeThresholdY = momentumY(-threshold);

    ctx.save();
    ctx.fillStyle = "rgba(216, 255, 114, 0.035)";
    ctx.fillRect(margin.left, momentumTop, plotWidth, positiveThresholdY - momentumTop);
    ctx.fillStyle = "rgba(255, 123, 115, 0.03)";
    ctx.fillRect(margin.left, negativeThresholdY, plotWidth, momentumBottom - negativeThresholdY);

    [threshold, 0, -threshold].forEach((value) => {
      const lineY = Math.round(momentumY(value)) + 0.5;
      ctx.beginPath();
      ctx.strokeStyle = value === 0
        ? "rgba(227, 235, 240, 0.18)"
        : "rgba(227, 235, 240, 0.07)";
      ctx.setLineDash(value === 0 ? [] : [3, 5]);
      ctx.moveTo(margin.left, lineY);
      ctx.lineTo(margin.left + plotWidth, lineY);
      ctx.stroke();
    });
    ctx.setLineDash([]);

    ctx.font = "9px ui-monospace, SFMono-Regular, monospace";
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    ctx.fillStyle = "#75838e";
    ctx.fillText("REL MOMENTUM", margin.left + 3, momentumTop + 8);
    ctx.textAlign = "right";
    ctx.fillStyle = "#5f6c77";
    ctx.fillText("+100", margin.left + plotWidth + margin.right - 2, momentumTop + 5);
    ctx.fillText("0", margin.left + plotWidth + margin.right - 2, momentumY(0));
    ctx.fillText("-100", margin.left + plotWidth + margin.right - 2, momentumBottom - 5);

    let previous = null;
    let activeColor = null;
    let activePath = false;
    const flush = () => {
      if (!activePath) return;
      ctx.strokeStyle = activeColor;
      ctx.lineWidth = 1.4;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();
      activePath = false;
    };

    bars.forEach((bar, index) => {
      const snapshot = this.momentumAt(bar.ts);
      if (!snapshot || !Number.isFinite(Number(snapshot.value))) {
        flush();
        previous = null;
        activeColor = null;
        return;
      }
      const point = {
        index,
        ts: Number(bar.ts),
        value: Number(snapshot.value),
        x: x(index),
        y: momentumY(snapshot.value),
      };
      const contiguous = previous
        && point.index === previous.index + 1
        && point.ts - previous.ts <= 90;
      if (!contiguous) {
        flush();
        previous = point;
        activeColor = null;
        return;
      }
      const color = (point.value + previous.value) / 2 >= 0 ? "#d8ff72" : "#ff7b73";
      if (!activePath || color !== activeColor) {
        flush();
        ctx.beginPath();
        ctx.moveTo(previous.x, previous.y);
        activePath = true;
        activeColor = color;
      }
      ctx.lineTo(point.x, point.y);
      previous = point;
    });
    flush();

    const highlightedIndex = this.highlightedVisibleIndex();
    if (highlightedIndex !== null) {
      const index = Math.max(0, Math.min(bars.length - 1, highlightedIndex));
      const snapshot = this.momentumAt(bars[index].ts);
      if (snapshot && Number.isFinite(Number(snapshot.value))) {
        ctx.beginPath();
        ctx.fillStyle = Number(snapshot.value) >= 0 ? "#d8ff72" : "#ff7b73";
        ctx.arc(x(index), momentumY(snapshot.value), this.hoverIndex === null ? 3.4 : 2.8, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "#101721";
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  drawCommonIndicatorPanel(ctx, bars, x, margin, plotWidth, panelTop, panelBottom) {
    if (this.indicator === "none") return;
    const values = this.indicators[this.indicator]?.slice(this.viewStart, this.viewEnd + 1) || [];
    const panelHeight = panelBottom - panelTop;
    const axisX = margin.left + plotWidth + margin.right - 2;
    const line = (series, y, color, accessor = (value) => value) => {
      ctx.beginPath();
      let active = false;
      series.forEach((item, index) => {
        const value = accessor(item);
        if (!Number.isFinite(Number(value))) {
          active = false;
          return;
        }
        if (active) ctx.lineTo(x(index), y(value));
        else ctx.moveTo(x(index), y(value));
        active = true;
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.25;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();
    };
    const grid = (levels, y) => {
      levels.forEach((level) => {
        ctx.beginPath();
        ctx.strokeStyle = level === 0 || level === 50
          ? "rgba(227, 235, 240, 0.16)"
          : "rgba(227, 235, 240, 0.07)";
        ctx.setLineDash(level === 0 || level === 50 ? [] : [3, 5]);
        ctx.moveTo(margin.left, Math.round(y(level)) + 0.5);
        ctx.lineTo(margin.left + plotWidth, Math.round(y(level)) + 0.5);
        ctx.stroke();
      });
      ctx.setLineDash([]);
    };
    const axisLabel = (value, suffix = "") => {
      const absolute = Math.abs(Number(value));
      const digits = absolute >= 10 ? 0 : absolute >= 1 ? 1 : 2;
      return `${Number(value) > 0 ? "+" : ""}${Number(value).toFixed(digits)}${suffix}`;
    };

    ctx.save();
    ctx.font = "9px ui-monospace, SFMono-Regular, monospace";
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    ctx.fillStyle = "#75838e";

    let y;
    let highlighted = null;
    const highlightedIndex = this.highlightedVisibleIndex();
    if (this.indicator === "rsi") {
      y = (value) => panelTop + ((100 - Math.max(0, Math.min(100, Number(value)))) / 100) * panelHeight;
      ctx.fillStyle = "rgba(255, 123, 115, 0.025)";
      ctx.fillRect(margin.left, panelTop, plotWidth, y(70) - panelTop);
      ctx.fillStyle = "rgba(112, 216, 220, 0.025)";
      ctx.fillRect(margin.left, y(30), plotWidth, panelBottom - y(30));
      grid([70, 50, 30], y);
      ctx.fillStyle = "#75838e";
      ctx.fillText("RSI 14", margin.left + 3, panelTop + 8);
      ctx.textAlign = "right";
      ctx.fillStyle = "#5f6c77";
      [70, 50, 30].forEach((level) => ctx.fillText(String(level), axisX, y(level)));
      line(values, y, "#70d8dc");
      highlighted = highlightedIndex === null ? null : values[highlightedIndex];
    } else if (this.indicator === "roc") {
      const numeric = values.filter((value) => Number.isFinite(Number(value))).map(Number);
      const maximum = Math.max(0.05, ...numeric.map((value) => Math.abs(value)));
      const bound = Math.ceil(maximum * 10) / 10;
      y = (value) => panelTop + ((bound - Math.max(-bound, Math.min(bound, Number(value)))) / (bound * 2)) * panelHeight;
      grid([0], y);
      ctx.fillText("ROC 10", margin.left + 3, panelTop + 8);
      ctx.textAlign = "right";
      ctx.fillStyle = "#5f6c77";
      ctx.fillText(axisLabel(bound, "%"), axisX, panelTop + 5);
      ctx.fillText("0", axisX, y(0));
      ctx.fillText(axisLabel(-bound, "%"), axisX, panelBottom - 5);
      line(values, y, "#d8ff72");
      highlighted = highlightedIndex === null ? null : values[highlightedIndex];
    } else {
      const numeric = values.flatMap((value) => (
        value ? [Number(value.value), Number(value.signal)] : []
      )).filter(Number.isFinite);
      const maximum = Math.max(0.01, ...numeric.map((value) => Math.abs(value)));
      const bound = Math.ceil(maximum * 100) / 100;
      y = (value) => panelTop + ((bound - Math.max(-bound, Math.min(bound, Number(value)))) / (bound * 2)) * panelHeight;
      grid([0], y);
      ctx.fillText("MACD 12·26·9", margin.left + 3, panelTop + 8);
      ctx.textAlign = "right";
      ctx.fillStyle = "#5f6c77";
      ctx.fillText(axisLabel(bound), axisX, panelTop + 5);
      ctx.fillText("0", axisX, y(0));
      ctx.fillText(axisLabel(-bound), axisX, panelBottom - 5);
      const zeroY = y(0);
      const barWidth = Math.max(1, Math.min(plotWidth / Math.max(1, values.length), 3));
      values.forEach((value, index) => {
        if (!value || !Number.isFinite(Number(value.histogram))) return;
        const valueY = y(value.histogram);
        ctx.fillStyle = Number(value.histogram) >= 0
          ? "rgba(216, 255, 114, 0.24)"
          : "rgba(255, 123, 115, 0.22)";
        ctx.fillRect(x(index), Math.min(zeroY, valueY), barWidth, Math.max(1, Math.abs(zeroY - valueY)));
      });
      line(values, y, "#d8ff72", (value) => value?.value);
      line(values, y, "#f7b955", (value) => value?.signal);
      highlighted = highlightedIndex === null ? null : values[highlightedIndex];
    }

    if (highlightedIndex !== null && highlighted !== null && y) {
      const pointValues = this.indicator === "macd"
        ? [
            { value: highlighted?.value, color: "#d8ff72" },
            { value: highlighted?.signal, color: "#f7b955" },
          ]
        : [{ value: highlighted, color: this.indicator === "rsi" ? "#70d8dc" : "#d8ff72" }];
      pointValues.forEach((point) => {
        if (!Number.isFinite(Number(point.value))) return;
        ctx.beginPath();
        ctx.fillStyle = point.color;
        ctx.arc(x(highlightedIndex), y(point.value), 2.8, 0, Math.PI * 2);
        ctx.fill();
      });
    }
    ctx.restore();
  }

  draw() {
    const { width, height } = this.size();
    const ctx = this.context;
    ctx.clearRect(0, 0, width, height);
    this.tradeMarkerHitAreas = [];
    this.tradeLineHitAreas = [];
    const bars = this.visibleBars();
    if (!bars.length) return;

    const margin = { top: 14, right: width < 500 ? 49 : 62, bottom: 32, left: 4 };
    const volumeHeight = 42;
    const momentumHeight = width < 500 ? 54 : 64;
    const indicatorHeight = width < 500 ? 50 : 60;
    const panelGap = 12;
    const indicatorVisible = this.indicator !== "none";
    const indicatorBottom = height - margin.bottom;
    const indicatorTop = indicatorBottom - (indicatorVisible ? indicatorHeight : 0);
    const momentumBottom = indicatorVisible ? indicatorTop - panelGap : indicatorBottom;
    const momentumTop = momentumBottom - momentumHeight;
    const volumeBottom = momentumTop - panelGap;
    const priceBottom = volumeBottom - volumeHeight - 18;
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
      const parts = pacificDateTime.formatToParts(dateFromEpoch(bar.ts));
      const session = `${parts.find((part) => part.type === "month")?.value}-${parts.find((part) => part.type === "day")?.value}`;
      if (session !== lastSession) sessionStarts.push({ index, label: session });
      lastSession = session;
    });
    const labelEvery = Math.max(1, Math.ceil(sessionStarts.length / Math.max(3, width / 110)));
    sessionStarts.forEach((item, index) => {
      const lineX = x(item.index);
      if (item.index > 0) {
        ctx.beginPath();
        ctx.strokeStyle = "rgba(227, 235, 240, 0.07)";
        ctx.setLineDash([3, 5]);
        ctx.moveTo(lineX, margin.top);
        ctx.lineTo(lineX, height - margin.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (sessionStarts.length > 1 && index % labelEvery === 0) {
        ctx.textAlign = "left";
        ctx.fillStyle = "#5f6c77";
        ctx.fillText(item.label, Math.min(lineX + (item.index > 0 ? 4 : 0), width - margin.right - 30), height - 10);
      }
    });

    if (sessionStarts.length <= 1 && bars.length > 1) {
      const tickCount = Math.max(2, Math.min(5, Math.floor(plotWidth / 90)));
      for (let tick = 0; tick < tickCount; tick += 1) {
        const index = Math.round((bars.length - 1) * (tick / (tickCount - 1)));
        ctx.textAlign = tick === 0 ? "left" : tick === tickCount - 1 ? "right" : "center";
        ctx.fillStyle = "#5f6c77";
        ctx.fillText(pacificClock.format(dateFromEpoch(bars[index].ts)), x(index), height - 10);
      }
    }

    const maximumVolume = Math.max(...bars.map((bar) => Number(bar.volume)), 1);
    bars.forEach((bar, index) => {
      const relativeVolume = Math.max(0, Number(bar.volume)) / maximumVolume;
      const barHeight = Math.max(1, Math.sqrt(relativeVolume) * volumeHeight);
      const barWidth = Math.max(1, Math.min(step, 3));
      const top = volumeBottom - barHeight;
      const open = Number(bar.open);
      const close = Number(bar.close);
      const color = close > open ? "216, 255, 114" : close < open ? "255, 123, 115" : "112, 216, 220";
      const opacity = 0.38 + Math.sqrt(relativeVolume) * 0.42;
      ctx.fillStyle = `rgba(${color}, ${opacity})`;
      ctx.fillRect(x(index), top, barWidth, barHeight);
      ctx.fillStyle = `rgba(${color}, ${Math.min(1, opacity + 0.18)})`;
      ctx.fillRect(x(index), top, barWidth, Math.min(1.25, barHeight));
    });

    ctx.font = "9px ui-monospace, SFMono-Regular, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#75838e";
    ctx.fillText("VOLUME", margin.left + 3, priceBottom + 8);

    this.drawMomentumPanel(
      ctx,
      bars,
      x,
      margin,
      plotWidth,
      momentumTop,
      momentumBottom,
    );
    if (indicatorVisible) {
      this.drawCommonIndicatorPanel(
        ctx,
        bars,
        x,
        margin,
        plotWidth,
        indicatorTop,
        indicatorBottom,
      );
    }

    const rising = Number(bars.at(-1).close) >= Number(bars[0].open);
    const color = rising ? "#d8ff72" : "#ff7b73";
    const fill = ctx.createLinearGradient(0, margin.top, 0, priceBottom);
    fill.addColorStop(0, rising ? "rgba(216,255,114,0.17)" : "rgba(255,123,115,0.15)");
    fill.addColorStop(1, "rgba(16,23,33,0)");

    if (this.style === "line") {
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

      ctx.beginPath();
      bars.forEach((bar, index) => {
        if (index === 0) ctx.moveTo(x(index), y(bar.close));
        else ctx.lineTo(x(index), y(bar.close));
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.lineJoin = "round";
      ctx.stroke();
    } else {
      const candleWidth = Math.max(0.55, Math.min(6, step * 0.7));
      const candleLineWidth = Math.max(0.5, Math.min(1, step * 0.55));
      const directions = [
        { rising: true, color: "#d8ff72" },
        { rising: false, color: "#ff7b73" },
      ];
      directions.forEach(({ rising: direction, color: candleColor }) => {
        ctx.beginPath();
        bars.forEach((bar, index) => {
          const isRising = Number(bar.close) >= Number(bar.open);
          if (isRising !== direction) return;
          const pointX = x(index);
          ctx.moveTo(pointX, y(bar.high));
          ctx.lineTo(pointX, y(bar.low));
        });
        ctx.strokeStyle = candleColor;
        ctx.lineWidth = candleLineWidth;
        ctx.stroke();
        ctx.fillStyle = candleColor;
        bars.forEach((bar, index) => {
          const isRising = Number(bar.close) >= Number(bar.open);
          if (isRising !== direction) return;
          const top = Math.min(y(bar.open), y(bar.close));
          const bodyHeight = Math.max(1, Math.abs(y(bar.open) - y(bar.close)));
          ctx.fillRect(x(index) - candleWidth / 2, top, candleWidth, bodyHeight);
        });
      });
    }

    this.drawTradeMarkers(ctx, bars, x, y, margin, priceBottom);

    const highlightedIndex = this.highlightedVisibleIndex();
    if (highlightedIndex !== null) {
      const index = Math.max(0, Math.min(bars.length - 1, highlightedIndex));
      const pointX = x(index);
      const pointY = y(bars[index].close);
      const focused = this.hoverIndex === null;
      ctx.beginPath();
      ctx.strokeStyle = focused ? "rgba(112, 216, 220, 0.62)" : "rgba(237, 241, 239, 0.36)";
      ctx.setLineDash([3, 4]);
      ctx.moveTo(pointX, margin.top);
      ctx.lineTo(pointX, height - margin.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.fillStyle = focused ? "#70d8dc" : "#edf1ef";
      ctx.arc(pointX, pointY, focused ? 3.8 : 3.2, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  pointerIndex(event) {
    const bars = this.visibleBars();
    if (!bars.length || !this.bounds) return null;
    const rectangle = this.canvas.getBoundingClientRect();
    const localX = event.clientX - rectangle.left;
    const ratio = (localX - this.bounds.margin.left) / this.bounds.plotWidth;
    return Math.max(0, Math.min(bars.length - 1, Math.round(ratio * (bars.length - 1))));
  }

  anchorForClientX(clientX) {
    const rectangle = this.canvas.getBoundingClientRect();
    const localX = clientX - rectangle.left;
    return Math.max(0, Math.min(1, (localX - this.bounds.margin.left) / this.bounds.plotWidth));
  }

  startPinch() {
    const points = Array.from(this.touchPointers.entries()).slice(0, 2);
    if (points.length < 2) return;
    const [[firstId, first], [secondId, second]] = points;
    const midpointX = (first.x + second.x) / 2;
    const startLength = this.viewLength();
    const startAnchor = this.anchorForClientX(midpointX);
    this.pinch = {
      pointerIds: [firstId, secondId],
      startDistance: Math.max(1, Math.hypot(second.x - first.x, second.y - first.y)),
      startLength,
      anchorIndex: this.viewStart + (startLength - 1) * startAnchor,
    };
    this.pinchGestureActive = true;
    this.drag = null;
    this.canvas.classList.remove("is-dragging");
    points.forEach(([pointerId]) => this.canvas.setPointerCapture?.(pointerId));
  }

  updatePinch() {
    if (!this.pinch) return;
    const [firstId, secondId] = this.pinch.pointerIds;
    const first = this.touchPointers.get(firstId);
    const second = this.touchPointers.get(secondId);
    if (!first || !second) return;
    const distance = Math.max(1, Math.hypot(second.x - first.x, second.y - first.y));
    const midpointX = (first.x + second.x) / 2;
    const total = this.totalBars();
    const minimum = Math.min(this.minimumViewPoints, total);
    const nextLength = Math.max(
      minimum,
      Math.min(total, Math.round(this.pinch.startLength * this.pinch.startDistance / distance)),
    );
    const anchor = this.anchorForClientX(midpointX);
    const maximumStart = total - nextLength;
    const nextStart = Math.max(
      0,
      Math.min(maximumStart, Math.round(this.pinch.anchorIndex - (nextLength - 1) * anchor)),
    );
    if (nextStart === this.viewStart && nextLength === this.viewLength()) return;
    this.focusedTimestamp = null;
    this.wheelPanRemainder = 0;
    this.setView(nextStart, nextStart + nextLength - 1);
  }

  onPointer(event) {
    const bars = this.visibleBars();
    const index = this.pointerIndex(event);
    if (!bars.length || index === null) return;
    const rectangle = this.canvas.getBoundingClientRect();
    const localX = event.clientX - rectangle.left;
    this.hoverIndex = index;
    const bar = bars[this.hoverIndex];
    const contents = [
      createElement("strong", "", pacificDateTime.format(dateFromEpoch(bar.ts))),
      createElement("span", "", `O ${formatPrice(bar.open)} · H ${formatPrice(bar.high)}`),
      createElement("span", "", `L ${formatPrice(bar.low)} · C ${formatPrice(bar.close)}`),
      createElement("span", "", `Vol ${compactFormat.format(bar.volume)}`),
    ];
    const momentum = this.momentumAt(bar.ts);
    if (momentum) {
      const state = momentum.persistent ? "persistent" : "developing";
      contents.push(
        createElement(
          "span",
          Number(momentum.value) >= 0 ? "momentum-up" : "momentum-down",
          `Rel momentum ${formatSigned(momentum.value)} · ${state}`,
        ),
      );
    }
    const indicator = this.indicatorAt(bar.ts);
    if (this.indicator === "rsi" && Number.isFinite(Number(indicator))) {
      contents.push(createElement("span", "indicator-value", `RSI 14 ${Number(indicator).toFixed(1)}`));
    } else if (this.indicator === "roc" && Number.isFinite(Number(indicator))) {
      contents.push(createElement("span", "indicator-value", `ROC 10 ${formatSigned(indicator, "%")}`));
    } else if (this.indicator === "macd" && indicator) {
      contents.push(
        createElement(
          "span",
          "indicator-value",
          `MACD ${formatSigned(indicator.value)} · signal ${formatSigned(indicator.signal)}`,
        ),
      );
    }
    this.tradesAt(bar.ts).forEach((trade) => {
      const action = trade.action === "exit_all" ? "exit" : "entry";
      const direction = Number(trade.direction) < 0 ? "Short" : "Long";
      const summary = action === "exit"
        ? this.tradePresentation().exitSummaries.get(this.tradeKey(trade))
        : null;
      const result = formatTradeReturn(summary?.averageReturnPct);
      const resultDetail = result
        ? ` · ${result}${summary.entryCount > 1 ? ` average across ${summary.entryCount} entries` : ""}`
        : "";
      contents.push(
        createElement(
          "span",
          `algo-action ${action}`,
          `${algorithmDisplayName(trade)} · ${direction} ${action}${resultDetail}`,
        ),
      );
    });
    this.tooltip.replaceChildren(...contents);
    this.tooltip.hidden = false;
    const tooltipWidth = this.tooltip.offsetWidth || 150;
    const tooltipHeight = this.tooltip.offsetHeight || 94;
    const left = Math.max(6, Math.min(rectangle.width - tooltipWidth - 6, localX + 14));
    const top = Math.max(6, Math.min(rectangle.height - tooltipHeight - 6, event.clientY - rectangle.top - 42));
    this.tooltip.style.left = `${left}px`;
    this.tooltip.style.top = `${top}px`;
    this.notifyInspection();
    this.draw();
  }

  onPointerDown(event) {
    if (event.button !== 0 || !this.totalBars() || !this.bounds) return;
    if (event.pointerType === "touch") {
      this.touchPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (this.pinchGestureActive) {
        event.preventDefault();
        return;
      }
      if (this.touchPointers.size >= 2) {
        this.startPinch();
        this.suppressClick = true;
        event.preventDefault();
        return;
      }
    }
    if (this.viewLength() === this.totalBars()) {
      this.onPointer(event);
      return;
    }
    this.drag = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startView: this.viewStart,
      moved: false,
    };
    this.canvas.setPointerCapture?.(event.pointerId);
    this.canvas.classList.add("is-dragging");
    this.onPointer(event);
  }

  onPointerMove(event) {
    const tradeTarget = !this.drag && !this.pinchGestureActive
      ? this.tradeTargetAt(event)
      : null;
    this.setTradeHighlight(tradeTarget?.pairs || []);
    this.canvas.classList.toggle("is-trade-link", Boolean(tradeTarget));
    if (event.pointerType === "touch" && this.touchPointers.has(event.pointerId)) {
      this.touchPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    }
    if (this.pinch) {
      this.updatePinch();
      event.preventDefault();
      return;
    }
    if (this.pinchGestureActive) return;
    if (!this.drag || this.drag.pointerId !== event.pointerId) {
      this.onPointer(event);
      return;
    }
    const distance = event.clientX - this.drag.startX;
    if (Math.abs(distance) < 3 && !this.drag.moved) return;
    if (!this.drag.moved) this.focusedTimestamp = null;
    this.drag.moved = true;
    const barsMoved = Math.round((-distance / this.bounds.plotWidth) * this.viewLength());
    this.panTo(this.drag.startView + barsMoved);
    event.preventDefault();
  }

  onPointerUp(event) {
    if (event.pointerType === "touch") this.touchPointers.delete(event.pointerId);
    if (this.pinchGestureActive) {
      this.pinch = null;
      this.pinchGestureActive = this.touchPointers.size > 0;
      this.drag = null;
      this.canvas.classList.remove("is-dragging");
      if (this.canvas.hasPointerCapture?.(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId);
      clearTimeout(this.pinchClickTimer);
      this.pinchClickTimer = setTimeout(() => {
        this.suppressClick = false;
      }, 500);
      return;
    }
    if (!this.drag || this.drag.pointerId !== event.pointerId) return;
    const moved = this.drag.moved;
    this.drag = null;
    this.suppressClick = moved && event.type !== "pointercancel";
    this.canvas.classList.remove("is-dragging");
    if (this.canvas.hasPointerCapture?.(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId);
    if (!moved && event.type !== "pointercancel") this.onPointer(event);
  }

  onClick(event) {
    if (this.suppressClick) {
      this.suppressClick = false;
      return;
    }
    const tradeTarget = this.tradeTargetAt(event);
    if (tradeTarget) {
      window.location.assign(algorithmUrl(tradeTarget.algo));
      return;
    }
    const index = this.pointerIndex(event);
    const bar = index === null ? null : this.visibleBars()[index];
    if (!bar) return;
    this.canvas.focus({ preventScroll: true });
    this.hoverIndex = index;
    this.focusedTimestamp = Number(bar.ts);
    this.notifyInspection();
    this.draw();
  }

  onWheel(event) {
    if (!this.totalBars() || !this.bounds) return;
    event.preventDefault();
    const horizontalDelta = Math.abs(event.deltaX);
    const verticalDelta = Math.abs(event.deltaY);
    const horizontal = event.shiftKey || (horizontalDelta > 0 && horizontalDelta >= verticalDelta * 0.65);
    const deltaScale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? this.bounds.plotWidth : 1;
    if (horizontal) {
      if (this.viewLength() === this.totalBars()) return;
      const rawDelta = event.shiftKey && Math.abs(event.deltaY) > Math.abs(event.deltaX)
        ? event.deltaY
        : event.deltaX;
      if (this.wheelPanRemainder && Math.sign(rawDelta) !== Math.sign(this.wheelPanRemainder)) {
        this.wheelPanRemainder = 0;
      }
      const exactBars = (rawDelta * deltaScale / this.bounds.plotWidth) * this.viewLength()
        + this.wheelPanRemainder;
      const barsMoved = exactBars < 0 ? Math.ceil(exactBars) : Math.floor(exactBars);
      this.wheelPanRemainder = exactBars - barsMoved;
      if (barsMoved) {
        const previousStart = this.viewStart;
        this.panTo(this.viewStart + barsMoved);
        if (this.viewStart === previousStart) this.wheelPanRemainder = 0;
      }
      return;
    }
    this.wheelPanRemainder = 0;
    const rectangle = this.canvas.getBoundingClientRect();
    const localX = event.clientX - rectangle.left;
    const anchor = (localX - this.bounds.margin.left) / this.bounds.plotWidth;
    this.zoom(Math.exp(event.deltaY * deltaScale * 0.0025), anchor);
  }

  clearPointer() {
    this.hoverIndex = null;
    this.clearTradeHighlight();
    this.canvas.classList.remove("is-trade-link");
    this.tooltip.hidden = true;
    this.notifyInspection();
    this.draw();
  }

  clearFocus() {
    if (this.focusedTimestamp === null) return;
    this.focusedTimestamp = null;
    this.notifyInspection();
    this.draw();
  }
}

function renderViewport({ firstDate, lastDate }) {
  renderDateSelection(firstDate && firstDate === lastDate ? firstDate : null);
}

function renderShapeForest(shape) {
  const title = $("#shape-forest-title");
  const list = $("#shape-forest-list");
  title.textContent = shape
    ? `Shape forest · ${pacificClock.format(dateFromEpoch(shape.ts))}`
    : "Shape forest";
  if (!shape) {
    list.replaceChildren(createElement("span", "shape-unavailable", "No evaluation yet"));
    return;
  }
  const fragment = document.createDocumentFragment();
  shape.top_shapes.slice(0, 3).forEach((item, index) => {
    const row = createElement("div", `shape-row rank-${index + 1}`);
    row.append(
      createElement("span", "shape-rank", String(index + 1)),
      createElement("span", "shape-name", humanize(item.shape)),
      createElement("strong", "shape-probability", formatProbability(item.probability)),
    );
    fragment.append(row);
  });
  list.replaceChildren(fragment);
}

function pairTradeActions(trades) {
  const pairs = [];
  const openPairsByAlgo = new Map();
  const ordered = trades.slice().sort((left, right) => (
    Number(left.ts) - Number(right.ts) || String(left.algo).localeCompare(String(right.algo))
  ));

  ordered.forEach((trade) => {
    const openPairs = openPairsByAlgo.get(trade.algo) || [];
    openPairsByAlgo.set(trade.algo, openPairs);
    if (trade.action === "entry") {
      const pair = { entry: trade, exit: null };
      openPairs.push(pair);
      pairs.push(pair);
      return;
    }
    if (trade.action !== "exit_all") return;
    openPairs.splice(0).forEach((pair) => {
      pair.exit = trade;
    });
  });

  return pairs;
}

function tradeTime(trade, className, label) {
  if (!trade) {
    const pending = createElement("span", `${className} pending`, "—");
    pending.title = "No exit yet";
    return pending;
  }
  const date = dateFromEpoch(trade.ts);
  const time = createElement("time", className, pacificClock.format(date));
  time.dateTime = date.toISOString();
  time.title = `${label} ${pacificDateTime.format(date)}`;
  return time;
}

let highlightedDayTradeKeys = new Set();

function highlightDayTradeRows(keys) {
  highlightedDayTradeKeys = new Set(keys);
  document.querySelectorAll("#day-trades-list .day-trade-row[data-trade-key]").forEach((row) => {
    row.classList.toggle("is-highlighted", highlightedDayTradeKeys.has(row.dataset.tradeKey));
  });
}

function renderDayTrades(date) {
  const title = $("#day-trades-title");
  const list = $("#day-trades-list");
  const session = state.sessions.find((candidate) => candidate.date === date);
  title.textContent = session
    ? `Entry / exit · ${pacificDateButton.format(dateFromEpoch(session.ts))}`
    : "Entry / exit";

  if (!date) {
    list.replaceChildren(createElement("span", "day-trades-unavailable", "Select a trading date"));
    return;
  }

  const pairs = pairTradeActions(state.bars?.trades || [])
    .filter((pair) => pacificDateKey(pair.entry.ts) === date);
  if (!pairs.length) {
    list.replaceChildren(createElement("span", "day-trades-unavailable", "No entries or exits"));
    return;
  }

  const fragment = document.createDocumentFragment();
  pairs.forEach((pair) => {
    const algoName = algorithmDisplayName(pair.entry);
    const directionName = Number(pair.entry.direction) < 0 ? "Short" : "Long";
    const entryTime = pacificClock.format(dateFromEpoch(pair.entry.ts));
    const exitTime = pair.exit ? pacificClock.format(dateFromEpoch(pair.exit.ts)) : "open";
    const row = createElement("div", "day-trade-row");
    row.dataset.tradeKey = tradePairKey(pair);
    row.setAttribute(
      "aria-label",
      `${algoName}, ${directionName}, entry ${entryTime}, exit ${exitTime}`,
    );
    const algo = createElement("span", "day-trade-algo", algoName);
    algo.title = algoName;
    row.append(
      algo,
      createElement("span", "day-trade-direction", directionName),
      tradeTime(pair.entry, "day-trade-time day-trade-entry-time", "Entry"),
      tradeTime(pair.exit, "day-trade-time day-trade-exit-time", "Exit"),
    );
    row.addEventListener("pointerenter", () => chart.setTradeHighlight([pair]));
    row.addEventListener("pointerleave", () => chart.clearTradeHighlight());
    fragment.append(row);
  });
  list.replaceChildren(fragment);
  highlightDayTradeRows(highlightedDayTradeKeys);
}

function renderInspection(inspection) {
  if (!inspection?.bar) return;
  $("#quote-price").textContent = `$${formatPrice(inspection.bar.close)}`;
  const change = $("#quote-change");
  change.textContent = `${formatSigned(inspection.change)}  ${formatSigned(inspection.changePct, "%")}`;
  change.classList.toggle("down", Number(inspection.change) < 0);
  $("#quote-time").textContent = pacificInspectionTime.format(dateFromEpoch(inspection.bar.ts));
  $(".chart-header").dataset.mode = inspection.mode;
  renderShapeForest(inspection.shape);
}

const chart = new PriceChart(
  $("#price-chart"),
  $("#chart-tooltip"),
  renderViewport,
  renderInspection,
  highlightDayTradeRows,
);

function renderDateStrip(payload) {
  const strip = $("#date-strip");
  const hadSessions = state.sessions.length > 0;
  const previousScroll = strip.scrollLeft;
  const wasAtEnd = !hadSessions
    || strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 12;
  state.sessions = sessionDateRanges(payload?.bars || [])
    .sort((left, right) => right.ts - left.ts);

  if (!state.sessions.length) {
    strip.replaceChildren(createElement("span", "date-strip-loading", "No dates available"));
    return;
  }

  const fragment = document.createDocumentFragment();
  state.sessions.forEach((session) => {
    const date = dateFromEpoch(session.ts);
    const button = createElement("button", "date-button");
    button.type = "button";
    button.dataset.date = session.date;
    button.setAttribute("aria-pressed", "false");
    button.title = `Show ${pacificFullDate.format(date)}`;
    button.append(
      createElement("small", "", pacificWeekday.format(date)),
      createElement("span", "", pacificDateButton.format(date)),
    );
    fragment.append(button);
  });
  strip.replaceChildren(fragment);
  renderDateSelection(state.selectedDate);

  requestAnimationFrame(() => {
    if (state.selectedDate) {
      strip.querySelector(`[data-date="${state.selectedDate}"]`)?.scrollIntoView({
        block: "nearest",
        inline: "center",
      });
    } else if (wasAtEnd) {
      strip.scrollLeft = strip.scrollWidth;
    } else {
      strip.scrollLeft = previousScroll;
    }
  });
}

function renderDateSelection(date) {
  state.selectedDate = date;
  document.querySelectorAll("#date-strip button[data-date]").forEach((button) => {
    const active = button.dataset.date === date;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  renderDayTrades(date);
  rememberChartView();
}

function focusDate(date) {
  const session = state.sessions.find((candidate) => candidate.date === date);
  if (!session) return;
  chart.setView(session.startIndex, session.endIndex);
  $("#date-strip").querySelector(`[data-date="${date}"]`)?.scrollIntoView({
    block: "nearest",
    inline: "center",
  });
}

async function loadOverview({ quiet = false } = {}) {
  try {
    const overview = await api("/api/overview");
    state.overview = overview;
    if (!state.ticker || !overview.quotes.some((quote) => quote.ticker === state.ticker)) {
      const preferred = overview.quotes.find((quote) => quote.ticker === "SNDK" && quote.available);
      state.ticker = preferred?.ticker || overview.quotes.find((quote) => quote.available)?.ticker || overview.config.tickers[0];
    }
    renderOverview();
    const quote = overview.quotes.find((item) => item.ticker === state.ticker);
    const chartIsCurrent = quiet
      && state.bars?.ticker === state.ticker
      && state.chartRevision !== null
      && Number(state.chartRevision) === Number(quote?.chart_revision);
    if (!chartIsCurrent) {
      const hasHistory = state.bars?.ticker === state.ticker
        && state.bars?.range === "HISTORY";
      if (quiet && hasHistory) {
        await refreshLatestSession();
      } else {
        await loadChart({ quiet });
      }
    }
  } catch (error) {
    showToast(error.message);
  }
}

function renderOverview() {
  const overview = state.overview;
  $("#market-status").setAttribute("aria-label", overview.market.label);
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
  if (chart.payload?.ticker === state.ticker && chart.totalBars()) {
    chart.notifyInspection();
    return;
  }
  const quote = state.overview?.quotes.find((item) => item.ticker === state.ticker);
  $("#quote-price").textContent = quote?.available ? `$${formatPrice(quote.price)}` : "—";
  const change = $("#quote-change");
  change.textContent = quote?.available
    ? `${formatSigned(quote.change)}  ${formatSigned(quote.change_pct, "%")}`
    : "No bars";
  change.classList.toggle("down", Number(quote?.change) < 0);
  $("#quote-time").textContent = quote?.available
    ? pacificInspectionTime.format(dateFromEpoch(quote.ts))
    : "Waiting for bars";
  $(".chart-header").dataset.mode = "latest";
  renderShapeForest(null);
  renderDayTrades(null);
}

async function selectTicker(ticker) {
  if (ticker === state.ticker) return;
  state.ticker = ticker;
  renderTickers(state.overview.quotes);
  renderQuote();
  await loadChart();
}

function applyBarsPayload(payload, { focusLatest = false } = {}) {
  state.bars = payload;
  renderDateStrip(payload);
  chart.setData(payload);
  if (state.requestedDate && state.sessions.some((session) => session.date === state.requestedDate)) {
    focusDate(state.requestedDate);
    state.requestedDate = null;
  } else if (focusLatest && state.sessions.length) {
    focusDate(state.sessions[0].date);
  }
  $("#chart-empty").hidden = payload.bars.length > 0;
}

async function loadBars({ quiet = false, range = state.range, apply = true } = {}) {
  if (!state.ticker) return;
  const ticker = state.ticker;
  const request = ++state.chartRequest;
  if (!quiet || !state.bars) $("#chart-loading").hidden = false;
  $("#chart-empty").hidden = true;
  try {
    const payload = await api(`/api/bars?ticker=${encodeURIComponent(ticker)}&range=${range}`);
    if (request !== state.chartRequest) return;
    const shouldFocusLatest = state.bars?.ticker !== payload.ticker
      || state.bars?.range !== payload.range;
    state.chartRevision = state.overview?.quotes.find(
      (item) => item.ticker === payload.ticker,
    )?.chart_revision ?? null;
    if (apply) applyBarsPayload(payload, { focusLatest: shouldFocusLatest });
    return payload;
  } catch (error) {
    if (request === state.chartRequest) showToast(error.message);
    return null;
  } finally {
    if (request === state.chartRequest) $("#chart-loading").hidden = true;
  }
}

function historyChunks(sessions) {
  const chunks = [];
  for (let index = 0; index < sessions.length; index += HISTORY_SESSIONS_PER_CHUNK) {
    const group = sessions.slice(index, index + HISTORY_SESSIONS_PER_CHUNK);
    chunks.push({
      start: Math.min(...group.map((session) => Number(session.start))),
      end: Math.max(...group.map((session) => Number(session.end))),
    });
  }
  return chunks;
}

async function hydrateHistory(base, manifest, request) {
  const ticker = base.ticker;
  const chunks = historyChunks(manifest.sessions || []);
  const parts = new Map();
  const historyLoad = ++state.historyLoad;
  let nextChunk = 0;

  async function worker() {
    while (nextChunk < chunks.length) {
      const chunkIndex = nextChunk;
      nextChunk += 1;
      const chunk = chunks[chunkIndex];
      try {
        const payload = await api(
          `/api/bars?ticker=${encodeURIComponent(ticker)}&range=HISTORY&start=${chunk.start}&end=${chunk.end}`,
        );
        if (
          request !== state.chartRequest
          || historyLoad !== state.historyLoad
          || ticker !== state.ticker
        ) return;
        parts.set(chunkIndex, payload);
        const orderedParts = [...parts.entries()]
          .sort(([left], [right]) => left - right)
          .map(([, part]) => part);
        const merged = mergeHistoryPayload(base, orderedParts, {
          includeBaseInterpolation: !parts.has(0),
        });
        applyBarsPayload(merged, { focusLatest: state.bars?.range !== "HISTORY" });
      } catch (error) {
        if (request === state.chartRequest && historyLoad === state.historyLoad) {
          showToast(error.message);
        }
      }
    }
  }

  const workerCount = Math.min(HISTORY_PARALLEL_REQUESTS, chunks.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
}

async function loadChart({ quiet = false } = {}) {
  if (state.range !== "HISTORY") return loadBars({ quiet, range: state.range });
  const ticker = state.ticker;
  const manifestPromise = api(
    `/api/calendar?ticker=${encodeURIComponent(ticker)}&range=${state.range}`,
  ).then((manifest) => ({ manifest })).catch((error) => ({ error }));
  const base = await loadBars({ quiet, range: "1D" });
  if (!base || ticker !== state.ticker) return base;
  const request = state.chartRequest;
  const result = await manifestPromise;
  if (result.error) {
    if (request === state.chartRequest) showToast(result.error.message);
  } else if (request === state.chartRequest && ticker === state.ticker) {
    hydrateHistory(base, result.manifest, request);
  }
  return base;
}

async function refreshLatestSession() {
  const history = state.bars;
  const latest = await loadBars({ quiet: true, range: "1D", apply: false });
  if (!latest || history?.ticker !== latest.ticker) return;
  const merged = mergeHistoryPayload(history, [latest], {
    includeBaseInterpolation: false,
  });
  merged.interpolated_count = history.interpolated_count;
  applyBarsPayload(merged);
}

$("#date-strip").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-date]");
  if (!button) return;
  focusDate(button.dataset.date);
});

$("#date-strip").addEventListener("wheel", (event) => {
  const strip = event.currentTarget;
  if (strip.scrollWidth <= strip.clientWidth) return;
  const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
  strip.scrollLeft += delta;
  event.preventDefault();
}, { passive: false });

$("#date-strip").addEventListener("keydown", (event) => {
  if (!event.target.matches("button[data-date]") || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const offset = event.key === "ArrowLeft" ? -1 : 1;
  const buttons = [...event.currentTarget.querySelectorAll("button[data-date]")];
  const target = buttons[buttons.indexOf(event.target) + offset];
  if (!target) return;
  target.focus();
  focusDate(target.dataset.date);
  event.preventDefault();
});

function renderStyleSelection() {
  document.querySelectorAll("#trader-menu-dropdown button[data-style]").forEach((button) => {
    const active = button.dataset.style === state.style;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", String(active));
  });
}

function renderIndicatorSelection() {
  document.querySelectorAll("#trader-menu-dropdown button[data-indicator]").forEach((button) => {
    const active = button.dataset.indicator === state.indicator;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", String(active));
  });
}

$("#trader-menu-dropdown").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-style], button[data-indicator]");
  if (!button) return;
  if (button.dataset.style) {
    state.style = button.dataset.style;
    renderStyleSelection();
    chart.setStyle(state.style);
  } else {
    state.indicator = button.dataset.indicator;
    renderIndicatorSelection();
    chart.setIndicator(state.indicator);
  }
  window.traderHeader?.setOpen(false);
  $("#trader-menu-button").focus();
});

async function scheduleRefresh() {
  await loadOverview({ quiet: true });
  setTimeout(scheduleRefresh, 30_000);
}

loadOverview().finally(() => setTimeout(scheduleRefresh, 30_000));
