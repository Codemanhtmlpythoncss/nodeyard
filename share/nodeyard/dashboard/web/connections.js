// Device connections on the Nodes page (connections.py): each machine's Wi-Fi/LAN and Tailscale address, checked
// separately, which one the dashboard uses, address changes, and per-device settings. Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, $$, chip, card, toast, getJSON, postJSON, ago } = U;
  const C = { devices: null, at: 0, busy: {}, err: "" };
  const pathName = { lan: "Wi-Fi/LAN", tailscale: "Tailscale" };
  const stateChip = (p) => (p.state === "reachable" ? chip("reachable" + (p.ms != null ? " · " + p.ms + " ms" : ""), "good") : p.state === "partial" ? chip("agent down", "warn")
    : p.state === "unreachable" ? chip("unreachable", "bad") : p.state === "off" ? chip("off") : p.state === "no address" ? chip("no address") : chip("not checked"));
  const overall = { both: ["both paths", "good"], lan: ["Wi-Fi/LAN only", "good"], tailscale: ["Tailscale only", "warn"], partial: ["agent not answering", "warn"], unreachable: ["unreachable", "bad"], unknown: ["unknown", ""] };

  async function load(force) {
    if (!force && C.devices && Date.now() - C.at < 15000) return;
    try { const r = await getJSON("/api/devices/connections"); if (r.ok) { C.devices = r.devices; C.err = ""; } else C.err = r.error || "Couldn't read the connections."; }
    catch (e) { C.err = e.message || "Couldn't reach the dashboard server."; }
    C.at = Date.now();
    if (S.view === "nodes") render();
  }
  function pathCell(d, path) {
    const p = d[path], s = d.settings, override = s[path + "_override"];
    const addr = p.address || p.candidate || "";
    return html`<div class="mono">${addr || "–"}${p.candidate && p.address && p.candidate !== p.address ? html` <span class="faint">→ ${p.candidate} (checking)</span>` : ""}</div>
      <div class="row wrap" style="gap:4px;margin-top:3px">${stateChip(p)}${override ? chip("manual", "accent") : p.detected && p.detected.length ? chip(p.detected[0].wireless ? "Wi-Fi" : (p.detected[0].iface || p.detected[0].source)) : ""}</div>
      ${p.error && p.state !== "reachable" ? html`<div class="faint small">${p.error}</div>` : ""}${p.last_good && p.last_good !== addr ? html`<div class="faint small">last known good: ${p.last_good}</div>` : ""}`;
  }
  function render() {
    const el = $("#nd-conn"); if (!el) return;
    if (!C.devices) { setHTML(el, card("Connections", C.err ? html`<div class="note bad">${C.err}</div>` : html`<div class="empty"><div class="spin"></div>Checking every machine's connections…</div>`)); return; }
    const cols = [
      { k: "name", t: "Device", v: (d) => d.name, r: (d) => html`<b>${d.name}</b>${d.k8s_ready === false ? html`<div class="sub">Kubernetes: NotReady</div>` : ""}` },
      { k: "status", t: "Reachable", v: (d) => d.status, r: (d) => chip((overall[d.status] || [d.status])[0], (overall[d.status] || ["", ""])[1]) },
      { k: "lan", t: "Wi-Fi/LAN", v: (d) => d.lan.address, r: (d) => pathCell(d, "lan") },
      { k: "ts", t: "Tailscale", v: (d) => d.tailscale.address, r: (d) => pathCell(d, "tailscale") },
      { k: "use", t: "Using", v: (d) => d.preferred || "", r: (d) => html`${d.preferred ? pathName[d.preferred] : html`<span class="faint">none</span>`}<div class="sub">${d.settings.preference === "auto" ? "automatic" : "prefers " + pathName[d.settings.preference]}${d.settings.failover ? "" : ", no failover"}</div>` },
      { k: "checked", t: "Checked", v: (d) => -(d.lan.checked_at || d.tailscale.checked_at || 0), r: (d) => { const t = Math.max(d.lan.checked_at || 0, d.tailscale.checked_at || 0); return t ? ago(t) + " ago" : "–"; } },
      { k: "act", t: "", cls: "right nowrap", r: (d) => raw('<button class="btn small" data-conn="test" data-id="' + esc(d.id) + '"' + (C.busy[d.id] ? " disabled" : "") + ">" + (C.busy[d.id] ? "Testing…" : "Test now") + '</button> <button class="btn small" data-conn="edit" data-id="' + esc(d.id) + '">Settings</button>') },
    ];
    const events = C.devices.flatMap((d) => (d.history || []).map((e) => Object.assign({ device: d.name }, e))).sort((a, b) => b.time - a.time).slice(0, 8);
    setHTML(el, card("Connections", html`<p class="muted small" style="margin-top:0">Each machine's Wi-Fi/LAN and Tailscale address, found by the node agents and Kubernetes and checked separately (node agent and kubelet ports, not ping). A changed address is used once it answers; until then the last good one is kept.</p>
      ${U.table("connections", cols, C.devices, { k: "name", empty: "No devices yet" })}
      ${events.length ? html`<details class="mt"><summary class="muted small">Recent connection changes (${events.length})</summary>${events.map((e) => html`<div class="row small" style="gap:8px"><span class="faint">${new Date(e.time * 1000).toLocaleString()}</span><b>${e.device}</b><span>${e.text}</span></div>`)}</details>` : ""}`,
      html`<button class="btn small" data-conn="refresh">Refresh</button>`));
  }

  function settingsDialog(d) {
    const s = d.settings, m = $("#confirmm");
    const det = (path) => (d[path].detected || []).map((a) => a.address + (a.wireless ? " (Wi-Fi)" : a.iface ? " (" + a.iface + ")" : "")).join(", ") || "none found";
    NY.freshHTML($("#confirmtitle"), "Connection settings: " + d.name);
    NY.freshHTML($("#confirmbody"), html`<div class="conn-form">
      <label class="check"><input type="checkbox" data-v="discovery" ${s.discovery ? raw("checked") : ""}> <span><b>Automatic discovery</b> (from the node agent and Kubernetes)</span></label>
      <label class="check"><input type="checkbox" data-v="auto_update" ${s.auto_update ? raw("checked") : ""}> <span>Follow address changes once the new address answers</span></label>
      <label class="check"><input type="checkbox" data-v="failover" ${s.failover ? raw("checked") : ""}> <span>Switch to the other path when the preferred one stops answering</span></label>
      <label class="check"><input type="checkbox" data-v="monitor" ${s.monitor ? raw("checked") : ""}> <span>Check this device regularly</span></label>
      <div class="grid mt" style="grid-template-columns:1fr 1fr;gap:12px">
        <div><label class="check"><input type="checkbox" data-v="lan_enabled" ${s.lan_enabled ? raw("checked") : ""}> <span><b>Wi-Fi/LAN</b></span></label>
          <input class="input" data-v="lan_override" value="${s.lan_override}" placeholder="automatic" spellcheck="false" aria-label="Wi-Fi/LAN address or host name"><div class="faint small">Found: ${det("lan")}</div></div>
        <div><label class="check"><input type="checkbox" data-v="tailscale_enabled" ${s.tailscale_enabled ? raw("checked") : ""}> <span><b>Tailscale</b></span></label>
          <input class="input" data-v="tailscale_override" value="${s.tailscale_override}" placeholder="automatic" spellcheck="false" aria-label="Tailscale address or name"><div class="faint small">Found: ${det("tailscale")}</div></div></div>
      <div class="grid mt" style="grid-template-columns:1fr 1fr 1fr;gap:12px">
        <label class="field-l">Use<select class="select" data-v="preference">${["auto", "lan", "tailscale"].map((v) => raw('<option value="' + v + '"' + (s.preference === v ? " selected" : "") + ">" + (v === "auto" ? "Automatic" : "Prefer " + pathName[v]) + "</option>"))}</select></label>
        <label class="field-l">Check every (s)<input class="input" type="number" min="15" max="3600" data-v="interval" value="${s.interval}"></label>
        <label class="field-l">Timeout (s)<input class="input" type="number" min="0.5" max="10" step="0.5" data-v="timeout" value="${s.timeout}"></label></div>
      <p class="muted small">Leave an address empty to use what discovery finds. A typed address or host name is never replaced automatically, and is shown as working only after it answers. <label class="check" style="display:inline-flex"><input type="checkbox" data-v="reset"> <span>Reset everything to automatic discovery</span></label></p></div>`);
    const ok = $("#confirmok"), cancel = $("#confirmcancel");
    ok.textContent = "Save and test"; ok.className = "btn primary";
    $("#scrim").classList.add("on"); m.classList.add("on");
    return new Promise((resolve) => {
      const done = (v) => {
        m.classList.remove("on"); if (!S.drawer) $("#scrim").classList.remove("on");
        const vals = {}; $$("[data-v]", m).forEach((el) => { vals[el.dataset.v] = el.type === "checkbox" ? el.checked : el.value; });
        ok.onclick = cancel.onclick = null; resolve(v ? vals : null);
      };
      ok.onclick = () => done(true); cancel.onclick = () => done(false);
    });
  }

  async function act(kind, id) {
    const d = (C.devices || []).find((x) => x.id === id);
    if (kind === "refresh") return load(true);
    if (!d) return;
    if (kind === "test") {
      C.busy[id] = true; render();
      try { const r = await postJSON("/api/devices/test", { id }); if (r.ok) Object.assign(d, r.device); else toast(r.error || "The test didn't run."); }
      catch (e) { toast(e.message || "Couldn't reach the dashboard server."); }
      finally { delete C.busy[id]; render(); }
      return;
    }
    const v = await settingsDialog(d); if (!v) return;
    const body = v.reset ? { id, reset: true } : { id, discovery: v.discovery, auto_update: v.auto_update, failover: v.failover, monitor: v.monitor, lan_enabled: v.lan_enabled,
      tailscale_enabled: v.tailscale_enabled, lan_override: v.lan_override.trim(), tailscale_override: v.tailscale_override.trim(), preference: v.preference, interval: +v.interval, timeout: +v.timeout };
    C.busy[id] = true; render();
    try { const r = await postJSON("/api/devices/connections", body); if (r.ok) { Object.assign(d, r.device); toast("Saved and tested " + d.name + "."); } else toast(r.error || "Not saved."); }
    catch (e) { toast(e.message || "Couldn't reach the dashboard server; nothing was saved."); }
    finally { delete C.busy[id]; render(); }
  }

  document.addEventListener("click", (e) => { const b = e.target.closest("[data-conn]"); if (b) { e.preventDefault(); act(b.dataset.conn, b.dataset.id); } });
  const nodes = V.nodes, build = nodes.build, update = nodes.update;
  nodes.build = function () { build.call(this); $("#view").insertAdjacentHTML("beforeend", '<div id="nd-conn" class="mt"></div>'); render(); load(true); };
  nodes.update = function () { update.call(this); load(false); render(); };
})();
