// The Devices sidebar: every machine at a glance, with its temperature and load, always beside whatever page you are on.
// Click a device to expand its sensors, or open the full details. Loaded after app.js.
(function () {
  "use strict";
  const { html, setHTML, fmt } = NY;
  const U = NY.ui;
  const { S, $, chip, bar, tempClass, nodeState } = U;

  const shell = document.querySelector(".shell"), side = $("#devs"), btn = $("#devsbtn");
  if (!shell || !side || !btn) return;
  let open = U.store.get("devices", "1") === "1";
  const TEMP = 4, LOAD = 6;      // positions in a history sample's per-node list
  const expanded = new Set();

  function apply() {
    shell.classList.toggle("devs-on", open);
    btn.classList.toggle("on", open);
    btn.setAttribute("aria-pressed", open ? "true" : "false");
    side.setAttribute("aria-hidden", open ? "false" : "true");
    if (open) paint();
  }
  function toggle(on) {
    open = on == null ? !open : on;
    U.store.set("devices", open ? "1" : "0");
    apply();
  }

  // The last stretch of a node's history for one value (null where the agent had nothing).
  function series(name, i, n) {
    const out = [];
    for (let k = Math.max(0, S.hist.length - n); k < S.hist.length; k++) {
      const row = S.hist[k].nodes && S.hist[k].nodes[name];
      out.push(row && row[i] != null ? row[i] : null);
    }
    return out;
  }
  const real = (a) => a.filter((x) => x != null);
  function tempSpark(vals) {
    const v = real(vals);
    if (v.length < 3) return "";
    const lo = Math.min(...v) - 4, hi = Math.max(...v);
    return html`<div class="dev-spark" title="Temperature, last few minutes">${NY.spark(vals.map((x) => (x == null ? lo : x - lo)), { max: hi - lo || 1, color: "var(--warn)" })}</div>`;
  }
  function loadSpark(vals, cores) {
    if (real(vals).length < 3) return "";
    return html`<div class="dev-spark" title="Load, last few minutes">${NY.spark(vals, { max: Math.max(cores || 1, ...real(vals)) * 1.05, color: "var(--accent)" })}</div>`;
  }

  function device(n) {
    const h = n.hw || {}, cores = n.cpu_cores || h.cores || 1;
    const health = nodeState(n);
    const load = h.load && h.load.length ? h.load : null;
    const cpu = n.cpu_used != null && n.cpu_cores ? n.cpu_used / n.cpu_cores : null;
    const mem = n.mem_used != null && n.mem_total ? n.mem_used / n.mem_total : null;
    const l1 = load ? load[0] : null;
    const temp = h.temp_c;
    const temps = h.temps || [];
    const gpus = h.gpu_live || [];
    const tseries = series(n.name, TEMP, 60), lseries = series(n.name, LOAD, 60);
    return html`<details class="dev ${!n.ready && !health.checking ? "down" : ""}" data-device="${n.name}" ${expanded.has(n.name) ? "open" : ""}>
      <summary>
        <i class="dot ${health.cls}"></i><b class="dev-name">${n.name}</b>${health.recent || !n.ready ? chip(health.label, health.cls) : ""}
        <span class="grow"></span>
        ${temp != null ? chip(fmt.temp(temp), tempClass(temp)) : html`<span class="faint small">no temp</span>`}
      </summary>
      <div class="dev-row"><span class="k">Load</span>${bar(l1 == null ? null : Math.min(1, l1 / cores), l1 != null && l1 / cores > 1 ? "bad" : "")}<span class="v">${l1 == null ? "–" : l1.toFixed(2)}</span></div>
      <div class="dev-row"><span class="k">CPU</span>${bar(cpu)}<span class="v">${cpu == null ? "–" : fmt.pct(cpu)}</span></div>
      <div class="dev-row"><span class="k">Memory</span>${bar(mem)}<span class="v">${mem == null ? "–" : fmt.pct(mem)}</span></div>
      <div class="dev-sparks">${tempSpark(tseries)}${loadSpark(lseries, cores)}</div>
      <div class="dev-more">
        ${load ? html`<div class="kvl"><span class="muted">Load 1 / 5 / 15 min</span><span>${load.map((x) => x.toFixed(2)).join(" · ")}</span></div>` : ""}
        <div class="kvl"><span class="muted">Cores</span><span>${cores}${h.freq_mhz ? " · " + fmt.mhz(h.freq_mhz) + (h.freq_max ? " of " + fmt.mhz(h.freq_max) : "") : ""}</span></div>
        ${temps.map((t) => html`<div class="kvl"><span class="muted">${t.name || "sensor"}</span><span>${chip(fmt.temp(t.c), tempClass(t.c))}</span></div>`)}
        ${gpus.map((g) => html`<div class="kvl"><span class="muted">GPU ${g.name || ""}</span><span>${g.use != null ? Math.round(g.use) + "%" : "–"}${g.temp_c != null ? " · " + fmt.temp(g.temp_c) : ""}</span></div>`)}
        ${h.undervoltage ? html`<div class="kvl"><span class="muted">Power</span><span>${chip("under-voltage", "bad")}</span></div>` : ""}
        ${h.uptime ? html`<div class="kvl"><span class="muted">Up for</span><span>${fmt.dur ? fmt.dur(h.uptime) : Math.round(h.uptime / 3600) + " h"}</span></div>` : ""}
        <div class="kvl"><span class="muted">Pods</span><span>${n.pods_running} / ${n.pods_capacity}</span></div>
        <button class="btn small" data-dev-open="${n.name}">Open details</button>
      </div>
    </details>`;
  }

  function paint() {
    if (!open) return;
    const d = S.d;
    if (!d) { setHTML(side, html`<div class="dev-head"><b>Devices</b></div><div class="faint small" style="padding:14px">Reading your cluster…</div>`); return; }
    const withTemp = d.nodes.filter((n) => n.hw && n.hw.temp_c != null);
    const hottest = withTemp.sort((a, b) => b.hw.temp_c - a.hw.temp_c)[0];
    const loads = d.nodes.filter((n) => n.hw && n.hw.load && n.hw.load.length);
    const busiest = loads.slice().sort((a, b) => b.hw.load[0] / (b.cpu_cores || 1) - a.hw.load[0] / (a.cpu_cores || 1))[0];
    const anyAgent = d.nodes.some((n) => n.hw);
    const checking = d.nodes.filter((n) => nodeState(n).checking).length;
    const recovering = d.nodes.filter((n) => nodeState(n).recovering).length;
    setHTML(side, html`
      <div class="dev-head"><b>Devices</b><span class="faint small">${d.nodes.filter((n) => n.ready).length} of ${d.nodes.length} ready${checking ? " · " + checking + " checking" : ""}${recovering ? " · " + recovering + " recovering" : ""}</span><span class="grow"></span>
        <button class="btn icon-only small" data-dev-close aria-label="Close the devices panel"><svg class="icon"><use href="#i-x"/></svg></button></div>
      ${hottest || busiest ? html`<div class="dev-sum">
        ${hottest ? html`<span>Hottest <b>${hottest.name}</b> ${chip(fmt.temp(hottest.hw.temp_c), tempClass(hottest.hw.temp_c))}</span>` : ""}
        ${busiest ? html`<span>Busiest <b>${busiest.name}</b> <span class="faint">load ${busiest.hw.load[0].toFixed(2)}</span></span>` : ""}</div>` : ""}
      ${anyAgent ? "" : html`<div class="dev-note">Temperatures and load come from the node agents. Install them in Settings → Node agents.</div>`}
      <div class="dev-list">${d.nodes.map(device)}</div>
      <div class="dev-foot">
        <button class="btn small danger" data-dev-restart>Restart Kubernetes…</button>
        <span class="faint small">Restarts k3s on every node, workers first.</span>
        <button class="btn small danger" data-dev-reboot>Reboot every machine…</button>
        <span class="faint small">Restarts the operating system on each node, then the control server.</span>
      </div>`);
  }

  btn.addEventListener("click", () => toggle());
  side.addEventListener("toggle", (e) => {
    const row = e.target;
    if (!row.matches || !row.matches("details.dev[data-device]")) return;
    if (row.open) expanded.add(row.dataset.device);
    else expanded.delete(row.dataset.device);
  }, true);
  document.addEventListener("click", (e) => {
    const o = e.target.closest("[data-dev-open]");
    if (o) { U.openDrawer("node:" + o.dataset.devOpen); return; }
    if (e.target.closest("[data-dev-close]")) { toggle(false); return; }
    if (e.target.closest("[data-dev-restart]")) {
      if (!window.confirm("Restart Kubernetes on every node?\n\nWorkers restart one at a time, then the control node. Containers keep running, but the cluster is unreachable for a minute or two.")) return;
      U.startJob("restart-cluster", {}, () => U.load(true));
    }
    if (e.target.closest("[data-dev-reboot]")) {
      if (!window.confirm("Fully reboot every machine in the Kubernetes cluster?\n\nWorkers are drained and rebooted one at a time; the control server reboots last. Every workload will be interrupted, and the dashboard will go offline briefly.")) return;
      U.startJob("reboot-cluster", {}, () => U.load(true));
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "d" || e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName || "")) return;
    toggle();
  });

  U.hooks.push(paint);
  apply();
})();
