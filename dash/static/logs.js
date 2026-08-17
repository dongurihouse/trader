const $ = (selector) => document.querySelector(selector);

const state = {
  service: "",
  level: "",
  limit: "250",
  request: 0,
};

const easternDateTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
});

const localUpdated = new Intl.DateTimeFormat("en-US", {
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
});

const numberFormat = new Intl.NumberFormat("en-US");

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
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

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function loadLogs({ quiet = false } = {}) {
  const request = ++state.request;
  const refresh = $("#logs-refresh");
  if (!quiet) refresh.classList.add("is-spinning");
  const parameters = new URLSearchParams({ limit: state.limit });
  if (state.service) parameters.set("service", state.service);
  if (state.level) parameters.set("level", state.level);
  try {
    const [logsResult, healthResult] = await Promise.allSettled([
      api(`/api/logs?${parameters}`),
      api("/api/health"),
    ]);
    if (request !== state.request) return;
    if (healthResult.status === "fulfilled") renderHealth(healthResult.value);
    else renderHealth(null);
    if (logsResult.status === "rejected") throw logsResult.reason;
    renderLogs(logsResult.value);
  } catch (error) {
    if (request === state.request) showToast(error.message);
  } finally {
    if (request === state.request) refresh.classList.remove("is-spinning");
  }
}

function formatUptime(startedAt) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(startedAt)));
  if (seconds < 60) return `${seconds}s uptime`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m uptime`;
  if (seconds < 86_400) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${hours}h ${minutes}m uptime`;
  }
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3600);
  return `${days}d ${hours}h uptime`;
}

function renderHealth(payload) {
  for (const service of ["bars", "algo"]) {
    renderHealthIndicator(service, payload?.services?.[service] || null, Boolean(payload));
  }
}

function renderHealthIndicator(service, health, responseAvailable) {
  const indicator = $(`#health-${service}`);
  const status = $(`#health-${service}-status`);
  const detail = $(`#health-${service}-detail`);
  let stateName = "offline";
  let statusText = "Unavailable";
  let detailText = responseAvailable ? "Health endpoint unavailable" : "Health check failed";

  if (health?.state === "active" && health.level !== "error") {
    stateName = "healthy";
    statusText = "Healthy";
    const details = [];
    if (health.pid) details.push(`PID ${health.pid}`);
    if (health.started_at) details.push(formatUptime(health.started_at));
    detailText = details.join(" · ") || "Endpoint responding";
  } else if (health && health.state !== "stale" && health.level !== "error") {
    stateName = "degraded";
    statusText = "Degraded";
    detailText = health.message || "Endpoint response is stale";
  }

  indicator.className = `health-indicator is-${stateName}`;
  indicator.setAttribute("aria-label", `${service} service: ${statusText}. ${detailText}`);
  status.textContent = statusText;
  detail.textContent = detailText;
}

function renderLogs(payload) {
  $("#logs-updated").textContent = `Updated ${localUpdated.format(new Date(payload.generated_at * 1000))}`;
  for (const level of ["total", "info", "warn", "error"]) {
    $(`#count-${level}`).textContent = numberFormat.format(payload.counts[level] || 0);
  }
  renderServices(payload.services);
  renderRows(payload.rows);
  const suffix = payload.has_more ? `latest ${numberFormat.format(payload.rows.length)}` : numberFormat.format(payload.rows.length);
  $("#shown-count").textContent = `Showing ${suffix} · newest first`;
}

function renderServices(services) {
  const select = $("#service-filter");
  const current = state.service;
  const fragment = document.createDocumentFragment();
  const all = createElement("option", "", "All services");
  all.value = "";
  fragment.append(all);
  services.forEach((service) => {
    const option = createElement("option", "", service);
    option.value = service;
    fragment.append(option);
  });
  select.replaceChildren(fragment);
  if (current && services.includes(current)) select.value = current;
  else if (current) state.service = "";
}

function renderRows(rows) {
  const list = $("#log-list");
  if (!rows.length) {
    const empty = createElement("div", "logs-empty");
    empty.append(
      createElement("strong", "", "No matching logs"),
      createElement("span", "", "Change the service or level filter to broaden this view."),
    );
    list.replaceChildren(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  rows.forEach((row) => {
    const item = createElement("article", `log-row ${row.level}`);
    const timestamp = createElement("time", "log-time", easternDateTime.format(new Date(row.ts * 1000)));
    timestamp.dateTime = new Date(row.ts * 1000).toISOString();
    item.append(timestamp);
    item.append(createElement("span", `level-badge ${row.level}`, row.level));
    item.append(createElement("strong", "log-service", row.service));
    item.append(createElement("p", "log-message", row.message));
    fragment.append(item);
  });
  list.replaceChildren(fragment);
}

$("#service-filter").addEventListener("change", (event) => {
  state.service = event.target.value;
  loadLogs();
});

$("#level-filter").addEventListener("change", (event) => {
  state.level = event.target.value;
  loadLogs();
});

$("#limit-filter").addEventListener("change", (event) => {
  state.limit = event.target.value;
  loadLogs();
});

$("#logs-refresh").addEventListener("click", () => loadLogs());

loadLogs();
setInterval(() => loadLogs({ quiet: true }), 5000);
