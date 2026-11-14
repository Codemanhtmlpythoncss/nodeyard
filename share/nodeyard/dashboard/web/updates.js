// Updates (updates.py): outdated software and drivers on every machine, and the groups you choose to keep updated
// automatically. Checking reads the machine as root (read-only) through a short-lived helper; applying an update, or
// turning automatic updates on, always shows the exact command first and needs a confirm. Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, card, chip, toast, getJSON, postJSON, ago, empty } = U;
  const UP = { data: null, timer: 0 };
  const GROUP_LABEL = { drivers: "Drivers & firmware", kernel: "Kernel", security: "Security", other: "Other software" };
  const GROUP_HINT = {
    drivers: "GPU, firmware, microcode and DKMS modules.", kernel: "New Linux kernel images and headers (a reboot applies them).",
    security: "Fixes released through the security channel.", other: "Everything else with a newer version available.",
  };

  async function load() {
    clearTimeout(UP.timer);
    try { const r = await getJSON("/api/updates"); if (r.ok) UP.data = r; } catch (e) { /* signed out */ }
    paint();
    if (S.view === "updates" && UP.data && UP.data.nodes.some((n) => n.probing)) UP.timer = setTimeout(load, 2500);
  }
  async function post(url, body) {
    try { const r = await postJSON(url, body); if (!r.ok) { toast(r.error || "That didn't work."); return null; } return r; }
    catch (e) { toast(e.message || "That didn't work."); return null; }
  }

  const checkBtn = (n) => raw('<button class="btn small" data-up-check="' + esc(n.node) + '"' + (n.probing ? " disabled" : "") + ">" + (n.probing ? "Checking…" : n.checked ? "Check again" : "Check this machine") + "</button>");
  const checked = (n) => (n.probe_error ? html`<span class="err small">${n.probe_error}</span>` : n.probe_at ? html`<span class="faint small">checked ${ago(n.probe_at)} ago</span>` : html`<span class="faint small">not checked yet</span>`);

  function versions(n) {
    const bits = [];
    if (n.os) bits.push(["OS", n.os]);
    if (n.kernel) bits.push(["Kernel", n.kernel]);
    if (n.driver) bits.push(["NVIDIA driver", n.driver]);
    if (n.kubelet) bits.push(["Kubernetes", n.kubelet]);
    if (n.runtime) bits.push(["Container runtime", n.runtime]);
    return bits.length ? html`<div class="row wrap" style="gap:6px;margin:6px 0">${bits.map(([k, v]) => chip(k + ": " + v))}</div>` : "";
  }

  function pkgRow(p) {
    return html`<div class="row" style="padding:4px 0;border-bottom:1px solid var(--line);gap:8px">
      <b class="mono">${p.name}</b>${p.security ? chip("security", "warn") : ""}<span class="grow"></span>
      <span class="faint small mono">${p.current || "–"}</span><span class="muted small">→</span><span class="small mono">${p.candidate}</span></div>`;
  }

  function groupBlock(n, g) {
    const items = (n.groups[g] || []);
    if (!items.length) return "";
    const on = (n.auto || []).indexOf(g) >= 0;
    return html`<div class="up-group mt">
      <div class="row wrap" style="gap:8px;align-items:center">
        <b>${GROUP_LABEL[g]}</b>${chip(items.length)}<span class="muted small grow">${GROUP_HINT[g]}</span>
        <button class="btn small primary" data-up-apply="${n.node}" data-up-group="${g}">Update now</button>
        <label class="row small" style="gap:5px;cursor:pointer" title="Keep this group up to date automatically on ${n.node}">
          <input type="checkbox" data-up-auto="${n.node}" data-up-group="${g}"${on ? " checked" : ""}> automatic</label>
      </div>
      <div class="mt">${items.map(pkgRow)}</div></div>`;
  }

  function autoLine(n) {
    if (!n.auto || !n.auto.length) return "";
    const names = n.auto.map((g) => GROUP_LABEL[g]).join(", ");
    const when = n.auto_last ? "last run " + ago(n.auto_last) + " ago" + (n.auto_last_result ? " (" + n.auto_last_result + ")" : "") : "hasn't run yet";
    if (n.auto_paused) {
      return html`<div class="banner warn mt"><svg class="icon"><use href="#i-warn"/></svg><span>Automatic updates for <b>${names}</b> are paused because the groups changed. Tick them again to review and confirm.</span></div>`;
    }
    return html`<div class="note good mt">Automatic updates on for <b>${names}</b> — up to once every ${UP.data.auto_interval_h} h, ${when}.</div>`;
  }

  function nodeCard(n) {
    const body = html`
      <div class="row wrap" style="gap:8px;margin-bottom:6px">${checkBtn(n)} ${checked(n)}
        ${n.refresh_failed ? html`<span class="err small">couldn't refresh the package list (offline?)</span>` : ""}</div>
      ${versions(n)}
      ${!n.checked ? html`<div class="muted small">Check this machine to list what's out of date.</div>`
        : n.total === 0 ? html`<div class="note good">Everything apt knows about is up to date.</div>`
        : html`<div class="muted small">${n.total} package${n.total === 1 ? "" : "s"} can be upgraded.</div>
            ${UP.data.groups.map((g) => groupBlock(n, g))}`}
      ${autoLine(n)}`;
    const right = html`<i class="dot ${n.ready ? "ok" : "warn"}" title="${n.ready ? "ready" : "not ready"}"></i>${n.auto && n.auto.length ? chip("auto", n.auto_paused ? "warn" : "good") : ""}`;
    return card(n.node, body, right, "up-node");
  }

  function paint() {
    const root = $("#up-root"); if (!root) return;
    const d = UP.data;
    if (!d) { setHTML(root, html`<div class="muted"><span class="spin small"></span> Loading…</div>`); return; }
    setHTML(root, html`
      <div class="card"><div class="row wrap" style="gap:10px">
        <span class="muted small grow">Outdated software and drivers on each machine. Checking reads it as root (read-only) through a short-lived helper and refreshes its package list. Nothing is installed until you press Update and confirm; automatic updates are off until you turn them on.</span>
        <button class="btn primary" data-up-check="all">Check every machine</button></div></div>
      ${d.demo ? html`<div class="banner info mt"><svg class="icon"><use href="#i-help"/></svg><span>Demo: these machines are made up and nothing runs anywhere.</span></div>` : ""}
      ${d.nodes.length ? d.nodes.map((n) => html`<div class="mt">${nodeCard(n)}</div>`) : empty("No machines", "Machines in your cluster appear here.")}`);
  }

  // ---- review and confirm (same shape as Hardware care) --------------------------------------------------------
  function dialog(title, lines, script, node, runLabel, run) {
    let dlg = document.getElementById("up-dialog");
    if (!dlg) { dlg = document.createElement("div"); dlg.id = "up-dialog"; document.body.appendChild(dlg); }
    setHTML(dlg, html`<div class="tu-back" data-up-close></div><div class="tu-box" role="dialog" aria-modal="true" aria-label="${title}">
      <h3 style="margin-top:0">${title}</h3><ul>${lines.map((x) => html`<li>${x}</li>`)}</ul>
      ${script ? html`<details><summary class="small">What will run as root on ${node}</summary><pre class="hl-pre">${script}</pre></details>` : ""}
      <div class="row wrap mt">${runLabel ? html`<button class="btn primary" data-up-run>${runLabel}</button>` : ""}
        <button class="btn" data-up-close>${runLabel ? "Cancel" : "Close"}</button></div></div>`);
    dlg._run = run; dlg.hidden = false;
  }

  async function reviewApply(node, group) {
    const r = await post("/api/updates/apply", { node, groups: [group] });   // no confirm -> returns the plan
    if (!r) return;
    dialog("Update " + GROUP_LABEL[group] + " on " + node + "?", r.plan, r.script || "", node, r.script ? "Run on " + node : "",
      () => post("/api/updates/apply", { node, groups: [group], confirm: true }));
  }

  async function reviewAuto(node, groups) {
    if (!groups.length) { const r = await post("/api/updates/auto", { node, groups: [] }); if (r) { UP.data = r; paint(); toast("Automatic updates off for " + node + "."); } return; }
    const r = await post("/api/updates/plan", { node, groups });            // the plan + the hash to confirm with
    if (!r) return;
    const names = groups.map((g) => GROUP_LABEL[g]).join(", ");
    // turning auto on is about future updates, so always offer it -- even when nothing in these groups is outstanding today.
    const first = r.script ? "Keep " + names + " updated automatically on " + node + " — these are outstanding now, and new ones as they appear, up to once every " + UP.data.auto_interval_h + " hours."
                           : "Keep " + names + " updated automatically on " + node + " — nothing is outstanding today; new updates are applied up to once every " + UP.data.auto_interval_h + " hours.";
    dialog("Automatic updates for " + node, [first].concat(r.script ? r.plan : []), r.script || "", node, "Turn on",
      () => post("/api/updates/auto", { node, groups, confirm: true, hash: r.hash }));
  }

  async function run() {
    const dlg = document.getElementById("up-dialog"), b = dlg.querySelector("[data-up-run]");
    if (b) { b.disabled = true; b.textContent = "Working…"; }
    const r = await dlg._run();
    dlg.hidden = true;
    if (r) { if (r.nodes) UP.data = r; toast((r.done || []).slice(-1)[0] || "Done."); }
    load();
  }

  document.addEventListener("click", async (e) => {
    const t = e.target; if (!t.closest) return;
    let b;
    if (t.closest("#up-dialog")) {
      if (t.closest("[data-up-close]")) { document.getElementById("up-dialog").hidden = true; paint(); }  // paint resets a checkbox the confirm was cancelled on
      else if (t.closest("[data-up-run]")) run();
      return;
    }
    if (!t.closest("#up-root")) return;
    if ((b = t.closest("[data-up-check]"))) {
      b.disabled = true;
      const r = await post("/api/updates/check", { node: b.dataset.upCheck });
      if (r) { UP.data = r; paint(); setTimeout(load, 1500); }
    } else if ((b = t.closest("[data-up-apply]"))) {
      reviewApply(b.dataset.upApply, b.dataset.upGroup);
    }
  });
  document.addEventListener("change", (e) => {
    const t = e.target; if (!t.dataset || t.dataset.upAuto === undefined || !t.closest("#up-root")) return;
    const node = t.dataset.upAuto, group = t.dataset.upGroup;
    const n = (UP.data.nodes || []).find((x) => x.node === node) || { auto: [] };
    const set = new Set(n.auto || []);
    if (t.checked) set.add(group); else set.delete(group);
    reviewAuto(node, UP.data.groups.filter((g) => set.has(g)));   // keep the canonical group order
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const d = document.getElementById("up-dialog"); if (d && !d.hidden) { d.hidden = true; paint(); } } });

  V.updates = {
    build() { NY.freshHTML($("#view"), html`<div id="up-root"></div>`); paint(); load(); },
    update() { },
  };
})();
