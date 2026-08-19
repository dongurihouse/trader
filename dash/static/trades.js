const wholeNumber = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const localTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});
const marketDay = new Intl.DateTimeFormat("en-US", {
  month: "long",
  day: "numeric",
  year: "numeric",
});
let localRequest = 0;
let robinhoodRequest = 0;

function numeric(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function currency(value) {
  const number = numeric(value);
  return number === null ? "—" : money.format(number);
}

function price(value) {
  return currency(value);
}

function signed(value, suffix = "%") {
  const number = numeric(value);
  if (number === null) return "—";
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}${suffix}`;
}

function rate(value) {
  const number = numeric(value);
  return number === null ? "—" : `${number.toFixed(1)}%`;
}

function valueClass(value) {
  const number = numeric(value);
  if (number === null || number === 0) return "";
  return number > 0 ? "positive" : "negative";
}

function duration(minutes) {
  const value = numeric(minutes);
  if (value === null) return "—";
  if (value < 60) return `${Math.round(value)}m`;
  const hours = Math.floor(value / 60);
  const remainder = Math.round(value % 60);
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

function titleCase(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function summaryCard(label, value, note, className = "") {
  const card = createElement("article", "trade-summary-card surface");
  card.append(
    createElement("small", "", label),
    createElement("strong", className, value),
    createElement("span", "", note),
  );
  return card;
}

function renderSummary(payload) {
  const stats = payload.summary || {};
  const summary = $("#trade-summary");
  summary.replaceChildren(
    summaryCard(
      "Gross realized",
      signed(stats.realized_return_pct, " pts"),
      `${wholeNumber.format(stats.closed_units || 0)} closed strategy units`,
      valueClass(stats.realized_return_pct),
    ),
    summaryCard(
      "Win rate",
      rate(stats.win_rate),
      `${wholeNumber.format(stats.wins || 0)} wins · ${wholeNumber.format(stats.losses || 0)} losses`,
    ),
    summaryCard(
      "Active",
      wholeNumber.format(stats.open_units || 0),
      `${wholeNumber.format(stats.real_open_units || 0)} broker-routed · ${wholeNumber.format(stats.paper_open_units || 0)} paper`,
    ),
    summaryCard(
      "Trading days",
      wholeNumber.format(stats.trading_days || 0),
      `${wholeNumber.format(stats.missing_real_closes || 0)} broker closes need attention`,
      Number(stats.missing_real_closes || 0) ? "negative" : "",
    ),
  );
}

function brokerBadge(trade) {
  const states = {
    paper: ["Paper", "paper"],
    real_open: [`Routed ${trade.execution_symbol || "entry"}`, "live"],
    real_closed: [`Closed ${trade.execution_symbol || "live"}`, "live"],
    close_missing: ["Close unrecorded", "danger"],
    closed_mismatch: ["Broker mismatch", "danger"],
  };
  const [label, className] = states[trade.broker_state] || [titleCase(trade.broker_state), "paper"];
  return createElement("span", `trade-broker-badge ${className}`, label);
}

function tradeChartUrl(trade) {
  const parameters = new URLSearchParams({
    ticker: trade.ticker,
    date: pacificDateKey(trade.entry_ts),
  });
  return `/?${parameters}`;
}

function tradeIdentity(trade) {
  const identity = createElement("div", "trade-record-identity");
  identity.append(
    createElement("strong", "", trade.ticker),
    createElement(
      "span",
      Number(trade.direction) < 0 ? "short" : "long",
      Number(trade.direction) < 0 ? "Short" : "Long",
    ),
  );
  return identity;
}

function renderActive(payload) {
  const active = payload.active || [];
  $("#active-count").textContent = `${wholeNumber.format(active.length)} open`;
  const container = $("#active-trades");
  if (!active.length) {
    const empty = createElement("div", "trade-empty");
    empty.append(
      createElement("strong", "", "No active strategy trades"),
      createElement("span", "", "The local ledger has no unmatched entry units."),
    );
    container.replaceChildren(empty);
    return;
  }

  const list = createElement("div", "trade-record-list active-records");
  active.forEach((trade) => {
    const row = createElement("a", "trade-record active-record");
    row.href = tradeChartUrl(trade);
    row.append(
      tradeIdentity(trade),
      createElement("div", "trade-record-algo", trade.display_name),
      createElement("time", "trade-record-time", localTime.format(dateFromEpoch(trade.entry_ts))),
      createElement(
        "span",
        "trade-record-prices",
        `${price(trade.entry_price)} → ${price(trade.mark_price)}`,
      ),
      brokerBadge(trade),
      createElement("strong", `trade-record-return ${valueClass(trade.return_pct)}`, signed(trade.return_pct)),
    );
    list.append(row);
  });
  container.replaceChildren(list);
}

function renderClosedTrade(trade) {
  const row = createElement("a", "trade-record closed-record");
  row.href = tradeChartUrl(trade);
  row.append(
    tradeIdentity(trade),
    createElement("div", "trade-record-algo", trade.display_name),
    createElement(
      "span",
      "trade-record-prices",
      `${price(trade.entry_price)} → ${price(trade.exit_price)}`,
    ),
    createElement("span", "trade-record-hold", duration(trade.hold_minutes)),
    brokerBadge(trade),
    createElement("strong", `trade-record-return ${valueClass(trade.return_pct)}`, signed(trade.return_pct)),
  );
  return row;
}

function shadowMetric(label, value, note, className = "") {
  const metric = createElement("article", "shadow-metric");
  metric.append(
    createElement("small", "", label),
    createElement("strong", className, value),
    createElement("span", "", note),
  );
  return metric;
}

function shadowStatus(shadow) {
  const labels = {
    open: ["Open", "open"],
    closed: ["Closed", "closed"],
    entry_error: ["Entry unavailable", "error"],
    exit_error: ["Exit unavailable", "error"],
  };
  const [label, className] = labels[shadow.status] || [titleCase(shadow.status), "error"];
  return createElement("span", `shadow-status ${className}`, label);
}

function optionContract(shadow) {
  if (!shadow.expiration_date || shadow.strike_price === null || !shadow.option_type) {
    return "Contract unavailable";
  }
  const expiration = new Date(`${shadow.expiration_date}T12:00:00`);
  return `${marketDay.format(expiration)} · $${Number(shadow.strike_price).toFixed(2)} ${titleCase(shadow.option_type)}`;
}

function renderOptionShadows(payload) {
  const container = $("#option-shadows");
  const shadows = payload.option_shadows || [];
  const stats = payload.option_summary || {};
  if (!shadows.length) {
    const empty = createElement("div", "trade-empty");
    empty.append(
      createElement("strong", "", "No live option shadows yet"),
      createElement("span", "", "The first fresh mapped entry will record the nearest-expiry near-OTM contract at its ask."),
    );
    container.replaceChildren(empty);
    return;
  }

  const summary = createElement("div", "shadow-summary-grid");
  summary.append(
    shadowMetric("Closed", wholeNumber.format(stats.closed || 0), `${wholeNumber.format(stats.open || 0)} open`),
    shadowMetric("Win rate", rate(stats.win_rate), `${wholeNumber.format(stats.wins || 0)} wins · ${wholeNumber.format(stats.losses || 0)} losses`),
    shadowMetric(
      "Return",
      signed(stats.realized_return_pct, " pts"),
      "Ask-to-bid percentage points",
      valueClass(stats.realized_return_pct),
    ),
    shadowMetric(
      "One-contract P&L",
      currency(stats.pnl_dollars),
      `${wholeNumber.format(stats.errors || 0)} quote errors`,
      valueClass(stats.pnl_dollars),
    ),
  );

  const list = createElement("div", "shadow-list");
  shadows.forEach((shadow) => {
    const row = createElement("article", "shadow-row");
    const identity = createElement("div", "shadow-identity");
    identity.append(
      createElement("strong", "", shadow.ticker),
      createElement("span", Number(shadow.direction) < 0 ? "short" : "long", Number(shadow.direction) < 0 ? "Put" : "Call"),
    );
    const prices = createElement("div", "shadow-prices");
    prices.append(
      createElement("span", "", shadow.entry_ask === null ? "Ask —" : `Ask ${currency(shadow.entry_ask)}`),
      createElement("span", "", shadow.exit_bid === null ? "Bid —" : `Bid ${currency(shadow.exit_bid)}`),
    );
    const outcome = createElement("div", "shadow-outcome");
    if (shadow.status === "closed") {
      outcome.append(
        createElement("strong", valueClass(shadow.return_pct), signed(shadow.return_pct)),
        createElement("span", valueClass(shadow.pnl_dollars), currency(shadow.pnl_dollars)),
      );
    } else {
      outcome.append(shadowStatus(shadow));
    }
    row.append(
      identity,
      createElement("div", "shadow-contract", optionContract(shadow)),
      createElement("div", "shadow-algo", shadow.display_name),
      createElement("time", "shadow-time", localTime.format(dateFromEpoch(shadow.entry_ts))),
      prices,
      outcome,
    );
    if (shadow.error) row.title = shadow.error;
    list.append(row);
  });
  container.replaceChildren(summary, list);
}

function renderDaily(payload) {
  const container = $("#trade-days");
  const days = payload.daily || [];
  $("#return-basis").textContent = payload.return_basis || "Gross returns use exact stored bar-close prices.";
  if (!days.length) {
    const empty = createElement("div", "trade-empty");
    empty.append(
      createElement("strong", "", "No closed trades"),
      createElement("span", "", "Closed strategy units will appear here by market day."),
    );
    container.replaceChildren(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  days.forEach((day, index) => {
    const details = createElement("details", "trade-day");
    details.open = index === 0;
    const summary = document.createElement("summary");
    const date = createElement("div", "trade-day-date");
    date.append(
      createElement("strong", "", marketDay.format(new Date(`${day.date}T12:00:00`))),
      createElement("span", "", `${wholeNumber.format(day.stats.closed_units || 0)} closed`),
    );
    const metrics = createElement("div", "trade-day-metrics");
    metrics.append(
      createElement("span", "", `${rate(day.stats.win_rate)} win`),
      createElement("strong", valueClass(day.stats.realized_return_pct), signed(day.stats.realized_return_pct, " pts")),
    );
    summary.append(date, metrics, createElement("span", "trade-day-chevron", "⌄"));
    const list = createElement("div", "trade-record-list");
    (day.trades || []).forEach((trade) => list.append(renderClosedTrade(trade)));
    details.append(summary, list);
    fragment.append(details);
  });
  container.replaceChildren(fragment);
}

function accountMetric(label, value, className = "") {
  const metric = createElement("span", "account-metric");
  metric.append(createElement("small", "", label), createElement("strong", className, value));
  return metric;
}

function renderPositions(positions) {
  const section = createElement("section", "account-subsection");
  section.append(createElement("h3", "", "Actual equity positions"));
  if (!positions.length) {
    section.append(createElement("p", "trade-inline-empty", "No open equity positions in Robinhood."));
    return section;
  }
  const list = createElement("div", "account-position-list");
  positions.forEach((position) => {
    const row = createElement("article", "account-position");
    const identity = createElement("div", "account-position-name");
    identity.append(
      createElement("strong", "", position.symbol),
      createElement("span", "", `${position.quantity || "—"} shares`),
    );
    row.append(
      identity,
      accountMetric("Average", currency(position.average_buy_price)),
      accountMetric("Last", currency(position.last_trade_price)),
      accountMetric("Value", currency(position.market_value)),
      accountMetric(
        "Unrealized",
        currency(position.unrealized_profit_loss),
        valueClass(position.unrealized_profit_loss),
      ),
    );
    list.append(row);
  });
  section.append(list);
  return section;
}

function orderSize(order) {
  if (numeric(order.dollar_based_amount) !== null) return currency(order.dollar_based_amount);
  if (numeric(order.quantity) !== null) return `${order.quantity} shares`;
  return "—";
}

function renderOrders(orders) {
  const section = createElement("section", "account-subsection");
  section.append(createElement("h3", "", "Recent agentic equity orders"));
  if (!orders.length) {
    section.append(createElement("p", "trade-inline-empty", "No agentic equity orders in the last 30 days."));
    return section;
  }
  const list = createElement("div", "account-order-list");
  orders.forEach((order) => {
    const row = createElement("article", "account-order");
    const state = String(order.state || "unknown").toLowerCase();
    const timestamp = order.last_transaction_at || order.created_at;
    row.append(
      createElement("strong", "", order.symbol || "—"),
      createElement("span", `order-side ${order.side === "sell" ? "sell" : "buy"}`, titleCase(order.side)),
      createElement("span", "", orderSize(order)),
      createElement("span", `order-state ${state}`, titleCase(state)),
      createElement("span", "", order.average_price ? `Avg ${currency(order.average_price)}` : titleCase(order.type)),
      createElement("time", "", timestamp ? localTime.format(new Date(timestamp)) : "—"),
    );
    list.append(row);
  });
  section.append(list);
  return section;
}

function renderRobinhood(payload) {
  const account = payload.account || {};
  const portfolio = payload.portfolio || {};
  const container = $("#robinhood-account");
  const identity = createElement("div", "account-identity");
  const accountName = account.nickname || titleCase(account.brokerage_account_type) || "Robinhood";
  const identityCopy = createElement("div", "account-identity-copy");
  identityCopy.append(
    createElement("strong", "", `${accountName} · ${account.masked_number || "masked"}`),
    createElement(
      "span",
      "",
      `${titleCase(account.trading_type)} account · ${account.agentic_allowed ? "Agent access enabled" : "Agent access unavailable"}`,
    ),
  );
  identity.append(
    createElement("span", `account-live-dot ${account.state === "active" ? "active" : ""}`),
    identityCopy,
    createElement("time", "", `Live ${localTime.format(dateFromEpoch(payload.generated_at))}`),
  );

  const metrics = createElement("div", "account-metrics");
  metrics.append(
    accountMetric("Account value", currency(portfolio.total_value)),
    accountMetric("Buying power", currency(portfolio.buying_power)),
    accountMetric("Cash", currency(portfolio.cash)),
    accountMetric("Equities", currency(portfolio.equity_value)),
  );
  const children = [identity, metrics, renderPositions(payload.positions || []), renderOrders(payload.orders || [])];
  (payload.warnings || []).forEach((warning) => {
    children.push(createElement("p", "account-warning", warning));
  });
  container.replaceChildren(...children);
}

function renderRobinhoodError(error) {
  const empty = createElement("div", "trade-empty account-error");
  empty.append(
    createElement("strong", "", "Live Robinhood data is unavailable"),
    createElement("span", "", error.message),
  );
  $("#robinhood-account").replaceChildren(empty);
}

async function loadTrades() {
  const request = ++localRequest;
  try {
    const payload = await api("/api/trades");
    if (request !== localRequest) return;
    renderSummary(payload);
    renderActive(payload);
    renderOptionShadows(payload);
    renderDaily(payload);
    $("#trade-updated").textContent = `Ledger updated ${localTime.format(dateFromEpoch(payload.generated_at))}`;
  } catch (error) {
    if (request !== localRequest) return;
    showToast(error.message);
    $("#trade-summary").replaceChildren(createElement("div", "trade-error surface", error.message));
    $("#active-trades").replaceChildren(createElement("div", "trade-error", error.message));
    $("#option-shadows").replaceChildren(createElement("div", "trade-error", error.message));
    $("#trade-days").replaceChildren(createElement("div", "trade-error", error.message));
  }
}

async function loadRobinhood() {
  const request = ++robinhoodRequest;
  const button = $("#refresh-robinhood");
  button.disabled = true;
  button.textContent = "Refreshing…";
  try {
    const payload = await api("/api/robinhood");
    if (request === robinhoodRequest) renderRobinhood(payload);
  } catch (error) {
    if (request === robinhoodRequest) renderRobinhoodError(error);
  } finally {
    if (request === robinhoodRequest) {
      button.disabled = false;
      button.textContent = "Refresh";
    }
  }
}

$("#refresh-robinhood").addEventListener("click", loadRobinhood);
window.traderHeader?.rememberView("trades");
loadTrades();
loadRobinhood();
setInterval(loadTrades, 30_000);
