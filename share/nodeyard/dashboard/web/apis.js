// The "APIs" page: register your own self-hosted (or third-party) HTTP APIs — search engines and plain REST — so the
// dashboard and the AI chat can use them. The server calls them (the client only needs the dashboard), and the model
// gets one tool per API. Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, $$, card, toast, getJSON, postJSON } = U;

  const A = { list: [], editing: null, result: {}, preset: null };   // editing: an id, "new", or null

  // One-click setups: fill the fiddly fields so you only type the base URL.
  const PRESETS = {
    searxng: { name: "SearXNG", kind: "search", method: "GET", base: "http://searxng.nodeyard-apps.svc:8080/search", description: "Search the web through my SearXNG",
               query_param: "q", extra_params: { format: "json" }, result: { items: "results", title: "title", url: "url", snippet: "content" } },
    jellyfin: { name: "Jellyfin", kind: "rest", method: "GET", base: "http://jellyfin.lan:8096", description: "My Jellyfin media server (try path /Items or /Users)",
                auth: { type: "header", name: "X-Emby-Token", value: "" } },
    ollama: { name: "Ollama", kind: "rest", method: "GET", base: "http://ollama.lan:11434", description: "My Ollama server (try path /api/tags)", auth: { type: "none" } },
    homeassistant: { name: "Home Assistant", kind: "rest", method: "GET", base: "http://homeassistant.lan:8123/api", description: "My Home Assistant (try path /states)",
                     auth: { type: "bearer", value: "" } },
    grafana: { name: "Grafana", kind: "rest", method: "GET", base: "http://grafana.lan:3000/api", description: "My Grafana (try path /search or /dashboards)",
               auth: { type: "bearer", value: "" } },
    immich: { name: "Immich", kind: "rest", method: "GET", base: "http://immich.lan:2283/api", description: "My Immich photos (try path /server-info)",
              auth: { type: "header", name: "x-api-key", value: "" } },
    search: { name: "", kind: "search", method: "GET", base: "", description: "", query_param: "q", extra_params: {}, result: { items: "", title: "title", url: "url", snippet: "" } },
    rest: { name: "", kind: "rest", method: "GET", base: "", description: "" },
  };

  async function load() {
    try { const r = await getJSON("/api/apis"); if (r.ok) A.list = r.apis || []; } catch (e) { /* signed out */ }
  }

  const field = (label, inner, hint) =>
    html`<label class="field-l">${label}${inner}${hint ? html`<span class="faint small" style="display:block;margin-top:4px">${hint}</span>` : ""}</label>`;

  function form(e) {
    const isSearch = (e.kind || "search") === "search";
    const r = e.result || {};
    const a = e.auth || {};
    const extra = Object.entries(e.extra_params || {}).map(([k, v]) => k + "=" + v).join("\n");
    return html`<div class="card mt" id="api-form" data-kind="${e.kind || "search"}">
      <h3>${e.id ? "Edit API" : "Add an API"}</h3>
      <div class="grid g-2">
        ${field("Name", raw('<input class="input" data-v="name" value="' + esc(e.name || "") + '" placeholder="My SearXNG" spellcheck="false">'))}
        ${field("Kind", raw('<select class="select" data-v="kind"><option value="search"' + (isSearch ? " selected" : "") + '>Search (returns a list of results)</option><option value="rest"' + (!isSearch ? " selected" : "") + ">REST (call an endpoint, get JSON)</option></select>"))}
      </div>
      ${field("Base URL", raw('<input class="input" data-v="base" value="' + esc(e.base || "") + '" placeholder="https://searx.my.lan/search" spellcheck="false">'),
              "The exact endpoint. The model never changes the host — only the query and parameters.")}
      <div class="grid g-2">
        ${field("Method", raw('<select class="select" data-v="method"><option' + ((e.method || "GET") === "GET" ? " selected" : "") + ">GET</option><option" + (e.method === "POST" ? " selected" : "") + ">POST</option></select>"))}
        ${field("Description (shown to the AI)", raw('<input class="input" data-v="description" value="' + esc(e.description || "") + '" placeholder="Search our internal wiki">'))}
      </div>
      ${isSearch ? html`<div class="grid g-2">
          ${field("Query parameter", raw('<input class="input" data-v="query_param" value="' + esc(e.query_param || "q") + '" spellcheck="false">'), "The parameter the search text goes in (e.g. q).")}
          ${field("Results are under (JSON path)", raw('<input class="input" data-v="result.items" value="' + esc(r.items || "") + '" placeholder="results" spellcheck="false">'), "Dotted path to the list, or blank if the reply is a list itself.")}
        </div>
        <div class="grid g-3">
          ${field("Title field", raw('<input class="input" data-v="result.title" value="' + esc(r.title || "title") + '" spellcheck="false">'))}
          ${field("URL field", raw('<input class="input" data-v="result.url" value="' + esc(r.url || "url") + '" spellcheck="false">'))}
          ${field("Snippet field", raw('<input class="input" data-v="result.snippet" value="' + esc(r.snippet || "") + '" spellcheck="false">'))}
        </div>` : html`<p class="muted small">The AI calls this with an optional path (added to the base URL) and query parameters; it gets the JSON back.</p>`}
      ${field("Fixed parameters (one per line, key=value)", raw('<textarea class="input" data-v="extra_params" rows="2" spellcheck="false" placeholder="format=json">' + esc(extra) + "</textarea>"))}
      <div class="grid g-3">
        ${field("Auth", raw('<select class="select" data-v="auth.type">' + ["none", "header", "bearer", "query"].map((t) => '<option value="' + t + '"' + ((a.type || "none") === t ? " selected" : "") + ">" + (t === "none" ? "None" : t === "bearer" ? "Bearer token" : t === "header" ? "Header" : "Query parameter") + "</option>").join("") + "</select>"))}
        ${field("Auth name", raw('<input class="input" data-v="auth.name" value="' + esc(a.name || "") + '" placeholder="X-API-Key / api_key" spellcheck="false">'), "Header or parameter name (not for Bearer).")}
        ${field("Auth secret", raw('<input class="input" type="password" data-v="auth.value" value="' + (e.has_auth ? "********" : "") + '" placeholder="kept on the server" spellcheck="false" autocomplete="off">'))}
      </div>
      <div class="row mt" style="gap:8px">
        <button class="btn primary" data-api="save">${e.id ? "Save changes" : "Add API"}</button>
        <button class="btn" data-api="cancel">Cancel</button>
      </div>
    </div>`;
  }

  function apiCard(e) {
    const tool = "api_" + (e.name || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
    const res = A.result[e.id];
    return html`<section class="card mt">
      <h3>${esc(e.name)} <span class="right"><button class="btn small" data-api="edit" data-id="${e.id}">Edit</button> <button class="btn small danger" data-api="delete" data-id="${e.id}">Delete</button></span></h3>
      <div class="mono small faint">${esc(e.kind === "search" ? "search" : e.method + " REST")} · ${esc(e.base)}${e.has_auth ? " · auth set" : ""}${e.private_host ? " · LAN" : ""}</div>
      ${e.description ? html`<div class="small mt">${esc(e.description)}</div>` : ""}
      <div class="small faint mt">The AI calls this as <span class="mono">${esc(tool)}</span>.</div>
      <div class="row mt" style="gap:8px">
        <input class="input" data-tryid="${e.id}" placeholder="${e.kind === "search" ? "Try a search…" : "path (optional)"}" style="max-width:320px" spellcheck="false">
        <button class="btn small" data-api="try" data-id="${e.id}">${e.kind === "search" ? "Search" : "Call"}</button>
      </div>
      ${res ? html`<pre class="mt" style="max-height:260px;overflow:auto;white-space:pre-wrap">${esc(res)}</pre>` : ""}
    </section>`;
  }

  function render() {
    const el = $("#view"); if (!el || S.view !== "apis") return;
    const intro = html`<section class="card"><h3>Your self-hosted APIs</h3>
      <p class="muted small" style="margin-top:0">Register search engines (SearXNG, your own) and REST APIs. The dashboard calls them for you, and the AI chat gets one tool per API. The host you set is the only one ever contacted — the model only supplies the query and parameters.</p>
      ${A.editing ? "" : html`<div class="row mt" style="gap:8px;flex-wrap:wrap;align-items:center">
        <span class="small faint">Quick setup — fills everything but the URL and key:</span>
        <button class="btn small primary" data-api="preset" data-preset="searxng">SearXNG</button>
        <button class="btn small" data-api="preset" data-preset="jellyfin">Jellyfin</button>
        <button class="btn small" data-api="preset" data-preset="ollama">Ollama</button>
        <button class="btn small" data-api="preset" data-preset="homeassistant">Home Assistant</button>
        <button class="btn small" data-api="preset" data-preset="grafana">Grafana</button>
        <button class="btn small" data-api="preset" data-preset="immich">Immich</button>
        <button class="btn small" data-api="preset" data-preset="search">Blank search</button>
        <button class="btn small" data-api="preset" data-preset="rest">Blank REST</button></div>`}</section>`;
    const body = A.editing === "new" ? form(A.preset || {}) : (A.editing ? form(A.list.find((x) => x.id === A.editing) || {}) : "");
    const list = A.list.length ? A.list.map(apiCard) : (A.editing ? "" : html`<div class="card mt"><div class="empty">No APIs yet. Add one to use it from chat.</div></div>`);
    NY.freshHTML(el, html`${intro}${body}${list}`);
  }

  function collect() {
    const f = $("#api-form"); if (!f) return null;
    const v = (k) => { const el = f.querySelector('[data-v="' + k + '"]'); return el ? el.value.trim() : ""; };
    const extra = {};
    v("extra_params").split("\n").forEach((line) => { const i = line.indexOf("="); if (i > 0) extra[line.slice(0, i).trim()] = line.slice(i + 1).trim(); });
    const body = {
      id: A.editing !== "new" ? A.editing : undefined,
      name: v("name"), kind: v("kind"), base: v("base"), method: v("method"), description: v("description"),
      extra_params: extra,
      auth: { type: v("auth.type"), name: v("auth.name"), value: v("auth.value") },
    };
    if (body.kind === "search") {
      body.query_param = v("query_param") || "q";
      body.result = { items: v("result.items"), title: v("result.title"), url: v("result.url"), snippet: v("result.snippet") };
    }
    return body;
  }

  async function onClick(e) {
    if (S.view !== "apis") return;
    const t = e.target.closest("[data-api]"); if (!t) return;
    const act = t.dataset.api, id = t.dataset.id;
    if (act === "new") { A.preset = null; A.editing = "new"; render(); }
    else if (act === "preset") { A.preset = JSON.parse(JSON.stringify(PRESETS[t.dataset.preset] || {})); A.editing = "new"; render(); }
    else if (act === "cancel") { A.preset = null; A.editing = null; render(); }
    else if (act === "edit") { A.editing = id; render(); }
    else if (act === "delete") {
      if (!confirm("Delete this API?")) return;
      const r = await postJSON("/api/apis/delete", { id }); if (r.ok) { await load(); A.editing = null; render(); toast("Deleted."); }
    } else if (act === "save") {
      const body = collect(); if (!body) return;
      if (!body.name || !body.base) { toast("Give it a name and a base URL."); return; }
      const r = await postJSON("/api/apis/save", body);
      if (r.ok) { await load(); A.editing = null; render(); toast("Saved."); } else toast(r.error || "Not saved.");
    } else if (act === "try") {
      const input = $('[data-tryid="' + id + '"]');
      const entry = A.list.find((x) => x.id === id);
      const args = entry.kind === "search" ? { query: (input && input.value) || "" } : { path: (input && input.value) || "" };
      A.result[id] = "Running…"; render();
      try { const r = await postJSON("/api/apis/call", { id, args }); A.result[id] = r.ok ? (r.text || "(no output)") : (r.error || "The call failed."); }
      catch (err) { A.result[id] = "Couldn't reach the dashboard."; }
      render();
    }
  }

  document.addEventListener("click", onClick);
  document.addEventListener("change", (e) => { if (S.view === "apis" && e.target.closest && e.target.closest('[data-v="kind"]')) render(); });

  V.apis = {
    build() { NY.freshHTML($("#view"), html`<div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div>`); A.editing = null; A.result = {}; load().then(render); },
    update() { },
  };
})();
