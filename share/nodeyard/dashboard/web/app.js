// The nodeyard dashboard. It reads /api/* from the local server; explicit operations are confirmed before running.
(function () {
  "use strict";
  const { esc, html, raw, setHTML, fmt, bar, donut, spark, area, level, PALETTE } = NY;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const store = {
    get(k, d) { try { const v = localStorage.getItem("nodeyard." + k); return v == null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("nodeyard." + k, v); } catch (e) { /* storage blocked */ } },
  };

  const VIEWS = [
    { id: "overview", label: "Overview", icon: "overview", sub: "Your cluster at a glance" },
    { id: "nodes", label: "Nodes", icon: "nodes", sub: "Every machine, its address and its load" },
    { id: "processes", label: "Processes", icon: "processes", sub: "What is running on each machine, and what is using the memory and CPU" },
    { id: "pods", label: "Pods", icon: "pods", sub: "Everything that is running, with live usage and logs" },
    { id: "workloads", label: "Workloads", icon: "workloads", sub: "Deployments, daemon sets, stateful sets, jobs and cron jobs" },
    { id: "network", label: "Network", icon: "network", sub: "IP addresses, ports and how to reach things" },
    { id: "storage", label: "Storage", icon: "storage", sub: "Disks and volumes" },
    { id: "hardware", label: "Hardware", icon: "hardware", sub: "What every machine is made of, and how fast it really is" },
    { id: "ai", label: "AI", icon: "ai", sub: "Models running on your cluster" },
    { id: "events", label: "Events", icon: "events", sub: "What Kubernetes has been doing" },
    { id: "alerts", label: "Alerts", icon: "alerts", sub: "Things that need your attention" },
    { id: "doctor", label: "Doctor", icon: "doctor", sub: "Checks for common problems, with one-click fixes" },
    { id: "commands", label: "Commands", icon: "commands", sub: "Every nodeyard command, runnable from here" },
    { id: "terminal", label: "Terminal", icon: "terminal", sub: "A shell on the server" },
    { id: "settings", label: "Settings", icon: "settings", sub: "Passwords, keys, the dashboard itself and how it looks" },
  ];

  const S = {
    res: null, d: null, t: null, hist: [], agents: null, view: "overview", built: null, online: true, skew: 0, lastUpdated: 0,
    range: +store.get("range", 900), interval: +store.get("interval", 5), live: store.get("live", "1") === "1",
    sort: {}, drawer: null, timer: null, nodeColor: {}, nodeBy: {}, podBy: {}, topBy: "cpu",
    f: {
      proc: { q: "", node: "", mode: "procs" }, pods: { q: "", ns: "", status: "", node: "" }, workloads: { q: "", ns: "", kind: "" }, network: { q: "", kind: "all" },
      events: { q: "", type: "", ns: "" }, storage: { q: "" },
    },
    logs: null,
  };

  const nowSrv = () => Date.now() / 1000 + S.skew;
  const ago = (ts) => fmt.ago(ts, nowSrv());
  const statusClass = (s) => (/^(Running|Succeeded|Completed|Ready|Bound|Normal)$/.test(s) ? "good" : /^(Pending|ContainerCreating|PodInitializing|Terminating|Init:|Released|Available)/.test(s) ? "warn" : "bad");
  const chip = (text, cls) => html`<span class="chip ${cls || ""}">${text}</span>`;
  const statusChip = (s) => chip(s, statusClass(s));
  const ip = (v) => (v ? html`<span class="mono copy" data-copy="${v}" title="Click to copy">${v}</span>` : html`<span class="faint">–</span>`);
  const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");

  // ---------------------------------------------------------------- data
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (r.status === 401) { location.href = "/login"; throw new Error("sign in"); }
    return r.json();
  }
  async function postJSON(url, body) {
    const r = await fetch(url, { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify(body || {}) });
    if (r.status === 401) { location.href = "/login"; throw new Error("sign in"); }
    return r.json();
  }
  function derive() {
    const d = S.d;
    S.nodeColor = {}; d.nodes.forEach((n, i) => { S.nodeColor[n.name] = PALETTE[i % PALETTE.length]; });
    S.nodeBy = Object.fromEntries(d.nodes.map((n) => [n.name, n]));
    S.podBy = Object.fromEntries(d.pods.map((p) => [p.namespace + "/" + p.name, p]));
  }
  function pushHistory(res) {
    if (!res.updated || res.updated === S.lastUpdated) return;
    S.lastUpdated = res.updated;
    const nodes = {};
    res.state.nodes.forEach((n) => { const h = n.hw || {}; const g = h.gpu_live || []; nodes[n.name] = [n.cpu_used, n.mem_used, n.net_rx_rate, n.net_tx_rate, h.temp_c == null ? null : h.temp_c, h.freq_mhz == null ? null : h.freq_mhz, h.load ? h.load[0] : null,
      g.length ? g.reduce((a, x) => a + (x.use || 0), 0) / g.length : null, g.length ? g.reduce((a, x) => a + x.mem_used, 0) : null]; });
    S.hist.push({ t: res.updated, pods: res.totals.pods_running, nodes });
    if (S.hist.length > 720) S.hist.splice(0, S.hist.length - 720);
  }
  function apply(res) {
    S.online = true;
    S.skew = res.now - Date.now() / 1000;
    S.res = res;
    if (res.ok) { S.d = res.state; S.t = res.totals; derive(); pushHistory(res); }
    if (res.agents_full && res.agents_full.ok) S.agents = res.agents_full;
  }
  const needAgents = () => S.view === "processes" || (S.drawer || "").startsWith("node:");
  async function load(fresh) {
    try {
      const res = await getJSON("/api/state" + (fresh ? "?fresh=1" : ""));
      apply(res);
      if (res.ok && res.state.agents && res.state.agents.installed && needAgents()) {
        try { const a = await getJSON("/api/agents"); if (a.ok) S.agents = a; } catch (e) { /* keep the last one */ }
      }
    } catch (e) { S.online = false; }
    render();
  }
  // Real time: the server pushes a new snapshot the moment it has one (server-sent events).
  // Polling below only covers a stream that is down.
  let es = null, esAgents = false, esAt = 0, renderQueued = false;
  const streamHealthy = () => !!es && es.readyState === 1 && Date.now() - esAt < 25000;
  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    const go = () => { if (!renderQueued) return; renderQueued = false; render(); };
    requestAnimationFrame(go); // smooth when the tab is visible...
    setTimeout(go, 250);       // ...and still updates when the browser pauses animation frames
  }
  function connectStream() {
    if (!S.live || typeof EventSource === "undefined") return;
    const want = needAgents();
    if (es && esAgents === want) return;
    if (es) es.close();
    esAgents = want;
    es = new EventSource("/api/stream" + (want ? "?agents=1" : ""));
    es.addEventListener("state", (ev) => { esAt = Date.now(); let r; try { r = JSON.parse(ev.data); } catch (e) { return; } apply(r); queueRender(); });
    es.addEventListener("auth", () => { location.href = "/login"; });
    es.onopen = () => { esAt = Date.now(); };
  }
  function closeStream() { if (es) { es.close(); es = null; } }
  async function start() {
    try { const h = await getJSON("/api/history?points=720"); if (h.ok) S.hist = h.points; } catch (e) { /* the chart fills in as data arrives */ }
    await load();
    connectStream();
    schedule();
  }
  function schedule() {
    clearTimeout(S.timer);
    if (!S.live) return;
    S.timer = setTimeout(async () => { if (!streamHealthy()) { await load(); connectStream(); } schedule(); }, Math.max(2, S.interval) * 1000);
  }

  // ---------------------------------------------------------------- helpers
  const getPath = (p) => (p === "range" ? S.range : p.split(".").reduce((o, k) => (o == null ? o : o[k]), S.f));
  const setPath = (p, v) => { const k = p.split("."); const last = k.pop(); k.reduce((o, x) => o[x], S.f)[last] = v; };
  function syncControls() {
    $$("[data-f]").forEach((el) => { const v = getPath(el.dataset.f); if (el !== document.activeElement && v != null) el.value = v; });
    $$("[data-set]").forEach((b) => { const [p, v] = b.dataset.set.split(":"); b.classList.toggle("on", String(getPath(p)) === v); });
  }
  function fillSelect(el, opts, label) {
    if (!el) return;
    const sig = opts.join("\u0001");
    if (el.dataset.sig !== sig) {
      el.dataset.sig = sig;
      const cur = getPath(el.dataset.f);
      el.innerHTML = '<option value="">' + esc(label) + "</option>" + opts.map((o) => '<option value="' + esc(o) + '">' + esc(o) + "</option>").join("");
      el.value = cur || "";
    }
  }
  function cmp(a, b) {
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    if (typeof a === "number" && typeof b === "number") return a - b;
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
  }
  // cols: [{k, t, cls, v(row) -> sort value, r(row) -> cell}]
  function table(id, cols, rows, o) {
    o = o || {};
    const st = S.sort[id] || (S.sort[id] = { k: o.k || cols[0].k, dir: o.dir || 1 });
    const col = cols.find((c) => c.k === st.k) || cols[0];
    const sorted = rows.slice().sort((a, b) => { const r = cmp(col.v ? col.v(a) : 0, col.v ? col.v(b) : 0); return (r === 0 ? cmp(cols[0].v ? cols[0].v(a) : 0, cols[0].v ? cols[0].v(b) : 0) : r) * st.dir; });
    const shown = sorted.slice(0, o.limit || 500);
    if (!rows.length) return html`<div class="empty"><b>${o.empty || "Nothing to show"}</b>${o.emptySub || ""}</div>`;
    return html`<div class="tablewrap"><table class="tbl"><thead><tr>${cols.map((c) => raw(
      "<th class=\"" + esc(c.cls || "") + (c.v ? " sort" : "") + "\"" + (c.v ? " data-sort=\"" + id + "|" + c.k + "\"" : "") + ">" + esc(c.t) +
      (c.k === st.k ? '<span class="arrow">' + (st.dir > 0 ? "↑" : "↓") + "</span>" : "") + "</th>"))}</tr></thead>
      <tbody>${shown.map((r) => { const open = o.open ? o.open(r) : ""; return html`<tr class="${open ? "click" : ""}" ${open ? raw('data-open="' + esc(open) + '"') : ""}>${cols.map((c) => raw("<td class=\"" + esc(c.cls || "") + "\">" + (c.r(r) instanceof NY.Raw ? c.r(r).s : esc(c.r(r))) + "</td>"))}</tr>`; })}</tbody></table></div>
      ${rows.length > shown.length ? html`<div class="muted" style="padding:10px 12px;font-size:12.5px">Showing the first ${shown.length} of ${rows.length}. Narrow the filter to see the rest.</div>` : ""}`;
  }
  const metric = (label, frac, text) => html`<div class="metric"><span class="k">${label}</span>${bar(frac)}<span class="v">${text}</span></div>`;
  const seg = (path, opts) => html`<span class="seg" role="group">${opts.map(([v, l]) => raw('<button data-set="' + path + ":" + esc(v) + '">' + esc(l) + "</button>"))}</span>`;
  const empty = (title, sub) => html`<div class="empty"><b>${title}</b>${sub || ""}</div>`;
  const card = (title, body, right, cls) => html`<section class="card ${cls || ""}"><h3>${title}${right ? html`<span class="right">${right}</span>` : ""}</h3>${body}</section>`;
  const cpuText = (n) => fmt.cores(n.cpu_used) + " / " + n.cpu_cores + " cores";
  const memText = (n) => fmt.bytes(n.mem_used) + " / " + fmt.bytes(n.mem_total);
  const frac = (a, b) => (a != null && b ? a / b : null);

  function histPts() { const from = nowSrv() - S.range; return S.hist.filter((p) => p.t >= from); }
  function nodeSeries(idx, only) {
    const pts = histPts();
    return { ts: pts.map((p) => p.t), series: S.d.nodes.filter((n) => !only || n.name === only).map((n) => ({ name: n.name, color: only ? "var(--accent)" : S.nodeColor[n.name], values: pts.map((p) => (p.nodes[n.name] ? p.nodes[n.name][idx] : null)) })) };
  }
  function sumSeries(idx) { const pts = histPts(); return pts.map((p) => Object.values(p.nodes).reduce((s, a) => s + (a[idx] || 0), 0)); }
  const legend = (items) => html`<div class="legend">${items.map((i) => html`<span><i style="background:${i.color}"></i>${i.name}</span>`)}</div>`;

  function alertCard(a) {
    const open = a.kind === "node" || a.kind === "pod" ? a.kind + ":" + a.ref : a.kind === "workload" ? "wl:" + a.ref : "";
    return html`<div class="alert ${a.level} ${open ? "click" : ""}" ${open ? raw('data-open="' + esc(open) + '"') : ""}><span class="sev"></span><div><b>${a.title}</b><span>${a.detail}</span></div></div>`;
  }
  function eventRow(e) {
    return html`<div class="row" style="padding:7px 0;border-bottom:1px solid var(--line);align-items:flex-start"><i class="dot ${e.type === "Warning" ? "warn" : ""}" style="margin-top:6px"></i>
      <div class="grow" style="min-width:0"><div><b>${e.reason}</b> <span class="muted">${e.object}</span></div><div class="muted" style="font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${e.message}">${e.message}</div></div>
      <span class="faint nowrap" style="font-size:12px">${ago(e.last)} ago${e.count > 1 ? " ×" + e.count : ""}</span></div>`;
  }

  // ---------------------------------------------------------------- views
  const V = {};

  // ----- Overview
  V.overview = {
    build() {
      NY.freshHTML($("#view"), html`<div id="ov-hero"></div><div class="grid g-kpi mt" id="ov-kpi"></div>
        <div class="section-title">Usage over time <span class="grow"></span>${seg("range", [["300", "5 min"], ["900", "15 min"], ["1800", "30 min"], ["3600", "1 hour"]])}</div>
        <div class="grid g-2" id="ov-charts"></div>
        <div class="section-title">Nodes <span class="count" id="ov-nc"></span></div><div class="grid g-nodes" id="ov-nodes"></div>
        <div class="grid g-2 mt" id="ov-mid"></div><div class="grid g-2 mt" id="ov-low"></div><div class="mt" id="ov-ai"></div>`);
    },
    update() {
      const d = S.d, t = S.t, al = S.res.alerts, crit = al.filter((a) => a.level === "critical"), warn = al.filter((a) => a.level === "warning");
      const h = crit.length ? ["bad", plural(crit.length, "critical issue") + (warn.length ? " and " + plural(warn.length, "warning") : "")] : warn.length ? ["warn", plural(warn.length, "thing") + " need" + (warn.length === 1 ? "s" : "") + " attention"] : ["good", "All systems healthy"];
      const cp = d.nodes.filter((n) => n.roles.some((r) => /control-plane|master/.test(r))).length;
      setHTML($("#ov-hero"), html`<div class="hero fade"><div><div class="faint" style="font-size:12px;text-transform:uppercase;letter-spacing:.08em">Cluster</div><h2>${d.cluster.name}</h2>
        <div class="meta">${chip(d.cluster.k3s_version || "k3s", "accent")}${d.cluster.api_server ? html`<span class="chip">API <span class="mono copy" data-copy="${d.cluster.api_server}">${d.cluster.api_server.replace(/^https?:\/\//, "")}</span></span>` : ""}
        ${chip(plural(t.nodes, "node") + " · " + cp + " control plane")}${d.cluster.pod_cidr ? chip("pods " + d.cluster.pod_cidr) : ""}${d.cluster.service_cidr ? chip("services " + d.cluster.service_cidr) : ""}${d.cluster.created ? chip("up " + ago(d.cluster.created)) : ""}</div></div>
        <div class="health"><div class="big"><svg class="icon" style="color:var(--${h[0] === "good" ? "good" : h[0] === "warn" ? "warn" : "bad"})"><use href="#i-${h[0] === "good" ? "check" : "warn"}"/></svg>${h[1]}</div>
        <div class="muted" style="font-size:12.5px;margin-top:4px">${al.length ? html`<a href="#alerts">See ${plural(al.length, "alert")}</a>` : "Nothing needs your attention"}</div></div></div>`);

      const rxS = sumSeries(2), nodePorts = d.services.reduce((s, x) => s + x.node_ports.length, 0), bad = t.pods - t.pods_running;
      const healthyW = d.workloads.filter((w) => !["Deployment", "StatefulSet", "DaemonSet"].includes(w.kind) || w.ready >= w.desired).length;
      const kpi = (label, value, detail, ring, sp) => html`<div class="card kpi fade">${ring ? html`<div class="ring">${ring}</div>` : ""}<div class="txt"><div class="label">${label}</div><div class="value">${value}</div><div class="detail">${detail}</div></div>${sp ? html`<div class="spark">${sp}</div>` : ""}</div>`;
      setHTML($("#ov-kpi"), html`
        ${kpi("Nodes", html`${t.nodes_ready}<small> / ${t.nodes} ready</small>`, t.nodes_ready === t.nodes ? "Every node is healthy" : t.nodes - t.nodes_ready + " not ready", donut(frac(t.nodes_ready, t.nodes), { label: t.nodes_ready + "/" + t.nodes, color: t.nodes_ready === t.nodes ? "var(--good)" : "var(--bad)" }))}
        ${kpi("Pods", html`${t.pods_running}<small> running</small>`, t.pods + " total" + (bad ? " · " + bad + " not running" : "") + " · room for " + fmt.num(t.pods_capacity), donut(frac(t.pods_running, t.pods), { color: bad ? "var(--warn)" : "var(--good)" }))}
        ${kpi("CPU", html`${fmt.cores(t.cpu_used)}<small> of ${fmt.num(t.cpu_total)} cores</small>`, fmt.pct(frac(t.cpu_used, t.cpu_total)) + " in use · " + fmt.cores(t.cpu_total - (t.cpu_used || 0)) + " free", donut(frac(t.cpu_used, t.cpu_total)))}
        ${kpi("Memory", html`${fmt.bytes(t.mem_used)}<small> of ${fmt.bytes(t.mem_total)}</small>`, fmt.pct(frac(t.mem_used, t.mem_total)) + " in use · " + fmt.bytes(t.mem_total - (t.mem_used || 0)) + " free", donut(frac(t.mem_used, t.mem_total)))}
        ${kpi("Storage", t.disk_total ? html`${fmt.bytes(t.disk_used)}<small> of ${fmt.bytes(t.disk_total)}</small>` : "–", t.disk_total ? fmt.pct(frac(t.disk_used, t.disk_total)) + " used across all nodes" : "Disk usage isn't reported", t.disk_total ? donut(frac(t.disk_used, t.disk_total)) : null)}
        ${gpuKpi(t, kpi)}
        ${kpi("Network", html`↓ ${fmt.rate(t.net_rx_rate)}`, "↑ " + fmt.rate(t.net_tx_rate) + " · all nodes", null, spark(rxS, { color: "var(--accent2)" }))}
        ${kpi("Workloads", html`${d.workloads.length}<small> · ${healthyW} healthy</small>`, plural(d.namespaces.length, "namespace") + " · " + plural(d.services.length, "service") + " · " + plural(nodePorts, "NodePort"), null)}
        ${kpi("Restarts", t.restarts, d.pods.filter((p) => p.restarts > 0).length + " pods have restarted", null)}`);

      const cpu = nodeSeries(0), mem = nodeSeries(1), nodeLeg = legend(cpu.series);
      const rx = sumSeries(2), tx = sumSeries(3), pts = histPts(), ts = pts.map((p) => p.t);
      setHTML($("#ov-charts"), html`
        ${card("CPU usage", html`${area("c-cpu", cpu.ts, cpu.series, { stacked: true, max: t.cpu_total || undefined, fmt: fmt.cores, title: "CPU usage" })}<div class="mt">${nodeLeg}</div>`, "of " + fmt.num(t.cpu_total) + " cores")}
        ${card("Memory usage", html`${area("c-mem", mem.ts, mem.series, { stacked: true, max: t.mem_total || undefined, fmt: (v) => fmt.bytes(v), title: "Memory usage" })}<div class="mt">${nodeLeg}</div>`, "of " + fmt.bytes(t.mem_total))}
        ${t.gpus ? (() => { const gu = nodeSeries(7), gm = nodeSeries(8); return html`${card("GPU usage", html`${area("c-gpu", gu.ts, gu.series.filter((x) => x.values.some((v) => v != null)), { max: 100, fmt: (v) => Math.round(v) + "%", title: "GPU usage" })}`, "busy %")}${card("Video memory", html`${area("c-vram", gm.ts, gm.series.filter((x) => x.values.some((v) => v != null)), { stacked: true, max: t.gpu_mem_total || undefined, fmt: (v) => fmt.bytes(v), title: "Video memory" })}`, "of " + fmt.bytes(t.gpu_mem_total))}`; })() : ""}
        ${card("Network throughput", html`${area("c-net", ts, [{ name: "Received", color: "#22d3ee", values: rx }, { name: "Sent", color: "#a78bfa", values: tx }], { fmt: fmt.rate, left: 66, title: "Network throughput" })}<div class="mt">${legend([{ name: "Received", color: "#22d3ee" }, { name: "Sent", color: "#a78bfa" }])}</div>`, "all nodes")}
        ${card("Pods running", html`${area("c-pods", ts, [{ name: "Running pods", color: "#34d399", values: pts.map((p) => p.pods) }], { fmt: (v) => Math.round(v), title: "Pods running" })}`, fmt.num(t.pods_running) + " now")}`);

      setHTML($("#ov-nc"), html`${d.nodes.length}`);
      setHTML($("#ov-nodes"), d.nodes.map((n) => html`<div class="card node" data-open="node:${n.name}">
        <div class="head"><i class="dot ${n.ready ? "good" : "bad"}"></i><span class="name">${n.name}</span><span class="grow"></span><span class="ip copy" data-copy="${n.internal_ip}">${n.internal_ip}</span></div>
        <div class="muted" style="font-size:12.5px">${n.os}</div>
        <div class="tags">${n.roles.map((r) => chip(r, /control-plane|master|etcd/.test(r) ? "accent" : ""))}${chip(n.arch)}${n.ready ? "" : chip("NotReady", "bad")}${n.unschedulable ? chip("cordoned", "warn") : ""}</div>
        ${metric("CPU", frac(n.cpu_used, n.cpu_cores), cpuText(n))}${metric("RAM", frac(n.mem_used, n.mem_total), memText(n))}${n.disk_total ? metric("Disk", frac(n.disk_used, n.disk_total), fmt.bytes(n.disk_used, 0) + " / " + fmt.bytes(n.disk_total, 0)) : ""}
        <div class="foot"><span>${n.pods_running}/${n.pods_capacity} pods${n.hw && n.hw.temp_c != null ? " · " + fmt.temp(n.hw.temp_c) : ""}${n.hw && n.hw.freq_mhz ? " · " + fmt.mhz(n.hw.freq_mhz) : ""}</span><span>↓ ${fmt.rate(n.net_rx_rate)} · ↑ ${fmt.rate(n.net_tx_rate)}</span></div></div>`));

      const withUse = d.pods.filter((p) => p.cpu != null), key = S.topBy;
      const top = withUse.slice().sort((a, b) => (b[key] || 0) - (a[key] || 0)).slice(0, 8), mx = Math.max(...top.map((p) => p[key] || 0), 1e-9);
      const topRows = top.length ? top.map((p) => html`<div class="metric" style="grid-template-columns:minmax(0,1.3fr) 1fr auto;cursor:pointer" data-open="pod:${p.namespace}/${p.name}">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><b>${p.name}</b> <span class="faint">${p.namespace}</span></span>${bar((p[key] || 0) / mx, "")}<span class="v">${key === "cpu" ? fmt.cores(p.cpu) : fmt.bytes(p.mem)}</span></div>`) : empty("No usage data yet", "Pod usage comes from the cluster's metrics service.");
      const att = al.filter((a) => a.level !== "info").slice(0, 5);
      setHTML($("#ov-mid"), html`
        ${card("Top consumers", html`${topRows}`, html`<span class="seg"><button data-act="top:cpu" class="${key === "cpu" ? "on" : ""}">CPU</button><button data-act="top:mem" class="${key === "mem" ? "on" : ""}">Memory</button></span>`)}
        ${card("Needs attention", att.length ? att.map(alertCard) : html`<div class="allgood"><svg class="icon"><use href="#i-check"/></svg>Everything looks good</div>`, al.length ? html`<a href="#alerts">All ${al.length}</a>` : "")}`);

      const ns = {};
      d.pods.forEach((p) => { const o = ns[p.namespace] || (ns[p.namespace] = { n: 0, cpu: 0, mem: 0 }); o.n++; o.cpu += p.cpu || 0; o.mem += p.mem || 0; });
      const nsRows = Object.entries(ns).sort((a, b) => b[1].mem - a[1].mem), nm = Math.max(...nsRows.map((r) => r[1].mem), 1);
      const ev = d.events.slice(0, 7);
      setHTML($("#ov-low"), html`
        ${card("Resources by namespace", nsRows.map(([k, o]) => html`<div class="metric" style="grid-template-columns:minmax(0,1.1fr) 1fr auto;cursor:pointer" data-goto="pods?ns=${k}"><span><b>${k}</b> <span class="faint">${plural(o.n, "pod")}</span></span>${bar(o.mem / nm, "")}<span class="v">${fmt.bytes(o.mem)} · ${fmt.cores(o.cpu)}</span></div>`))}
        ${card("Recent events", ev.length ? ev.map(eventRow) : empty("No events"), html`<a href="#events">All events</a>`)}`);

      const sp = d.ai && d.ai.split;
      setHTML($("#ov-ai"), sp ? card("AI model", aiSummary(sp), html`<a href="#ai">Details</a>`) : "");
    },
  };

  // The GPU card: live usage from cards that are set up (nodeyard ai gpu enable), otherwise what was
  // found and that it isn't set up yet, so the card is always there.
  function gpuKpi(t, kpi) {
    const cards = t.gpu_cards || [], live = cards.filter((c) => c.live && c.nvidia), idle = cards.filter((c) => c.nvidia && !c.live);
    const short = (c) => c.name.replace(/^NVIDIA\s+/, "").replace(/^GeForce\s+/, "") + " on " + c.node;
    if (t.gpus) {
      const names = live.length ? live.map(short).join(", ") + " · " : "";
      return kpi("GPU", html`${Math.round(t.gpu_use || 0)}%<small> busy</small>`, names + fmt.bytes(t.gpu_mem_used) + " of " + fmt.bytes(t.gpu_mem_total) + " video memory" +
        (t.gpu_temp ? " · " + fmt.temp(t.gpu_temp) : "") + (t.gpu_power ? " · " + Math.round(t.gpu_power) + " W" : ""), donut((t.gpu_use || 0) / 100, { color: "var(--violet)" }), spark(sumSeries(7), { color: "var(--violet)" }));
    }
    if (idle.length) return kpi("GPU", html`${idle.length}<small> not set up</small>`, idle.map(short).join(", ") + ": not used by containers yet. Set it up: nodeyard ai gpu setup (on that machine)", donut(0, { color: "var(--violet)", label: "–" }));
    const other = cards.filter((c) => !c.nvidia);
    return kpi("GPU", other.length ? html`${other.length}<small> built-in</small>` : "–", other.length ? [...new Set(other.map((c) => c.name))].slice(0, 3).join(", ") + ": no usage readings (only NVIDIA cards report them)" : "No graphics cards reported (are the node agents installed?)", null);
  }

  function aiSummary(sp) {
    const total = sp.shares.reduce((s, x) => s + x.mib, 0) || 1;
    return html`<div class="row wrap" style="gap:14px 22px"><div class="grow" style="min-width:260px"><div style="font-size:17px;font-weight:650">${sp.alias || sp.model}</div><div class="muted mono" style="margin:3px 0 10px;font-size:12px;word-break:break-all">${sp.model}</div>
      <div class="row wrap">${sp.loaded === false ? chip("Unloaded", "warn") : sp.ready ? chip("Serving", "good") : chip(sp.download === "running" ? "Downloading" : "Loading", "warn")}${chip("context " + (sp.ctx || "?"))}${sp.gate ? chip("no key on your network", "good") : sp.auth ? chip("API key required") : chip("no API key", "warn")}${sp.node_port || (sp.gate && sp.gate.port) ? chip("port " + (sp.node_port || sp.gate.port), "accent") : ""}</div></div>
      <div class="grow" style="min-width:260px"><div class="muted" style="font-size:12px;margin-bottom:6px">Model split across nodes</div>
      <div class="stack">${sp.shares.map((s) => raw('<i title="' + esc(s.node) + ": " + fmt.bytes(s.mib * 1048576) + '" style="width:' + ((s.mib / total) * 100).toFixed(1) + "%;background:" + (s.gpu ? "var(--violet)" : S.nodeColor[s.node] || "var(--accent)") + '"></i>'))}</div>
      <div class="legend mt">${sp.shares.map((s) => html`<span><i style="background:${s.gpu ? "var(--violet)" : S.nodeColor[s.node] || "var(--accent)"}"></i>${s.node} ${fmt.bytes(s.mib * 1048576)}</span>`)}</div></div></div>`;
  }

  // ----- Nodes
  V.nodes = {
    build() { NY.freshHTML($("#view"), html`<div id="nd-cap"></div><div class="card mt"><div id="nd-table"></div></div>`); },
    update() {
      const d = S.d, t = S.t;
      const cap = (label, used, total, f, extra) => html`<div><div class="muted" style="font-size:12px;text-transform:uppercase;letter-spacing:.07em;font-weight:600">${label}</div><div style="font-size:20px;font-weight:700;margin:2px 0 6px">${used}<small class="muted" style="font-size:13px;font-weight:500"> / ${total}</small></div>${bar(f, null, true)}<div class="muted" style="font-size:12px;margin-top:5px">${extra}</div></div>`;
      setHTML($("#nd-cap"), html`<div class="card"><div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:22px">
        ${cap("Total CPU", fmt.cores(t.cpu_used), t.cpu_total + " cores", frac(t.cpu_used, t.cpu_total), fmt.pct(frac(t.cpu_used, t.cpu_total)) + " in use")}
        ${cap("Total memory", fmt.bytes(t.mem_used), fmt.bytes(t.mem_total), frac(t.mem_used, t.mem_total), fmt.pct(frac(t.mem_used, t.mem_total)) + " in use")}
        ${cap("Total disk", fmt.bytes(t.disk_used, 0), fmt.bytes(t.disk_total, 0), frac(t.disk_used, t.disk_total), fmt.pct(frac(t.disk_used, t.disk_total)) + " used")}
        ${cap("Pod slots", t.pods_running, fmt.num(t.pods_capacity), frac(t.pods_running, t.pods_capacity), plural(t.pods, "pod") + " scheduled")}</div></div>`);
      const cols = [
        { k: "name", t: "Node", v: (n) => n.name, r: (n) => html`<b>${n.name}</b>` },
        { k: "status", t: "Status", v: (n) => n.status, r: (n) => html`${statusChip(n.status)}${n.unschedulable ? chip("cordoned", "warn") : ""}` },
        { k: "roles", t: "Roles", v: (n) => n.roles.join(), r: (n) => html`${n.roles.map((r) => chip(r, /control-plane|master|etcd/.test(r) ? "accent" : ""))}` },
        { k: "ip", t: "IP address", cls: "mono", v: (n) => n.internal_ip.split(".").map((x) => x.padStart(3, "0")).join("."), r: (n) => ip(n.internal_ip) },
        { k: "cpu", t: "CPU", cls: "barcell", v: (n) => frac(n.cpu_used, n.cpu_cores), r: (n) => html`${bar(frac(n.cpu_used, n.cpu_cores))}<small>${cpuText(n)}</small>` },
        { k: "mem", t: "Memory", cls: "barcell", v: (n) => frac(n.mem_used, n.mem_total), r: (n) => html`${bar(frac(n.mem_used, n.mem_total))}<small>${memText(n)}</small>` },
        { k: "disk", t: "Disk", cls: "barcell", v: (n) => frac(n.disk_used, n.disk_total), r: (n) => (n.disk_total ? html`${bar(frac(n.disk_used, n.disk_total))}<small>${fmt.bytes(n.disk_used, 0)} / ${fmt.bytes(n.disk_total, 0)}</small>` : "–") },
        ...(d.nodes.some((n) => n.hw) ? [
          { k: "clock", t: "CPU clock", cls: "nowrap", v: (n) => (n.hw ? n.hw.freq_mhz : null), r: (n) => (n.hw && n.hw.freq_mhz ? html`${fmt.mhz(n.hw.freq_mhz)}<div class="sub">of ${fmt.mhz(n.hw.freq_max)}</div>` : "–") },
          { k: "temp", t: "Temp", v: (n) => (n.hw ? n.hw.temp_c : null), r: (n) => (n.hw && n.hw.temp_c != null ? chip(fmt.temp(n.hw.temp_c), tempClass(n.hw.temp_c)) : "–") },
          { k: "load", t: "Load", cls: "nowrap", v: (n) => (n.hw && n.hw.load && n.hw.load.length ? n.hw.load[0] : null), r: (n) => (n.hw && n.hw.load && n.hw.load.length ? n.hw.load.map((x) => x.toFixed(2)).join(" ") : "–") },
        ] : []),
        { k: "pods", t: "Pods", cls: "num", v: (n) => n.pods_running, r: (n) => n.pods_running + " / " + n.pods_capacity },
        { k: "net", t: "Network", cls: "nowrap", v: (n) => (n.net_rx_rate || 0) + (n.net_tx_rate || 0), r: (n) => html`↓ ${fmt.rate(n.net_rx_rate)}<br><span class="sub">↑ ${fmt.rate(n.net_tx_rate)}</span>` },
        { k: "os", t: "System", cls: "wrap", v: (n) => n.os, r: (n) => html`${n.os}<div class="sub">${n.arch} · kernel ${n.kernel}</div>` },
        { k: "age", t: "Age", cls: "num", v: (n) => -n.created, r: (n) => ago(n.created) },
      ];
      setHTML($("#nd-table"), table("nodes", cols, d.nodes, { open: (n) => "node:" + n.name }));
    },
  };

  // ----- hardware helpers
  const tempClass = (c) => (c == null ? "" : c >= 85 ? "bad" : c >= 75 ? "warn" : "good");
  const memParts = (h) => {
    const total = h.mem_total || 1, apps = h.mem_anon || 0, cache = h.mem_cached || 0, buf = h.mem_buffers || 0, free = h.mem_free || 0;
    return [
      { v: apps, color: "var(--accent)", label: "Programs" }, { v: Math.max(0, total - apps - cache - buf - free), color: "var(--warn)", label: "Kernel and other" },
      { v: cache, color: "var(--accent2)", label: "Cache (reclaimable)" }, { v: buf, color: "var(--violet)", label: "Buffers" }, { v: free, color: "var(--line2)", label: "Free" },
    ];
  };
  const memBar = (h) => {
    const parts = memParts(h), total = h.mem_total || 1;
    return html`<div class="stack">${parts.map((p) => raw('<i title="' + esc(p.label) + ": " + esc(fmt.bytes(p.v)) + '" style="width:' + ((p.v / total) * 100).toFixed(2) + "%;background:" + p.color + '"></i>'))}</div>
      <div class="legend mt">${parts.filter((p) => p.v > 0).map((p) => html`<span><i style="background:${p.color}"></i>${p.label} ${fmt.bytes(p.v)}</span>`)}</div>`;
  };
  const clockBars = (cores) => html`<div class="clocks">${cores.map((c) => raw('<i title="cpu' + c.id + ": " + esc(fmt.mhz(c.mhz)) + (c.use != null ? ", " + c.use + "% busy" : "") + '" style="height:' + Math.max(8, Math.min(100, ((c.mhz || 0) / (c.max || c.mhz || 1)) * 100)).toFixed(0) + "%;opacity:" + (0.45 + 0.55 * Math.min(1, (c.use || 0) / 60)).toFixed(2) + '"></i>'))}</div>`;
  function hwCard(n) {
    const h = n.hw, full = S.agents && S.agents.nodes && S.agents.nodes[n.name], cores = full ? full.cpu.cores : [];
    const swapUsed = h.swap_total ? h.swap_total - (h.swap_free || 0) : 0;
    return html`<section class="card node-hw" data-open="node:${n.name}">
      <div class="head row"><i class="dot ${n.ready ? "good" : "bad"}"></i><b style="font-size:15.5px">${n.name}</b><span class="grow"></span>${h.temp_c != null ? chip(fmt.temp(h.temp_c), tempClass(h.temp_c)) : ""}${h.undervoltage ? chip("low voltage", "warn") : ""}</div>
      <div class="muted" style="font-size:12.5px;margin:2px 0 10px">${h.cpu_model || n.arch} · ${h.cores || n.cpu_cores} cores</div>
      <div class="grid" style="grid-template-columns:1fr auto;gap:12px;align-items:end"><div><div class="muted" style="font-size:12px">CPU clock</div><div style="font-size:18px;font-weight:650">${fmt.mhz(h.freq_mhz)}<small class="muted" style="font-size:12px;font-weight:500"> of ${fmt.mhz(h.freq_max)}${h.governor ? " · " + h.governor : ""}</small></div></div>
        <div class="right"><div class="muted" style="font-size:12px">Load</div><div style="font-weight:650">${(h.load || []).map((x) => x.toFixed(2)).join(" · ")}</div></div></div>
      ${cores.length ? clockBars(cores) : ""}
      <div class="row" style="justify-content:space-between;margin:12px 0 6px;font-size:12.5px"><span class="muted">Memory</span><span>${fmt.bytes(h.mem_available)} available of ${fmt.bytes(h.mem_total)}</span></div>
      ${memBar(h)}
      <div class="row wrap" style="gap:6px;margin-top:10px">${chip("up " + fmt.dur(h.uptime))}${chip((h.processes || 0) + " processes")}${h.psi_memory != null ? chip("memory pressure " + h.psi_memory.toFixed(1) + "%", h.psi_memory >= 10 ? "warn" : "") : ""}${h.oom_kills ? chip(h.oom_kills + " OOM kill" + (h.oom_kills === 1 ? "" : "s") + " since boot", h.oom_new ? "bad" : "") : ""}${swapUsed ? chip("swap " + fmt.bytes(swapUsed), "warn") : ""}</div></section>`;
  }
  const whereChip = (g) => (g.kind === "pod" ? html`<span class="chip accent btnlike" data-open="pod:${g.name}">${g.name}</span>` : g.kind === "service" ? chip(g.name, "violet") : g.kind === "kernel" ? chip(g.name) : chip(g.name || g.kind));

  // ----- Processes
  V.processes = {
    build() {
      NY.freshHTML($("#view"), html`<div id="pr-top"></div>
        <div class="toolbar mt"><select class="select" data-f="proc.node" aria-label="Machine"></select>
        <input class="input" data-f="proc.q" placeholder="Filter by process, user, pod, command…" style="min-width:300px" aria-label="Filter processes">
        ${seg("proc.mode", [["procs", "Processes"], ["groups", "By pod / service"]])}<span class="grow"></span><span class="muted" id="pr-count"></span></div>
        <div class="card"><div id="pr-table"></div></div>`);
    },
    update() {
      const d = S.d, ag = d.agents || {};
      if (!ag.installed) {
        setHTML($("#pr-top"), html`<div class="card"><div class="empty"><b>Turn on the node agents to see processes</b>The kubelets can't tell the dashboard which programs run on each machine, or how fast their CPUs are running. A tiny read-only agent on each node can: processes, per-core clock speeds, temperatures, load and a real memory breakdown.
          <div style="margin-top:16px"><button class="btn primary" data-act="agent-install">Install the node agents</button></div>
          <div class="cmd" style="max-width:480px;margin:16px auto 0;text-align:left">sudo nodeyard dashboard agent install</div></div></div>`);
        setHTML($("#pr-table"), ""); setHTML($("#pr-count"), "");
        return;
      }
      if (!S.agents || !S.agents.nodes || !Object.keys(S.agents.nodes).length) {
        setHTML($("#pr-top"), html`<div class="card"><div class="empty"><b>Waiting for the agents…</b>They're starting on each node${ag.pods ? " (" + ag.ready + " of " + ag.pods + " answering)" : ""}. This fills in by itself.</div></div>`);
        setHTML($("#pr-table"), "");
        return;
      }
      const f = S.f.proc, q = f.q.trim().toLowerCase(), names = Object.keys(S.agents.nodes).sort();
      fillSelect($('[data-f="proc.node"]'), names, "All machines");
      const scope = f.node ? names.filter((n) => n === f.node) : names;
      const nodeBy = S.nodeBy;
      // what is using the memory, across the machines in view
      const agg = {};
      scope.forEach((nm) => (S.agents.nodes[nm].groups || []).forEach((g) => { const k = g.kind + ":" + (g.name || g.uid); const a = agg[k] || (agg[k] = { g, rss: 0, cpu: 0 }); a.rss += g.rss; a.cpu += g.cpu; }));
      const top = Object.values(agg).sort((a, b) => b.rss - a.rss).slice(0, 6), totalMem = scope.reduce((s, nm) => s + ((nodeBy[nm] && nodeBy[nm].hw && nodeBy[nm].hw.mem_total) || 0), 0) || 1;
      const colors = PALETTE;
      setHTML($("#pr-top"), html`
        ${card("What is using the memory", html`<div class="stack" style="height:16px">${top.map((a, i) => raw('<i title="' + esc(a.g.name || a.g.kind) + ": " + esc(fmt.bytes(a.rss)) + '" style="width:' + ((a.rss / totalMem) * 100).toFixed(2) + "%;background:" + colors[i % colors.length] + '"></i>'))}</div>
          <div class="legend mt">${top.map((a, i) => html`<span><i style="background:${colors[i % colors.length]}"></i>${a.g.name || a.g.kind} <b>${fmt.bytes(a.rss)}</b> <span class="faint">(${fmt.pct(a.rss / totalMem)})</span></span>`)}</div>`,
          html`${fmt.bytes(top.reduce((s, a) => s + a.rss, 0))} of ${fmt.bytes(totalMem)} in the top ${top.length}`)}
        <div class="grid g-hw mt">${scope.map((nm) => (nodeBy[nm] && nodeBy[nm].hw ? hwCard(nodeBy[nm]) : "")).filter(Boolean)}</div>`);

      if (f.mode === "groups") {
        const rows = [];
        scope.forEach((nm) => (S.agents.nodes[nm].groups || []).forEach((g) => rows.push({ node: nm, g, name: g.name || g.kind, rss: g.rss, cpu: g.cpu, procs: g.procs })));
        const shown = rows.filter((r) => !q || (r.name + " " + r.node).toLowerCase().includes(q));
        setHTML($("#pr-count"), html`${shown.length} groups`);
        const cols = [
          { k: "name", t: "Pod or service", v: (r) => r.name, r: (r) => whereChip(r.g) },
          { k: "node", t: "Machine", v: (r) => r.node, r: (r) => r.node },
          { k: "procs", t: "Processes", cls: "num", v: (r) => r.procs, r: (r) => r.procs },
          { k: "mem", t: "Memory", cls: "barcell", v: (r) => r.rss, r: (r) => { const t = (nodeBy[r.node].hw || {}).mem_total; return html`${bar(frac(r.rss, t), "")}<small>${fmt.bytes(r.rss)}${t ? " · " + fmt.pct(r.rss / t) + " of the machine" : ""}</small>`; } },
          { k: "cpu", t: "CPU", cls: "num", v: (r) => r.cpu, r: (r) => r.cpu.toFixed(1) + "%" },
        ];
        setHTML($("#pr-table"), table("pgroups", cols, shown, { k: "mem", dir: -1, empty: "Nothing matches" }));
      } else {
        const rows = [];
        scope.forEach((nm) => (S.agents.nodes[nm].processes || []).forEach((p) => rows.push(Object.assign({ node: nm }, p))));
        const shown = rows.filter((p) => !q || (p.name + " " + p.cmd + " " + p.user + " " + p.node + " " + (p.group.name || "")).toLowerCase().includes(q));
        setHTML($("#pr-count"), html`${shown.length} processes (the biggest of each machine)`);
        const cols = [
          { k: "name", t: "Process", cls: "wrap", v: (p) => p.name, r: (p) => html`<b>${p.name}</b> <span class="faint mono" style="font-size:11.5px">${p.pid}</span><div class="sub mono" style="max-width:420px;overflow:hidden;text-overflow:ellipsis" title="${p.cmd}">${p.cmd}</div>` },
          { k: "where", t: "Belongs to", v: (p) => p.group.name || p.group.kind, r: (p) => whereChip(p.group) },
          { k: "node", t: "Machine", v: (p) => p.node, r: (p) => p.node },
          { k: "user", t: "User", v: (p) => p.user, r: (p) => p.user },
          { k: "mem", t: "Memory", cls: "barcell", v: (p) => p.rss, r: (p) => { const t = (nodeBy[p.node].hw || {}).mem_total; return html`${bar(frac(p.rss, t), "")}<small>${fmt.bytes(p.rss)}${p.swap ? " · swap " + fmt.bytes(p.swap) : ""}</small>`; } },
          { k: "cpu", t: "CPU", cls: "num", v: (p) => p.cpu, r: (p) => p.cpu.toFixed(1) + "%" },
          { k: "threads", t: "Threads", cls: "num", v: (p) => p.threads, r: (p) => p.threads },
          { k: "state", t: "State", v: (p) => p.state, r: (p) => p.state },
          { k: "age", t: "Running", cls: "num", v: (p) => -p.started, r: (p) => (p.started ? ago(p.started) : "–") },
        ];
        setHTML($("#pr-table"), table("procs", cols, shown, { k: "mem", dir: -1, limit: 300, empty: "No processes match" }));
      }
      syncControls();
    },
  };

  // ----- Pods
  const podProblem = (p) => !/^(Running|Succeeded|Completed)$/.test(p.status) || (p.status === "Running" && p.ready.split("/")[0] !== p.ready.split("/")[1]);
  V.pods = {
    build() {
      NY.freshHTML($("#view"), html`<div class="toolbar"><input class="input" data-f="pods.q" placeholder="Filter by name, IP, node, image…" style="min-width:300px" aria-label="Filter pods">
        <select class="select" data-f="pods.ns" aria-label="Namespace"></select>
        <select class="select" data-f="pods.node" aria-label="Node"></select>
        ${seg("pods.status", [["", "All"], ["running", "Running"], ["problem", "Not healthy"]])}<span class="grow"></span><span class="muted" id="pods-count"></span></div>
        <div class="card"><div id="pods-table"></div></div>`);
    },
    update() {
      const d = S.d, f = S.f.pods, q = f.q.trim().toLowerCase();
      fillSelect($('[data-f="pods.ns"]'), d.namespaces, "All namespaces");
      fillSelect($('[data-f="pods.node"]'), d.nodes.map((n) => n.name), "All nodes");
      const rows = d.pods.filter((p) => (!f.ns || p.namespace === f.ns) && (!f.node || p.node === f.node) &&
        (!f.status || (f.status === "running" ? !podProblem(p) : podProblem(p))) &&
        (!q || (p.name + " " + p.namespace + " " + p.ip + " " + p.node + " " + p.host_ip + " " + p.containers.map((c) => c.image).join(" ")).toLowerCase().includes(q)));
      setHTML($("#pods-count"), html`${rows.length} of ${d.pods.length} pods`);
      const cols = [
        { k: "name", t: "Pod", v: (p) => p.name, r: (p) => html`<b>${p.name}</b><div class="sub">${p.owner}</div>` },
        { k: "ns", t: "Namespace", v: (p) => p.namespace, r: (p) => chip(p.namespace) },
        { k: "status", t: "Status", v: (p) => p.status, r: (p) => statusChip(p.status) },
        { k: "ready", t: "Ready", cls: "num", v: (p) => p.ready, r: (p) => p.ready },
        { k: "restarts", t: "Restarts", cls: "num", v: (p) => p.restarts, r: (p) => (p.restarts > 4 ? chip(p.restarts, "warn") : p.restarts) },
        { k: "node", t: "Node", v: (p) => p.node, r: (p) => p.node || html`<span class="faint">unscheduled</span>` },
        { k: "ip", t: "Pod IP", cls: "mono", v: (p) => p.ip, r: (p) => ip(p.ip) },
        { k: "cpu", t: "CPU", cls: "num", v: (p) => p.cpu, r: (p) => (p.cpu == null ? "–" : fmt.cores(p.cpu)) },
        { k: "mem", t: "Memory", cls: "num", v: (p) => p.mem, r: (p) => (p.mem == null ? "–" : fmt.bytes(p.mem)) },
        { k: "age", t: "Age", cls: "num", v: (p) => -p.created, r: (p) => ago(p.created) },
      ];
      setHTML($("#pods-table"), table("pods", cols, rows, { k: "ns", open: (p) => "pod:" + p.namespace + "/" + p.name, empty: "No pods match", emptySub: "Try clearing the filters." }));
      syncControls();
    },
  };

  // ----- Workloads
  const podsOfWorkload = (w) => S.d.pods.filter((p) => p.namespace === w.namespace && (() => { const o = p.owner.split("/")[1] || ""; return o === w.name || o.startsWith(w.name + "-"); })());
  V.workloads = {
    build() {
      NY.freshHTML($("#view"), html`<div class="toolbar"><input class="input" data-f="workloads.q" placeholder="Filter by name or image…" style="min-width:280px" aria-label="Filter workloads"><select class="select" data-f="workloads.ns" aria-label="Namespace"></select>
        ${seg("workloads.kind", [["", "All"], ["Deployment", "Deployments"], ["DaemonSet", "DaemonSets"], ["StatefulSet", "StatefulSets"], ["Job", "Jobs"], ["CronJob", "CronJobs"]])}<span class="grow"></span><span class="muted" id="wl-count"></span></div>
        <div class="card"><div id="wl-table"></div></div>`);
    },
    update() {
      const d = S.d, f = S.f.workloads, q = f.q.trim().toLowerCase();
      fillSelect($('[data-f="workloads.ns"]'), d.namespaces, "All namespaces");
      const rows = d.workloads.filter((w) => (!f.ns || w.namespace === f.ns) && (!f.kind || w.kind === f.kind) && (!q || (w.name + " " + w.images.join(" ")).toLowerCase().includes(q)));
      setHTML($("#wl-count"), html`${rows.length} of ${d.workloads.length}`);
      const cols = [
        { k: "name", t: "Name", v: (w) => w.name, r: (w) => html`<b>${w.name}</b>` },
        { k: "kind", t: "Kind", v: (w) => w.kind, r: (w) => chip(w.kind, "violet") },
        { k: "ns", t: "Namespace", v: (w) => w.namespace, r: (w) => chip(w.namespace) },
        { k: "ready", t: "Ready", cls: "barcell", v: (w) => frac(w.ready, w.desired), r: (w) => (w.kind === "CronJob" ? html`<span class="muted">${w.schedule}</span><small>${w.suspended ? "suspended" : w.last ? "last ran " + ago(w.last) + " ago" : "never ran"}</small>` : html`${bar(frac(w.ready, w.desired) == null ? 1 : frac(w.ready, w.desired), w.ready >= w.desired ? "good" : "warn")}<small>${w.ready} of ${w.desired}${w.failed ? " · " + w.failed + " failed" : ""}</small>`) },
        { k: "images", t: "Images", cls: "wrap mono", v: (w) => w.images.join(), r: (w) => w.images.map((i) => i.split("/").slice(-1)[0]).join(", ") || "–" },
        { k: "age", t: "Age", cls: "num", v: (w) => -w.created, r: (w) => ago(w.created) },
      ];
      setHTML($("#wl-table"), table("workloads", cols, rows, { k: "ns", open: (w) => "wl:" + w.namespace + "/" + w.name, empty: "No workloads match" }));
      syncControls();
    },
  };

  // ----- Network
  function reachable() {
    const out = [];
    S.d.services.forEach((s) => {
      if (!s.node_ports.length && s.type !== "LoadBalancer") return;
      const urls = [];
      s.node_ports.forEach((np) => S.d.nodes.forEach((n) => urls.push({ url: n.internal_ip + ":" + np, node: n.name })));
      out.push({ s, urls });
    });
    return out;
  }
  function addressBook() {
    const d = S.d, rows = [];
    d.nodes.forEach((n) => {
      n.addresses.filter((a) => a.type !== "Hostname").forEach((a) => rows.push({ kind: "node", type: "Node", name: n.name, ns: "", addr: a.address, ports: "", note: a.type + " · " + n.roles.join(", "), open: "node:" + n.name }));
      if (n.pod_cidr) rows.push({ kind: "node", type: "Pod range", name: n.name, ns: "", addr: n.pod_cidr, ports: "", note: "pods on " + n.name + " get addresses here", open: "node:" + n.name });
    });
    d.services.forEach((s) => {
      if (s.cluster_ip && s.cluster_ip !== "None") rows.push({ kind: "service", type: "Service", name: s.name, ns: s.namespace, addr: s.cluster_ip, ports: s.ports.join(", "), note: s.type + " · " + plural(s.endpoints, "endpoint"), open: "svc:" + s.namespace + "/" + s.name });
      s.external_ips.forEach((x) => rows.push({ kind: "service", type: "External IP", name: s.name, ns: s.namespace, addr: x, ports: s.ports.join(", "), note: s.type, open: "svc:" + s.namespace + "/" + s.name }));
    });
    d.ingresses.forEach((i) => i.hosts.forEach((h) => rows.push({ kind: "ingress", type: "Ingress", name: h, ns: i.namespace, addr: i.addresses.join(", "), ports: "80, 443", note: i.paths.join(", "), open: "" })));
    d.pods.forEach((p) => { if (p.ip) rows.push({ kind: "pod", type: p.host_network ? "Pod (host network)" : "Pod", name: p.name, ns: p.namespace, addr: p.ip, ports: "", note: p.node, open: "pod:" + p.namespace + "/" + p.name }); });
    return rows;
  }
  V.network = {
    build() {
      NY.freshHTML($("#view"), html`<div class="grid g-3" id="nw-top"></div><div class="mt" id="nw-reach"></div>
        <div class="section-title">Address book <span class="count" id="nw-count"></span></div>
        <div class="toolbar"><input class="input" data-f="network.q" placeholder="Search any IP address, name, port or namespace…" style="min-width:320px" aria-label="Search addresses">
        ${seg("network.kind", [["all", "Everything"], ["node", "Nodes"], ["service", "Services"], ["ingress", "Ingress"], ["pod", "Pods"]])}</div>
        <div class="card"><div id="nw-book"></div></div>`);
    },
    update() {
      const d = S.d, t = S.t, c = d.cluster;
      const kvl = (k, v) => html`<div class="row" style="justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--line)"><span class="muted">${k}</span><span>${v}</span></div>`;
      const traffic = d.nodes.map((n) => html`<div class="metric" style="grid-template-columns:70px 1fr auto"><span class="k">${n.name}</span>${bar(((n.net_rx_rate || 0) + (n.net_tx_rate || 0)) / Math.max(...d.nodes.map((x) => (x.net_rx_rate || 0) + (x.net_tx_rate || 0)), 1), "")}<span class="v" style="min-width:150px;font-size:12px">↓ ${fmt.rate(n.net_rx_rate)} ↑ ${fmt.rate(n.net_tx_rate)}</span></div>`);
      setHTML($("#nw-top"), html`
        ${card("Cluster addressing", html`${kvl("API server", ip(c.api_server ? c.api_server.replace(/^https?:\/\//, "") : ""))}${kvl("Pod network", ip(c.pod_cidr))}${kvl("Service network", ip(c.service_cidr))}${kvl("Nodes", d.nodes.length)}${kvl("Services", d.services.length)}${kvl("Ingress hosts", d.ingresses.reduce((s, i) => s + i.hosts.length, 0))}`)}
        ${card("Node addresses", d.nodes.map((n) => html`<div class="row" style="padding:7px 0;border-bottom:1px solid var(--line);cursor:pointer" data-open="node:${n.name}"><i class="dot ${n.ready ? "good" : "bad"}"></i><b>${n.name}</b><span class="grow"></span>${ip(n.internal_ip)}${n.external_ip ? ip(n.external_ip) : ""}</div>`))}
        ${card("Throughput", traffic, html`↓ ${fmt.rate(t.net_rx_rate)} · ↑ ${fmt.rate(t.net_tx_rate)}`)}`);

      const reach = reachable(), hosts = d.ingresses.filter((i) => i.hosts.length);
      setHTML($("#nw-reach"), card("Reach your services from your network", html`${reach.length ? reach.map(({ s, urls }) => html`<div style="padding:10px 0;border-bottom:1px solid var(--line)"><div class="row wrap"><b style="cursor:pointer" data-open="svc:${s.namespace}/${s.name}">${s.name}</b>${chip(s.namespace)}${chip(s.type, "violet")}<span class="muted" style="font-size:12.5px">${s.ports.join(", ")}</span></div>
        <div class="urls mt">${urls.length ? urls.map((u) => html`<span class="chip btnlike" data-copy="${u.url}" title="Copy ${u.url} (${u.node})"><span class="mono">${u.url}</span></span>`) : s.external_ips.map((x) => html`<span class="chip btnlike" data-copy="${x}"><span class="mono">${x}</span></span>`)}</div></div>`) : empty("No NodePort or LoadBalancer services", "Services of those types appear here with a ready-to-use address on every node.")}
        ${hosts.length ? html`<div style="padding-top:12px"><div class="muted" style="font-size:12px;margin-bottom:6px;text-transform:uppercase;letter-spacing:.07em;font-weight:600">Ingress hostnames</div><div class="urls">${hosts.flatMap((i) => i.hosts.map((h) => html`<span class="chip btnlike" data-copy="${h}"><span class="mono">${h}</span> <span class="faint">→ ${i.namespace}</span></span>`))}</div></div>` : ""}`, "click an address to copy it"));

      const f = S.f.network, q = f.q.trim().toLowerCase();
      const rows = addressBook().filter((r) => (f.kind === "all" || r.kind === f.kind) && (!q || (r.type + " " + r.name + " " + r.ns + " " + r.addr + " " + r.ports + " " + r.note).toLowerCase().includes(q)));
      setHTML($("#nw-count"), html`${rows.length}`);
      const cols = [
        { k: "type", t: "Type", v: (r) => r.type, r: (r) => chip(r.type, r.kind === "node" ? "accent" : r.kind === "service" ? "violet" : r.kind === "ingress" ? "info" : "") },
        { k: "name", t: "Name", v: (r) => r.name, r: (r) => html`<b>${r.name}</b>` },
        { k: "ns", t: "Namespace", v: (r) => r.ns, r: (r) => (r.ns ? chip(r.ns) : "") },
        { k: "addr", t: "Address", cls: "mono", v: (r) => (r.addr.match(/^\d+\.\d+\.\d+\.\d+/) ? r.addr.split("/")[0].split(".").map((x) => x.padStart(3, "0")).join(".") : r.addr), r: (r) => ip(r.addr) },
        { k: "ports", t: "Ports", cls: "mono wrap", v: (r) => r.ports, r: (r) => r.ports },
        { k: "note", t: "Details", cls: "wrap", v: (r) => r.note, r: (r) => html`<span class="muted">${r.note}</span>` },
      ];
      setHTML($("#nw-book"), table("book", cols, rows, { k: "type", open: (r) => r.open, limit: 400, empty: "No addresses match" }));
      syncControls();
    },
  };

  // ----- Storage
  V.storage = {
    build() { NY.freshHTML($("#view"), html`<div class="grid g-2" id="st-top"></div><div class="section-title">Volume claims</div><div class="card"><div id="st-pvc"></div></div><div class="section-title">Persistent volumes</div><div class="card"><div id="st-pv"></div></div>`); },
    update() {
      const d = S.d, t = S.t;
      const disk = d.nodes.filter((n) => n.disk_total);
      const pvcs = d.volumes.filter((v) => v.kind === "PVC"), pvs = d.volumes.filter((v) => v.kind === "PV");
      const reserved = pvcs.reduce((s, v) => s + v.capacity, 0);
      setHTML($("#st-top"), html`
        ${card("Disk space on each node", disk.length ? disk.map((n) => html`<div style="padding:8px 0"><div class="row" style="justify-content:space-between"><b>${n.name}</b><span class="muted" style="font-size:12.5px">${fmt.bytes(n.disk_used, 0)} of ${fmt.bytes(n.disk_total, 0)} · ${fmt.bytes(n.disk_total - n.disk_used, 0)} free</span></div>${bar(frac(n.disk_used, n.disk_total), null, true)}</div>`) : empty("Disk usage isn't reported", "It comes from each node's kubelet."), t.disk_total ? fmt.pct(frac(t.disk_used, t.disk_total)) + " used overall" : "")}
        ${card("Volumes", html`<div class="grid" style="grid-template-columns:repeat(3,1fr);gap:14px;text-align:center"><div><div style="font-size:26px;font-weight:700">${pvcs.length}</div><div class="muted">claims</div></div><div><div style="font-size:26px;font-weight:700">${pvs.length}</div><div class="muted">volumes</div></div><div><div style="font-size:26px;font-weight:700">${fmt.bytes(reserved, 0)}</div><div class="muted">requested</div></div></div>
          <div class="muted" style="margin-top:14px;font-size:12.5px">Local-path volumes live on the disk of the node that runs the pod, so the pod can't move to another node.</div>`)}`);
      const cols = [
        { k: "name", t: "Name", v: (v) => v.name, r: (v) => html`<b>${v.name}</b>` },
        { k: "ns", t: "Namespace", v: (v) => v.namespace, r: (v) => (v.namespace ? chip(v.namespace) : "") },
        { k: "status", t: "Status", v: (v) => v.status, r: (v) => statusChip(v.status) },
        { k: "cap", t: "Size", cls: "num", v: (v) => v.capacity, r: (v) => fmt.bytes(v.capacity, 0) },
        { k: "class", t: "Class", v: (v) => v.storage_class, r: (v) => v.storage_class },
        { k: "access", t: "Access", v: (v) => v.access.join(), r: (v) => v.access.join(", ") },
        { k: "vol", t: "Bound to", cls: "mono", v: (v) => v.volume || v.claim, r: (v) => v.volume || v.claim || "" },
      ];
      setHTML($("#st-pvc"), table("pvc", cols, pvcs, { empty: "No volume claims" }));
      setHTML($("#st-pv"), table("pv", cols, pvs, { empty: "No persistent volumes" }));
    },
  };

  // ----- Events
  V.events = {
    build() {
      NY.freshHTML($("#view"), html`<div class="toolbar"><input class="input" data-f="events.q" placeholder="Filter events…" style="min-width:280px" aria-label="Filter events"><select class="select" data-f="events.ns" aria-label="Namespace"></select>
        ${seg("events.type", [["", "All"], ["Warning", "Warnings"], ["Normal", "Normal"]])}<span class="grow"></span><span class="muted" id="ev-count"></span></div><div class="card"><div id="ev-table"></div></div>`);
    },
    update() {
      const d = S.d, f = S.f.events, q = f.q.trim().toLowerCase();
      fillSelect($('[data-f="events.ns"]'), d.namespaces, "All namespaces");
      const rows = d.events.filter((e) => (!f.type || e.type === f.type) && (!f.ns || e.namespace === f.ns) && (!q || (e.reason + " " + e.message + " " + e.object).toLowerCase().includes(q)));
      setHTML($("#ev-count"), html`${rows.length} of ${d.events.length}`);
      const cols = [
        { k: "last", t: "When", cls: "nowrap", v: (e) => -e.last, r: (e) => html`${ago(e.last)} ago` },
        { k: "type", t: "Type", v: (e) => e.type, r: (e) => chip(e.type, e.type === "Warning" ? "warn" : "") },
        { k: "reason", t: "Reason", v: (e) => e.reason, r: (e) => html`<b>${e.reason}</b>` },
        { k: "object", t: "Object", cls: "mono", v: (e) => e.object, r: (e) => e.object },
        { k: "ns", t: "Namespace", v: (e) => e.namespace, r: (e) => (e.namespace ? chip(e.namespace) : "") },
        { k: "message", t: "Message", cls: "wrap", v: (e) => e.message, r: (e) => e.message },
        { k: "count", t: "Count", cls: "num", v: (e) => e.count, r: (e) => e.count },
      ];
      setHTML($("#ev-table"), table("events", cols, rows, { k: "last", empty: "No events match" }));
      syncControls();
    },
  };

  // ----- Alerts
  V.alerts = {
    build() { NY.freshHTML($("#view"), html`<div id="al-body"></div>`); },
    update() {
      const al = S.res.alerts;
      if (!al.length) { setHTML($("#al-body"), html`<div class="card"><div class="empty"><svg class="icon" style="width:44px;height:44px;margin:0 auto 10px;color:var(--good)"><use href="#i-check"/></svg><b>No alerts</b>Every node is ready, nothing is crashing, and resources have room to spare.</div></div>`); return; }
      const group = (lvl, title) => { const g = al.filter((a) => a.level === lvl); return g.length ? html`<div class="section-title" style="margin-top:6px">${title} <span class="count">${g.length}</span></div>${g.map(alertCard)}` : ""; };
      setHTML($("#al-body"), html`<div class="card">${group("critical", "Critical")}${group("warning", "Warnings")}${group("info", "For your information")}</div>`);
    },
  };

  // ---------------------------------------------------------------- drawer
  function parseRef(ref) { const i = ref.indexOf(":"); return { kind: ref.slice(0, i), rest: ref.slice(i + 1) }; }
  function openDrawer(ref, noHash) {
    S.drawer = ref;
    if (S.d) connectStream();
    if (S.logs && S.logs.timer) clearInterval(S.logs.timer);
    S.logs = null;
    $("#scrim").classList.add("on");
    const dr = $("#drawer");
    dr.classList.add("on"); dr.setAttribute("aria-hidden", "false");
    setHTML(dr, html`<header id="dr-head"></header><div class="body"><div id="dr-main"></div><div id="dr-logs" class="mt"></div></div>`);
    if (!noHash) setRoute();
    updateDrawer(true);
  }
  function closeDrawer(noHash) {
    S.drawer = null;
    if (S.d) connectStream();
    if (S.logs && S.logs.timer) clearInterval(S.logs.timer);
    S.logs = null;
    $("#scrim").classList.remove("on");
    const dr = $("#drawer");
    dr.classList.remove("on"); dr.setAttribute("aria-hidden", "true");
    if (!noHash) setRoute();
  }
  const closeBtn = html`<button class="btn icon-only" data-act="close" aria-label="Close"><svg class="icon"><use href="#i-x"/></svg></button>`;
  const kv = (pairs) => html`<dl class="kv">${pairs.filter((p) => p[1] != null && p[1] !== "").map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>`;
  const chips = (obj) => { const e = Object.entries(obj || {}); return e.length ? html`<div class="row wrap" style="gap:6px">${e.map(([k, v]) => chip(k + (v === "" ? "" : "=" + v)))}</div>` : html`<span class="faint">none</span>`; };
  const sub = (t) => html`<h3 style="margin:22px 0 10px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em">${t}</h3>`;

  function updateDrawer(first) {
    if (!S.drawer || !S.d) return;
    const { kind, rest } = parseRef(S.drawer);
    let head = null, main = null, notFound = false;
    if (kind === "node") {
      const n = S.nodeBy[rest];
      if (!n) notFound = true; else {
        head = html`<i class="dot ${n.ready ? "good" : "bad"}" style="margin-top:9px"></i><div class="grow"><h2>${n.name}</h2><div class="sub">${n.os} · ${n.arch}</div></div>${closeBtn}`;
        const cpu = nodeSeries(0, n.name), mem = nodeSeries(1, n.name), pods = S.d.pods.filter((p) => p.node === n.name);
        const pcols = [{ k: "name", t: "Pod", v: (p) => p.name, r: (p) => html`<b>${p.name}</b>` }, { k: "ns", t: "Namespace", v: (p) => p.namespace, r: (p) => chip(p.namespace) }, { k: "status", t: "Status", v: (p) => p.status, r: (p) => statusChip(p.status) },
          { k: "cpu", t: "CPU", cls: "num", v: (p) => p.cpu, r: (p) => (p.cpu == null ? "–" : fmt.cores(p.cpu)) }, { k: "mem", t: "Memory", cls: "num", v: (p) => p.mem, r: (p) => (p.mem == null ? "–" : fmt.bytes(p.mem)) }];
        main = html`<div class="grid" style="grid-template-columns:repeat(3,1fr);gap:10px;text-align:center">
          <div class="card" style="padding:12px">${donut(frac(n.cpu_used, n.cpu_cores), { size: 78 })}<div class="muted" style="font-size:12px;margin-top:4px">CPU · ${cpuText(n)}</div></div>
          <div class="card" style="padding:12px">${donut(frac(n.mem_used, n.mem_total), { size: 78 })}<div class="muted" style="font-size:12px;margin-top:4px">Memory · ${memText(n)}</div></div>
          <div class="card" style="padding:12px">${n.disk_total ? donut(frac(n.disk_used, n.disk_total), { size: 78 }) : donut(0, { size: 78, label: "–" })}<div class="muted" style="font-size:12px;margin-top:4px">Disk · ${n.disk_total ? fmt.bytes(n.disk_used, 0) + " / " + fmt.bytes(n.disk_total, 0) : "n/a"}</div></div></div>
          <div class="grid g-2 mt" style="grid-template-columns:1fr">${card("CPU over time", area("n-cpu", cpu.ts, cpu.series, { max: n.cpu_cores, fmt: fmt.cores, height: 150, title: "CPU" }))}${card("Memory over time", area("n-mem", mem.ts, mem.series, { max: n.mem_total, fmt: (v) => fmt.bytes(v), height: 150, title: "Memory" }))}</div>
          ${n.hw ? html`<div class="grid g-2 mt" style="grid-template-columns:1fr">${(() => { const t = nodeSeries(4, n.name), f2 = nodeSeries(5, n.name); return html`${card("Temperature", area("n-temp", t.ts, t.series, { fmt: (v) => v.toFixed(0) + " °C", height: 130, title: "Temperature" }), n.hw.temp_c != null ? fmt.temp(n.hw.temp_c) : "")}${card("CPU clock", area("n-freq", f2.ts, f2.series, { fmt: (v) => fmt.mhz(v), left: 62, height: 130, title: "CPU clock", max: n.hw.freq_max || undefined }), fmt.mhz(n.hw.freq_mhz) + " of " + fmt.mhz(n.hw.freq_max))}`; })()}</div>
            ${sub("Memory breakdown")}${memBar(n.hw)}${(S.agents && S.agents.nodes && S.agents.nodes[n.name]) ? html`${sub("Biggest programs")}<div class="card" style="padding:4px">${table("nproc", [
              { k: "name", t: "Process", v: (p) => p.name, r: (p) => html`<b>${p.name}</b> <span class="faint mono" style="font-size:11.5px">${p.pid}</span>` },
              { k: "where", t: "Belongs to", v: (p) => p.group.name || p.group.kind, r: (p) => whereChip(p.group) },
              { k: "mem", t: "Memory", cls: "num", v: (p) => p.rss, r: (p) => fmt.bytes(p.rss) }, { k: "cpu", t: "CPU", cls: "num", v: (p) => p.cpu, r: (p) => p.cpu.toFixed(1) + "%" },
            ], S.agents.nodes[n.name].processes.slice(0, 10), { k: "mem", dir: -1, empty: "No data" })}</div>` : ""}` : ""}
          ${sub("Details")}${kv([["Status", statusChip(n.status)], ["Roles", n.roles.join(", ")], ["Internal IP", ip(n.internal_ip)], ["External IP", n.external_ip ? ip(n.external_ip) : ""], ["Pod range", ip(n.pod_cidr)],
            ["Kernel", n.kernel], ["Container runtime", n.runtime], ["Kubelet", n.kubelet], ["Allocatable", fmt.num(n.allocatable.cpu) + " cores · " + fmt.bytes(n.allocatable.memory) + " · " + n.allocatable.pods + " pods"],
            ["Network", "↓ " + fmt.rate(n.net_rx_rate) + " · ↑ " + fmt.rate(n.net_tx_rate)], ["Traffic since boot", n.net_rx != null ? "↓ " + fmt.bytes(n.net_rx) + " · ↑ " + fmt.bytes(n.net_tx) : ""], ["Images cached", n.images], ["Joined", ago(n.created) + " ago"], ["Kubelet up", n.kubelet_start ? ago(n.kubelet_start) : ""]])}
          ${sub("Conditions")}${chips(Object.fromEntries(Object.entries(n.conditions).map(([k, v]) => [k, v])))}${sub("Taints")}${n.taints.length ? html`<div class="row wrap" style="gap:6px">${n.taints.map((x) => chip(x, "warn"))}</div>` : html`<span class="faint">none</span>`}
          ${sub("Labels")}${chips(n.labels)}${sub("Pods on this node (" + pods.length + ")")}<div class="card" style="padding:4px">${table("npods", pcols, pods, { open: (p) => "pod:" + p.namespace + "/" + p.name, empty: "No pods" })}</div>`;
      }
    } else if (kind === "pod") {
      const p = S.podBy[rest];
      if (!p) notFound = true; else {
        head = html`<div class="grow"><h2>${p.name}</h2><div class="sub">${p.namespace} · ${p.node || "not scheduled"}</div></div>${statusChip(p.status)}${closeBtn}`;
        const ccols = [{ k: "n", t: "Container", r: (c) => html`<b>${c.name}</b>` }, { k: "i", t: "Image", cls: "mono wrap", r: (c) => c.image }, { k: "s", t: "State", r: (c) => html`${statusChip(c.reason || c.state.charAt(0).toUpperCase() + c.state.slice(1))}` }, { k: "r", t: "Restarts", cls: "num", r: (c) => c.restarts }];
        main = html`${kv([["Status", statusChip(p.status)], ["Ready", p.ready], ["Restarts", p.restarts], ["Node", p.node ? html`<a href="#" data-open="node:${p.node}">${p.node}</a>` : "–"], ["Pod IP", ip(p.ip)], ["Node IP", ip(p.host_ip)], ["Host network", p.host_network ? "yes (uses the node's address)" : ""], ["Owner", p.owner], ["QoS class", p.qos],
          ["CPU now", p.cpu == null ? "" : fmt.cores(p.cpu) + " cores"], ["Memory now", p.mem == null ? "" : fmt.bytes(p.mem)], ["Age", ago(p.created)]])}
          ${sub("Containers")}<div class="card" style="padding:4px">${table("ctrs", ccols, p.containers, {})}</div>${sub("Labels")}${chips(p.labels)}`;
        if (first) initLogs(p);
      }
    } else if (kind === "wl") {
      const i = rest.indexOf("/"), ns = rest.slice(0, i), name = rest.slice(i + 1), w = S.d.workloads.find((x) => x.namespace === ns && x.name === name);
      if (!w) notFound = true; else {
        head = html`<div class="grow"><h2>${w.name}</h2><div class="sub">${w.kind} · ${w.namespace}</div></div>${closeBtn}`;
        const pods = podsOfWorkload(w);
        const pcols = [{ k: "name", t: "Pod", v: (p) => p.name, r: (p) => html`<b>${p.name}</b>` }, { k: "node", t: "Node", v: (p) => p.node, r: (p) => p.node }, { k: "status", t: "Status", v: (p) => p.status, r: (p) => statusChip(p.status) }, { k: "ip", t: "IP", cls: "mono", v: (p) => p.ip, r: (p) => ip(p.ip) }, { k: "mem", t: "Memory", cls: "num", v: (p) => p.mem, r: (p) => (p.mem == null ? "–" : fmt.bytes(p.mem)) }];
        main = html`${kv([["Kind", w.kind], ["Ready", w.kind === "CronJob" ? "" : w.ready + " of " + w.desired], ["Schedule", w.schedule], ["Images", w.images.join(", ")], ["Age", ago(w.created)]])}
          ${sub("Pods (" + pods.length + ")")}<div class="card" style="padding:4px">${table("wpods", pcols, pods, { open: (p) => "pod:" + p.namespace + "/" + p.name, empty: "No pods" })}</div>`;
      }
    } else if (kind === "svc") {
      const i = rest.indexOf("/"), ns = rest.slice(0, i), name = rest.slice(i + 1), s = S.d.services.find((x) => x.namespace === ns && x.name === name);
      if (!s) notFound = true; else {
        head = html`<div class="grow"><h2>${s.name}</h2><div class="sub">Service · ${s.namespace}</div></div>${chip(s.type, "violet")}${closeBtn}`;
        const sel = Object.entries(s.selector || {}), pods = sel.length ? S.d.pods.filter((p) => p.namespace === ns && sel.every(([k, v]) => p.labels[k] === v)) : [];
        main = html`${kv([["Type", s.type], ["Cluster IP", ip(s.cluster_ip)], ["External IPs", s.external_ips.length ? s.external_ips.join(", ") : ""], ["Ports", s.ports.join(", ")], ["Endpoints", s.endpoints], ["Age", ago(s.created)]])}
          ${s.node_ports.length ? html`${sub("Reach it on your network")}<div class="urls">${s.node_ports.flatMap((np) => S.d.nodes.map((n) => html`<span class="chip btnlike" data-copy="${n.internal_ip}:${np}"><span class="mono">${n.internal_ip}:${np}</span></span>`))}</div>` : ""}
          ${sub("Selector")}${chips(s.selector)}${sub("Pods behind it (" + pods.length + ")")}<div class="row wrap" style="gap:6px">${pods.map((p) => html`<span class="chip btnlike" data-open="pod:${p.namespace}/${p.name}">${p.name} ${statusChip(p.status)}</span>`)}</div>`;
      }
    } else notFound = true;
    if (notFound) { setHTML($("#dr-head"), html`<div class="grow"><h2>Not found</h2><div class="sub">It may have been deleted.</div></div>${closeBtn}`); setHTML($("#dr-main"), html``); return; }
    setHTML($("#dr-head"), head);
    setHTML($("#dr-main"), main);
  }

  // ---- logs (pods only)
  function initLogs(p) {
    S.logs = { ns: p.namespace, pod: p.name, container: p.containers[0] ? p.containers[0].name : "", lines: 200, q: "", wrap: false, follow: false, text: null, error: "", loading: false, timer: null, containers: p.containers.map((c) => c.name) };
    renderLogs();
  }
  function renderLogs() {
    const L = S.logs; if (!L) return;
    setHTML($("#dr-logs"), html`<h3 style="margin:0 0 10px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em">Logs</h3>
      <div class="toolbar" style="margin-bottom:10px">${L.containers.length > 1 ? raw('<select class="select" id="lg-c" aria-label="Container">' + L.containers.map((c) => '<option' + (c === L.container ? " selected" : "") + ">" + esc(c) + "</option>").join("") + "</select>") : ""}
        <select class="select" id="lg-n" aria-label="Lines">${[100, 200, 500, 1000, 2000].map((n) => raw("<option" + (n === L.lines ? " selected" : "") + ' value="' + n + '">last ' + n + " lines</option>"))}</select>
        <input class="input grow" id="lg-q" placeholder="Filter lines…" value="${L.q}" aria-label="Filter log lines">
        <button class="btn small" data-act="logs-load"><svg class="icon" style="width:14px;height:14px"><use href="#i-refresh"/></svg>${L.text == null ? "Load logs" : "Reload"}</button>
        <button class="btn small ${L.follow ? "primary" : ""}" data-act="logs-follow">Follow</button><button class="btn small ${L.wrap ? "primary" : ""}" data-act="logs-wrap">Wrap</button>
        <button class="btn small" data-act="logs-copy">Copy</button><button class="btn small" data-act="logs-save">Save</button></div><div id="lg-box"></div>`);
    renderLogBox();
  }
  function highlight(line, q) {
    if (!q) return esc(line);
    const lo = line.toLowerCase(), ql = q.toLowerCase();
    let out = "", i = 0, j;
    while ((j = lo.indexOf(ql, i)) >= 0) { out += esc(line.slice(i, j)) + "<mark>" + esc(line.slice(j, j + q.length)) + "</mark>"; i = j + q.length; }
    return out + esc(line.slice(i));
  }
  function renderLogBox() {
    const L = S.logs, box = $("#lg-box"); if (!L || !box) return;
    if (L.loading && L.text == null) return setHTML(box, html`<div class="logbox">Loading…</div>`);
    if (L.error) return setHTML(box, html`<div class="logbox"><span class="err">${L.error}</span></div>`);
    if (L.text == null) return setHTML(box, html`<div class="logbox faint">Press “Load logs” to read this container's recent output.</div>`);
    const q = L.q.trim(), lines = L.text.replace(/\n$/, "").split("\n").filter((l) => !q || l.toLowerCase().includes(q.toLowerCase()));
    const body = lines.map((l) => {
      const m = l.match(/^(\d{4}-\d\d-\d\dT[\d:.]+Z)\s(.*)$/), ts = m ? m[1] : "", msg = m ? m[2] : l;
      const cls = /\b(error|fatal|panic|exception|fail(ed)?)\b/i.test(msg) ? "err" : /\bwarn(ing)?\b/i.test(msg) ? "wrn" : "";
      return (ts ? '<span class="ts">' + esc(ts.slice(11, 19)) + "</span> " : "") + (cls ? '<span class="' + cls + '">' : "") + highlight(msg, q) + (cls ? "</span>" : "");
    }).join("\n");
    box.innerHTML = '<div class="logbox' + (L.wrap ? " wrap" : "") + '" id="lg-pre">' + (body || '<span class="faint">No lines match.</span>') + "</div>";
    const pre = $("#lg-pre"); if (pre && L.follow) pre.scrollTop = pre.scrollHeight;
  }
  async function loadLogs(silent) {
    const L = S.logs; if (!L) return;
    if (!silent) { L.loading = true; L.error = ""; renderLogBox(); }
    try {
      const r = await getJSON("/api/logs?ns=" + encodeURIComponent(L.ns) + "&pod=" + encodeURIComponent(L.pod) + "&container=" + encodeURIComponent(L.container) + "&lines=" + L.lines);
      if (S.logs !== L) return;
      if (r.ok) { L.text = r.text; L.error = ""; } else L.error = r.error || "Couldn't read the logs.";
    } catch (e) { L.error = "Couldn't reach the dashboard server."; }
    L.loading = false;
    renderLogs();
  }

  // ---------------------------------------------------------------- chrome, routing, search
  function buildNav() {
    setHTML($("#nav"), VIEWS.map((v) => raw('<a href="#' + v.id + '" data-view="' + v.id + '"><svg class="icon"><use href="#i-' + v.icon + '"/></svg>' + esc(v.label) + '<span class="n" id="nav-n-' + v.id + '"></span></a>')));
  }
  function renderChrome() {
    const v = VIEWS.find((x) => x.id === S.view);
    $$("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.view === S.view));
    $("#title").textContent = v.label;
    $("#subtitle").textContent = S.d ? v.sub : "";
    const th = store.get("theme", "auto");
    $("#theme use").setAttribute("href", "#i-" + (["light", "solarized-light", "rose", "paper"].includes(th) ? "sun" : "moon"));
    $("#theme").title = "Theme: " + th + " (t)";
    const live = $("#live"); live.setAttribute("aria-checked", S.live ? "true" : "false");
    $("#interval").value = String(S.interval);
    $("#interval").classList.toggle("hide", streamHealthy());
    $("#refresh-label").textContent = streamHealthy() ? "Real time" : "Refresh";
    $("#signout").classList.toggle("hide", !(S.res && S.res.auth));
    if (S.d) {
      const set = (id, n, cls) => { const el = $("#nav-n-" + id); if (el) { el.textContent = n || ""; el.className = "n " + (cls || ""); } };
      const al = S.res.alerts.filter((a) => a.level !== "info"), crit = al.some((a) => a.level === "critical");
      set("nodes", S.t.nodes); set("pods", S.t.pods); set("workloads", S.d.workloads.length); set("alerts", al.length, crit ? "bad" : "warn");
      $("#brand-sub").textContent = S.d.cluster.name;
      $("#ver").textContent = S.res.version ? "v" + S.res.version : "";
      document.title = (al.length ? "(" + al.length + ") " : "") + "nodeyard · " + S.d.cluster.name;
    }
    tickStatus();
  }
  function tickStatus() {
    const pill = $("#status"); if (!pill) return;
    let cls = "good", text = "Live";
    if (!S.online) { cls = "bad"; text = "Connection lost – retrying"; }
    else if (!S.d) { cls = "warn"; text = S.res && S.res.error ? "Waiting for the cluster" : "Connecting…"; }
    else {
      const age = nowSrv() - S.res.updated;
      if (age > Math.max(20, S.res.interval * 4)) { cls = "warn"; text = "Cluster data is " + fmt.dur(age) + " old"; }
      else if (!S.live) { cls = ""; text = "Paused · " + fmt.dur(age) + " old"; }
      else text = streamHealthy() ? "Live · real time" : "Live · " + fmt.dur(age) + " ago";
    }
    // Updated in place: replacing the dot every second restarted its pulsing ring, which looked like a loading circle jumping back to the start.
    let dot = pill.querySelector("i.dot"), label = pill.querySelector("span");
    if (!dot || !label) { pill.innerHTML = '<i class="dot"></i><span></span>'; dot = pill.querySelector("i.dot"); label = pill.querySelector("span"); }
    const want = "dot" + (cls ? " " + cls : "");
    if (dot.className !== want) dot.className = want;
    if (label.textContent !== text) label.textContent = text;
  }
  function banners() {
    const out = [];
    if (!S.online) out.push(html`<div class="banner bad"><svg class="icon"><use href="#i-warn"/></svg>Lost contact with the dashboard server. Retrying; the last data stays on screen.</div>`);
    if (S.res && S.res.error) out.push(html`<div class="banner warn"><svg class="icon"><use href="#i-warn"/></svg>The last read of the cluster failed: ${S.res.error}</div>`);
    if (S.res && S.res.mode === "demo") out.push(html`<div class="banner demo"><svg class="icon"><use href="#i-alerts"/></svg>Demo mode: this is a simulated cluster, not yours.</div>`);
    return html`${out}`;
  }
  function render() {
    renderChrome();
    const content = $("#content");
    if (!S.d) {
      if (!$("#view")) setHTML(content, html`<div id="banners"></div><div class="loading"><div><div class="spin"></div>${S.res && S.res.error ? "Waiting for the cluster: " + S.res.error : "Reading your cluster…"}</div></div>`);
      return;
    }
    if (!$("#view")) { setHTML(content, html`<div id="banners"></div><div id="view"></div>`); S.built = null; }
    setHTML($("#banners"), banners());
    const v = V[S.view], keep = $$(".tablewrap").map((el) => [el.scrollLeft, el.scrollTop]), rebuilt = S.built !== S.view;
    if (rebuilt) { v.build(); S.built = S.view; $("#view").classList.remove("fade"); void $("#view").offsetWidth; $("#view").classList.add("fade"); syncControls(); }
    v.update();
    syncControls();
    updateDrawer(false);
    (NY.ui.hooks || []).forEach((f) => { try { f(); } catch (e) { /* a side panel must never break the page */ } });
    if (!rebuilt) $$(".tablewrap").forEach((el, i) => { if (keep[i]) { el.scrollLeft = keep[i][0]; el.scrollTop = keep[i][1]; } });
  }
  function setRoute() {
    const h = "#" + S.view + (S.drawer ? "!" + encodeURIComponent(S.drawer) : "");
    if (location.hash !== h) history.replaceState(null, "", h);
  }
  function applyRoute() {
    const [v, d] = decodeURIComponent(location.hash.slice(1)).split("!");
    const id = VIEWS.some((x) => x.id === v) ? v : "overview";
    if (id !== S.view) { S.view = id; S.built = null; window.scrollTo(0, 0); }
    store.set("view", S.view);
    if (S.d) connectStream();
    if (d) { if (d !== S.drawer) { S.drawer = null; if (S.d) openDrawer(d, true); else S.pending = d; } } else if (S.drawer) closeDrawer(true);
    render();
  }
  function go(view, params) {
    if (params) Object.entries(params).forEach(([k, v]) => { setPath(view + "." + k, v); });
    if (location.hash === "#" + view) applyRoute(); else location.hash = "#" + view;
  }

  // ---- command palette
  let pSel = 0, pItems = [];
  function paletteItems(q) {
    const items = [];
    VIEWS.forEach((v) => items.push({ label: v.label, sub: v.sub, kind: "page", run: () => go(v.id), hay: v.label.toLowerCase() }));
    if (S.d) {
      S.d.nodes.forEach((n) => items.push({ label: n.name, sub: n.internal_ip + " · " + n.os, kind: "node", run: () => openDrawer("node:" + n.name), hay: (n.name + " " + n.internal_ip + " " + n.addresses.map((a) => a.address).join(" ")).toLowerCase() }));
      S.d.workloads.forEach((w) => items.push({ label: w.name, sub: w.kind + " · " + w.namespace, kind: "workload", run: () => openDrawer("wl:" + w.namespace + "/" + w.name), hay: (w.name + " " + w.namespace + " " + w.images.join(" ")).toLowerCase() }));
      S.d.services.forEach((s) => items.push({ label: s.name, sub: s.cluster_ip + " · " + s.namespace, kind: "service", run: () => openDrawer("svc:" + s.namespace + "/" + s.name), hay: (s.name + " " + s.namespace + " " + s.cluster_ip + " " + s.external_ips.join(" ") + " " + s.node_ports.join(" ")).toLowerCase() }));
      S.d.pods.forEach((p) => items.push({ label: p.name, sub: p.namespace + " · " + (p.ip || p.status), kind: "pod", run: () => openDrawer("pod:" + p.namespace + "/" + p.name), hay: (p.name + " " + p.namespace + " " + p.ip + " " + p.node).toLowerCase() }));
    }
    items.push({ label: "Switch theme", sub: "Dark, light or automatic", kind: "action", run: cycleTheme, hay: "switch theme dark light" }, { label: "Refresh now", sub: "Read the cluster again", kind: "action", run: () => load(true), hay: "refresh reload now" });
    const ql = q.trim().toLowerCase();
    return (ql ? items.filter((i) => ql.split(/\s+/).every((w) => i.hay.includes(w))) : items.filter((i) => i.kind === "page" || i.kind === "node" || i.kind === "action")).slice(0, 40);
  }
  function renderPalette() {
    pItems = paletteItems($("#pq").value);
    pSel = Math.min(pSel, Math.max(0, pItems.length - 1));
    setHTML($("#presults"), pItems.length ? pItems.map((it, i) => html`<div class="item ${i === pSel ? "sel" : ""}" data-pi="${i}"><div><div>${it.label}</div><div class="sub">${it.sub}</div></div><span class="kind">${it.kind}</span></div>`) : html`<div class="empty">Nothing matches</div>`);
    const sel = $(".item.sel", $("#presults")); if (sel) sel.scrollIntoView({ block: "nearest" });
  }
  function openPalette() { closeModals(); $("#scrim").classList.add("on"); $("#palette").classList.add("on"); const i = $("#pq"); i.value = ""; pSel = 0; renderPalette(); i.focus(); }
  function closeModals() { $("#palette").classList.remove("on"); $("#helpm").classList.remove("on"); $("#jobm").classList.remove("on"); clearInterval(jobTimer); if (!S.drawer) $("#scrim").classList.remove("on"); }
  function runPalette(i) { const it = pItems[i]; if (!it) return; closeModals(); it.run(); }

  // ---- misc actions
  function cycleTheme() {
    const order = ["auto", "dark", "light"], cur = store.get("theme", "auto"), next = order[(order.indexOf(cur) + 1) % 3];
    store.set("theme", next);
    if (next === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", next);
    renderChrome(); toast("Theme: " + next);
  }
  function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), 1600); }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); } catch (e) {
      const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (e2) { /* nothing more to try */ } ta.remove();
    }
    toast("Copied " + (text.length > 40 ? text.slice(0, 40) + "…" : text));
  }
  function setLive(on) { S.live = on; store.set("live", on ? "1" : "0"); if (on) { connectStream(); load(true); } else closeStream(); schedule(); renderChrome(); }
  function download(name, text) { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" })); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); }

  // ---- tasks (nodeyard commands the page can start): a modal that follows the output
  let jobTimer = null;
  async function startJob(action, params, onDone) {
    let r;
    try { r = await postJSON("/api/run", Object.assign({ action }, params || {})); } catch (e) { toast(e.message || "Couldn't reach the dashboard server."); return false; }
    if (!r.ok) { toast(r.error || "Couldn't start that."); return false; }
    openJob(r.job, onDone);
    return true;
  }
  function openJob(id, onDone) {
    let since = 0, lines = [];
    $("#scrim").classList.add("on");
    const m = $("#jobm");
    m.classList.add("on");
    setHTML($("#jobbody"), html`<div class="logbox" id="jobout">Starting…</div>`);
    clearInterval(jobTimer);
    const tick = async () => {
      let v;
      try { v = await getJSON("/api/job?id=" + encodeURIComponent(id) + "&since=" + since); } catch (e) { return; }
      if (!v.ok) { clearInterval(jobTimer); setHTML($("#jobout"), v.error || "Lost track of that task."); return; }
      since = v.next; lines = lines.concat(v.lines);
      $("#jobtitle").textContent = v.title;
      setHTML($("#jobstatus"), v.status === "running" ? chip("running…", "warn") : v.status === "ok" ? chip("done", "good") : chip("failed (exit " + v.rc + ")", "bad"));
      const out = $("#jobout");
      if (out) { out.textContent = lines.join("\n") || "(no output yet)"; out.scrollTop = out.scrollHeight; }
      if (v.status !== "running") { clearInterval(jobTimer); load(true); if (onDone) onDone(v); }
    };
    tick();
    jobTimer = setInterval(tick, 1200);
  }
  function closeJob() { clearInterval(jobTimer); $("#jobm").classList.remove("on"); if (!S.drawer) $("#scrim").classList.remove("on"); }

  function act(name, el) {
    const L = S.logs;
    if (name === "close") closeDrawer();
    else if (name === "job-close") closeJob();
    else if (name === "agent-install") startJob("agent-install");
    else if (name.startsWith("top:")) { S.topBy = name.slice(4); if (S.d) V.overview.update(); }
    else if (name === "logs-load") loadLogs(false);
    else if (name === "logs-wrap" && L) { L.wrap = !L.wrap; renderLogs(); }
    else if (name === "logs-follow" && L) { L.follow = !L.follow; clearInterval(L.timer); if (L.follow) { L.timer = setInterval(() => loadLogs(true), 3000); loadLogs(true); } renderLogs(); }
    else if (name === "logs-copy" && L && L.text != null) copyText(L.text);
    else if (name === "logs-save" && L && L.text != null) download(L.pod + ".log", L.text);
  }

  // ---------------------------------------------------------------- events
  document.addEventListener("click", (e) => {
    const t = e.target; let el;
    if ((el = t.closest("[data-copy]"))) { e.preventDefault(); e.stopPropagation(); copyText(el.dataset.copy); return; }
    if ((el = t.closest("[data-act]"))) { e.preventDefault(); act(el.dataset.act, el); return; }
    if ((el = t.closest("[data-set]"))) { const [p, v] = el.dataset.set.split(":"); if (p === "range") { S.range = +v; store.set("range", v); } else setPath(p, v); if (S.d) { V[S.view].update(); updateDrawer(false); } syncControls(); return; }
    if ((el = t.closest("th[data-sort]"))) { const [id, k] = el.dataset.sort.split("|"), st = S.sort[id] || (S.sort[id] = { k, dir: 1 }); if (st.k === k) st.dir = -st.dir; else { st.k = k; st.dir = 1; } V[S.view].update(); updateDrawer(false); return; }
    if ((el = t.closest("[data-open]"))) { e.preventDefault(); closeModals(); openDrawer(el.dataset.open); return; }
    if ((el = t.closest("[data-goto]"))) { const [view, qs] = el.dataset.goto.split("?"); const o = {}; if (qs) qs.split("&").forEach((kv2) => { const [k, v] = kv2.split("="); o[k] = decodeURIComponent(v); }); go(view, o); return; }
    if ((el = t.closest("[data-pi]"))) { runPalette(+el.dataset.pi); return; }
    if (t.closest("#scrim")) { closeModals(); closeDrawer(); return; }
  });
  document.addEventListener("input", (e) => {
    const t = e.target;
    if (t.dataset && t.dataset.f) { setPath(t.dataset.f, t.value); if (S.d) V[S.view].update(); }
    else if (t.id === "pq") { pSel = 0; renderPalette(); }
    else if (t.id === "lg-q" && S.logs) { S.logs.q = t.value; renderLogBox(); }
  });
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset && t.dataset.f) { setPath(t.dataset.f, t.value); if (S.d) V[S.view].update(); }
    else if (t.id === "lg-c" && S.logs) { S.logs.container = t.value; S.logs.text = null; renderLogs(); }
    else if (t.id === "lg-n" && S.logs) { S.logs.lines = +t.value; if (S.logs.text != null) loadLogs(false); }
    else if (t.id === "interval") { S.interval = +t.value; store.set("interval", t.value); schedule(); }
  });
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test((e.target.tagName || ""));
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    if ($("#palette").classList.contains("on")) {
      if (e.key === "Escape") closeModals();
      else if (e.key === "ArrowDown") { e.preventDefault(); pSel = Math.min(pItems.length - 1, pSel + 1); renderPalette(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); pSel = Math.max(0, pSel - 1); renderPalette(); }
      else if (e.key === "Enter") { e.preventDefault(); runPalette(pSel); }
      return;
    }
    if (e.key === "Escape") { if (typing) e.target.blur(); closeModals(); closeDrawer(); return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); openPalette(); }
    else if (e.key === "?") { closeModals(); $("#scrim").classList.add("on"); $("#helpm").classList.add("on"); }
    else if (/^[0-9]$/.test(e.key) && VIEWS[(+e.key + 9) % 10]) go(VIEWS[(+e.key + 9) % 10].id);
    else if (e.key === "r") load(true);
    else if (e.key === " ") { e.preventDefault(); setLive(!S.live); }
    else if (e.key === "t") cycleTheme();
  });
  window.addEventListener("hashchange", applyRoute);
  $("#searchbtn").addEventListener("click", openPalette);
  $("#refresh").addEventListener("click", () => load(true));
  $("#theme").addEventListener("click", cycleTheme);
  $("#helpbtn").addEventListener("click", () => { closeModals(); $("#scrim").classList.add("on"); $("#helpm").classList.add("on"); });
  $("#live").addEventListener("click", () => setLive(!S.live));
  $("#signout").addEventListener("click", async () => {
    try { await fetch("/api/logout", { method: "POST", headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: "{}" }); } catch (e) { /* leaving anyway */ }
    location.href = "/login";
  });

  NY.ui = { hooks: [], bar, tempClass, S, V, $, $$, store, chip, statusChip, ip, card, table, metric, seg, empty, ago, plural, frac, toast, copyText, getJSON, postJSON, setPath, getPath, syncControls,
    fillSelect, openDrawer, startJob, openJob, aiSummary, load, go };
  buildNav();
  const saved = store.get("view", "overview");
  if (!location.hash && VIEWS.some((v) => v.id === saved)) S.view = saved;
  applyRoute();
  setInterval(tickStatus, 1000);
  start().then(() => { if (S.pending) { openDrawer(S.pending, true); S.pending = null; } });
})();
