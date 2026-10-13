// Processes › Free up memory: processes over 5 MB a cluster node doesn't need, moved to swap or stopped (memfree.py).
// Every change is shown in full (what will happen and the exact script) and needs a "Run". Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML, fmt, bar } = NY;
  const U = NY.ui;
  const { S, chip, toast, getJSON, postJSON, ago } = U;
  const MF = { data: null, at: 0, busy: false, showNeeded: false };

  async function load(force) {
    if (MF.busy || (!force && Date.now() - MF.at < 5000)) return;
    MF.busy = true;
    try { const r = await getJSON("/api/memory"); if (r.ok) { MF.data = r; MF.at = Date.now(); } } catch (e) { /* signed out */ }
    MF.busy = false;
    if (S.view === "processes" && S.f.proc.mode === "free") U.queueRender();
  }
  async function post(url, body) {
    try { const r = await postJSON(url, body); if (!r.ok) { toast(r.error || "That didn't work."); return null; } return r; }
    catch (e) { toast(e.message || "That didn't work."); return null; }
  }
  const verdictChip = (p) => (p.verdict === "not-needed" ? chip("not needed", "good") : p.verdict === "maybe" ? chip("your call", "warn") : chip("needed"));

  function rows(list, node, d, withButtons) {
    return list.map((p) => html`<tr>
      <td class="wrap"><b>${p.name}</b> <span class="faint mono" style="font-size:11.5px">${p.pid}</span>
        <div class="sub mono" style="max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${p.cmd}">${p.cmd}</div></td>
      <td>${node}</td>
      <td class="wrap">${verdictChip(p)} <span class="small">${p.what}</span></td>
      <td class="barcell">${bar(d.mem_total ? p.rss / d.mem_total : null, "")}<small>${fmt.bytes(p.rss)}${p.swap ? " · " + fmt.bytes(p.swap) + " in swap" : ""}</small></td>
      <td class="num" style="white-space:nowrap">${withButtons ? html`
        ${p.can_swap ? html`<button class="btn small" data-mf-swap="${node}" data-pid="${p.pid}" data-start="${p.start == null ? "" : p.start}" ${d.swap_total ? "" : "disabled title=\"Set up swap on this machine first\""}>Move to swap</button>` : ""}
        ${p.can_stop ? html`<button class="btn small" data-mf-stop="${node}" data-pid="${p.pid}" data-start="${p.start == null ? "" : p.start}" data-unit="${p.unit}">Stop…</button>` : ""}` : ""}</td>
    </tr>`);
  }
  function section(title, sub, items, withButtons) {
    if (!items.length) return "";
    const total = items.reduce((s, x) => s + x.p.rss, 0);
    return html`<h4 style="margin:14px 0 4px">${title} <span class="muted small">${items.length} · ${fmt.bytes(total)}</span></h4><div class="muted small" style="margin-bottom:6px">${sub}</div>
      <div class="tablewrap"><table class="tbl"><thead><tr><th>Process</th><th>Machine</th><th>What it is</th><th>Memory</th><th></th></tr></thead>
      <tbody>${items.map((x) => rows([x.p], x.node, x.d, withButtons))}</tbody></table></div>`;
  }

  function paint(el) {
    load(false);
    const d = MF.data;
    if (!d) { setHTML(el, html`<div class="empty"><div class="spin"></div>Looking at every machine's processes…</div>`); return; }
    if (!d.nodes.length) { setHTML(el, html`<div class="empty"><b>No process lists yet</b>The node agents fill this in (Processes needs them installed).</div>`); return; }
    const f = S.f.proc, scope = d.nodes.filter((n) => !f.node || n.node === f.node), q = (f.q || "").trim().toLowerCase();
    const pick = (v) => [].concat(...scope.map((n) => n.procs.filter((p) => p.verdict === v && (!q || (p.name + " " + p.cmd + " " + p.what + " " + n.node).toLowerCase().includes(q))).map((p) => ({ p, node: n.node, d: n }))));
    const notNeeded = pick("not-needed"), maybe = pick("maybe"), needed = MF.showNeeded ? pick("needed") : [];
    const swapRow = scope.map((n) => {
      const sd = /SD card|eMMC/.test(n.disk_kind || "");
      return html`<div class="row wrap" style="gap:8px;align-items:center;padding:6px 0;border-top:1px solid var(--line)"><b style="min-width:120px">${n.node}</b>
        <span class="small">${n.mem_available != null ? fmt.bytes(n.mem_available) + " free of " + fmt.bytes(n.mem_total) : ""}</span>
        ${n.swap_total ? chip("swap " + fmt.bytes(n.swap_total - n.swap_free) + " / " + fmt.bytes(n.swap_total), "info")
          : html`${chip("no swap", "warn")}<button class="btn small" data-mf-zram="${n.node}">Set up zram${sd ? " (best for this SD card)" : ""}…</button>${sd ? "" : html`<button class="btn small" data-mf-file="${n.node}">Swap file…</button>`}`}
        ${n.native ? "" : html`<span class="faint small">(older node agent: moves its whole service group)</span>`}</div>`;
    });
    const stopped = (d.stopped || []).filter((s) => !f.node || s.node === f.node);
    setHTML(el, html`<div style="padding:12px 14px">
      <div class="muted small">Programs over 5 MB that a cluster node doesn't need. <b>Move to swap</b> pushes one program's memory out of RAM (it comes back by itself when used, so it's
        safe); <b>Stop</b> stops it (its service, if it has one). Kubernetes, the AI model, networking, SSH and nodeyard are never offered. Every change shows exactly what runs first.</div>
      <div class="mt">${swapRow}</div>
      ${section("Not needed on a cluster node", "Desktop, printing, Bluetooth, update checkers and the like. Stopping these is safe on a server.", notNeeded, true)}
      ${section("Your call", "Not on either list: check what each one is before stopping it. Moving to swap is always safe.", maybe, true)}
      ${!notNeeded.length && !maybe.length ? html`<div class="empty"><b>Nothing to free</b>Every program over 5 MB here is needed${q ? " (or filtered out)" : ""}.</div>` : ""}
      <label class="row small mt"><input type="checkbox" data-mf-needed ${MF.showNeeded ? "checked" : ""}> Show the needed ones too</label>
      ${section("Needed", "Kept as they are.", needed, false)}
      ${stopped.length ? html`<h4 style="margin:14px 0 4px">Stopped from here</h4>${stopped.map((s) => html`<div class="row wrap" style="gap:8px;padding:4px 0"><b>${s.unit}</b><span class="small">${s.node} · ${s.what} · ${fmt.bytes(s.rss)}${s.kept_off ? " · kept off at boot" : ""} · ${ago(s.at)} ago</span>
        <button class="btn small" data-mf-undo="${s.node}" data-unit="${s.unit}">Start again…</button></div>`)}` : ""}
      <div class="faint small mt">Updated ${MF.at ? Math.round((Date.now() - MF.at) / 1000) + " s ago" : "–"}</div></div>`);
  }

  // ---- review, confirm, run (the same dialog as Tuning's)
  async function review(url, body, title, extra) {
    const r = await post(url, Object.assign({}, body, { confirm: false })); if (!r) return;
    let dlg = document.getElementById("mf-dialog");
    if (!dlg) { dlg = document.createElement("div"); dlg.id = "mf-dialog"; document.body.appendChild(dlg); }
    setHTML(dlg, html`<div class="tu-back" data-mf-close></div><div class="tu-box" role="dialog" aria-modal="true" aria-label="${title}">
      <h3 style="margin-top:0">${title}</h3><ul>${r.plan.map((x) => html`<li>${x}</li>`)}</ul>${extra || ""}
      <details><summary class="small">What will run as root on ${body.node}</summary><pre class="hl-pre">${r.script}</pre></details>
      <div class="row wrap mt"><button class="btn primary" data-mf-run>Run on ${body.node}</button><button class="btn" data-mf-close>Cancel</button></div></div>`);
    dlg._req = { url, body }; dlg.hidden = false;
    dlg.querySelector("[data-mf-run]").focus();
  }
  async function run() {
    const dlg = document.getElementById("mf-dialog"), b = dlg.querySelector("[data-mf-run]");
    b.disabled = true; b.textContent = "Running…";
    const keep = dlg.querySelector("[data-mf-keep]");
    const body = Object.assign({}, dlg._req.body, { confirm: true }, keep ? { keep_off: keep.checked } : {});
    const r = await post(dlg._req.url, body);
    dlg.hidden = true;
    if (r) toast(r.done);
    MF.at = 0; setTimeout(() => load(true), 2500);
  }
  document.addEventListener("click", (e) => {
    const t = e.target; if (!t.closest) return;
    let b;
    if (t.closest("#mf-dialog")) {
      if (t.closest("[data-mf-close]")) document.getElementById("mf-dialog").hidden = true;
      else if (t.closest("[data-mf-run]")) run();
      else if ((b = t.closest("[data-mf-keep]"))) {
        // ticking "keep it off" changes the plan: ask again so the shown script is the one that runs
        const dlg = document.getElementById("mf-dialog"); const req = dlg._req;
        review(req.url, Object.assign({}, req.body, { keep_off: b.checked }), "Stop it?", keepBox(b.checked));
      }
      return;
    }
    if (!t.closest("#pr-table")) return;
    if ((b = t.closest("[data-mf-swap]"))) review("/api/memory/action", { node: b.dataset.mfSwap, pid: +b.dataset.pid, start: b.dataset.start, action: "swap" }, "Move it to swap?");
    else if ((b = t.closest("[data-mf-stop]"))) review("/api/memory/action", { node: b.dataset.mfStop, pid: +b.dataset.pid, start: b.dataset.start, action: "stop", keep_off: false }, "Stop it?", b.dataset.unit ? keepBox(false) : "");
    else if ((b = t.closest("[data-mf-undo]"))) review("/api/memory/undo", { node: b.dataset.mfUndo, unit: b.dataset.unit }, "Start it again?");
    else if ((b = t.closest("[data-mf-zram]"))) swapAsk(b.dataset.mfZram, "zram");
    else if ((b = t.closest("[data-mf-file]"))) swapAsk(b.dataset.mfFile, "file");
    else if (t.closest("[data-mf-needed]")) { MF.showNeeded = t.closest("[data-mf-needed]").checked; U.queueRender(); }
  });
  const keepBox = (on) => html`<label class="row small" style="margin:0 0 10px"><input type="checkbox" data-mf-keep ${on ? "checked" : ""}> Keep it off after reboots</label>`;
  function swapAsk(node, kind) {
    const n = MF.data.nodes.find((x) => x.node === node); const ram = n && n.mem_total ? n.mem_total / 1073741824 : 4;
    // zram: half the RAM (at most 8 GB) holds about 1.5x that once compressed; a swap file: as much as the RAM, at most 8 GB
    const gib = Math.max(1, Math.min(8, Math.round(kind === "zram" ? ram / 2 : ram)));
    review("/api/memory/swap", { node, kind, gib }, kind === "zram" ? "Set up compressed swap?" : "Set up a swap file?");
  }

  NY.memfree = { paint, load };
})();
