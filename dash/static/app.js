const $ = (selector) => document.querySelector(selector);

const state = {
  overview: null,
  ticker: null,
  range: "HISTORY",
  style: "line",
  algo: null,
  bars: null,
  sessions: [],
  selectedDate: null,
  chartRequest: 0,
};

const easternDateTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

const easternInspectionTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});

const easternClock = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "numeric",
  minute: "2-digit",
});

const easternDateKeyFormat = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

const easternDateButton = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
});

const easternWeekday = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  weekday: "short",
});

const easternFullDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  year: "numeric",
  month: "long",
  day: "numeric",
});

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

function formatShapeName(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/^./, (letter) => letter.toUpperCase());
}

function formatProbability(value) {
  const probability = Number(value);
  if (!Number.isFinite(probability)) return "—";
  return `${(probability * 100).toFixed(1)}%`;
}

function dateFromEpoch(epoch) {
  return new Date(Number(epoch) * 1000);
}

function easternDateKey(epoch) {
  const parts = easternDateKeyFormat.formatToParts(dateFromEpoch(epoch));
  const value = (type) => parts.find((part) => part.type === type)?.value || "";
  return `${value("year")}-${value("month")}-${value("day")}`;
}

function sessionDateRanges(bars) {
  const sessions = [];
  bars.forEach((bar, index) => {
    const date = easternDateKey(bar.ts);
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
  constructor(canvas, tooltip, onViewChange, onInspect) {
    this.canvas = canvas;
    this.tooltip = tooltip;
    this.onViewChange = onViewChange;
    this.onInspect = onInspect;
    this.context = canvas.getContext("2d");
    this.style = "line";
    this.algo = null;
    this.payload = null;
    this.momentumByTimestamp = new Map();
    this.hoverIndex = null;
    this.focusedTimestamp = null;
    this.bounds = null;
    this.viewStart = 0;
    this.viewEnd = -1;
    this.drag = null;
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

  setAlgo(algo) {
    const next = algo || null;
    if (next === this.algo) return;
    this.algo = next;
    this.updateLabel();
    this.draw();
  }

  visibleTrades(bars = this.visibleBars()) {
    if (!this.algo || !bars.length) return [];
    const timestamps = new Set(bars.map((bar) => Number(bar.ts)));
    return (this.payload?.trades || []).filter(
      (trade) => trade.algo === this.algo && timestamps.has(Number(trade.ts)),
    );
  }

  tradesAt(timestamp) {
    return (this.payload?.trades || []).filter(
      (trade) => trade.algo === this.algo && Number(trade.ts) === Number(timestamp),
    );
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
    return easternDateKey(snapshot.ts) === easternDateKey(timestamp) ? snapshot : null;
  }

  momentumAt(timestamp) {
    return this.momentumByTimestamp.get(Number(timestamp)) || null;
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
    const session = easternDateKey(bar.ts);
    let sessionStart = index;
    while (sessionStart > 0 && easternDateKey(bars[sessionStart - 1].ts) === session) {
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
      mode: this.hoverIndex !== null ? "hover" : focused !== null ? "focused" : "latest",
    });
  }

  updateLabel() {
    const label = this.style === "candles" ? "candlestick" : "line";
    const visible = this.visibleBars();
    const first = visible[0];
    const last = visible.at(-1);
    const actionCount = this.visibleTrades(visible).length;
    const overlayLabel = this.algo
      ? `, ${this.algo} overlay with ${actionCount.toLocaleString()} ${actionCount === 1 ? "action" : "actions"}`
      : "";
    const momentumLabel = this.payload?.relative_momentum?.snapshots?.length
      ? ", with relative momentum below volume"
      : "";
    this.canvas.setAttribute(
      "aria-label",
      visible.length
        ? `${this.payload.ticker} ${label} chart, ${visible.length.toLocaleString()} visible points from ${easternDateTime.format(dateFromEpoch(first.ts))} to ${easternDateTime.format(dateFromEpoch(last.ts))}${overlayLabel}${momentumLabel}`
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
      firstDate: visibleBars.length ? easternDateKey(visibleBars[0].ts) : null,
      lastDate: visibleBars.length ? easternDateKey(visibleBars.at(-1).ts) : null,
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
    const markerSize = 5.5;
    ctx.save();
    ctx.lineJoin = "round";
    trades.forEach((trade) => {
      const index = indexByTimestamp.get(Number(trade.ts));
      if (index === undefined) return;
      const bar = bars[index];
      const isEntry = trade.action === "entry";
      const isLong = Number(trade.direction) >= 0;
      const pointsUp = isEntry ? isLong : !isLong;
      const pointX = x(index);
      const priceY = y(pointsUp ? bar.low : bar.high);
      const markerY = pointsUp
        ? Math.min(priceBottom - markerSize, priceY + 13)
        : Math.max(margin.top + markerSize, priceY - 13);
      const tipY = markerY + (pointsUp ? -markerSize : markerSize);
      const color = isEntry ? "#70d8dc" : "#ff7b73";

      ctx.beginPath();
      ctx.moveTo(pointX, priceY + (pointsUp ? 2 : -2));
      ctx.lineTo(pointX, tipY);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.stroke();

      ctx.beginPath();
      ctx.moveTo(pointX, tipY);
      ctx.lineTo(pointX + markerSize, markerY + (pointsUp ? markerSize * 0.7 : -markerSize * 0.7));
      ctx.lineTo(pointX - markerSize, markerY + (pointsUp ? markerSize * 0.7 : -markerSize * 0.7));
      ctx.closePath();
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = "#101721";
      ctx.lineWidth = 1.4;
      ctx.stroke();
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

  draw() {
    const { width, height } = this.size();
    const ctx = this.context;
    ctx.clearRect(0, 0, width, height);
    const bars = this.visibleBars();
    if (!bars.length) return;

    const margin = { top: 14, right: width < 500 ? 49 : 62, bottom: 32, left: 4 };
    const volumeHeight = 42;
    const momentumHeight = width < 500 ? 54 : 64;
    const panelGap = 12;
    const momentumBottom = height - margin.bottom;
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
      const parts = easternDateTime.formatToParts(dateFromEpoch(bar.ts));
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
        ctx.fillText(easternClock.format(dateFromEpoch(bars[index].ts)), x(index), height - 10);
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

  onPointer(event) {
    const bars = this.visibleBars();
    const index = this.pointerIndex(event);
    if (!bars.length || index === null) return;
    const rectangle = this.canvas.getBoundingClientRect();
    const localX = event.clientX - rectangle.left;
    this.hoverIndex = index;
    const bar = bars[this.hoverIndex];
    const contents = [
      createElement("strong", "", easternDateTime.format(dateFromEpoch(bar.ts))),
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
    this.tradesAt(bar.ts).forEach((trade) => {
      const action = trade.action === "exit_all" ? "exit" : "entry";
      const direction = Number(trade.direction) < 0 ? "Short" : "Long";
      contents.push(
        createElement(
          "span",
          `algo-action ${action}`,
          `${trade.algo.toUpperCase()} · ${direction} ${action}`,
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
    ? `Shape forest · ${easternClock.format(dateFromEpoch(shape.ts))}`
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
      createElement("span", "shape-name", formatShapeName(item.shape)),
      createElement("strong", "shape-probability", formatProbability(item.probability)),
    );
    fragment.append(row);
  });
  list.replaceChildren(fragment);
}

function renderInspection(inspection) {
  if (!inspection?.bar) return;
  $("#quote-price").textContent = `$${formatPrice(inspection.bar.close)}`;
  const change = $("#quote-change");
  change.textContent = `${formatSigned(inspection.change)}  ${formatSigned(inspection.changePct, "%")}`;
  change.classList.toggle("down", Number(inspection.change) < 0);
  $("#quote-time").textContent = easternInspectionTime.format(dateFromEpoch(inspection.bar.ts));
  $(".chart-header").dataset.mode = inspection.mode;
  renderShapeForest(inspection.shape);
}

const chart = new PriceChart(
  $("#price-chart"),
  $("#chart-tooltip"),
  renderViewport,
  renderInspection,
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
    button.title = `Show ${easternFullDate.format(date)}`;
    button.append(
      createElement("small", "", easternWeekday.format(date)),
      createElement("span", "", easternDateButton.format(date)),
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

function renderAlgoOverlay() {
  const configured = Object.entries(state.overview?.config?.algos || {})
    .filter(([, definition]) => definition?.trades === true)
    .map(([name]) => name);
  if (!configured.includes(state.algo)) state.algo = configured[0] || null;

  chart.setAlgo(state.algo);
}

async function loadOverview({ quiet = false } = {}) {
  try {
    const overview = await api("/api/overview?compact=1");
    state.overview = overview;
    if (!state.ticker || !overview.quotes.some((quote) => quote.ticker === state.ticker)) {
      const preferred = overview.quotes.find((quote) => quote.ticker === "SNDK" && quote.available);
      state.ticker = preferred?.ticker || overview.quotes.find((quote) => quote.available)?.ticker || overview.config.tickers[0];
    }
    renderOverview();
    await loadBars({ quiet });
  } catch (error) {
    showToast(error.message);
  }
}

function renderOverview() {
  const overview = state.overview;
  $("#market-status").setAttribute("aria-label", overview.market.label);
  $("#market-status").title = overview.market.label;
  $("#market-dot").classList.toggle("live", overview.market.state === "live");
  renderAlgoOverlay();
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
    ? easternInspectionTime.format(dateFromEpoch(quote.ts))
    : "Waiting for bars";
  $(".chart-header").dataset.mode = "latest";
  renderShapeForest(null);
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
    const shouldFocusLatest = state.bars?.ticker !== payload.ticker;
    state.bars = payload;
    renderAlgoOverlay();
    renderDateStrip(payload);
    chart.setData(payload);
    if (shouldFocusLatest && state.sessions.length) {
      focusDate(state.sessions[0].date);
    }
    $("#chart-empty").hidden = payload.bars.length > 0;
  } catch (error) {
    if (request === state.chartRequest) showToast(error.message);
  } finally {
    if (request === state.chartRequest) $("#chart-loading").hidden = true;
  }
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

function setTraderMenu(open) {
  $("#trader-menu-button").setAttribute("aria-expanded", String(open));
  $("#trader-menu-dropdown").hidden = !open;
}

function renderStyleSelection() {
  document.querySelectorAll("#trader-menu-dropdown button[data-style]").forEach((button) => {
    const active = button.dataset.style === state.style;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", String(active));
  });
}

$("#trader-menu-button").addEventListener("click", () => {
  const open = $("#trader-menu-button").getAttribute("aria-expanded") !== "true";
  setTraderMenu(open);
});

$("#trader-menu-button").addEventListener("keydown", (event) => {
  if (event.key !== "ArrowDown") return;
  setTraderMenu(true);
  $("#trader-menu-dropdown button.active")?.focus();
  event.preventDefault();
});

$("#trader-menu-dropdown").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-style]");
  if (!button) return;
  state.style = button.dataset.style;
  renderStyleSelection();
  chart.setStyle(state.style);
  setTraderMenu(false);
  $("#trader-menu-button").focus();
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".trader-menu")) setTraderMenu(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || $("#trader-menu-dropdown").hidden) return;
  setTraderMenu(false);
  $("#trader-menu-button").focus();
});

loadOverview();
setInterval(() => loadOverview({ quiet: true }), 30_000);
