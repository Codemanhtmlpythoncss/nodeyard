// The Services page (a monitor for your own HTTP, TCP and ping services, with notifications) and the Tools page
// (network diagnostics run from the server, Wake-on-LAN and Prometheus metrics). Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, $$, card, chip, toast, getJSON, postJSON, store, ago } = U;

  const H = { services: [], notify: null, wol: [], loaded: false, error: "", editing: null, tab: "list", q: "", group: "",
    suggestions: null, timer: 0, busy: new Set(), tool: "ping", runs: [] };
  const KIND_LABEL = { http: "HTTP", tcp: "TCP", ping: "Ping" };
  const NOTIFY_LABEL = { ntfy: "ntfy", discord: "Discord", slack: "Slack / Mattermost", gotify: "Gotify", json: "Plain JSON" };

  async function load() {
    try {
      const r = await getJSON("/api/homelab/services");
      if (!r.ok) throw new Error(r.error || "The service monitor isn't available.");
      H.services = r.services || []; H.notify = r.notify; H.wol = r.wol || []; H.error = "";
    } catch (e) { H.error = e.message || "Couldn't load the services."; }
    H.loaded = true;
  }
  async function act(url, body, okText) {
    try {
      const r = await postJSON(url, body);
      if (!r.ok) { toast(r.error || "That didn't work."); return null; }
      if (okText) toast(okText);
      return r;
    } catch (e) { toast(e.message || "That didn't work."); return null; }
  }

  // ---- shared bits -----------------------------------------------------------------------------
  const statusCls = (s) => ({ up: "good", down: "bad", degraded: "warn", paused: "", pending: "info" })[s] || "";
  const statusText = (s) => ({ up: "Up", down: "Down", degraded: "Failing", paused: "Paused", pending: "Checking…" })[s] || s;
  const ms = (v) => (v == null ? "–" : v < 10 ? v.toFixed(1) + " ms" : Math.round(v) + " ms");
  function bars(list) {
    return raw('<div class="hl-bars" aria-hidden="true">' + (list || []).map((v) => '<i class="' + (v == null ? "none" : v >= 0.999 ? "up" : v >= 0.8 ? "part" : "down") + '"' +
      (v == null ? "" : ' title="' + Math.round(v * 1000) / 10 + '% good"') + "></i>").join("") + "</div>");
  }
  function spark(values) {
    const pts = (values || []).map((v, i) => [i, v]).filter((p) => p[1] != null);
    if (pts.length < 2) return "";
    const max = Math.max(...pts.map((p) => p[1])) || 1, w = 120, h = 26, n = Math.max(1, (values.length - 1));
    const d = pts.map((p, i) => (i ? "L" : "M") + ((p[0] / n) * w).toFixed(1) + " " + (h - 2 - (p[1] / max) * (h - 4)).toFixed(1)).join(" ");
    return raw('<svg class="hl-spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" aria-hidden="true"><path d="' + d + '"/></svg>');
  }
  const groups = () => [...new Set(H.services.map((s) => s.group || ""))].sort((a, b) => (a || "~").localeCompare(b || "~"));

  // ---- Services: the list ----------------------------------------------------------------------------
  function kpis() {
    const by = (st) => H.services.filter((s) => s.status === st).length;
    const lat = H.services.filter((s) => s.status === "up" && s.last && s.last.ms != null).map((s) => s.last.ms);
    const certs = H.services.filter((s) => s.cert_days != null && s.cert_days < 14).length;
    const up = H.services.filter((s) => s.uptime != null);
    const avgUp = up.length ? up.reduce((a, s) => a + s.uptime, 0) / up.length : null;
    const k = (label, value, sub, cls) => html`<div class="card kpi"><div class="txt"><div class="label">${label}</div><div class="value ${cls || ""}">${value}<small>${sub}</small></div></div></div>`;
    return html`<div class="grid hl-kpis">
      ${k("Up", by("up") + " / " + H.services.filter((s) => s.status !== "paused").length, "services answering", by("down") ? "" : "good-t")}
      ${k("Down", by("down") + by("degraded"), by("degraded") ? by("degraded") + " failing once, checking again" : "two failed checks in a row", by("down") ? "bad-t" : "")}
      ${k("Uptime · 24 h", avgUp == null ? "–" : avgUp.toFixed(avgUp >= 99.95 ? 2 : 1) + "%", "average over all services")}
      ${k("Latency", lat.length ? ms(lat.reduce((a, b) => a + b, 0) / lat.length) : "–", "average of the last checks")}
      ${k("Certificates", certs ? certs + " expiring" : "OK", certs ? "within 14 days" : "none expire within 14 days", certs ? "warn-t" : "")}
    </div>`;
  }
  function serviceCard(s) {
    const busy = H.busy.has(s.id), last = s.last || {};
    const meta = [s.uptime == null ? "no checks yet" : s.uptime.toFixed(s.uptime >= 99.95 ? 2 : 1) + "% up", s.avg_ms != null ? ms(s.avg_ms) + " avg" : "",
      s.checked ? "checked " + ago(s.checked) + " ago" : "", "every " + (s.interval >= 60 ? Math.round(s.interval / 60) + " min" : s.interval + " s")].filter(Boolean).join(" · ");
    return html`<article class="hl-svc ${statusCls(s.status)}" data-id="${s.id}">
      <div class="hl-svc-head"><i class="dot ${statusCls(s.status)}"></i><b class="hl-name">${s.name}</b>${chip(KIND_LABEL[s.kind] || s.kind)}${chip(statusText(s.status), statusCls(s.status))}
        ${s.cert_days != null ? chip("cert " + Math.max(0, Math.floor(s.cert_days)) + " d", s.cert_days < 3 ? "bad" : s.cert_days < 14 ? "warn" : "") : ""}
        <span class="grow"></span>${s.link ? html`<a class="btn small" href="${s.link}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : ""}</div>
      <div class="hl-target mono" title="${s.target}">${s.target}</div>
      ${s.status !== "up" && last.error ? html`<div class="hl-err">${last.error}</div>` : ""}
      <div class="hl-svc-graph">${bars(s.bars)}${spark(s.spark)}</div>
      <div class="hl-svc-foot"><span class="faint small">${meta}${last.ms != null && s.status === "up" ? " · now " + ms(last.ms) : ""}</span><span class="grow"></span>
        <button class="btn small" data-hl="check" data-id="${s.id}" ${busy ? "disabled" : ""}>${busy ? "Checking…" : "Check now"}</button>
        <button class="btn small" data-hl="pause" data-id="${s.id}">${s.paused ? "Resume" : "Pause"}</button>
        <button class="btn small" data-hl="edit" data-id="${s.id}">Edit</button>
        <button class="btn small danger-t" data-hl="delete" data-id="${s.id}" aria-label="Delete ${s.name}">Delete</button></div>
    </article>`;
  }
  function list() {
    if (H.error) return html`<div class="card"><div class="empty">${H.error}</div></div>`;
    if (!H.services.length) {
      return html`<div class="card"><div class="empty"><b>No services yet.</b><br>Add the web apps, routers, NAS boxes and other machines you want watched. Checks run from this server every minute by default.
        <div class="row wrap mt" style="justify-content:center"><button class="btn primary" data-hl="new">＋ Add a service</button><button class="btn" data-hl="suggest">Suggest from the cluster</button></div></div></div>`;
    }
    const q = H.q.trim().toLowerCase();
    const shown = H.services.filter((s) => (!H.group || (s.group || "-") === H.group) && (!q || (s.name + " " + s.target + " " + (s.group || "")).toLowerCase().includes(q)));
    if (!shown.length) return html`<div class="card"><div class="empty">Nothing matches that filter.</div></div>`;
    const order = { down: 0, degraded: 1, pending: 2, up: 3, paused: 4 };
    return html`${groups().filter((g) => shown.some((s) => (s.group || "") === g)).map((g) => html`<section class="hl-group">
      <h3 class="hl-group-h">${g || "Ungrouped"} <span class="faint">${shown.filter((s) => (s.group || "") === g).length}</span></h3>
      <div class="hl-grid">${shown.filter((s) => (s.group || "") === g).sort((a, b) => (order[a.status] - order[b.status]) || a.name.localeCompare(b.name)).map(serviceCard)}</div></section>`)}`;
  }
  const field = (label, inner, hint) => html`<label class="field-l">${label}${inner}${hint ? html`<span class="faint small" style="display:block;margin-top:4px">${hint}</span>` : ""}</label>`;
  function form() {
    const e = H.editing; if (!e) return "";
    const kind = e.kind || "http";
    const placeholder = { http: "https://jellyfin.home.lan/", tcp: "192.168.1.30:22", ping: "192.168.1.1" }[kind];
    return html`<div class="card mt" id="hl-form"><h3>${e.id ? "Edit " + (e.name || "service") : "Add a service"}</h3>
      <div class="grid g-2">
        ${field("Name", raw('<input class="input" data-f="name" maxlength="60" value="' + esc(e.name || "") + '" placeholder="Jellyfin">'))}
        ${field("Check", raw('<select class="select" data-f="kind">' + Object.entries(KIND_LABEL).map(([k, v]) => '<option value="' + k + '"' + (k === kind ? " selected" : "") + ">" + v + (k === "http" ? " (a web page or API)" : k === "tcp" ? " (a port answers)" : " (the machine answers)") + "</option>").join("") + "</select>"))}
      </div>
      ${field(kind === "http" ? "Address" : kind === "tcp" ? "Host and port" : "Host or IP", raw('<input class="input mono" data-f="target" value="' + esc(e.target || "") + '" placeholder="' + placeholder + '" spellcheck="false" autocapitalize="off">'),
        kind === "http" ? "Any status below 400 counts as up, unless you set the codes below. Redirects count as answers." : kind === "ping" ? "Needs ping on the server; some networks block it — use TCP if so." : "")}
      <div class="grid g-2">
        ${field("Group", raw('<input class="input" data-f="group" maxlength="40" list="hl-groups" value="' + esc(e.group || "") + '" placeholder="Media, Network, Home…"><datalist id="hl-groups">' + groups().filter(Boolean).map((g) => '<option value="' + esc(g) + '">').join("") + "</datalist>"))}
        ${field("Check every", raw('<select class="select" data-f="interval">' + [[30, "30 seconds"], [60, "minute"], [120, "2 minutes"], [300, "5 minutes"], [900, "15 minutes"], [3600, "hour"]].map(([v, t]) => '<option value="' + v + '"' + (v === (+e.interval || 60) ? " selected" : "") + ">" + t + "</option>").join("") + "</select>"))}
      </div>
      ${kind === "http" ? html`<div class="grid g-2">
        ${field("Expected status codes", raw('<input class="input" data-f="expect" maxlength="40" value="' + esc(e.expect || "") + '" placeholder="any below 400 · or 200, 2xx, 200-299">'))}
        ${field("Must contain", raw('<input class="input" data-f="keyword" maxlength="120" value="' + esc(e.keyword || "") + '" placeholder="optional text on the page">'))}
      </div>
      <label class="row small mt"><input type="checkbox" data-f="verify" ${e.verify === false ? "" : "checked"}> Check the HTTPS certificate (turn off for self-signed services; expiry is tracked only when on)</label>` : ""}
      ${field("Link to open (optional)", raw('<input class="input mono" data-f="link" value="' + esc(e.link && e.link !== e.target ? e.link : "") + '" placeholder="' + (kind === "http" ? "the address above" : "http://… (for the Open button)") + '" spellcheck="false">'))}
      <div class="row wrap mt"><button class="btn primary" data-hl="save">${e.id ? "Save" : "Add and check"}</button><button class="btn" data-hl="cancel">Cancel</button></div>
    </div>`;
  }
  function suggestPanel() {
    if (H.suggestions === null) return "";
    return html`<div class="card mt"><h3>From your cluster <span class="right"><button class="btn small" data-hl="suggest-close">Close</button></span></h3>
      ${H.suggestions.length ? html`<div class="hl-sugg">${H.suggestions.map((s, i) => html`<div class="row"><div class="grow" style="min-width:0"><b>${s.name}</b> ${chip(KIND_LABEL[s.kind])}<div class="faint small mono" style="overflow:hidden;text-overflow:ellipsis">${s.target} · ${s.from}</div></div><button class="btn small" data-hl="sugg-add" data-i="${i}">Add</button></div>`)}</div>`
        : html`<div class="empty">Nothing new: services with a NodePort, LoadBalancer address or Ingress host show up here.</div>`}</div>`;
  }
  function notifyTab() {
    const n = H.notify || { kind: "ntfy", level: "critical", resolved: true, enabled: false };
    const perm = typeof Notification === "undefined" ? "unsupported" : Notification.permission;
    const browserOn = store.get("hl-browser-notify", "0") === "1" && perm === "granted";
    return html`<div class="grid g-2">
      <div class="card"><h3>Send alerts to your phone or chat</h3>
        <p class="muted small">When an alert has lasted a minute (short flaps stay quiet) the server posts it to your webhook, and again when it clears. It works with the page closed.</p>
        ${field("Service", raw('<select class="select" data-n="kind">' + Object.entries(NOTIFY_LABEL).map(([k, v]) => '<option value="' + k + '"' + (k === n.kind ? " selected" : "") + ">" + v + "</option>").join("") + "</select>"))}
        ${field("Webhook address", raw('<input class="input mono" data-n="url" value="" placeholder="' + esc(n.has_url ? "Saved: " + n.url_hint + " (type to replace)" : { ntfy: "https://ntfy.sh/your-private-topic", discord: "https://discord.com/api/webhooks/…", slack: "https://hooks.slack.com/services/…", gotify: "https://gotify.lan/message?token=…", json: "https://your-endpoint/…" }[n.kind] || "") + '" spellcheck="false" autocapitalize="off">'),
          "It stays on the server and is only shown shortened. ntfy topics are public by name: pick a long, random one.")}
        ${field("Send", raw('<select class="select" data-n="level"><option value="critical"' + (n.level === "critical" ? " selected" : "") + '>Critical alerts</option><option value="warning"' + (n.level === "warning" ? " selected" : "") + ">Critical and warning alerts</option></select>"))}
        <label class="row small mt"><input type="checkbox" data-n="resolved" ${n.resolved ? "checked" : ""}> Also say when an alert clears</label>
        <label class="row small mt"><input type="checkbox" data-n="enabled" ${n.enabled ? "checked" : ""}> Notifications on</label>
        <div class="row wrap mt"><button class="btn primary" data-hl="notify-save">Save</button><button class="btn" data-hl="notify-test" ${n.has_url ? "" : "disabled"}>Send a test</button></div>
      </div>
      <div class="card"><h3>This browser</h3>
        <p class="muted small">Pop up a desktop notification for new critical alerts while a dashboard tab is open in the background.</p>
        <label class="row small"><input type="checkbox" data-hl-browser ${browserOn ? "checked" : ""} ${perm === "unsupported" || perm === "denied" ? "disabled" : ""}> Desktop notifications in this browser</label>
        ${perm === "denied" ? html`<p class="faint small">Notifications are blocked for this site in the browser's settings.</p>` : perm === "unsupported" ? html`<p class="faint small">This browser can't show notifications (on iPhone, add the dashboard to the Home Screen first).</p>` : ""}
        <h3 class="mt">Recent deliveries</h3>
        ${n.log && n.log.length ? html`<div class="hl-log">${n.log.map((l) => html`<div class="row"><i class="dot ${l.ok ? "good" : "bad"}"></i><div class="grow" style="min-width:0"><b>${l.title}</b><div class="faint small">${l.result}</div></div><span class="faint small nowrap">${ago(l.at)} ago</span></div>`)}</div>`
          : html`<div class="empty small">Nothing sent yet.</div>`}
      </div></div>`;
  }
  function paint() {
    if (S.view !== "services" || !$("#hl-root")) return;
    const top = $("#hl-top"), body = $("#hl-body");
    setHTML(top, H.loaded ? kpis() : "");
    if (H.tab === "notify") { if (!body.dataset.notify) { setHTML(body, notifyTab()); body.dataset.notify = "1"; } return; }
    delete body.dataset.notify;
    setHTML(body, html`${suggestPanel()}${H.loaded ? list() : html`<div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div>`}`);
  }
  function paintForm() { const host = $("#hl-form-host"); if (host) setHTML(host, form()); const f = $("#hl-form [data-f=name]"); if (f && !f.value) f.focus(); }
  function toolbar() {
    const gs = groups();
    return html`<div class="card hl-toolbar"><div class="row wrap">
      <div class="seg" role="tablist"><button data-hl="tab" data-tab="list" class="${H.tab === "list" ? "on" : ""}">Services</button><button data-hl="tab" data-tab="notify" class="${H.tab === "notify" ? "on" : ""}">Notifications</button></div>
      <input class="input" data-hl-q placeholder="Filter services…" value="${H.q}" style="max-width:220px">
      ${gs.length > 1 || (gs.length === 1 && gs[0]) ? raw('<select class="select" data-hl-group style="max-width:180px"><option value="*">All groups</option>' + gs.map((g) => '<option value="' + esc(g || "-") + '"' + (H.group === (g || "-") ? " selected" : "") + ">" + esc(g || "Ungrouped") + "</option>").join("") + "</select>") : ""}
      <span class="grow"></span>
      <button class="btn" data-hl="suggest">Suggest</button>
      <a class="btn" href="/api/homelab/export" download>Export</a>
      <label class="btn">Import<input type="file" accept="application/json,.json" data-hl-import hidden></label>
      <button class="btn primary" data-hl="new">＋ Add service</button>
    </div></div>`;
  }
  function schedule() {
    clearTimeout(H.timer);
    H.timer = setTimeout(async () => { if (S.view !== "services" || document.hidden) return; await load(); paint(); schedule(); }, 10000);
  }

  V.services = {
    build() {
      NY.freshHTML($("#view"), html`<div id="hl-root"><div id="hl-toolbar"></div><div id="hl-top"></div><div id="hl-form-host"></div><div id="hl-body"></div></div>`);
      setHTML($("#hl-toolbar"), toolbar()); paint(); paintForm();
      load().then(() => { setHTML($("#hl-toolbar"), toolbar()); paint(); schedule(); });
    },
    update() { },
  };

  async function saveService() {
    const f = $("#hl-form"); if (!f) return;
    const v = (k) => { const el = f.querySelector('[data-f="' + k + '"]'); return el ? (el.type === "checkbox" ? el.checked : el.value) : undefined; };
    const body = { name: v("name"), kind: v("kind"), target: v("target"), group: v("group"), interval: +v("interval"), link: v("link") || "" };
    if (body.kind === "http") Object.assign(body, { expect: v("expect"), keyword: v("keyword"), verify: v("verify") });
    if (H.editing.id) { body.id = H.editing.id; body.paused = !!H.editing.paused; }
    const btn = f.querySelector("[data-hl=save]"); btn.disabled = true;
    const r = await act("/api/homelab/service", body, H.editing.id ? "Saved." : "Added. Checking it now…");
    btn.disabled = false;
    if (!r) return;
    H.editing = null; paintForm();
    if (!body.id) await act("/api/homelab/check", { id: r.service.id });
    await load(); setHTML($("#hl-toolbar"), toolbar()); paint();
  }
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-hl]"); if (!b || !b.closest("#hl-root")) return;
    const a = b.dataset.hl, id = b.dataset.id, svc = H.services.find((s) => s.id === id);
    if (a === "tab") { H.tab = b.dataset.tab; setHTML($("#hl-toolbar"), toolbar()); paint(); }
    else if (a === "new") { H.editing = { kind: "http", interval: 60, group: H.group === "-" ? "" : H.group }; H.tab = "list"; paintForm(); $("#hl-form").scrollIntoView({ block: "nearest", behavior: "smooth" }); }
    else if (a === "edit" && svc) { H.editing = { ...svc }; paintForm(); $("#hl-form").scrollIntoView({ block: "nearest", behavior: "smooth" }); }
    else if (a === "cancel") { H.editing = null; paintForm(); }
    else if (a === "save") saveService();
    else if (a === "check" && svc) {
      H.busy.add(id); paint();
      const r = await act("/api/homelab/check", { id });
      H.busy.delete(id);
      if (r) { const i = H.services.findIndex((s) => s.id === id); if (i >= 0) H.services[i] = r.service; toast(r.service.status === "up" ? svc.name + " is up (" + ms(r.service.last.ms) + ")." : svc.name + ": " + ((r.service.last || {}).error || "not answering")); }
      paint();
    }
    else if (a === "pause" && svc) { const r = await act("/api/homelab/service", { ...svc, paused: !svc.paused }); if (r) { await load(); paint(); } }
    else if (a === "delete" && svc) { if (!confirm("Stop watching " + svc.name + "? Its history is deleted too.")) return; if (await act("/api/homelab/service-delete", { id }, "Deleted.")) { await load(); setHTML($("#hl-toolbar"), toolbar()); paint(); } }
    else if (a === "suggest") { const r = await getJSON("/api/homelab/suggestions").catch(() => null); H.suggestions = r && r.ok ? r.suggestions : []; H.tab = "list"; setHTML($("#hl-toolbar"), toolbar()); paint(); }
    else if (a === "suggest-close") { H.suggestions = null; paint(); }
    else if (a === "sugg-add") {
      const s = (H.suggestions || [])[+b.dataset.i]; if (!s) return; b.disabled = true;
      const r = await act("/api/homelab/service", { name: s.name, kind: s.kind, target: s.target, group: "Cluster" }, "Added " + s.name + ".");
      if (r) { H.suggestions.splice(+b.dataset.i, 1); await act("/api/homelab/check", { id: r.service.id }); await load(); setHTML($("#hl-toolbar"), toolbar()); paint(); } else b.disabled = false;
    }
    else if (a === "notify-save" || a === "notify-test") {
      const root = $("#hl-body"), val = (k) => root.querySelector('[data-n="' + k + '"]');
      const body = { kind: val("kind").value, level: val("level").value, resolved: val("resolved").checked, enabled: val("enabled").checked };
      if (val("url").value.trim()) body.url = val("url").value.trim();
      const r = await act("/api/homelab/notify", body, a === "notify-save" ? "Notification settings saved." : "");
      if (!r) return;
      H.notify = r.notify;
      if (a === "notify-test") { const t = await act("/api/homelab/notify-test", {}, ""); if (t) { H.notify = t.notify; toast(t.message); } }
      delete $("#hl-body").dataset.notify; paint();
    }
  });
  document.addEventListener("input", (e) => {
    if (e.target.matches("[data-hl-q]")) { H.q = e.target.value; paint(); }
  });
  document.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.matches("[data-hl-group]")) { H.group = t.value === "*" ? "" : t.value; paint(); }
    else if (t.matches('#hl-form [data-f="kind"]')) { const f = $("#hl-form"); ["name", "target", "group", "interval", "link"].forEach((k) => { const el = f.querySelector('[data-f="' + k + '"]'); if (el) H.editing[k] = el.value; }); H.editing.kind = t.value; paintForm(); }
    else if (t.matches("[data-hl-browser]")) {
      if (t.checked && typeof Notification !== "undefined" && Notification.permission !== "granted") {
        const p = await Notification.requestPermission();
        if (p !== "granted") { t.checked = false; toast("The browser didn't allow notifications."); return; }
      }
      store.set("hl-browser-notify", t.checked ? "1" : "0");
      toast(t.checked ? "Desktop notifications on for this browser." : "Desktop notifications off.");
    }
    else if (t.matches("[data-hl-import]") && t.files && t.files[0]) {
      try {
        const data = JSON.parse(await t.files[0].text());
        const replace = confirm("Replace your current services and Wake-on-LAN list?\n\nOK replaces them; Cancel adds the new ones to what you have.");
        const r = await act("/api/homelab/import", { data, replace });
        if (r) { toast(r.message); await load(); setHTML($("#hl-toolbar"), toolbar()); paint(); }
      } catch (err) { toast("That file isn't a nodeyard homelab backup."); }
      t.value = "";
    }
  });

  // ---- Alerts as desktop notifications, and the Services nav badge ------------------------------------------------
  let known = null;
  U.hooks.push(() => {
    if (!S.res) return;
    const alerts = S.res.alerts || [];
    const down = S.res.monitor ? S.res.monitor.down : 0;
    ["#nav-n-services", "#mobile-n-services"].forEach((sel) => { const el = $(sel); if (el) { el.textContent = down || ""; el.className = "n " + (down ? "bad" : ""); } });
    const crit = new Map(alerts.filter((a) => a.level === "critical").map((a) => [a.kind + "|" + a.ref + "|" + a.title.replace(/[\d.,]+/g, "#"), a]));
    if (known === null) { known = new Set(crit.keys()); return; }
    const fresh = [...crit.keys()].filter((k) => !known.has(k));
    known = new Set(crit.keys());
    if (!fresh.length || store.get("hl-browser-notify", "0") !== "1" || typeof Notification === "undefined" || Notification.permission !== "granted" || !document.hidden) return;
    fresh.slice(0, 3).forEach((k) => { const a = crit.get(k); try { new Notification(a.title, { body: a.detail || "", tag: "nodeyard-" + k }); } catch (err) { /* some browsers only allow it from a service worker */ } });
  });

  // ---- Tools ------------------------------------------------------------------------------------------------
  const TOOLS = { ping: ["Ping", "Is the machine there, and how far away?"], dns: ["DNS lookup", "Names to addresses, or an address to its name"],
    port: ["Port check", "Does something answer on this port?"], http: ["HTTP check", "Status, redirect, server and certificate"], trace: ["Traceroute", "The network path, hop by hop"] };
  function toolsPage() {
    const t = H.tool;
    return html`<div class="grid g-2">
      <div class="card"><h3>Network tools <span class="right faint small">run from this server</span></h3>
        <div class="seg hl-tool-seg">${Object.entries(TOOLS).map(([k, v]) => html`<button data-tool="${k}" class="${k === t ? "on" : ""}">${v[0]}</button>`)}</div>
        <p class="muted small">${TOOLS[t][1]}</p>
        <form class="row wrap" data-tool-form>
          <input class="input mono grow" name="target" value="${H.lastTarget || ""}" placeholder="${t === "http" ? "https://example.lan/ or host:port" : "host name or IP address"}" required spellcheck="false" autocapitalize="off" style="min-width:180px">
          ${t === "port" ? raw('<input class="input" name="port" type="number" min="1" max="65535" placeholder="port" required style="width:100px">') : ""}
          <button class="btn primary" type="submit">Run</button>
        </form>
        <div id="hl-tool-out">${H.runs.length ? H.runs.map((r) => html`<div class="hl-run"><div class="row"><i class="dot ${r.running ? "warn" : r.ok ? "good" : "bad"}"></i><b>${TOOLS[r.tool][0]}</b><span class="mono small">${r.target}</span><span class="grow"></span><span class="faint small">${r.running ? "running…" : r.seconds + " s"}</span></div>
          <div class="small mt">${r.summary || ""}</div>${r.text ? html`<pre class="hl-pre">${r.text}</pre>` : ""}</div>`) : html`<div class="empty small">Results show here. Up to three tools run at once.</div>`}</div>
      </div>
      <div class="card"><h3>Wake-on-LAN</h3>
        <p class="muted small">Sends a magic packet on this server's network. The machine needs Wake-on-LAN turned on in its firmware and must be on the same network (or the broadcast address you give).</p>
        <div class="hl-wol">${H.wol.length ? H.wol.map((d) => html`<div class="row"><div class="grow" style="min-width:0"><b>${d.name}</b><div class="faint small mono">${d.mac} · ${d.broadcast}:${d.port}</div></div>
          <button class="btn small primary" data-wol="wake" data-id="${d.id}">Wake</button><button class="btn small" data-wol="delete" data-id="${d.id}" aria-label="Remove ${d.name}">×</button></div>`) : html`<div class="empty small">No machines saved yet.</div>`}</div>
        <form class="grid hl-wol-form mt" data-wol-form>
          <input class="input" name="name" placeholder="Name (Gaming PC)" maxlength="60">
          <input class="input mono" name="mac" placeholder="MAC aa:bb:cc:dd:ee:ff" required spellcheck="false">
          <input class="input mono" name="broadcast" placeholder="broadcast (255.255.255.255)" spellcheck="false">
          <input class="input" name="port" type="number" min="1" max="65535" placeholder="port (9)">
          <div class="row"><button class="btn" type="submit">Save machine</button><button class="btn" type="button" data-wol="once">Wake once</button></div>
        </form>
      </div>
      <div class="card"><h3>Prometheus and Grafana</h3>
        <p class="muted small">Node, pod, workload, alert and service-monitor numbers in Prometheus format at <span class="mono">/api/v1/metrics</span>. Use the server API key as a bearer token (Settings shows it).</p>
        <pre class="hl-pre">scrape_configs:
  - job_name: nodeyard
    scheme: ${location.protocol.replace(":", "")}
    metrics_path: /api/v1/metrics
    authorization:
      credentials: YOUR-SERVER-API-KEY
    static_configs:
      - targets: ["${location.host}"]</pre>
        <div class="row wrap"><a class="btn" href="/api/v1/metrics" target="_blank" rel="noopener">View metrics</a><a class="btn" href="/api/health" target="_blank" rel="noopener">Health check URL</a></div>
        <p class="faint small mt">Point an outside uptime monitor at <span class="mono">/api/health</span>: it needs no sign-in, says whether the cluster data is fresh, and tells nothing about the cluster.</p>
      </div>
    </div>`;
  }
  function paintTools() { if (S.view === "tools" && $("#view")) setHTML($("#view"), toolsPage()); }
  V.tools = {
    build() { NY.freshHTML($("#view"), html`<div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div>`); load().then(paintTools); },
    update() { },
  };
  document.addEventListener("click", async (e) => {
    const tb = e.target.closest("[data-tool]");
    if (tb && S.view === "tools") { H.tool = tb.dataset.tool; paintTools(); $("[data-tool-form] input")?.focus(); return; }
    const w = e.target.closest("[data-wol]"); if (!w || S.view !== "tools") return;
    if (w.dataset.wol === "wake") { w.disabled = true; const r = await act("/api/homelab/wake", { id: w.dataset.id }); w.disabled = false; if (r) toast(r.message); }
    else if (w.dataset.wol === "delete") { const r = await act("/api/homelab/wol", { action: "delete", id: w.dataset.id }); if (r) { H.wol = r.wol; paintTools(); } }
    else if (w.dataset.wol === "once") {
      const f = $("[data-wol-form]"), fd = new FormData(f);
      const r = await act("/api/homelab/wake", { mac: fd.get("mac"), broadcast: fd.get("broadcast") || "", port: fd.get("port") || "" }); if (r) toast(r.message);
    }
  });
  document.addEventListener("submit", async (e) => {
    if (e.target.matches("[data-tool-form]")) {
      e.preventDefault();
      const fd = new FormData(e.target), run = { tool: H.tool, target: String(fd.get("target") || "").trim(), port: fd.get("port") || "", running: true };
      if (!run.target) return;
      H.lastTarget = run.target;
      H.runs.unshift(run); H.runs = H.runs.slice(0, 8); paintTools();
      try {
        const r = await postJSON("/api/homelab/tool", { tool: run.tool, target: run.target, port: run.port });
        Object.assign(run, r.ok !== false || r.summary ? r : { ok: false, summary: r.error }, { running: false });
        if (r.error && !r.summary) run.summary = r.error;
      } catch (err) { Object.assign(run, { ok: false, summary: err.message, running: false }); }
      if (run.port) run.target += ":" + run.port;
      paintTools();
    } else if (e.target.matches("[data-wol-form]")) {
      e.preventDefault();
      const fd = new FormData(e.target);
      const r = await act("/api/homelab/wol", { name: fd.get("name"), mac: fd.get("mac"), broadcast: fd.get("broadcast") || "", port: fd.get("port") || "" }, "Saved.");
      if (r) { H.wol = r.wol; paintTools(); }
    }
  });

  NY.homelab = { load: async () => { await load(); return H.services; } };
})();
