// The Tuning page: per-device AI settings, and CPU/GPU power, clock and overclock controls (tune.py does the work).
// Power changes are shown in full and need a "Run" before anything happens on the machine. Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, card, chip, toast, getJSON, postJSON, ago } = U;
  const TU = { data: null, timer: 0, open: {} };

  async function load() {
    try { const r = await getJSON("/api/tune"); if (r.ok) TU.data = r; } catch (e) { /* signed out */ }
    paint();
    clearTimeout(TU.timer);
    if (S.view === "tune" && TU.data && TU.data.nodes.some((n) => n.probing)) TU.timer = setTimeout(load, 2500);
  }
  async function post(url, body) {
    try { const r = await postJSON(url, body); if (!r.ok) { toast(r.error || "That didn't work."); return null; } return r; }
    catch (e) { toast(e.message || "That didn't work."); return null; }
  }
  const mhz = (khz) => (khz ? (khz / 1e6).toFixed(2) + " GHz" : "–");
  const gib = (b) => (b ? (b / 1073741824).toFixed(0) + " GB" : "–");

  function nodeCard(n) {
    const p = n.probe, ai = n.ai, cpu = n.cpu;
    const recommendedThreads = cpu.cores ? Math.max(1, cpu.cores - (n.roles.some((r) => /control-plane|master/.test(r)) ? 1 : 0)) : null;
    const tips = n.tips.length ? html`<div class="tu-tips">${n.tips.map((t, i) => html`<div class="tu-tip ${t.kind}">
        <div class="grow" style="min-width:0"><b>${t.what}</b><div class="muted small">${t.why}</div></div>
        ${t.fix ? html`<button class="btn small primary" data-tu-fix="${n.node}" data-i="${i}">${t.fix.ai ? "Save" : "Apply…"}</button>` : ""}</div>`)}</div>`
      : html`<div class="muted small">${p ? "Nothing to change: this machine is set up well for AI." : "Read this machine's settings for power and clock advice."}</div>`;
    const power = !p ? html`<div class="empty small">${n.probing ? html`<div class="spin"></div>Reading ${n.node}'s settings…` : n.probe_error ? html`<span class="bad-t">${n.probe_error}</span>` : "Not read yet."}
        <div class="mt"><button class="btn" data-tu-probe="${n.node}" ${n.probing || !n.ready ? "disabled" : ""}>${n.ready ? "Read settings" : "Node isn't ready"}</button></div></div>`
      : html`<div class="tu-grid" data-tu-form="${n.node}">
        ${p.governors && p.governors.length ? html`<label>CPU governor<select class="select" data-k="governor">${raw(p.governors.map((g) => '<option' + (g === p.governor ? " selected" : "") + ">" + esc(g) + "</option>").join(""))}</select>
          <span class="faint small">performance keeps the clock up between tokens</span></label>` : ""}
        ${p.hw_max ? html`<label>CPU speed limit <b data-tu-out="max">${Math.round((p.max || p.hw_max) / 1000)} MHz</b>
          <input type="range" data-k="max_mhz" min="${Math.round(p.hw_min / 1000)}" max="${Math.round(p.hw_max / 1000)}" step="100" value="${Math.round((p.max || p.hw_max) / 1000)}">
          <span class="faint small">${mhz(p.hw_min)} to ${mhz(p.hw_max)} (the hardware's own range)</span></label>` : ""}
        ${p.turbo != null ? html`<label class="row small"><input type="checkbox" data-k="turbo" ${p.turbo ? "checked" : ""}> Turbo boost</label>` : ""}
        ${p.swappiness != null ? html`<label>Swappiness <input class="input" type="number" min="0" max="100" data-k="swappiness" value="${p.swappiness}" style="width:90px">
          <span class="faint small">lower keeps the model in RAM (10 is good for AI)</span></label>` : ""}
        ${p.gpus.map((g) => html`<label>GPU ${g.index} power limit · ${g.name} <b data-tu-out="gpu${g.index}">${Math.round(g.power)} W</b>
          <input type="range" data-gpu="${g.index}" min="${Math.round(g.min)}" max="${Math.round(g.max)}" step="5" value="${Math.round(g.power)}">
          <span class="faint small">${Math.round(g.min)} to ${Math.round(g.max)} W · default ${Math.round(g.default)} W</span></label>`)}
        ${n.pi_presets.length ? html`<label>Raspberry Pi overclock<select class="select" data-k="pi_preset"><option value="">Leave as it is</option>${raw(n.pi_presets.map((x) => '<option value="' + esc(x.id) + '">' + esc(x.label) + "</option>").join(""))}</select>
          <span class="faint small">Needs the official power supply and a fan or heatsink; applies after a reboot. ${p.pi.throttled && p.pi.throttled !== "0x0" ? "This Pi has been throttled: fix power or cooling first." : ""}</span></label>` : ""}
        <label class="row small"><input type="checkbox" data-k="persist" ${p.persist ? "checked" : ""}> Keep after a reboot</label>
        <div class="row wrap"><button class="btn primary" data-tu-review="${n.node}">Review changes…</button><button class="btn" data-tu-restore="${n.node}">Restore defaults…</button>
          <button class="btn small" data-tu-probe="${n.node}" ${n.probing ? "disabled" : ""}>Read again</button></div>
        <div class="faint small">Read ${n.probe_at ? ago(n.probe_at) + " ago" : ""}${p.model ? " · " + p.model : ""}</div>
      </div>`;
    return html`<section class="card tu-node">
      <div class="row wrap" style="gap:8px;align-items:baseline"><h3 style="margin:0">${n.node}</h3>
        ${n.roles.filter((r) => r !== "worker").map((r) => chip(r, "accent"))}${n.ai.main ? chip("runs the model", "violet") : n.ai.share_mib ? chip("holds model layers", "violet") : ""}
        ${n.gpus.length ? chip("GPU", "info") : ""}${!n.ready ? chip("not ready", "warn") : ""}<span class="grow"></span>
        <span class="muted small">${cpu.model || n.arch}${cpu.cores ? " · " + cpu.cores + " cores / " + (cpu.threads || cpu.cores) + " threads" : ""}${cpu.freq_mhz ? " · " + Math.round(cpu.freq_mhz) + " MHz now" : ""}${cpu.temp_c != null ? " · " + Math.round(cpu.temp_c) + " °C" : ""}</span></div>
      <h4>Suggestions</h4>${tips}
      <h4>AI on this machine</h4>
      <div class="tu-grid" data-tu-ai="${n.node}">
        <label>Threads for the model <input class="input" type="number" min="1" max="256" data-a="threads" value="${ai.saved.threads || ""}" placeholder="${recommendedThreads ? "auto (" + recommendedThreads + " suggested)" : "auto"}" style="width:170px">
          <span class="faint small">${ai.rpc_threads ? "The running model uses " + ai.rpc_threads + "." : "Not running here now."}</span></label>
        <label>Memory to keep free (GiB) <input class="input" type="number" min="0" max="64" step="0.5" data-a="reserve_gib" value="${ai.saved.reserve_gib != null ? ai.saved.reserve_gib : ""}" placeholder="auto (1)" style="width:120px">
          <span class="faint small">${n.mem_total ? gib(n.mem_total) + " in this machine. " : ""}Less leaves more room for the model.</span></label>
        <div class="row"><button class="btn" data-tu-ai-save="${n.node}">Save</button><span class="faint small">Used the next time you run or switch a model in AI › Models.</span></div>
      </div>
      <h4>Power and overclock</h4>${power}
    </section>`;
  }
  function paint() {
    if (S.view !== "tune" || !$("#tu-root")) return;
    const d = TU.data;
    if (!d) { setHTML($("#tu-root"), html`<div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div>`); return; }
    if (document.activeElement && document.activeElement.closest && document.activeElement.closest("#tu-root input, #tu-root select")) return;   // (don't redraw what's being edited)
    setHTML($("#tu-root"), html`<div class="card tu-head"><div class="row wrap" style="justify-content:space-between;gap:10px"><div>
        <h2 style="margin:0">Tuning</h2><div class="muted small mt">Per-device settings for running AI, and power, clock and overclock controls. Nothing on a machine changes until you've seen exactly what will run and pressed Run. Every change is in the audit log.</div></div>
        <button class="btn" data-tu-probe="all">Read every machine</button></div>
        ${!d.agents ? html`<p class="warn-t small">Install the node agents (Processes page) for CPU and temperature details.</p>` : ""}</div>
      ${d.nodes.map(nodeCard)}`);
  }

  // ---- review, confirm, run ----
  function collect(node) {
    const f = $('[data-tu-form="' + node + '"]'), n = TU.data.nodes.find((x) => x.node === node), p = n.probe, out = {};
    const v = (k) => f.querySelector('[data-k="' + k + '"]');
    if (v("governor") && v("governor").value !== p.governor) out.governor = v("governor").value;
    if (v("max_mhz") && Math.abs(+v("max_mhz").value * 1000 - (p.max || p.hw_max)) > 50000) out.max_mhz = +v("max_mhz").value;
    if (v("turbo") && (v("turbo").checked ? 1 : 0) !== p.turbo) out.turbo = v("turbo").checked;
    if (v("swappiness") && +v("swappiness").value !== p.swappiness) out.swappiness = +v("swappiness").value;
    f.querySelectorAll("[data-gpu]").forEach((el) => { const g = p.gpus.find((x) => x.index === el.dataset.gpu); if (g && Math.abs(+el.value - g.power) >= 1) (out.gpu_power = out.gpu_power || {})[g.index] = +el.value; });
    if (v("pi_preset") && v("pi_preset").value) out.pi_preset = v("pi_preset").value;
    if (v("persist") && v("persist").checked && Object.keys(out).length) out.persist = true;
    return out;
  }
  async function review(node, settings) {
    if (!settings || !Object.keys(settings).length) { toast("Nothing has changed yet."); return; }
    const r = await post("/api/tune/apply", { node, settings, confirm: false }); if (!r) return;
    let dlg = document.getElementById("tu-dialog");
    if (!dlg) { dlg = document.createElement("div"); dlg.id = "tu-dialog"; document.body.appendChild(dlg); }
    const risky = settings.pi_preset && settings.pi_preset !== "stock";
    setHTML(dlg, html`<div class="tu-back" data-tu-close></div><div class="tu-box" role="dialog" aria-modal="true" aria-label="Review changes on ${node}">
      <h3 style="margin-top:0">Change ${node}?</h3>
      <ul>${r.plan.map((x) => html`<li>${x}</li>`)}</ul>
      ${risky ? html`<p class="warn-t small">Overclocking runs the chip faster than it was sold at. It can make the machine crash or not boot until the change is undone (remove the "nodeyard overclock" lines from config.txt; a backup is kept next to it). Only do it with good cooling and the official power supply.</p>` : ""}
      <details><summary class="small">What will run as root on ${node}</summary><pre class="hl-pre">${r.script}</pre></details>
      <div class="row wrap mt"><button class="btn primary" data-tu-run="${node}">Run on ${node}</button><button class="btn" data-tu-close>Cancel</button></div></div>`);
    dlg.dataset.node = node; dlg._settings = settings; dlg.hidden = false;
    dlg.querySelector("[data-tu-run]").focus();
  }
  async function run(node) {
    const dlg = document.getElementById("tu-dialog"), b = dlg.querySelector("[data-tu-run]");
    b.disabled = true; b.textContent = "Running…";
    const r = await post("/api/tune/apply", { node, settings: dlg._settings, confirm: true });
    dlg.hidden = true;
    if (r) toast("Done on " + node + ": " + r.done.join("; ") + ".");
    load();
  }
  document.addEventListener("click", async (e) => {
    const t = e.target;
    if (!t.closest || (!t.closest("#tu-root") && !t.closest("#tu-dialog"))) return;
    let b;
    if ((b = t.closest("[data-tu-close]"))) { document.getElementById("tu-dialog").hidden = true; return; }
    if ((b = t.closest("[data-tu-run]"))) { run(b.dataset.tuRun); return; }
    if ((b = t.closest("[data-tu-probe]"))) { b.disabled = true; const r = await post("/api/tune/probe", { node: b.dataset.tuProbe }); if (r) { TU.data = r; setTimeout(load, 1500); } return; }
    if ((b = t.closest("[data-tu-review]"))) { review(b.dataset.tuReview, collect(b.dataset.tuReview)); return; }
    if ((b = t.closest("[data-tu-restore]"))) { review(b.dataset.tuRestore, { restore: true }); return; }
    if ((b = t.closest("[data-tu-ai-save]"))) {
      const f = $('[data-tu-ai="' + b.dataset.tuAiSave + '"]');
      const r = await post("/api/tune/ai", { node: b.dataset.tuAiSave, threads: f.querySelector('[data-a="threads"]').value, reserve_gib: f.querySelector('[data-a="reserve_gib"]').value });
      if (r) { TU.data = r; toast("Saved. It's used the next time you run or switch a model."); document.activeElement.blur(); paint(); }
      return;
    }
    if ((b = t.closest("[data-tu-fix]"))) {
      const n = TU.data.nodes.find((x) => x.node === b.dataset.tuFix), tip = n && n.tips[+b.dataset.i]; if (!tip || !tip.fix) return;
      if (tip.fix.ai) { const r = await post("/api/tune/ai", Object.assign({ node: n.node, reserve_gib: n.ai.saved.reserve_gib }, tip.fix.ai)); if (r) { TU.data = r; toast("Saved for the next model run."); paint(); } }
      else review(n.node, tip.fix);
    }
  });
  document.addEventListener("input", (e) => {
    const t = e.target; if (!t.closest || !t.closest("#tu-root")) return;
    const f = t.closest("[data-tu-form]"); if (!f) return;
    if (t.dataset.k === "max_mhz") f.querySelector('[data-tu-out="max"]').textContent = t.value + " MHz";
    if (t.dataset.gpu != null) f.querySelector('[data-tu-out="gpu' + t.dataset.gpu + '"]').textContent = t.value + " W";
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const d = document.getElementById("tu-dialog"); if (d && !d.hidden) d.hidden = true; } });

  V.tune = {
    build() { NY.freshHTML($("#view"), html`<div id="tu-root"></div>`); paint(); load(); },
    update() { },
  };
})();
