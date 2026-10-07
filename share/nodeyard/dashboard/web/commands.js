// The Commands page: every nodeyard command, with its help, runnable from the page (so anything
// the command line can do, the dashboard can do). Built from `nodeyard commands --json`. Loaded after app.js.
(function () {
  "use strict";
  const { html, raw } = NY;
  const U = NY.ui;
  const { S, V, $, $$, store, chip, card, empty, toast, getJSON, startJob, copyText } = U;
  const C = { list: null, error: "", q: "", sel: store.get("cmd.sel", ""), public: false };

  async function load() {
    try {
      const r = await getJSON("/api/commands");
      if (r.ok) { C.list = r.commands; C.public = r.public; } else C.error = r.error || "Couldn't list the commands.";
    } catch (e) { C.error = "Couldn't reach the dashboard server."; }
    paint();
  }
  const match = (c) => !C.q || (c.path + " " + c.summary + " " + c.group).toLowerCase().includes(C.q.toLowerCase());
  const INTERACTIVE = new Set(["menu", "wizard", "dashboard run", "completion"]);

  function paintList() {
    const el = $("#cmd-list"); if (!el || !C.list) return;
    const groups = [...new Set(C.list.map((c) => c.group))];
    NY.setHTML(el, html`${groups.map((g) => { const cs = C.list.filter((c) => c.group === g && match(c)); return cs.length ? html`<div class="section-title" style="margin:10px 0 4px">${g}</div>
      ${cs.map((c) => html`<button class="cmd-item ${C.sel === c.path ? "on" : ""}" data-cmd="${c.path}"><span class="mono">${c.path}</span><span class="faint small">${c.summary}</span></button>`)}` : ""; })}`);
  }
  function paintDetail() {
    const el = $("#cmd-detail"); if (!el) return;
    const c = C.list && C.list.find((x) => x.path === C.sel);
    if (!c) { NY.freshHTML(el, card("Pick a command", empty("Every nodeyard command is here", "Choose one on the left to see what it does and run it."))); return; }
    const inter = INTERACTIVE.has(c.path);
    NY.freshHTML(el, card("nodeyard " + c.path, html`<p style="margin-top:0">${c.summary}</p>
      ${inter ? html`<p class="small" style="color:var(--warn)">This one is interactive: run it on the <a href="#terminal">Terminal</a> page.</p>` : html`
      <label class="field-l">Options and arguments<input class="input mono" id="cmd-args" placeholder="e.g. --node debian-1" spellcheck="false" autocomplete="off"></label>
      <div class="row wrap" style="gap:16px;margin:4px 0 10px"><label class="check"><input type="checkbox" id="cmd-yes" checked> <span>Answer yes to questions</span></label>
        <label class="check"><input type="checkbox" id="cmd-dry"> <span>Dry run (only show what would change)</span></label>
        <label class="check"><input type="checkbox" id="cmd-json"> <span>JSON output</span></label></div>
      <details><summary class="muted small" style="cursor:pointer">Input to send (for commands with --stdin, e.g. a password)</summary><textarea class="input mono" id="cmd-stdin" rows="3" spellcheck="false" autocomplete="off"></textarea></details>
      <div class="row wrap" style="gap:8px;margin-top:12px"><button class="btn primary" data-cmd-run="1" ${C.public ? raw("disabled") : ""}>Run</button><button class="btn" data-cmd-copy="1">Copy as a command</button></div>
      ${C.public ? html`<p class="small" style="color:var(--warn)">Running commands only works over Tailscale or your own network, not through public access.</p>` : ""}`}
      <div class="section-title">Help</div><pre class="logbox wrap" style="max-height:none">${c.help}</pre>`));
  }
  function paint() {
    const host = $("#cmd-body"); if (!host) return;
    if (!C.list) { NY.freshHTML(host, C.error ? card("Commands", html`<div class="empty"><b>Couldn't list the commands</b>${C.error}</div>`) : card("Commands", html`<div class="empty"><div class="spin"></div>Reading the command list…</div>`)); return; }
    if (!$("#cmd-list")) NY.freshHTML(host, html`<div class="cmd-grid"><div class="card"><input class="input" id="cmd-q" placeholder="Find a command…" value="${C.q}" spellcheck="false" aria-label="Find a command">
      <div class="faint small" style="margin-top:6px">${C.list.length} commands. Everything the command line can do.</div><div id="cmd-list"></div></div><div id="cmd-detail"></div></div>`);
    paintList(); paintDetail();
  }
  function line() {
    const args = (($("#cmd-args") || {}).value || "").trim();
    return "sudo nodeyard " + C.sel + (args ? " " + args : "") + (($("#cmd-yes") || {}).checked ? " --yes" : "") + (($("#cmd-dry") || {}).checked ? " --dry-run" : "") + (($("#cmd-json") || {}).checked ? " --json" : "");
  }

  V.commands = {
    build() { NY.freshHTML($("#view"), html`<div id="cmd-body"></div>`); if (!C.list) load(); else paint(); },
    update() { },
  };
  document.addEventListener("click", (e) => {
    const item = e.target.closest("[data-cmd]");
    if (item) { C.sel = item.dataset.cmd; store.set("cmd.sel", C.sel); paintList(); paintDetail(); return; }
    if (e.target.closest("[data-cmd-copy]")) { copyText(line()); return; }
    if (e.target.closest("[data-cmd-run]")) {
      const stdin = ($("#cmd-stdin") || {}).value || "";
      startJob("cli", { path: C.sel, args: ($("#cmd-args") || {}).value || "", yes: !!($("#cmd-yes") || {}).checked, dry_run: !!($("#cmd-dry") || {}).checked,
        json: !!($("#cmd-json") || {}).checked, stdin: stdin ? (stdin.endsWith("\n") ? stdin : stdin + "\n") : "" }, () => U.load(true));
      if ($("#cmd-stdin")) $("#cmd-stdin").value = "";
    }
  });
  document.addEventListener("input", (e) => { if (e.target.id === "cmd-q") { C.q = e.target.value; paintList(); } });
  document.addEventListener("keydown", (e) => { if (e.target.id === "cmd-args" && e.key === "Enter") { e.preventDefault(); const b = $("[data-cmd-run]"); if (b && !b.disabled) b.click(); } });
  void $$; void chip; void toast; void S;
})();
