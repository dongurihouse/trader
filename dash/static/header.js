(() => {
  const viewPaths = Object.freeze({ chart: "/", algos: "/algos", trades: "/trades" });
  const viewStoragePrefix = "trader.last-view.";
  const button = document.querySelector("#trader-menu-button");
  const dropdown = document.querySelector("#trader-menu-dropdown");
  if (!button || !dropdown) return;

  let healthRequest = 0;

  function storedViewUrl(view) {
    const expectedPath = viewPaths[view];
    if (!expectedPath) return null;
    try {
      const stored = sessionStorage.getItem(`${viewStoragePrefix}${view}`);
      if (!stored) return null;
      const url = new URL(stored, window.location.origin);
      if (url.origin !== window.location.origin || url.pathname !== expectedPath) return null;
      return `${url.pathname}${url.search}`;
    } catch {
      return null;
    }
  }

  function updateViewLink(view) {
    const link = document.querySelector(`[data-dashboard-view="${view}"]`);
    if (!link || link.getAttribute("aria-current") === "page") return;
    link.href = storedViewUrl(view) || viewPaths[view];
  }

  function rememberView(view, parameters = {}) {
    const path = viewPaths[view];
    if (!path) return;
    const url = new URL(path, window.location.origin);
    Object.entries(parameters).forEach(([name, value]) => {
      if (value !== null && value !== undefined && value !== "") {
        url.searchParams.set(name, String(value));
      }
    });
    try {
      sessionStorage.setItem(`${viewStoragePrefix}${view}`, `${url.pathname}${url.search}`);
    } catch {
      return;
    }
    updateViewLink(view);
  }

  function setOpen(open) {
    button.setAttribute("aria-expanded", String(open));
    dropdown.hidden = !open;
    if (open) loadHealth();
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

  function renderMarket(market) {
    const marker = document.querySelector("#market-dot");
    const status = document.querySelector("#market-status");
    if (!marker || !status) return;
    marker.classList.toggle("live", market?.state === "live");
    status.setAttribute("aria-label", market?.label || "Market status unavailable");
  }

  function renderHealthIndicator(service, health, responseAvailable) {
    const indicator = document.querySelector(`#health-${service}`);
    const status = document.querySelector(`#health-${service}-status`);
    const detail = document.querySelector(`#health-${service}-detail`);
    if (!indicator || !status || !detail) return;

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
    indicator.title = detailText;
    status.textContent = statusText;
    detail.textContent = detailText;
  }

  function renderHealth(payload) {
    renderMarket(payload?.market || null);
    for (const service of ["bars", "algo"]) {
      renderHealthIndicator(service, payload?.services?.[service] || null, Boolean(payload));
    }
  }

  async function loadHealth() {
    const request = ++healthRequest;
    try {
      const response = await fetch("/api/health", { headers: { Accept: "application/json" } });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
      if (request === healthRequest) renderHealth(payload);
    } catch {
      if (request === healthRequest) renderHealth(null);
    }
  }

  button.addEventListener("click", () => {
    setOpen(button.getAttribute("aria-expanded") !== "true");
  });

  button.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowDown") return;
    setOpen(true);
    dropdown.querySelector("button.active, button")?.focus();
    event.preventDefault();
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".trader-menu")) setOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || dropdown.hidden) return;
    setOpen(false);
    button.focus();
  });

  Object.keys(viewPaths).forEach(updateViewLink);
  window.traderHeader = Object.freeze({ rememberView, setOpen });
  loadHealth();
  setInterval(loadHealth, 5000);
})();
