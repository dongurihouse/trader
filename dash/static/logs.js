const $ = (selector) => document.querySelector(selector);

const state = {
  service: "",
  level: "",
  limit: "250",
  request: 0,
};

const pacificDateTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
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

async function loadLogs() {
  const request = ++state.request;
  const parameters = new URLSearchParams({ limit: state.limit });
  if (state.service) parameters.set("service", state.service);
  if (state.level) parameters.set("level", state.level);
  try {
    const logsResult = await api(`/api/logs?${parameters}`);
    if (request !== state.request) return;
    renderLogs(logsResult);
  } catch (error) {
    if (request === state.request) showToast(error.message);
  }
}

function renderLogs(payload) {
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
    const timestamp = createElement("time", "log-time", pacificDateTime.format(new Date(row.ts * 1000)));
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

loadLogs();
setInterval(loadLogs, 5000);
