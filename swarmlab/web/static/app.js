/* SwarmLab · panel web: tema, progreso en vivo del run, pestañas y cajón lateral. */
(function () {
  // --- tema: preferencia del sistema, con conmutador guardado en localStorage
  const root = document.documentElement;
  try {
    const saved = localStorage.getItem("swarmlab-theme");
    if (saved === "dark" || saved === "light") root.dataset.theme = saved;
  } catch (e) { /* almacenamiento no disponible */ }
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-toggle-theme]");
    if (!btn) return;
    const dark = root.dataset.theme === "dark" || (!root.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("swarmlab-theme", root.dataset.theme); } catch (e) { /* ignorar */ }
  });

  // --- cajón lateral (ficha de agente): cerrar con el botón o con Escape
  document.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-close-drawer]")) { const d = document.getElementById("drawer"); if (d) d.innerHTML = ""; }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { const d = document.getElementById("drawer"); if (d) d.innerHTML = ""; }
  });

  // --- página de run: pestañas sin recargar (la URL se actualiza para poder compartirla)
  const page = document.getElementById("run-page");
  if (!page) return;
  document.addEventListener("click", (ev) => {
    const link = ev.target.closest(".tabs a[data-tab]");
    const tab = document.getElementById("tab-content");
    if (!link || !tab || !window.htmx) return;
    ev.preventDefault();
    document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a === link));
    tab.setAttribute("hx-get", link.dataset.url);
    htmx.process(tab);
    htmx.ajax("GET", link.dataset.url, { target: "#tab-content", swap: "innerHTML" });
    history.replaceState(null, "", link.getAttribute("href"));
  });

  // --- SSE con estado, log y recarga de la pestaña activa al cambiar de ronda
  const runUrl = "/runs/" + encodeURIComponent(page.dataset.runId);
  const seen = { status: page.dataset.status, round: Number(page.dataset.round || 0) };
  const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = (value === null || value === undefined || value === "") ? "—" : value; };
  const refreshTab = () => {
    const tab = document.getElementById("tab-content");
    if (tab && window.htmx) htmx.trigger(tab, "refresh");
    const controls = document.getElementById("controls");
    if (controls && window.htmx) htmx.ajax("GET", runUrl + "/controls", { target: "#controls", swap: "outerHTML" });
  };
  const es = new EventSource(runUrl + "/events");
  es.onmessage = (e) => {
    const d = JSON.parse(e.data);
    const badge = document.getElementById("st-status");
    if (badge) { badge.textContent = d.status_label; badge.className = "badge st-" + d.status; }
    set("st-stage", d.stage_label);
    set("st-round", d.current_round + "/" + d.rounds);
    set("st-calls", d.calls_used);
    set("st-pause", d.pause_reason);
    const bar = document.getElementById("st-bar");
    if (bar && d.rounds) bar.style.width = Math.min(100, Math.round(100 * d.current_round / d.rounds)) + "%";
    const log = document.getElementById("log");
    if (log) {
      for (const line of d.logs) { const div = document.createElement("div"); div.textContent = line; log.appendChild(div); }
      if (d.logs.length) log.scrollTop = log.scrollHeight;
    }
    const changed = d.status !== seen.status || d.current_round !== seen.round;
    seen.status = d.status; seen.round = d.current_round;
    if (d.final) es.close();
    if (changed) refreshTab();
  };
})();
