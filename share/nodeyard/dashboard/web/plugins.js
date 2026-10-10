// Settings › Plugins and Activity (plugins.py): what each tool area does, where it runs, what it may change, whether
// it works right now, on/off switches the server enforces, and the audit log of actions. Loaded after settings.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, chip, card, toast, getJSON, postJSON, ago } = U;
  const P = { list: null, log: null, at: 0, busy: "" };
  const tone = { ok: "good", warn: "warn", off: "", client: "accent", unavailable: "bad" };
  const label = { ok: "working", warn: "needs attention", off: "off", client: "in the Mac app", unavailable: "unavailable" };

  async function load(force) {
    if (!force && P.list && Date.now() - P.at < 20000) return;
    P.at = Date.now();
    try { const r = await getJSON("/api/plugins"); if (r.ok) P.list = r.plugins; } catch (e) { /* keep the last list */ }
    try { const r = await getJSON("/api/audit?limit=60"); if (r.ok) P.log = r.entries; } catch (e) { /* keep the last log */ }
    if (S.view === "settings") render();
  }
  function render() {
    const el = $("#st-plugins"); if (!el) return;
    if (!P.list) { setHTML(el, card("Plugins", html`<div class="empty"><div class="spin"></div>Checking the plugins…</div>`)); return; }
    setHTML(el, html`${card("Plugins", html`<p class="muted small" style="margin-top:0">The tool areas the AI and these pages can use. Turning one off makes the server refuse it (and removes its AI skills from chat); core parts can't be turned off.</p>
      <div class="plugins">${P.list.map((p) => html`<div class="plugin">
        <div class="row" style="gap:8px;align-items:flex-start"><div class="grow"><b>${p.name}</b> ${chip(label[p.status] || p.status, tone[p.status] || "")}
          <div class="muted small">${p.description}</div>
          <div class="faint small">${p.clients.length ? "Used by: " + p.clients.join(", ") + " · " : ""}Permission: ${p.permission}</div>
          ${p.detail ? html`<div class="small ${p.status === "unavailable" || p.status === "warn" ? "warnline" : "faint"}">${p.detail}</div>` : ""}</div>
          ${p.can_disable ? raw('<label class="check plug-toggle" title="' + (p.enabled ? "Turn off" : "Turn on") + '"><input type="checkbox" data-plugin="' + esc(p.id) + '"' + (p.enabled ? " checked" : "") + (P.busy === p.id ? " disabled" : "") + '><span>' + (p.enabled ? "On" : "Off") + "</span></label>") : html`<span class="faint small">core</span>`}</div></div>`)}</div>`)}
      <div class="mt">${card("Activity", P.log && P.log.length ? html`<div class="audit">${P.log.slice(0, 40).map((e) => html`<div class="row small" style="gap:8px"><span class="faint nowrap">${ago(e.time)} ago</span>${e.ok === false || e.code >= 400 ? chip("failed " + e.code, "bad") : chip("ok", "good")}<b class="nowrap">${e.plugin}</b><span class="mono faint">${e.route}</span><span>${e.summary}</span><span class="faint">· ${e.actor}</span></div>`)}</div>`
        : html`<p class="muted small">Nothing yet. Model actions, research, web tools, device settings and plugin switches are recorded here (never request contents, keys or passwords).</p>`, html`<button class="btn small" data-plugin-refresh>Refresh</button>`)}</div>`);
  }
  document.addEventListener("change", async (e) => {
    const t = e.target; if (!t.dataset || !t.dataset.plugin) return;
    P.busy = t.dataset.plugin; render();
    try { const r = await postJSON("/api/plugins", { id: t.dataset.plugin, enabled: t.checked }); if (r.ok) P.list = r.plugins; else toast(r.error || "Not changed."); }
    catch (err) { toast(err.message || "Couldn't reach the dashboard server; nothing changed."); }
    finally { P.busy = ""; load(true); }
  });
  document.addEventListener("click", (e) => { if (e.target.closest("[data-plugin-refresh]")) load(true); });
  const settings = V.settings, build = settings.build, update = settings.update;
  settings.build = function () { build.apply(this, arguments); $("#view").insertAdjacentHTML("beforeend", '<div id="st-plugins" class="mt"></div>'); render(); load(true); };
  settings.update = function () { if (update) update.apply(this, arguments); load(false); };
})();
