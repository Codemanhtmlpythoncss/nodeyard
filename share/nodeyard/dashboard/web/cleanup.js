// The Cleanup page: pods nobody has used for a while, removed by a policy you control (cleanup.py). Off by default;
// the preview is a dry run, and nothing is removed without your say-so. Loaded after app.js.
(function () {
  "use strict";
  const { html, raw, setHTML } = NY;
  const U = NY.ui;
  const { V, $, card, chip, toast, getJSON, postJSON, ago, empty } = U;
  const CL = { data: null, sel: {}, log: null, at: 0 };

  async function load() {
    try { const r = await getJSON("/api/cleanup"); if (r.ok) { CL.data = r; CL.at = Date.now(); } else CL.data = { fail: r.error }; } catch (e) { CL.data = { fail: "Couldn't reach the dashboard." }; }
    paint();
  }
  async function post(url, body) {
    try { const r = await postJSON(url, body); if (!r.ok) { toast(r.error || "That didn't work."); return null; } return r; }
    catch (e) { toast(e.message || "That didn't work."); return null; }
  }
  const conf = (c) => chip(c === "high" ? "stop time recorded" : c === "medium" ? "ready-condition time" : c, c === "high" ? "good" : "warn");
  const hours = (h) => (h == null ? "–" : h < 48 ? h + " h" : Math.round(h / 24) + " days");

  function policyCard(d) {
    const p = d.policy;
    return card("Policy", html`<div class="row wrap" style="gap:14px;align-items:flex-end">
        <label class="hc-field"><span class="muted small">Not used for</span><span class="row" style="gap:4px"><input id="cl-hours" type="number" min="1" max="2160" value="${p.hours}" style="width:80px"> hours</span></label>
        <label class="row small" style="gap:6px"><input type="checkbox" id="cl-stopped" ${p.stopped_only ? "checked" : ""}> Stopped containers only</label>
        <label class="row small" style="gap:6px"><input type="checkbox" id="cl-vols" ${p.include_volumes ? "checked" : ""}> Include pods that mount persistent data</label>
        <label class="row small" style="gap:6px"><input type="checkbox" id="cl-dry" ${p.dry_run ? "checked" : ""}> Automatic runs are a dry run (log only)</label></div>
      <label class="hc-field mt"><span class="muted small">Protected (never removed): namespace/pod, namespace/* or pod name, one per line. Pods labelled or annotated <span class="mono">nodeyard.io/keep=true</span> are protected too.</span>
        <textarea id="cl-protect" rows="3" class="mono" style="width:100%">${p.protect.join("\n")}</textarea></label>
      <div class="row wrap mt" style="gap:8px"><button class="btn small primary" data-cl="save">Save policy</button>
        <span class="faint small">"Not used" means its containers stopped that long ago, never how old it is. Running pods, kube-system and protected pods are never removed; removing a pod never deletes its volumes, Job or images.${p.stopped_only ? "" : " With stopped-only off, bare pods that never started a container (image or config errors) count too."}</span></div>`);
  }
  function autoCard(d) {
    const state = !d.auto ? chip("off") : d.auto_paused ? chip("paused: policy changed", "warn") : d.policy.dry_run ? chip("on (dry run)", "accent") : chip("on", "good");
    return card(html`Automatic cleanup ${state}`, html`<p class="small" style="margin-top:0">${d.auto && !d.auto_paused ? html`Every ${d.every_min} minutes it removes the pods the preview lists${d.policy.dry_run ? " (dry run: it only logs them)" : ""}.`
        : d.auto_paused ? "The policy changed after you turned it on, so it waits until you confirm the new one." : "Off: nothing is removed unless you remove it below. Turning it on shows the policy and what it would remove now."}</p>
      <div class="row wrap" style="gap:6px">${!d.auto || d.auto_paused ? raw('<button class="btn small primary" data-cl="auto-on">' + (d.auto_paused ? "Review and confirm" : "Turn on…") + "</button>") : ""}${d.auto ? raw('<button class="btn small" data-cl="auto-off">Turn off</button>') : ""}</div>`);
  }
  function lastCard(d) {
    const l = d.last;
    if (!l) return card("Last cleanup", html`<div class="muted small">None yet.</div>`);
    return card("Last cleanup", html`<div class="row wrap" style="gap:6px">${chip(l.mode)}${l.dry_run ? chip("dry run", "accent") : ""}${l.errors.length ? chip(l.errors.length + " error" + (l.errors.length === 1 ? "" : "s"), "bad") : chip("no errors", "good")}<span class="muted small">${ago(l.at)} ago · ${l.checked} pods checked</span></div>
      ${l.removed.length ? html`<div class="small mt">${l.dry_run ? "Would remove" : "Removed"}: ${l.removed.map((x) => x.pod + " (" + hours(x.idle_h) + ")").join(", ")}</div>` : html`<div class="small mt muted">Nothing ${l.dry_run ? "would have been" : "was"} removed.</div>`}
      ${l.errors.length ? html`<ul class="err small">${l.errors.map((e) => html`<li>${e}</li>`)}</ul>` : ""}
      <details class="mt" data-cl-log><summary class="small">Decision log</summary><div id="cl-log" class="small"></div></details>`);
  }
  function previewCard(d) {
    if (d.error) return card("Preview", html`<div class="err">${d.error}</div>`);
    const rows = d.candidates.map((r) => html`<tr><td><input type="checkbox" data-cl-pick="${r.uid}" ${CL.sel[r.uid] !== false ? "checked" : ""}></td><td class="wrap"><b>${r.name}</b><div class="sub">${r.namespace}${r.owner ? " · " + r.owner : ""}${r.node ? " · " + r.node : ""}</div></td>
      <td>${chip(r.reason_k8s || r.phase, r.phase === "Failed" ? "warn" : "")}</td><td class="num">${hours(r.idle_h)}</td><td>${conf(r.confidence)}</td><td class="wrap small">${r.reason}${r.mounts.length ? html`<div class="faint">mounts ${r.mounts.join(", ")}</div>` : ""}</td></tr>`);
    const kept = d.kept.map((r) => html`<tr><td class="wrap"><b>${r.name}</b><div class="sub">${r.namespace}</div></td><td>${chip(r.reason_k8s || r.phase)}</td><td class="wrap small">${r.reason}</td></tr>`);
    const n = d.candidates.filter((r) => CL.sel[r.uid] !== false).length;
    return card(html`Preview <span class="muted small">(dry run: ${d.total} pods checked)</span>`, html`${d.candidates.length ? html`<div class="tablewrap"><table class="tbl"><thead><tr><th></th><th>Pod</th><th>State</th><th class="num">Not used for</th><th>Last use from</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table></div>
        <div class="row mt" style="gap:8px"><button class="btn small danger" data-cl="run" ${n ? "" : "disabled"}>Remove ${n} pod${n === 1 ? "" : "s"}</button><span class="faint small">Each is checked again just before it's removed.</span></div>`
      : empty("Nothing to clean up", html`<div class="muted small">No pod meets the policy right now.</div>`)}
      ${kept.length ? html`<details class="mt"><summary class="small">${kept.length} stopped or waiting pod${kept.length === 1 ? "" : "s"} kept, and why</summary><div class="tablewrap"><table class="tbl"><tbody>${kept}</tbody></table></div></details>` : ""}`);
  }
  function paint() {
    const root = $("#cl-root"); if (!root) return;
    const d = CL.data;
    if (!d) return setHTML(root, html`<div class="muted"><span class="spin small"></span> Loading…</div>`);
    if (d.fail) return setHTML(root, card("Cleanup", html`<div class="err">${d.fail}</div>`));
    setHTML(root, html`${d.demo ? html`<div class="banner info"><svg class="icon"><use href="#i-help"/></svg><span>Demo: made-up pods; removing them only changes this demo.</span></div>` : ""}
      ${previewCard(d)}<div class="grid g-2 mt">${policyCard(d)}<div>${autoCard(d)}<div class="mt">${lastCard(d)}</div></div></div>`);
  }
  async function showLog() {
    const r = await getJSON("/api/cleanup/log").catch(() => null);
    const el = $("#cl-log"); if (!el || !r || !r.ok) return;
    setHTML(el, r.log.length ? html`<div class="tablewrap"><table class="tbl"><tbody>${r.log.slice(0, 100).map((e) => html`<tr><td style="white-space:nowrap">${ago(e.t)} ago</td><td>${e.event || e.decision}</td><td class="wrap">${e.pod || ""}${e.why ? " · " + e.why : e.reason ? " · " + e.reason : ""}</td></tr>`)}</tbody></table></div>` : html`<span class="muted">Empty.</span>`);
  }
  function dlg(title, body, okLabel, danger) {
    return new Promise((resolve) => {
      let d = document.getElementById("cl-dialog");
      if (!d) { d = document.createElement("div"); d.id = "cl-dialog"; document.body.appendChild(d); }
      setHTML(d, html`<div class="tu-back" data-cl-no></div><div class="tu-box" role="dialog" aria-modal="true" aria-label="${title}"><h3 style="margin-top:0">${title}</h3>${body}
        <div class="row wrap mt" style="gap:6px"><button class="btn ${danger ? "danger" : "primary"}" data-cl-yes>${okLabel}</button><button class="btn" data-cl-no>Cancel</button></div></div>`);
      d.hidden = false;
      d.onclick = (e) => { if (e.target.closest("[data-cl-yes]")) { d.hidden = true; resolve(true); } else if (e.target.closest("[data-cl-no]")) { d.hidden = true; resolve(false); } };
    });
  }

  document.addEventListener("click", async (e) => {
    const t = e.target; if (!t.closest || !t.closest("#cl-root")) return;
    const b = t.closest("[data-cl]"); if (!b) return;
    const a = b.dataset.cl, d = CL.data;
    if (a === "save") {
      const r = await post("/api/cleanup/policy", { hours: +$("#cl-hours").value, stopped_only: $("#cl-stopped").checked, include_volumes: $("#cl-vols").checked,
        dry_run: $("#cl-dry").checked, protect: $("#cl-protect").value });
      if (r) { CL.data = r; paint(); toast(r.auto_paused ? "Saved. Automatic cleanup waits until you confirm the new policy." : "Policy saved."); }
    } else if (a === "auto-on") {
      const p = d.policy;
      const ok = await dlg("Turn on automatic cleanup?", html`<p style="margin-top:0">Every ${d.every_min} minutes it removes pods whose containers stopped more than <b>${p.hours} hours</b> ago${p.stopped_only ? "" : ", and bare pods that never started in that time"}${p.include_volumes ? html`, <b>including pods that mount persistent data</b> (the volumes themselves stay)` : ", except pods that mount persistent data"}.
        ${p.protect.length ? "Protected: " + p.protect.join(", ") + "." : ""} ${p.dry_run ? html`<b>Dry run:</b> it only logs what it would remove.` : ""}</p>
        <p class="small">Right now that would ${p.dry_run ? "log" : "remove"} <b>${d.candidates.length}</b> pod${d.candidates.length === 1 ? "" : "s"}${d.candidates.length ? ": " + d.candidates.slice(0, 12).map((r) => r.namespace + "/" + r.name).join(", ") + (d.candidates.length > 12 ? "…" : "") : ""}.</p>
        <p class="faint small">Running pods, kube-system and protected pods are never removed. If you change the policy later, it pauses until you confirm again.</p>`, "Turn on");
      if (!ok) return;
      const r = await post("/api/cleanup/auto", { enabled: true, confirm: true, policy: d.hash }); if (r) { CL.data = r; paint(); }
    } else if (a === "auto-off") {
      const r = await post("/api/cleanup/auto", { enabled: false }); if (r) { CL.data = r; paint(); }
    } else if (a === "run") {
      const pick = d.candidates.filter((r) => CL.sel[r.uid] !== false);
      const ok = await dlg("Remove " + pick.length + " pod" + (pick.length === 1 ? "" : "s") + "?", html`<ul class="small">${pick.slice(0, 30).map((r) => html`<li>${r.namespace}/${r.name}: ${r.reason}</li>`)}</ul><p class="faint small">Only these pods: their volumes, Jobs and images stay.</p>`, "Remove", true);
      if (!ok) return;
      const r = await post("/api/cleanup/run", { confirm: true, uids: pick.map((x) => x.uid) });
      if (r) { const l = r.result; toast("Removed " + l.removed.length + (l.errors.length ? "; " + l.errors.length + " failed (see Last cleanup)" : ".")); }
      load();
    }
  });
  document.addEventListener("change", (e) => { const t = e.target; if (t.dataset && t.dataset.clPick) { CL.sel[t.dataset.clPick] = t.checked; paint(); } });
  document.addEventListener("toggle", (e) => { if (e.target.matches && e.target.matches("[data-cl-log]") && e.target.open) showLog(); }, true);

  V.cleanup = {
    build() { NY.freshHTML($("#view"), html`<div id="cl-root"></div>`); paint(); load(); },
    update() { if (Date.now() - CL.at > 60000) load(); },
  };
})();
