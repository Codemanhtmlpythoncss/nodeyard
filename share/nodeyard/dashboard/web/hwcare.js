// Hardware › Maintenance, Cooling and Lid (hwcare.py): a maintenance guide per machine from measured facts, the fans and
// an idle fan setting where Linux may set them, and what a laptop does when its lid closes. Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { $, card, chip, toast, getJSON, postJSON, ago, empty } = U;
  const HC = { data: null, tab: "", timer: 0, edits: {}, open: {} };
  const DIFF = { Beginner: "good", Intermediate: "warn", Advanced: "bad" };

  async function load() {
    clearTimeout(HC.timer);
    try { const r = await getJSON("/api/hwcare"); if (r.ok) { HC.data = r; HC.at = Date.now(); } } catch (e) { /* signed out */ }
    paint();
    if (HC.tab && HC.data && HC.data.nodes.some((n) => n.probing)) HC.timer = setTimeout(load, 2500);
  }
  async function post(url, body) {
    try { const r = await postJSON(url, body); if (!r.ok) { toast(r.error || "That didn't work."); return null; } return r; }
    catch (e) { toast(e.message || "That didn't work."); return null; }
  }
  const checkBtn = (n) => raw('<button class="btn small" data-hc-probe="' + esc(n.node) + '"' + (n.probing ? " disabled" : "") + ">" + (n.probing ? "Checking…" : n.probe_at ? "Check again" : "Check this machine") + "</button>");
  const checked = (n) => (n.probe_error ? html`<span class="err small">${n.probe_error}</span>` : n.probe_at ? html`<span class="faint small">checked ${ago(n.probe_at)} ago</span>` : html`<span class="faint small">not checked yet</span>`);
  const head = (n, extra) => html`<div class="row wrap" style="gap:8px;margin-bottom:10px">${checkBtn(n)} ${checked(n)}<span class="grow"></span>${extra || ""}</div>`;

  // ---- maintenance
  function taskHTML(t, key) {
    const open = HC.open[key] ? " open" : "";
    return raw('<details class="hc-task"' + open + ' data-hc-key="' + esc(key) + '"><summary><b>' + esc(t.title) + "</b> " + '<span class="chip ' + (DIFF[t.difficulty] || "") + '">' + esc(t.difficulty) + "</span></summary>" +
      '<div class="hc-body"><p><span class="muted">Why: </span>' + esc(t.why) + "</p>" +
      (t.steps.length ? "<ol>" + t.steps.map((s) => "<li>" + esc(s) + "</li>").join("") + "</ol>" : "") +
      (t.tools.length ? '<p><span class="muted">Tools and supplies: </span>' + esc(t.tools.join(", ")) + "</p>" : "") +
      '<p><span class="muted">Expected benefit: </span>' + esc(t.benefit) + "</p>" +
      '<p><span class="muted">Risks and precautions: </span>' + esc(t.risks) + "</p>" +
      '<p class="faint small">Based on: ' + esc(t.basis) + "</p></div></details>");
  }
  function maintCard(n) {
    const g = n.guide;
    const facts = g.facts.length ? html`<div class="tablewrap"><table class="tbl"><tbody>${g.facts.map((f) => html`<tr><td class="muted" style="white-space:nowrap">${f.k}</td><td class="wrap">${f.v}</td><td class="faint small wrap">${f.source}</td></tr>`)}</tbody></table></div>` : "";
    const tasks = g.tasks.length ? html`<div class="section-title">What to do</div>${g.tasks.map((t, i) => taskHTML(t, n.node + ":" + i))}` : "";
    const unknown = g.unknown.length ? html`<div class="section-title">Not known yet</div><ul class="small">${g.unknown.map((u) => html`<li>${u}</li>`)}</ul>` : "";
    return card(html`${n.node}${g.kind ? html` ${chip(g.kind)}` : ""}`, html`${head(n)}<div class="section-title" style="margin-top:0">Measured</div>${facts}${tasks}${unknown}`);
  }

  // ---- cooling
  const FIELDS = [["idle_c", "Idle up to", "°C", 25, 85], ["idle_pct", "Idle fan speed", "%", 0, 100], ["high_c", "Full speed from", "°C", 30, 89], ["emergency_c", "Emergency (firmware takes over)", "°C", 40, 90], ["min_pct", "Never below", "%", 0, 100]];
  function coolCard(n) {
    const f = n.fan;
    const now = html`${(f.fans || []).length ? html`<div>${f.fans.map((x) => chip((x.label || x.fan) + " " + (x.rpm == null ? "?" : x.rpm) + " RPM", x.rpm === 0 ? "warn" : ""))}</div>` : ""}
      ${(f.cooling || []).length ? html`<div class="mt">${f.cooling.map((c) => chip(c.type + " " + c.cur + "/" + c.max))}</div>` : ""}
      ${f.temp_now != null ? html`<div class="muted small mt">Hottest sensor now ${NY.fmt.temp(f.temp_now)}.${(f.trips || []).length ? " Kernel limits: " + f.trips.map((t) => t.zone + " " + t.kind + " " + Math.round(t.c) + " °C").join(", ") + "." : ""}</div>` : ""}`;
    const mode = { hwmon: chip("Linux can set it", "good"), pi5: chip("firmware settings (config.txt)", "good"), monitor: chip("firmware runs it"), none: chip("no fan seen"), unknown: chip("not checked") }[f.mode] || "";
    let form = "";
    if (f.mode === "hwmon" || f.mode === "pi5") {
      const e = HC.edits[n.node] || (HC.edits[n.node] = Object.assign({}, f.defaults, f.current || {}, { persist: true }));
      const inp = ([k, label, unit, lo, hi]) => raw('<label class="hc-field"><span class="muted small">' + esc(label) + '</span><span class="row" style="gap:4px"><input type="number" min="' + lo + '" max="' + hi + '" data-hc-edit="' + esc(n.node) + '" data-k="' + k + '" value="' + esc(e[k]) + '" style="width:72px"> ' + unit + "</span></label>");
      const channels = f.mode === "hwmon" ? html`<label class="hc-field"><span class="muted small">Fan</span><select data-hc-edit="${n.node}" data-k="channel">${f.channels.map((c) => raw('<option value="' + esc(c.chip + "/" + c.pwm) + '"' + (e.channel === c.chip + "/" + c.pwm ? " selected" : "") + ">" + esc(c.chip + " " + c.pwm) + "</option>"))}</select></label>
        <label class="hc-field"><span class="muted small">Follows</span><select data-hc-edit="${n.node}" data-k="sensor">${(f.sensors || []).map((s) => raw('<option value="' + esc(s.chip + "/" + s.temp) + '"' + (e.sensor === s.chip + "/" + s.temp ? " selected" : "") + ">" + esc(s.chip + " " + (s.label || s.temp)) + "</option>"))}</select></label>` : "";
      form = html`<div class="section-title">Idle profile ${f.ours ? chip("nodeyard's settings in use", "accent") : chip("firmware default")}</div>
        <div class="row wrap" style="gap:10px;align-items:flex-end">${FIELDS.map(inp)}${channels}</div>
        ${f.mode === "hwmon" ? raw('<label class="row small mt" style="gap:6px"><input type="checkbox" data-hc-edit="' + esc(n.node) + '" data-k="persist"' + (e.persist ? " checked" : "") + "> Keep after reboots</label>") : ""}
        <div class="row wrap mt" style="gap:6px"><button class="btn small primary" data-hc-fan="${n.node}">Review and apply</button>
          <button class="btn small" data-hc-fan-defaults="${n.node}">Defaults</button>${f.ours || f.mode === "pi5" ? raw('<button class="btn small" data-hc-fan-restore="' + esc(n.node) + '">Give the fan back to the firmware</button>') : ""}</div>
        <div class="faint small mt">Above the emergency temperature${f.mode === "hwmon" ? ", or if the sensor can't be read, the fan goes back to the firmware's own control at full speed" : " the Pi runs its fan at full speed"}. The CPU's own thermal throttling and shutdown limits always apply; nothing here can turn them off.</div>`;
    }
    return card(html`${n.node} ${mode}`, html`${head(n)}${now}<p class="small">${f.why}</p>${form}`);
  }

  // ---- the lid
  function lidCard(n) {
    const l = n.lid;
    if (!l.supported) return card(n.node, html`${head(n)}<p class="muted small" style="margin:0">${l.why}</p>${(l.conflicts || []).length ? html`<ul class="small">${l.conflicts.map((c) => html`<li>${c}</li>`)}</ul>` : ""}`);
    const e = HC.edits["lid:" + n.node] || (HC.edits["lid:" + n.node] = { battery: l.live.HandleLidSwitch || l.defaults.battery, ac: l.live.HandleLidSwitchExternalPower || l.defaults.ac, docked: l.live.HandleLidSwitchDocked || l.defaults.docked });
    const sel = (k, label) => raw('<label class="hc-field"><span class="muted small">' + esc(label) + '</span><select data-hc-lid="' + esc(n.node) + '" data-k="' + k + '">' +
      Object.entries(l.labels).map(([a, t]) => '<option value="' + a + '"' + (e[k] === a ? " selected" : "") + (l.actions[a] ? "" : " disabled") + ">" + esc(t) + (l.actions[a] ? "" : " (not available)") + "</option>").join("") + "</select></label>");
    const keep = [e.battery, e.ac, e.docked].includes("ignore");
    return card(html`${n.node} ${chip(l.manager)}`, html`${head(n)}
      <div class="row wrap" style="gap:6px">${l.lid ? chip("lid " + l.lid) : ""}${l.ac != null ? chip(l.ac === "1" ? "on mains power" : "on battery", l.ac === "1" ? "good" : "warn") : ""}${l.battery ? chip("battery " + (l.battery.capacity != null ? l.battery.capacity + "%" : l.battery.status)) : ""}</div>
      <div class="muted small mt">Now: on battery ${l.labels[l.live.HandleLidSwitch] || l.live.HandleLidSwitch || "?"} · on power ${l.labels[l.live.HandleLidSwitchExternalPower] || l.live.HandleLidSwitchExternalPower || "?"} · docked ${l.labels[l.live.HandleLidSwitchDocked] || l.live.HandleLidSwitchDocked || "?"}</div>
      <div class="row wrap mt" style="gap:10px">${sel("battery", "Lid closed on battery")}${sel("ac", "Lid closed on mains power")}${sel("docked", "Lid closed with a screen or dock")}</div>
      ${l.hibernate_why && l.hibernate_why.length ? html`<div class="faint small mt">Hibernate isn't offered: ${l.hibernate_why.join("; ")}.</div>` : ""}
      ${keep ? html`<div class="banner warn mt"><svg class="icon"><use href="#i-warn"/></svg><span>Running with the lid shut traps heat on many laptops (some breathe through the keyboard): keep its vents clear, never put it in a bag or on a bed running, and check its temperature on the node page. The battery's critical action${l.upower_critical ? " (" + l.upower_critical + ")" : ""} and the CPU's thermal limits still apply.</span></div>`
        : html`<div class="faint small mt">Suspend, hibernate and shut down take this machine out of the cluster until it's opened again: its pods move or stop.</div>`}
      ${(l.conflicts || []).length ? html`<div class="section-title">Conflicts</div><ul class="small">${l.conflicts.map((c) => html`<li>${c}</li>`)}</ul>` : ""}
      <div class="row wrap mt" style="gap:6px"><button class="btn small primary" data-hc-lid-apply="${n.node}">Review and apply</button>${l.ours ? raw('<button class="btn small" data-hc-lid-restore="' + esc(n.node) + '">Restore defaults</button>') : ""}</div>`);
  }

  function paint() {
    const root = $("#hw-body"); if (!root || !HC.tab) return;
    const d = HC.data;
    if (!d) { setHTML(root, html`<div class="muted"><span class="spin small"></span> Loading…</div>`); return; }
    const intro = { maint: "A guide for each machine from what it measures: what to clean, check, upgrade or leave alone, with steps, tools, difficulty, benefit and risks.",
      cool: "Fan speeds, and an idle fan setting on machines whose fans Linux may control. Overheating protection always comes first.",
      lid: "What a laptop does when its lid closes, on battery and on mains power." }[HC.tab];
    const list = HC.tab === "lid" ? d.nodes.slice().sort((a, b) => (b.lid.laptop ? 1 : 0) - (a.lid.laptop ? 1 : 0)) : d.nodes;
    const cardFor = HC.tab === "maint" ? maintCard : HC.tab === "cool" ? coolCard : lidCard;
    setHTML(root, html`<div class="card"><div class="row wrap" style="gap:10px"><span class="muted small grow">${intro} Checking reads the machine as root (read-only) through a short-lived helper, about 10 s each.</span>
        <button class="btn primary" data-hc-probe="all">Check every machine</button></div></div>
      ${d.demo ? html`<div class="banner info mt"><svg class="icon"><use href="#i-help"/></svg><span>Demo: these machines are made up and nothing runs anywhere.</span></div>` : ""}
      ${list.length ? list.map((n) => html`<div class="mt">${cardFor(n)}</div>`) : empty("No machines", "")}`);
  }

  function review(url, body, title) {
    return post(url, Object.assign({}, body, { confirm: false })).then((r) => {
      if (!r) return;
      let dlg = document.getElementById("hc-dialog");
      if (!dlg) { dlg = document.createElement("div"); dlg.id = "hc-dialog"; document.body.appendChild(dlg); }
      setHTML(dlg, html`<div class="tu-back" data-hc-close></div><div class="tu-box" role="dialog" aria-modal="true" aria-label="${title}">
        <h3 style="margin-top:0">${title}</h3><ul>${r.plan.map((x) => html`<li>${x}</li>`)}</ul>
        <details><summary class="small">What will run as root on ${body.node}</summary><pre class="hl-pre">${r.script}</pre></details>
        <div class="row wrap mt"><button class="btn primary" data-hc-run>Run on ${body.node}</button><button class="btn" data-hc-close>Cancel</button></div></div>`);
      dlg._req = { url, body }; dlg.hidden = false;
    });
  }
  async function run() {
    const dlg = document.getElementById("hc-dialog"), b = dlg.querySelector("[data-hc-run]");
    b.disabled = true; b.textContent = "Running…";
    const r = await post(dlg._req.url, Object.assign({}, dlg._req.body, { confirm: true }));
    dlg.hidden = true;
    if (r) { toast(r.warning ? r.warning : (r.done || []).slice(-1)[0] || "Done."); delete HC.edits[dlg._req.body.node]; delete HC.edits["lid:" + dlg._req.body.node]; }
    load();
  }

  document.addEventListener("click", async (e) => {
    const t = e.target; if (!t.closest) return;
    let b;
    if (t.closest("#hc-dialog")) {
      if (t.closest("[data-hc-close]")) document.getElementById("hc-dialog").hidden = true;
      else if (t.closest("[data-hc-run]")) run();
      return;
    }
    if (!t.closest("#hw-body")) return;
    if ((b = t.closest("[data-hc-probe]"))) {
      b.disabled = true;
      const r = await post("/api/hwcare/probe", { node: b.dataset.hcProbe });
      if (r) { HC.data = r; paint(); setTimeout(load, 1500); }
    } else if ((b = t.closest("[data-hc-fan]"))) {
      const node = b.dataset.hcFan, ed = HC.edits[node] || {};
      review("/api/hwcare/fan", Object.assign({ node }, ed), "Change " + node + "'s fan?");
    } else if ((b = t.closest("[data-hc-fan-defaults]"))) {
      const node = b.dataset.hcFanDefaults, f = HC.data.nodes.find((x) => x.node === node).fan;
      HC.edits[node] = Object.assign({}, f.defaults, { persist: true }); paint();
    } else if ((b = t.closest("[data-hc-fan-restore]"))) review("/api/hwcare/fan", { node: b.dataset.hcFanRestore, restore: true }, "Give " + b.dataset.hcFanRestore + "'s fan back to the firmware?");
    else if ((b = t.closest("[data-hc-lid-apply]"))) { const node = b.dataset.hcLidApply; review("/api/hwcare/lid", Object.assign({ node }, HC.edits["lid:" + node] || {}), "Change what " + node + "'s lid does?"); }
    else if ((b = t.closest("[data-hc-lid-restore]"))) review("/api/hwcare/lid", { node: b.dataset.hcLidRestore, restore: true }, "Restore " + b.dataset.hcLidRestore + "'s lid defaults?");
  });
  document.addEventListener("toggle", (e) => { const d = e.target; if (d.classList && d.classList.contains("hc-task")) HC.open[d.dataset.hcKey] = d.open; }, true);
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset && t.dataset.hcEdit) { const ed = HC.edits[t.dataset.hcEdit] || (HC.edits[t.dataset.hcEdit] = {}); ed[t.dataset.k] = t.type === "checkbox" ? t.checked : t.type === "number" ? +t.value : t.value; }
    if (t.dataset && t.dataset.hcLid) { const k = "lid:" + t.dataset.hcLid, ed = HC.edits[k] || (HC.edits[k] = {}); ed[t.dataset.k] = t.value; paint(); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const d = document.getElementById("hc-dialog"); if (d && !d.hidden) d.hidden = true; } });

  // The Hardware page asks for a tab: "" is its own Machines view
  NY.hwcare = { show(tab) { HC.tab = tab; paint(); if (!HC.data || Date.now() - (HC.at || 0) > 30000) load(); }, hide() { HC.tab = ""; clearTimeout(HC.timer); } };
})();
