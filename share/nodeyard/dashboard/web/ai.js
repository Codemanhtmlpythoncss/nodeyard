// The AI page: chat with the models running on your cluster, load and unload them, download new ones, search
// Hugging Face, and copy-paste examples for the API. Loaded after app.js, which it borrows its helpers from.
(function () {
  "use strict";
  const { esc, html, raw, setHTML, fmt } = NY;
  const U = NY.ui;
  const { S, V, $, $$, store, chip, statusChip, card, seg, empty, ago, toast, copyText, getJSON, postJSON, startJob, openJob } = U;
  const GiB = 1024 ** 3;
  const uid = () => Math.random().toString(36).slice(2, 10);
  const num = (n) => (n == null ? "–" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k" : String(n));

  const A = {
    blocks: {}, 
    tab: store.get("ai.tab", "chat"), targets: null, targetsAt: 0, ollama: null, ollamaAt: 0, disk: null, diskAt: 0, diskErr: "", chats: [], cur: null, stream: null,
    search: { q: "", sort: "downloads", results: null, loading: false, error: "", files: {}, open: "" }, key: "", useKey: false, locked: false,
    attach: [],
  };
  try { A.chats = JSON.parse(store.get("chats", "[]")) || []; } catch (e) { A.chats = []; }
  A.cur = store.get("chat.cur", "") || (A.chats[0] && A.chats[0].id) || "";
  const saveChats = () => {
    // attached files are kept too; if that doesn't fit in the browser's storage, only their names are
    const pack = (keep) => JSON.stringify(A.chats.slice(0, 30).map((c) => Object.assign({}, c, { messages: c.messages.slice(-200).map((m) => ({ role: m.role, content: m.content, meta: m.meta, thinking: m.thinking,
      files: m.files ? m.files.map((f) => ({ name: f.name, size: f.size, text: keep ? f.text : null })) : undefined })) })));
    const put = (v) => { try { localStorage.setItem("nodeyard.chats", v); return true; } catch (e) { return false; } };
    try { put(pack(true)) || put(pack(false)); } catch (e) { /* storage blocked */ }
    store.set("chat.cur", A.cur || "");
  };

  // ------------------------------------------------------------------ data
  async function loadTargets(force) {
    if (!force && A.targets && Date.now() - A.targetsAt < 8000) return;
    A.targetsAt = Date.now();
    try { const r = await getJSON("/api/ai/targets"); A.targets = r.ok ? r : { targets: [], error: r.error, can_run: false, recent: [] }; } catch (e) { return; }
    if (S.view === "ai") refreshTab();
  }
  async function loadOllama(force) {
    if (!force && A.ollama && Date.now() - A.ollamaAt < 6000) return;
    A.ollamaAt = Date.now();
    try { const r = await getJSON("/api/ai/ollama"); A.ollama = r.ok ? r.pods : []; } catch (e) { return; }
    if (S.view === "ai" && A.tab === "models") refreshTab();
  }
  async function loadDisk(force) {
    const dl = A.disk && A.disk.downloads && A.disk.downloads.some((d) => d.state === "running");
    if (!force && A.diskAt && Date.now() - A.diskAt < (dl ? 5000 : 30000)) return;
    A.diskAt = Date.now();
    try { const r = await getJSON("/api/ai/models"); if (r.ok) { A.disk = r; A.diskErr = ""; } else A.diskErr = r.error || "Couldn't look at the disks."; } catch (e) { return; }
    refreshModelPick();
    if (S.view === "ai" && A.tab === "models") refreshTab();
  }
  const targetList = () => (A.targets && A.targets.targets) || [];
  const findTarget = (id) => targetList().find((t) => t.id === id);

  // ------------------------------------------------------------------ markdown (everything is escaped first)
  const inline = (t) => t.split(/(`[^`]+`)/).map((seg) => (/^`[^`]+`$/.test(seg) ? "<code>" + esc(seg.slice(1, -1)) + "</code>" : esc(seg)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'))).join("");
  const RUNNABLE = { python: "python", py: "python", python3: "python", bash: "bash", sh: "bash", shell: "bash", zsh: "bash", javascript: "node", js: "node", node: "node" };
  const runOut = (r) => r.running ? '<div class="runout running"><span class="spin small"></span> Running…</div>' :
    '<div class="runout ' + (r.rc === 0 ? "ok" : "bad") + '"><div class="runhead"><b>' + (r.rc === 0 ? "✓ ran" : r.failed ? "✗ couldn't run" : "✗ failed (exit " + r.rc + ")") + "</b>" + (r.secs != null ? ' <span class="faint">' + r.secs + " s</span>" : "") +
    '<span class="grow"></span><button class="btn small ' + (r.rc === 0 ? "" : "primary") + '" data-ai="fix-code">' + (r.rc === 0 ? "Tell the AI the result" : "Ask the AI to fix it") + '</button></div><pre>' + esc(r.out || "(no output)") + "</pre></div>";
  const codeBlock = (lang, text, mi, bi) => {
    const run = RUNNABLE[(lang || "").toLowerCase()], c = curChat(), m = c && mi != null && mi >= 0 ? c.messages[mi] : null, r = m && m.runs && m.runs[bi];
    if (m && mi >= 0) { (A.blocks[mi] = A.blocks[mi] || [])[bi] = { lang: run || "", code: text }; }
    return '<div class="code" data-mi="' + mi + '" data-bi="' + bi + '" data-lang="' + esc(run || "") + '"><div class="code-head"><span>' + esc(lang || "text") + "</span><span>" + (run && mi != null && mi >= 0 ? '<button class="btn small primary" data-ai="run-code" title="Run it on the server and see what happens">▶ Run</button> ' : "") +
      '<button class="btn small" data-ai="copy-code">Copy</button> <button class="btn small" data-ai="dl-code">Download</button></span></div><pre><code>' + esc(text) + "</code></pre>" + (r ? runOut(r) : "") + "</div>";
  };
  function md(src, mi, pending) {
    if (mi != null && mi >= 0) A.blocks[mi] = [];
    let bi = 0;
    let think = "";
    src = (src || "").replace(/<think>([\s\S]*?)(<\/think>|$)/g, (m, t) => { think += t; return ""; });
    // files the model made: taken out first, put back as file cards after the markdown
    const pf = parseFiles(src);
    src = pf.text.replace(/```[\w+#.-]*[ \t]*\n((?:\s*\u0001F\d+\u0001\s*)+)```/g, "$1");
    if (pending) src = src.replace(/<\/?(?:f|fi|fil|file|fo|fol|fold|folde|folder)?(?:\s[^>\n]*)?$/, "");  // a tag still being written
    const out = []; let inCode = false, lang = "", code = [], list = null, para = [];
    const flushPara = () => { if (para.length) { out.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; } };
    const flushList = () => { if (list) { out.push("<" + list.t + ">" + list.items.map((x) => "<li>" + inline(x) + "</li>").join("") + "</" + list.t + ">"); list = null; } };
    for (const line of src.replace(/\r/g, "").split("\n")) {
      if (inCode) { if (/^\s*```/.test(line)) { out.push(codeBlock(lang, code.join("\n"), mi, bi++)); inCode = false; code = []; } else code.push(line); continue; }
      let m;
      if ((m = line.match(/^\s*```\s*([\w+#.-]*)/))) { flushPara(); flushList(); inCode = true; lang = m[1]; continue; }
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flushPara(); flushList(); out.push("<h" + (m[1].length + 2) + ">" + inline(m[2]) + "</h" + (m[1].length + 2) + ">"); continue; }
      if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) { flushPara(); if (!list || list.t !== "ul") { flushList(); list = { t: "ul", items: [] }; } list.items.push(m[1]); continue; }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushPara(); if (!list || list.t !== "ol") { flushList(); list = { t: "ol", items: [] }; } list.items.push(m[1]); continue; }
      if ((m = line.match(/^>\s?(.*)$/))) { flushPara(); flushList(); out.push("<blockquote>" + inline(m[1]) + "</blockquote>"); continue; }
      if (/^\s*---+\s*$/.test(line)) { flushPara(); flushList(); out.push("<hr>"); continue; }
      if (!line.trim()) { flushPara(); flushList(); continue; }
      flushList(); para.push(line.trim());
    }
    if (inCode) out.push(codeBlock(lang, code.join("\n"), -1, bi++));   // (still being written: no Run yet)
    flushPara(); flushList();
    const body = out.join("").replace(/<p>\u0001F(\d+)\u0001<\/p>/g, (m, k) => fileCard(pf.files[+k], mi, +k, pending)).replace(/\u0001F(\d+)\u0001/g, (m, k) => fileCard(pf.files[+k], mi, +k, pending));
    return (think.trim() ? '<details class="think"><summary>Thinking</summary><div>' + esc(think.trim()).replace(/\n/g, "<br>") + "</div></details>" : "") + body + filesBar(pf, mi, pending);
  }

  // ------------------------------------------------------------------ files the model makes
  // The model is asked (FILES_PROMPT) to write every file in a <file path="..."> block. Each shows as a
  // card to open, copy or download; several (or folders) also download together as one .zip.
  // (kept short: on a slow cluster every word of it is read at the start of a chat)
  const FILES_PROMPT = [
    "When asked to write or create a script, program, page, config or document, give each file in this format, not as plain text:",
    '<file path="folder/name.ext">',
    "complete file contents",
    "</file>",
    'Folders in the path make folders (app/src/main.py); an empty folder is <folder path="app/data"/>. Always the whole file, no ``` fences inside. Then say briefly what each file does and how to use it. Attached files arrive in the same format.',
  ].join("\n");
  const FILE_RX = /<file\s+(?:path|name)\s*=\s*["']([^"'\n]{1,300})["'][^>\n]*>\n?([\s\S]*?)(<\/file>|$)/g;
  const FOLDER_RX = /<folder\s+(?:path|name)\s*=\s*["']([^"'\n]{1,300})["'][^>\n]*?\/?>(?:\s*<\/folder>)?/g;
  const cleanPath = (p) => p.replace(/\\/g, "/").split("/").filter((x) => x && x !== "." && x !== "..").map((x) => x.replace(/[\u0000-\u001f<>:"|?*]/g, "_").slice(0, 120)).join("/") || "file.txt";
  const baseName = (p) => p.split("/").pop() || "file.txt";
  function parseFiles(src) {
    const files = [], folders = [];
    const text = (src || "").replace(FILE_RX, (m, path, body, end) => {
      const fenced = body.match(/^\s*```[\w+#.-]*[ \t]*\n([\s\S]*?)\n?```\s*$/);  // (some models fence it anyway)
      files.push({ path: cleanPath(path), content: fenced ? fenced[1] + "\n" : body, done: !!end });
      return "\n\n\u0001F" + (files.length - 1) + "\u0001\n\n";
    }).replace(FOLDER_RX, (m, path) => { folders.push(cleanPath(path)); return ""; });
    return { text, files, folders };
  }
  const lineCount = (t) => (t ? t.split("\n").length - (t.endsWith("\n") ? 1 : 0) : 0);
  function fileCard(f, mi, k, pending) {
    if (!f) return "";
    const writing = pending && !f.done;
    return '<details class="filecard" data-mi="' + mi + '" data-fi="' + k + '"><summary><svg class="icon"><use href="#i-file"/></svg><span class="fname">' + esc(f.path) + '</span><span class="faint small nowrap">' +
      (writing ? "writing… " : !f.done ? '<span class="chip warn">unfinished: the answer stopped</span> ' : "") + lineCount(f.content) + " lines · " + fmt.bytes(f.content.length) + '</span><span class="grow"></span>' +
      (writing ? '<span class="spin small"></span>' : '<button class="btn small" data-ai="file-copy">Copy</button><button class="btn small primary" data-ai="file-dl"><svg class="icon"><use href="#i-download"/></svg>Download</button>') +
      "</summary><pre><code>" + esc(f.content) + "</code></pre></details>";
  }
  function filesBar(pf, mi, pending) {
    if (pending || !(pf.files.length > 1 || pf.folders.length)) return "";
    const n = pf.files.length, d = pf.folders.length;
    return '<div class="filebar"><svg class="icon"><use href="#i-zip"/></svg><span>' + n + " file" + (n === 1 ? "" : "s") + (d ? " and " + d + " empty folder" + (d === 1 ? "" : "s") : "") +
      '</span><span class="grow"></span><button class="btn small primary" data-ai="files-zip" data-mi="' + mi + '"><svg class="icon"><use href="#i-download"/></svg>Download all (.zip)</button></div>';
  }
  function saveBlob(blob, name) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name; a.rel = "noopener";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  }
  const saveText = (text, name) => saveBlob(new Blob([text], { type: "application/octet-stream" }), name);
  const msgFiles = (mi) => { const c = curChat(), m = c && c.messages[+mi]; return m ? parseFiles(m.content) : { files: [], folders: [] }; };
  function zipName(pf) {
    const paths = pf.files.map((f) => f.path).concat(pf.folders), tops = new Set(paths.map((p) => p.split("/")[0]));
    return (tops.size === 1 && paths.every((p) => p.includes("/")) ? [...tops][0] : "files") + ".zip";
  }
  // a .zip without compression ("stored"): small, and every unzip tool reads it
  const CRC = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
  const crc32 = (b) => { let c = 0xffffffff; for (let i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
  function makeZip(entries) {  // [{path, text}] files, [{path, dir: true}] folders
    const enc = new TextEncoder(), parts = [], central = [], d = new Date();
    const time = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1), date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
    let off = 0;
    for (const e of entries) {
      const name = enc.encode(e.dir ? e.path.replace(/\/?$/, "/") : e.path), data = e.dir ? new Uint8Array(0) : enc.encode(e.text), crc = crc32(data);
      const mode = e.dir ? 0o40755 : /^#!/.test(e.text || "") ? 0o100755 : 0o100644;
      const h = new DataView(new ArrayBuffer(30));
      [[0, 0x04034b50, 4], [4, 20, 2], [6, 0x0800, 2], [8, 0, 2], [10, time, 2], [12, date, 2], [14, crc, 4], [18, data.length, 4], [22, data.length, 4], [26, name.length, 2], [28, 0, 2]]
        .forEach(([o, v, n]) => (n === 4 ? h.setUint32(o, v, true) : h.setUint16(o, v, true)));
      parts.push(new Uint8Array(h.buffer), name, data);
      const c = new DataView(new ArrayBuffer(46));
      [[0, 0x02014b50, 4], [4, 0x031e, 2], [6, 20, 2], [8, 0x0800, 2], [10, 0, 2], [12, time, 2], [14, date, 2], [16, crc, 4], [20, data.length, 4], [24, data.length, 4], [28, name.length, 2],
        [30, 0, 2], [32, 0, 2], [34, 0, 2], [36, 0, 2], [38, ((mode << 16) | (e.dir ? 0x10 : 0)) >>> 0, 4], [42, off, 4]]
        .forEach(([o, v, n]) => (n === 4 ? c.setUint32(o, v, true) : c.setUint16(o, v, true)));
      central.push(new Uint8Array(c.buffer), name);
      off += 30 + name.length + data.length;
    }
    const size = central.reduce((n, x) => n + x.length, 0), end = new DataView(new ArrayBuffer(22));
    [[0, 0x06054b50, 4], [8, entries.length, 2], [10, entries.length, 2], [12, size, 4], [16, off, 4]].forEach(([o, v, n]) => (n === 4 ? end.setUint32(o, v, true) : end.setUint16(o, v, true)));
    return new Blob(parts.concat(central, [new Uint8Array(end.buffer)]), { type: "application/zip" });
  }
  const EXT = { python: "py", py: "py", javascript: "js", js: "js", jsx: "jsx", typescript: "ts", ts: "ts", tsx: "tsx", bash: "sh", sh: "sh", shell: "sh", zsh: "sh", html: "html", css: "css", json: "json",
    yaml: "yaml", yml: "yml", markdown: "md", md: "md", c: "c", cpp: "cpp", "c++": "cpp", h: "h", java: "java", go: "go", rust: "rs", rs: "rs", ruby: "rb", php: "php", sql: "sql", powershell: "ps1",
    ps1: "ps1", toml: "toml", ini: "ini", xml: "xml", csv: "csv", lua: "lua", kotlin: "kt", swift: "swift", csharp: "cs", cs: "cs", r: "r", perl: "pl", dockerfile: "Dockerfile", makefile: "Makefile" };
  function codeName(lang, text) {
    const m = ((text.split("\n")[0] || "").trim()).match(/^(?:#|\/\/|--|;|<!--|\/\*)\s*(?:file(?:name)?:\s*)?([\w./-]+\.[A-Za-z0-9]{1,8})\s*(?:-->|\*\/)?$/);
    if (m) return baseName(cleanPath(m[1]));
    const e = EXT[(lang || "").toLowerCase()] || "txt";
    return /^[A-Z]/.test(e) ? e : "code." + e;
  }

  // ------------------------------------------------------------------ dialogs
  function dialog(title, body, okLabel, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      const m = $("#confirmm");
      NY.freshHTML($("#confirmtitle"), title);
      NY.freshHTML($("#confirmbody"), body);
      const ok = $("#confirmok"), cancel = $("#confirmcancel");
      ok.textContent = okLabel || "OK";
      ok.className = "btn " + (opts.danger ? "danger" : "primary");
      $("#scrim").classList.add("on"); m.classList.add("on");
      const done = (v) => {
        m.classList.remove("on"); if (!S.drawer) $("#scrim").classList.remove("on");
        const vals = {}; $$("[data-v]", m).forEach((el) => { vals[el.dataset.v] = el.type === "checkbox" ? el.checked : el.value; });
        ok.onclick = cancel.onclick = null; resolve(v ? vals : null);
      };
      ok.onclick = () => done(true); cancel.onclick = () => done(false);
      const first = $("[data-v]", m); if (first) first.focus();
    });
  }

  // ------------------------------------------------------------------ view
  V.ai = {
    build() {
      A.built = null;
      NY.freshHTML($("#view"), html`<div class="toolbar"><span class="seg" id="ai-tabs" role="tablist">${[["chat", "Chat"], ["models", "Models"], ["search", "Find models"], ["api", "API"]].map(([id, l]) => raw('<button data-ai-tab="' + id + '">' + l + "</button>"))}</span>
        <label class="model-pick"><span>AI model</span><select class="select" id="ai-model" aria-label="AI model: pick the one that runs on your cluster"><option>Loading…</option></select></label><span class="grow"></span><span id="ai-status"></span></div><div id="ai-pane"></div>`);
      loadTargets(true);
      loadDisk();
      selectTab(A.tab);
    },
    update() { loadTargets(); if (A.tab === "models") { loadOllama(); loadDisk(); } refreshTab(); },
  };
  function selectTab(t) {
    A.tab = t; store.set("ai.tab", t);
    $$("#ai-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.aiTab === t));
    A.built = null;
    refreshTab();
    if (t === "models") { loadOllama(true); loadDisk(true); }
  }
  function refreshTab() {
    if (S.view !== "ai" || !$("#ai-pane")) return;
    const first = A.built !== A.tab;
    if (A.tab === "chat") (first ? buildChat : refreshChat)();
    else if (A.tab === "models") renderModels();
    else if (A.tab === "search") (first ? buildSearch : refreshSearch)();
    else renderApi();
    A.built = A.tab;
    refreshModelPick();
    const sp = S.d && S.d.ai && S.d.ai.split;
    setHTML($("#ai-status"), A.targets && A.targets.targets.length ? chip(plural(A.targets.targets.filter((t) => t.ready).length, "model") + " ready", "good") : (sp ? chip(sp.loaded === false ? "model unloaded" : sp.download === "running" ? "downloading" : "model loading" + (sp.load && sp.load.phase === "loading" ? " " + sp.load.pct.toFixed(0) + "%" : ""), "warn") : chip("no models running")));
  }
  const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");

  // ------------------------------------------------------------------ the AI model menu
  // Exactly the model files downloaded on your machines (from the same disk scan as Downloaded models).
  // Picking one runs it from the disk it is on: the old model is unloaded and its files deleted
  // (unless you keep them), then the new one loads. No download, no Hugging Face lookup.
  function installed() {
    const seen = {};
    ((A.disk && A.disk.nodes) || []).forEach((n) => n.items.forEach((it) => {
      if (it.kind === "model" && (!seen[it.name] || it.bytes > seen[it.name].size)) seen[it.name] = { file: it.name, size: it.bytes, node: n.node };
    }));
    return Object.values(seen).sort((a, b) => a.file.localeCompare(b.file));
  }
  function refreshModelPick() {
    const sel = $("#ai-model"); if (!sel || document.activeElement === sel) return;
    const sp = S.d && S.d.ai && S.d.ai.split, running = (sp && sp.model) || "", list = installed(), busy = A.targets && A.targets.busy;
    A.pick = {};
    const state = sp && sp.loaded === false ? "unloaded" : sp && sp.ready ? "running" : "loading";
    const opts = list.map((m) => { A.pick[m.file] = m; return '<option value="' + esc(m.file) + '"' + (m.file === running ? " selected" : "") + ">" + esc(m.file.replace(/\.gguf$/i, "") + " · " + fmt.bytes(m.size, 1) + " · " + m.node + (m.file === running ? " · " + state : "")) + "</option>"; });
    const none = !A.disk ? "Looking at the disks…" : !list.length ? "No models downloaded" : "No model running: pick one";
    const htmlStr = (running && A.pick[running] ? "" : '<option value="" selected>' + esc(running ? running.replace(/\.gguf$/i, "") + " · " + state : none) + "</option>") + opts.join("");
    if (sel.dataset.sig !== htmlStr) { sel.dataset.sig = htmlStr; sel.innerHTML = htmlStr; }
    sel.disabled = !!busy || !list.length;
    sel.title = busy ? "A change to the model is still running" : "";
  }
  async function pickModel(sel) {
    const m = A.pick && A.pick[sel.value], sp = S.d.ai && S.d.ai.split;
    const reset = () => { sel.dataset.sig = ""; sel.blur(); refreshModelPick(); };
    if (!m || (sp && sp.model === m.file)) { reset(); return; }
    await runModel("", m.file, m.size, m.node);
    reset();
  }


  // ---- context size, compression and web research
  const estTokens = (msgs) => Math.round(msgs.reduce((n, m) => n + (m.content || "").length + 16, 0) / 3.5);
  const chatCtx = (t) => (t && t.id === "split" ? +((S.d.ai && S.d.ai.split && S.d.ai.split.ctx) || 0) : 0);
  const SUMMARY_PROMPT = "Summarize the conversation so far so it can continue without the original messages. Use these sections and leave out empty ones: ## Request (what the user wants and any preferences), ## Done so far, ## Files and facts (exact names, numbers, errors), ## Decisions, ## Open questions and next steps. Be concise (under 350 words) but keep every detail needed to carry on. Never invent anything.";
  const WEB_PROMPT = "Some user messages include <web_results>: search results and page excerpts fetched from the internet for that question. Use them for current facts, quote carefully, and mention the source addresses you relied on. If they don't answer the question, say so.";
  // the messages the model gets: system prompt, a summary of what was compressed away, then the rest
  function chatMessages(c, extra, prune) {
    const sys = [c.files !== false ? FILES_PROMPT : "", c.system || "", c.web ? WEB_PROMPT : ""].filter(Boolean).join("\n\n");
    const from = Math.min(c.summaryUpTo || 0, c.messages.length), out = sys ? [{ role: "system", content: sys }] : [];
    if (c.summary && from > 0) out.push({ role: "user", content: "[Summary of the earlier part of our conversation, compressed to save space]\n\n" + c.summary }, { role: "assistant", content: "Understood. I'll continue from that summary." });
    const rest = c.messages.slice(from).concat(extra ? [extra] : []).filter((m) => !m.error && !m.pending);
    const keepFrom = prune ? Math.max(0, rest.length - 4) : 0;   // pruning: old attached files and web pages are left out of what is sent
    return out.concat(rest.map((m, i) => ({ role: m.role, content: apiContent(m, i < keepFrom) })));
  }
  async function streamText(target, messages, maxTok, signal) {
    const r = await fetch("/api/ai/chat", { method: "POST", cache: "no-store", signal, headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify({ target, messages, temperature: 0.2, max_tokens: maxTok, stream_id: uid() + uid() }) });
    if (r.status === 401) { location.href = "/login"; return ""; }
    if (!r.ok) { let j = {}; try { j = await r.json(); } catch (e) { /* not JSON */ } throw new Error(j.error || "The model didn't answer (HTTP " + r.status + ").") ; }
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "", text = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim(); if (payload === "[DONE]") continue;
        let j; try { j = JSON.parse(payload); } catch (e) { continue; }
        if (j.error) throw new Error(typeof j.error === "string" ? j.error : j.error.message || "The model reported an error.");
        const d = j.choices && j.choices[0] && j.choices[0].delta;
        if (d && d.content) text += d.content;
      }
    }
    return text.replace(/<think>[\s\S]*?<\/think>/g, "").trim();
  }
  // Replace the older messages by a summary the model writes. Returns true if it compressed something.
  async function compressChat(c, opts) {
    const t = findTarget(c.target);
    if (!t || !t.ready) { if (opts && opts.manual) toast("That model isn't ready."); return false; }
    const from = Math.min(c.summaryUpTo || 0, c.messages.length);
    let cut = c.messages.length - 4;
    while (cut > from && c.messages[cut].role !== "user") cut--;      // never cut between a question and its answer
    if (cut - from < 2) { if (opts && opts.manual) toast("There isn't enough earlier conversation to compress yet."); return false; }
    const old = c.messages.slice(from, cut).filter((m) => !m.error && !m.pending);
    let transcript = old.map((m) => (m.role === "user" ? "USER: " : "ASSISTANT: ") + (apiContent(m, true) || "").slice(0, 2500)).join("\n\n");
    const budget = Math.max(2000, Math.floor(((chatCtx(t) || 8192) * 3.5) / 2));
    if (transcript.length > budget) transcript = transcript.slice(0, budget >> 1) + "\n... [middle left out] ...\n" + transcript.slice(-(budget >> 1));
    const prompt = (c.summary && from > 0 ? "Summary so far:\n" + c.summary + "\n\nMore of the conversation follows. Update the summary to include it.\n\n" : "") + "<conversation>\n" + transcript + "\n</conversation>\n\n" + SUMMARY_PROMPT;
    A.compressing = c.id; refreshChat();
    const ctrl = new AbortController(); A.sumCtrl = ctrl;
    try {
      const summary = await streamText(c.target, [{ role: "system", content: "You write precise summaries of conversations between a user and an AI assistant." }, { role: "user", content: prompt }], 700, ctrl.signal);
      if (!summary) throw new Error("The model returned an empty summary.");
      c.summary = summary; c.summaryUpTo = cut; saveChats();
      toast("Compressed " + (cut - from) + " earlier messages into a summary.");
      return true;
    } catch (e) {
      if (e.name !== "AbortError") toast("Couldn't compress: " + (e.message || e));
      return false;
    } finally { A.compressing = null; A.sumCtrl = null; renderThread(); refreshChat(); }
  }
  async function webResearch(query) {
    const r = await postJSON("/api/ai/web", { query: query.slice(0, 300) });
    if (!r.ok) throw new Error(r.error || "Web search failed.");
    return r;
  }


  // ---- plugins: the AI uses tools (search, pages, Wikipedia, maths, Python, files) while it answers
  const pluginList = (c) => {
    if (!A.plugins) { fetch("/api/ai/plugins", { cache: "no-store" }).then((r) => r.json()).then((j) => { A.plugins = j.plugins || []; const el = $("#plugin-list"); if (el) setHTML(el, pluginList(curChat() || c)); }).catch(() => {}); return html`<span class="muted small">Loading…</span>`; }
    return raw(A.plugins.map((p) => '<label class="check plug"><input type="checkbox" id="cp-' + esc(p.id) + '"' + ((c.plugins || []).includes(p.id) ? " checked" : "") + (p.available ? "" : " disabled") + '> <span><b>' + esc(p.label) + "</b>: " + esc(p.desc) +
      (p.available ? "" : ' <span class="faint">(' + esc(p.why) + ")</span>") + "</span></label>").join(""));
  };
  const toolsHTML = (m) => {
    if (!m.agent) return "";
    const todo = m.todos && m.todos.length ? '<div class="toolcard todo"><b>Tasks</b>' + m.todos.map((x) => '<div class="' + esc(x.status) + '">' + (x.status === "completed" ? "☒ " : x.status === "in_progress" ? "◐ " : "☐ ") + esc(x.content) + "</div>").join("") + "</div>" : "";
    const cards = (m.tools || []).map((x) => '<details class="toolcard ' + (x.status || "") + '"><summary><span class="tdot"></span><b>' + esc(x.name) + "</b> <span class=\"mono small\">" + esc((x.summary || "").replace(/^[A-Za-z_]+\(/, "(")) + '</span><span class="tres">' + esc(x.result || (x.status === "running" ? "running…" : "")) + "</span></summary>" +
      (x.text ? "<pre>" + esc(x.text) + "</pre>" : "") + (x.diff ? "<pre>" + esc(x.diff.join("\n")) + "</pre>" : "") + "</details>").join("");
    const p = m.perm, perm = p ? '<div class="permcard"><b>' + esc(p.tool) + "</b>: " + esc(p.reason || "needs your OK") + (p.risk ? '<div class="err">This ' + esc(p.risk) + ".</div>" : "") + (p.command ? "<pre>" + esc(p.command) + "</pre>" : "") + (p.target ? '<div class="mono small">' + esc(p.target) + "</div>" : "") +
      (p.diff ? "<pre>" + esc(p.diff.lines.join("\n")) + "</pre>" : "") + '<div class="row" style="gap:8px;margin-top:8px"><button class="btn primary small" data-ai="agent-allow">Allow</button>' + (p.suggest ? '<button class="btn small" data-ai="agent-always">Allow for this chat</button>' : "") + '<button class="btn small danger" data-ai="agent-deny">Deny</button></div></div>' : "";
    return todo + cards + perm;
  };
  function agentReply(c, a) {
    const m = c && c.messages.slice().reverse().find((x) => x.perm); if (!m) return;
    const p = m.perm; m.perm = null; paintLast();
    postJSON("/api/ai/agent/reply", { chat: c.id, id: p.id, decision: a === "agent-deny" ? "deny" : "allow", scope: a === "agent-always" ? "session" : "once" }).catch(() => {});
  }
  async function sendAgent(c, mine, text, t) {
    c.messages.push(mine); A.attach = []; renderAttach();
    if (c.title === "New chat") c.title = text.slice(0, 48);
    const history = c.messages.slice(0, -1).filter((m) => !m.error && !m.pending).map((m) => ({ role: m.role, content: m.content || "" }));
    const msg = { role: "assistant", content: "", thinking: "", pending: true, agent: true, tools: [], todos: null, perm: null };
    c.messages.push(msg); renderThread(); renderChatList();
    const ctrl = new AbortController(), sid = uid() + uid();
    A.stream = { ctrl, chat: c.id, sid, agent: true }; paintSend();
    const t0 = performance.now(); let raf = 0, usage = null;
    const paint = () => { raf = 0; paintLast(); };
    try {
      const r = await fetch("/api/ai/agent", { method: "POST", cache: "no-store", signal: ctrl.signal, headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify({ chat: c.id, text: apiContent(mine), plugins: c.plugins, history, max_tokens: c.max_tokens || 0 }) });
      if (r.status === 401) { location.href = "/login"; return; }
      if (!r.ok) { let j = {}; try { j = await r.json(); } catch (e) { /* not JSON */ } throw new Error(j.error || "The AI helper didn't start (HTTP " + r.status + ")."); }
      const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
          if (!line.startsWith("data:")) continue;
          let ev; try { ev = JSON.parse(line.slice(5)); } catch (e) { continue; }
          if (ev.type === "text") msg.content += ev.delta;
          else if (ev.type === "thinking") msg.thinking += ev.delta;
          else if (ev.type === "tool_use") msg.tools.push({ id: ev.id, name: ev.name, summary: ev.summary, status: "running" });
          else if (ev.type === "tool_result") { const x = msg.tools.find((k) => k.id === ev.id); if (x) { x.status = ev.ok ? "ok" : "bad"; x.result = ev.summary; x.text = (ev.text || "").slice(0, 2500); x.diff = ev.diff ? ev.diff.lines.slice(0, 40) : null; } }
          else if (ev.type === "permission") msg.perm = ev;
          else if (ev.type === "todos") msg.todos = ev.items;
          else if (ev.type === "usage") usage = ev;
          else if (ev.type === "error") msg.error = ev.text;
          else if (ev.type === "warn" && !msg.content) msg.content = "_" + ev.text + "_";
          if (ev.type === "done" || ev.type === "exit") break;
          if (!raf) raf = requestAnimationFrame(paint);
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") msg.error = e.message || "Something went wrong.";
    } finally {
      msg.pending = false; msg.perm = null;
      if (!msg.error && !msg.content && !msg.tools.length) msg.error = ctrl.signal.aborted ? "Stopped." : "The AI sent an empty answer.";
      if (msg.error === "Stopped." && (msg.content || msg.tools.length)) msg.error = "";
      msg.meta = [usage && usage.tok_s ? usage.tok_s + " tokens/s" : "", msg.tools.length ? msg.tools.length + " tool call" + (msg.tools.length === 1 ? "" : "s") : "", ((performance.now() - t0) / 1000).toFixed(1) + " s", t.name].filter(Boolean).join(" · ");
      A.stream = null; paintSend(); saveChats(); paintLast(); refreshChat();
      setTimeout(() => autoRun(c, msg), 50);
    }
  }


  // ---- running the code the AI wrote, and telling the AI what happened
  const msgNode = (i) => { const el = $("#thread"); return el && el.querySelector('.msg.assistant[data-i="' + i + '"]'); };
  function repaintMsg(i) {
    const c = curChat(), m = c && c.messages[i], node = msgNode(i); if (!m || !node) return;
    const near = $("#thread").scrollHeight - $("#thread").scrollTop - $("#thread").clientHeight < 120;
    node.outerHTML = msgHTML(m, i, i === c.messages.length - 1);
    if (near) $("#thread").scrollTop = $("#thread").scrollHeight;
  }
  async function runCode(c, mi, bi, lang, code) {
    const m = c.messages[mi]; if (!m) return null;
    m.runs = m.runs || {}; m.runs[bi] = { running: true }; repaintMsg(mi);
    let res;
    try { const r = await postJSON("/api/ai/run-code", { lang, code }); res = r.ok ? { rc: r.rc, out: r.out, secs: r.seconds, lang, code } : { rc: -1, failed: true, out: r.error || "Couldn't run it.", lang, code }; }
    catch (e) { res = { rc: -1, failed: true, out: "Couldn't reach the dashboard.", lang, code }; }
    m.runs[bi] = res; saveChats(); repaintMsg(mi);
    return res;
  }
  function runCodeBlock(el) {
    const box = el.closest(".code"), c = curChat(); if (!box || !c) return;
    runCode(c, +box.dataset.mi, +box.dataset.bi, box.dataset.lang, box.querySelector("pre").textContent);
  }
  const fixPrompt = (r) => "I ran this " + r.lang + " code:\n```" + (r.lang === "node" ? "javascript" : r.lang) + "\n" + r.code + "\n```\n" + (r.rc === 0 ? "It printed:\n```\n" + (r.out || "(nothing)") + "\n```\nIs that right? If something is wrong, fix it and give me the complete corrected code." :
    "It failed" + (r.failed ? "" : " (exit code " + r.rc + ")") + ":\n```\n" + (r.out || "(no output)") + "\n```\nPlease find the problem and give me the complete corrected code.");
  function fixCode(el) {
    const box = el.closest(".code"), c = curChat(); if (!box || !c) return;
    const r = (c.messages[+box.dataset.mi] || {}).runs && c.messages[+box.dataset.mi].runs[+box.dataset.bi]; if (!r) return;
    send(fixPrompt(r));
  }
  // opt-in: after an answer, run its last code block; if it fails, send the error back (up to 3 times)
  async function autoRun(c, msg) {
    if (!c || !c.autofix || !msg || msg.error || !msg.content) return;
    const mi = c.messages.indexOf(msg); if (mi < 0) return;
    md(msg.content, mi, false);
    const blocks = (A.blocks[mi] || []).map((b, i) => [b, i]).filter(([b]) => b && b.lang);
    if (!blocks.length) return;
    const [b, bi] = blocks[blocks.length - 1];
    const r = await runCode(c, mi, bi, b.lang, b.code);
    if (r && r.rc !== 0 && (c.fixRounds || 0) < 3) { c.fixRounds = (c.fixRounds || 0) + 1; A.autoSend = true; send(fixPrompt(r)); }
  }


  // ---- / commands in the message box
  const esc2 = (t) => esc(String(t));
  const COMMANDS = [
    ["help", "", "Show every command"], ["new", "", "Start a new chat (also /clear)"], ["clear", "", "Start a new chat"], ["compact", "", "Compress the earlier messages to free up the model's memory"],
    ["model", "[name]", "Pick the model for this chat"], ["models", "", "Open the Models tab (load, download, delete)"], ["unload", "", "Unload the model to free memory"], ["max", "[tokens|none]", "Set the longest reply (none = no limit)"],
    ["web", "[on|off]", "Always search the web first"], ["plugins", "[on|off NAME]", "Show or switch plugins (search, Wikipedia, Python...)"], ["system", "[text]", "Set the system prompt"],
    ["temp", "[0-2]", "Set creativity"], ["run", "", "Run the last code block the AI wrote"], ["fix", "", "Ask the AI to fix the last code that failed"], ["autofix", "[on|off]", "Run code automatically and let the AI fix errors"],
    ["retry", "", "Answer the last message again"], ["stop", "", "Stop the answer"], ["copy", "", "Copy the last answer"], ["export", "", "Download this chat as a file"], ["context", "", "How full the model's memory is"],
  ];
  const lastBlock = (c) => { for (let i = c.messages.length - 1; i >= 0; i--) { const m = c.messages[i]; if (m.role !== "assistant" || !m.content || m.error) continue; md(m.content, i, false); const bl = (A.blocks[i] || []).map((b, k) => [b, k]).filter(([b]) => b && b.lang); if (bl.length) return { i, b: bl[bl.length - 1][0], k: bl[bl.length - 1][1] }; } return null; };
  async function slash(line) {
    const c = curChat(); if (!c) return true;
    const [cmd0, ...rest] = line.slice(1).trim().split(/\s+/), cmd = (cmd0 || "").toLowerCase(), arg = rest.join(" ");
    const on = (v) => !/^(off|no|false|0)$/i.test(v || "on");
    switch (cmd) {
      case "": case "help": case "?":
        await dialog("Chat commands", raw('<table class="cmds">' + COMMANDS.map((x) => "<tr><td><code>/" + esc(x[0]) + " " + esc(x[1]) + "</code></td><td>" + esc(x[2]) + "</td></tr>").join("") + "</table>"), "OK"); break;
      case "new": case "clear": newChat(); refreshChat(true); break;
      case "compact": await compressChat(c, { manual: true }); break;
      case "models": selectTab("models"); break;
      case "model": {
        const list = targetList();
        if (!arg) { await dialog("Models to chat with", raw("<p>" + (list.length ? list.map((t) => esc(t.name) + (t.ready ? "" : " (not ready)")).join("<br>") : "No model is running.") + "</p><p class=\"muted small\">Switch with /model NAME.</p>"), "OK"); break; }
        const hit = list.find((t) => t.name.toLowerCase().includes(arg.toLowerCase()) || t.id === arg);
        if (!hit) toast("No model matches “" + arg + "”."); else { c.target = hit.id; saveChats(); refreshChat(true); toast("Using " + hit.name); } break;
      }
      case "unload": { const v = await dialog("Unload the model?", raw("<p>Nothing can answer until you load one again. Its memory is freed on every machine.</p>"), "Unload"); if (v) { const r = await postJSON("/api/run", { action: "split-unload" }); toast(r.ok ? "Unloading…" : (r.error || "Couldn't unload.")); } break; }
      case "max": if (!arg) toast(c.max_tokens ? "Replies stop at " + c.max_tokens + " tokens." : "No limit on the reply length."); else if (/^(none|no|off|unlimited|0)$/i.test(arg)) { c.max_tokens = 0; saveChats(); toast("No limit on the reply length."); } else if (+arg > 0) { c.max_tokens = Math.min(65536, Math.max(16, +arg | 0)); saveChats(); toast("Replies stop at " + c.max_tokens + " tokens."); } else toast("Give a number, or none."); refreshChat(); break;
      case "web": c.web = on(arg); saveChats(); toast("Web search first: " + (c.web ? "on" : "off")); break;
      case "autofix": c.autofix = on(arg); saveChats(); toast("Run and fix code automatically: " + (c.autofix ? "on" : "off")); break;
      case "plugins": case "plugin": {
        if (!A.plugins) { try { A.plugins = (await (await fetch("/api/ai/plugins", { cache: "no-store" })).json()).plugins || []; } catch (e) { A.plugins = []; } }
        const [verb, name] = rest;
        if (verb === "on" || verb === "off") {
          const p = A.plugins.find((x) => x.id === (name || "").toLowerCase() || x.label.toLowerCase() === (name || "").toLowerCase());
          if (!p) { toast("No plugin called “" + (name || "") + "”."); break; } if (verb === "on" && !p.available) { toast(p.label + ": " + p.why); break; }
          c.plugins = (c.plugins || []).filter((x) => x !== p.id).concat(verb === "on" ? [p.id] : []); saveChats(); postJSON("/api/ai/agent/reset", { chat: c.id }).catch(() => {}); toast(p.label + " " + verb);
        } else await dialog("Plugins", raw("<p>" + A.plugins.map((p) => ((c.plugins || []).includes(p.id) ? "● " : "○ ") + "<b>" + esc(p.label) + "</b> <span class=\"muted small\">/plugins on " + esc(p.id) + (p.available ? "" : " (" + esc(p.why) + ")") + "</span>").join("<br>") + "</p>"), "OK");
        break;
      }
      case "system": c.system = arg; saveChats(); toast(arg ? "System prompt set." : "System prompt cleared."); break;
      case "temp": if (arg && !isNaN(+arg)) { c.temperature = Math.min(2, Math.max(0, +arg)); saveChats(); toast("Creativity " + c.temperature); } else toast("Creativity is " + c.temperature + ". Give a number from 0 to 2."); break;
      case "run": { const lb = lastBlock(c); if (!lb) { toast("No code the AI wrote can be run (python, bash or javascript)."); break; } await runCode(c, lb.i, lb.k, lb.b.lang, lb.b.code); break; }
      case "fix": { const lb = lastBlock(c); const r = lb && c.messages[lb.i].runs && c.messages[lb.i].runs[lb.k]; if (!r || r.running) { toast("Run the code first (▶ Run, or /run)."); break; } send(fixPrompt(r)); break; }
      case "retry": send("", true); break;
      case "stop": stopStream(); break;
      case "copy": { const m = c.messages.slice().reverse().find((x) => x.role === "assistant" && x.content); if (m) copyText(m.content); else toast("Nothing to copy."); break; }
      case "export": { const md = c.messages.filter((m) => m.content).map((m) => (m.role === "user" ? "## You\n\n" : "## AI\n\n") + m.content).join("\n\n"); const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" })); a.download = (c.title || "chat").replace(/[^\w.-]+/g, "-").slice(0, 40) + ".md"; a.click(); break; }
      case "context": { const t = findTarget(c.target), cx = chatCtx(t), used = estTokens(chatMessages(c, null)); toast(cx ? "About " + used.toLocaleString() + " of " + cx.toLocaleString() + " tokens used (" + Math.round((100 * used) / cx) + "%)." : "About " + used.toLocaleString() + " tokens so far."); break; }
      default: toast("Unknown command /" + cmd + ". Type /help."); break;
    }
    return true;
  }
  // the pop-up list while typing a /command
  function slashMenu() {
    const ta = $("#prompt"), host = $(".chat-main"); if (!ta || !host) return;
    let m = $("#slash-menu");
    const v = ta.value;
    if (!/^\/[a-z?]*$/i.test(v)) { if (m) m.remove(); return; }
    const q = v.slice(1).toLowerCase(), items = COMMANDS.filter((x) => x[0].startsWith(q));
    if (!items.length) { if (m) m.remove(); return; }
    if (!m) { m = document.createElement("div"); m.id = "slash-menu"; m.className = "slash-menu"; host.appendChild(m); }
    const sel = Math.min(+m.dataset.sel || 0, items.length - 1);
    m.dataset.sel = sel; m.dataset.items = items.map((x) => x[0]).join(",");
    m.innerHTML = items.map((x, i) => '<div class="slash-item' + (i === sel ? " on" : "") + '" data-ai="slash-pick" data-cmd="' + esc(x[0]) + '"><b>/' + esc(x[0]) + '</b> <span class="faint">' + esc(x[1]) + "</span><span class=\"grow\"></span><span class=\"muted\">" + esc(x[2]) + "</span></div>").join("");
  }
  function slashMenuRedraw() { const m = $("#slash-menu"); if (!m) return; [...m.children].forEach((c, i) => c.classList.toggle("on", i === +m.dataset.sel)); }
  function slashPick(name) { const ta = $("#prompt"); if (!ta) return; ta.value = "/" + name + " "; const m = $("#slash-menu"); if (m) m.remove(); ta.focus(); }

  // ------------------------------------------------------------------ chat
  const curChat = () => A.chats.find((c) => c.id === A.cur);
  function newChat(target) {
    const c = { id: uid(), title: "New chat", target: target || (targetList().find((t) => t.ready) || {}).id || "", messages: [], system: "", temperature: 0.7, max_tokens: 1024, compress: "auto", web: false, plugins: [], created: Date.now() };
    A.chats.unshift(c); A.cur = c.id; saveChats();
    return c;
  }
  function buildChat() {
    if (!curChat()) { if (A.chats.length) A.cur = A.chats[0].id; else newChat(); }
    setHTML($("#ai-pane"), html`<div class="chat">
      <aside class="chat-side card"><button class="btn primary wide" data-ai="new-chat">+ New chat</button><div id="chat-list" class="chat-list"></div></aside>
      <section class="chat-main card"><header class="chat-head"><select class="select grow" id="chat-target" aria-label="Model"></select>
        <button class="btn small" data-ai="chat-settings" aria-label="Chat settings">Settings</button><button class="btn small" data-ai="clear-chat">Clear</button></header>
        <div class="chat-settings hide" id="chat-settings"></div>
        <div class="thread" id="thread" aria-live="polite"></div>
        <div class="attach-list hide" id="attach-list"></div>
        <form class="composer" id="composer"><button type="button" class="btn icon-btn" data-ai="attach" title="Attach text files (code, notes, CSV, JSON, logs…). You can also drop or paste them." aria-label="Attach files"><svg class="icon"><use href="#i-clip"/></svg></button>
          <input type="file" id="attach-input" multiple hidden>
          <textarea id="prompt" rows="1" placeholder="Message the model…  (Enter to send, Shift+Enter for a new line)" aria-label="Message"></textarea>
          <button class="btn primary" type="submit" id="send">Send</button></form><div class="faint small" id="chat-note"></div><div class="dropzone" id="dropzone">Drop files to attach them</div></section></div>`);
    renderAttach();
    refreshChat(true);
    const ta = $("#prompt"); if (ta) ta.focus();
  }
  function refreshChat(force) {
    if (!$("#thread")) return;
    renderChatList();
    const c = curChat(), list = targetList();
    const sel = $("#chat-target");
    const sig = list.map((t) => t.id + t.ready).join("|") + "|" + (c ? c.target : "");
    if (sel && (force || sel.dataset.sig !== sig)) {
      sel.dataset.sig = sig;
      if (c && !findTarget(c.target) && list.length) c.target = (list.find((t) => t.ready) || list[0]).id;
      sel.innerHTML = list.length ? list.map((t) => '<option value="' + esc(t.id) + '"' + (c && t.id === c.target ? " selected" : "") + ">" + esc(t.name + " · " + t.detail + (t.ready ? "" : " (not ready)")) + "</option>").join("") : '<option value="">No model is running</option>';
    }
    if (force || (!A.stream && A.threadSig !== (c ? c.id + ":" + c.messages.length : ""))) renderThread();
    const t = c && findTarget(c.target);
    paintSend();
    const cx = c && t ? chatCtx(t) : 0, used = c && t && cx ? estTokens(chatMessages(c, null)) : 0;
    setHTML($("#chat-note"), !list.length && A.targets ? html`Nothing to chat with yet. <a href="#" data-ai-tab-link="models">Run a model</a> on the Models tab, or find one under <a href="#" data-ai-tab-link="search">Find models</a>.` : t && !t.ready ? html`${t.name} isn't loaded yet.` :
      A.compressing && c && A.compressing === c.id ? html`Compressing the earlier messages… <button class="btn small" type="button" data-ai="stop-compress">Stop</button>` : A.searching && c && A.searching === c.id ? html`Searching the web…` :
      cx ? html`Context: about ${used.toLocaleString()} of ${cx.toLocaleString()} tokens (${Math.min(100, Math.round((100 * used) / cx))}%)${c.summary ? " · earlier messages compressed" : ""}${c.max_tokens ? "" : " · no reply limit"}` : "");
  }
  // Send turns into Stop while an answer is coming, wherever you are (another chat, back from another page),
  // and Stop always works: it ends the request and tells the model to stop working on it.
  function paintSend() {
    const b = $("#send"); if (!b) return;
    const c = curChat(), t = c && findTarget(c.target);
    if (A.stream) { b.type = "button"; b.dataset.ai = "stop"; b.textContent = "Stop"; b.classList.remove("primary"); b.classList.add("danger"); b.disabled = false; b.title = "Stop the answer"; }
    else { b.type = "submit"; delete b.dataset.ai; b.textContent = "Send"; b.classList.add("primary"); b.classList.remove("danger"); b.disabled = !t || !t.ready; b.title = ""; }
  }
  function stopStream() {
    const st = A.stream; if (!st) return;
    if (st.agent) postJSON("/api/ai/agent/stop", { chat: st.chat }).catch(() => {}); else postJSON("/api/ai/stop", { id: st.sid }).catch(() => {});
    st.ctrl.abort();
  }
  // ---- attached files (text only: that's what the model can read)
  const MAX_FILE = 512 * 1024;
  async function addFiles(list) {
    for (const f of Array.from(list || [])) {
      if (A.attach.length >= 10) { toast("Up to 10 files at a time."); break; }
      if (f.size > MAX_FILE) { toast(f.name + " is over 512 KB: too much for the model to read."); continue; }
      let text = null;
      try { const buf = new Uint8Array(await f.arrayBuffer()); text = new TextDecoder("utf-8", { fatal: true }).decode(buf); } catch (e) { text = null; }
      if (text == null || text.slice(0, 8192).includes("\u0000")) { toast(f.name + " isn't a text file. The model reads text: code, notes, CSV, JSON, logs…"); continue; }
      A.attach.push({ name: cleanPath(f.name), size: f.size, text });
    }
    renderAttach();
  }
  function renderAttach() {
    const el = $("#attach-list"); if (!el) return;
    el.classList.toggle("hide", !A.attach.length);
    setHTML(el, A.attach.map((f, i) => raw('<span class="att"><svg class="icon"><use href="#i-file"/></svg><span class="mono">' + esc(f.name) + '</span><span class="faint">' + fmt.bytes(f.size) +
      '</span><button type="button" class="x" data-ai="att-rm" data-i="' + i + '" aria-label="Remove ' + esc(f.name) + '">×</button></span>')));
  }
  // what the model gets for a message: its text, then each attached file as a <file> block
  const apiContent = (m, prune) => {
    if (m.role !== "user") return m.content;
    let out = m.content || "";
    if (m.files && m.files.length) out = (out ? out + "\n\n" : "") + m.files.map((f) => prune ? '<file path="' + f.name.replace(/"/g, "'") + '">(omitted to save space)</file>' : '<file path="' + f.name.replace(/"/g, "'") + '">\n' +
      (f.text == null ? "(not kept by the browser: attach it again)\n" : f.text + (f.text.endsWith("\n") ? "" : "\n")) + "</file>").join("\n\n");
    if (m.web && m.web.text && !prune) out += "\n\n<web_results query=\"" + m.web.query.replace(/"/g, "'") + "\">\n" + m.web.text + "\n</web_results>";
    return out;
  };
  function renderChatList() {
    setHTML($("#chat-list"), A.chats.map((c) => raw('<div class="chat-item' + (c.id === A.cur ? " on" : "") + '" data-ai="open-chat" data-id="' + esc(c.id) + '"><span>' + esc(c.title) + '</span><button class="x" data-ai="del-chat" data-id="' + esc(c.id) + '" aria-label="Delete chat">×</button></div>')));
  }
  const webChips = (m) => m.web && m.web.sources && m.web.sources.length ? '<details class="srcs"><summary>Searched the web · ' + m.web.sources.length + " source" + (m.web.sources.length === 1 ? "" : "s") + "</summary>" +
    m.web.sources.map((x) => '<a href="' + esc(x.url) + '" target="_blank" rel="noopener noreferrer nofollow">' + esc(x.title || x.url) + '</a>').join("") + "</details>" : "";
  const msgHTML = (m, i, last) => {
    if (m.role === "user") return '<div class="msg user"><div class="bubble">' + esc(m.content || "").replace(/\n/g, "<br>") + webChips(m) + (m.files && m.files.length ? '<div class="att-chips">' + m.files.map((f, k) =>
      '<button class="att" data-ai="att-dl" data-mi="' + i + '" data-fi="' + k + '" title="Download"><svg class="icon"><use href="#i-file"/></svg><span>' + esc(f.name) + '</span><span class="dim">' + fmt.bytes(f.size) + "</span></button>").join("") + "</div>" : "") + "</div></div>";
    const body = m.error ? (m.content ? md(m.content, i, false) : "") + '<div class="err">' + esc(m.error) + '</div><button class="btn small" data-ai="retry">Try again</button>' : (m.content || m.thinking ? md((m.thinking ? "<think>" + m.thinking + "</think>" : "") + m.content, i, !!m.pending) : '<span class="typing"><i></i><i></i><i></i></span>');
    const meta = m.meta ? '<div class="meta">' + esc(m.meta) + "</div>" : "";
    const tools = !m.pending && !m.error && m.content ? '<div class="tools"><button data-ai="copy-msg" data-i="' + i + '">Copy</button>' + (last ? '<button data-ai="regen">Regenerate</button>' : "") + "</div>" : "";
    return '<div class="msg assistant" data-i="' + i + '"><div class="avatar">AI</div><div class="bubble">' + toolsHTML(m) + '<div class="md">' + body + (m.pending && m.content ? '<span class="caret"></span>' : "") + "</div>" + meta + tools + "</div></div>";
  };
  function renderThread() {
    const c = curChat(), el = $("#thread"); if (!c || !el) return;
    A.threadSig = c.id + ":" + c.messages.length;
    if (!c.messages.length) {
      el.innerHTML = '<div class="chat-empty"><div class="logo big"><svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2.2"/><circle cx="18" cy="8" r="2.2"/><circle cx="9" cy="18" r="2.2"/><path d="m8 7 8 1M7 8l2 8M17 10l-6 7"/></svg></div><h2>Chat with your cluster</h2><p class="muted">Runs on your own machines. Nothing leaves your network.</p><div class="row wrap" style="justify-content:center;gap:8px">' +
        ["Explain how a model is split across several machines", "Write a Python function that parses a log file", "What is the binary value 1010100101 << 2 in decimal?", "Give me a one-paragraph summary of what Kubernetes does"].map((s) => '<button class="chip btnlike" data-ai="suggest">' + esc(s) + "</button>").join("") + "</div></div>";
      return;
    }
    el.innerHTML = c.messages.map((m, i) => (c.summary && i === c.summaryUpTo && i > 0 ? '<div class="compress-note"><span>Earlier messages were compressed into a summary to save the model\'s memory</span><details><summary>Show the summary</summary><div class="md">' + md(c.summary, -1, false) + "</div></details></div>" : "") +
      msgHTML(m, i, i === c.messages.length - 1)).join("");
    el.scrollTop = el.scrollHeight;
  }
  function paintLast() {
    const c = curChat(), el = $("#thread"); if (!c || !el) return;
    const i = c.messages.length - 1, m = c.messages[i], node = el.querySelector('.msg.assistant[data-i="' + i + '"]');
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (!node) { renderThread(); return; }
    node.outerHTML = msgHTML(m, i, true);
    if (near) el.scrollTop = el.scrollHeight;
  }
  function settingsPanel() {
    const c = curChat(); if (!c) return;
    const p = $("#chat-settings");
    if (!p.classList.toggle("hide")) {
      setHTML(p, html`<label>System prompt<textarea id="cs-system" rows="2" placeholder="e.g. You are a concise assistant.">${c.system}</textarea></label>
        <div class="row" style="gap:18px;flex-wrap:wrap"><label>Creativity <b id="cs-t-v">${c.temperature}</b><input type="range" id="cs-temp" min="0" max="2" step="0.1" value="${c.temperature}"></label>
        <label>Max reply length (tokens)<span class="row" style="gap:10px"><input class="input" id="cs-max" type="number" min="16" max="65536" step="16" value="${c.max_tokens || 1024}" style="width:110px" ${c.max_tokens ? "" : raw("disabled")}>
          <span class="check"><input type="checkbox" id="cs-nolimit" ${c.max_tokens ? "" : raw("checked")}> <span>No limit</span></span></span></label></div>
        <div class="row" style="gap:18px;align-items:flex-end"><label>Context compression<select class="select" id="cs-compress"><option value="auto" ${c.compress !== "off" ? raw("selected") : ""}>Automatic (when it gets full)</option><option value="off" ${c.compress === "off" ? raw("selected") : ""}>Off</option></select></label>
          <button class="btn small" data-ai="compress-now" type="button" title="Summarise the earlier messages now to free up the model's memory">Compress now</button></div>
        <p class="muted small" style="margin:4px 0 0">Near the model's context length, older messages are replaced by a short summary the model writes, so the chat can go on.</p>
        <label class="check"><input type="checkbox" id="cs-web" ${c.web ? raw("checked") : ""}> <span><b>Web search</b>: look each question up on the internet and give the model what it finds (with sources)</span></label>
        <div class="plugins"><b>Plugins</b> <span class="muted small">the AI decides when to use them. Slower: every question carries the tool list, so a small cluster model takes a while.</span>
          <div id="plugin-list">${pluginList(c)}</div></div>
        <label class="check"><input type="checkbox" id="cs-autofix" ${c.autofix ? raw("checked") : ""}> <span><b>Run and fix code automatically</b>: runs the code the AI writes on the server (as your user) and sends errors back so it fixes them, up to 3 tries. Off by default; code only runs when you press ▶ Run.</span></label>
        <label class="check"><input type="checkbox" id="cs-files" ${c.files !== false ? raw("checked") : ""}> <span><b>Make files</b>: scripts, pages and documents the AI writes come as files to download (a .zip for folders)</span></label>`);
    }
  }
  async function send(text, regen) {
    const c = curChat(); if (!c || A.stream) return;
    const t = findTarget(c.target);
    if (!t || !t.ready) { toast("That model isn't ready."); return; }
    let mine = null;
    if (!A.autoSend) c.fixRounds = 0;
    A.autoSend = false;
    if (!regen) {
      text = (text || "").trim(); if (!text && !A.attach.length) return;
      mine = { role: "user", content: text, files: A.attach.length ? A.attach.slice() : undefined };
    }
    if (mine && c.plugins && c.plugins.length && text) return sendAgent(c, mine, text, t);   // plugins: the AI decides when to use tools
    const noLimit = !c.max_tokens;
    if (mine && c.web && text) {   // look things up first, so the model answers with what it found
      A.searching = c.id; refreshChat();
      try { const w = await webResearch(text); mine.web = { query: text.slice(0, 300), text: w.text, sources: w.sources || [] }; }
      catch (e) { toast("Web search didn't work: " + (e.message || e)); }
      finally { A.searching = null; refreshChat(); }
    }
    let msgs = chatMessages(c, mine);
    const ctx = chatCtx(t);
    let est = estTokens(msgs);
    if (ctx && c.compress !== "off" && est + Math.min(c.max_tokens || 512, 512) > ctx * 0.75) {   // nearly full: shorten it first
      msgs = chatMessages(c, mine, true); est = estTokens(msgs);                                    // cheap step: drop old attached files and pages
      if (est + Math.min(c.max_tokens || 512, 512) > ctx * 0.75 && await compressChat(c)) { msgs = chatMessages(c, mine); est = estTokens(msgs); }
    }
    // the model only remembers its context length: say so instead of sending something it can't read
    if (ctx && est + Math.min(c.max_tokens || 512, 512) > ctx) {
      await dialog("Too much for the model", html`<p style="margin-top:0">This would be about <b>${est.toLocaleString()}</b> tokens, but the model only remembers <b>${(+ctx).toLocaleString()}</b> (its context length).</p>
        <p>Attach a smaller file or fewer files, start a new chat, turn on <b>context compression</b> in the chat's Settings, or run the model with a longer context (pick it again in the <b>AI model</b> menu and raise the context length).</p>`, "OK");
      return;
    }
    if (mine) {
      c.messages.push(mine); A.attach = []; renderAttach();
      if (c.title === "New chat") c.title = (text || mine.files.map((f) => f.name).join(", ")).slice(0, 48);
    }
    const msg = { role: "assistant", content: "", thinking: "", pending: true };
    c.messages.push(msg);
    renderThread(); renderChatList();
    const ctrl = new AbortController(), sid = uid() + uid();
    A.stream = { ctrl, chat: c.id, sid };
    paintSend();
    const t0 = performance.now(); let first = 0, chunks = 0, timings = null, raf = 0;
    const paint = () => { raf = 0; paintLast(); };
    try {
      const r = await fetch("/api/ai/chat", { method: "POST", cache: "no-store", signal: ctrl.signal, headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify({ target: c.target, messages: msgs, temperature: c.temperature, max_tokens: noLimit ? 0 : c.max_tokens, stream_id: sid }) });
      if (r.status === 401) { location.href = "/login"; return; }
      if (!r.ok) { let j = {}; try { j = await r.json(); } catch (e) { /* not JSON */ } throw new Error(j.error || "The model didn't answer (HTTP " + r.status + ").") ; }
      const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim(); if (payload === "[DONE]") continue;
          let j; try { j = JSON.parse(payload); } catch (e) { continue; }
          if (j.error) throw new Error(typeof j.error === "string" ? j.error : j.error.message || "The model reported an error.");
          if (j.timings) timings = j.timings;
          const d = j.choices && j.choices[0] && j.choices[0].delta;
          if (d && (d.content || d.reasoning_content)) {
            if (!first) first = performance.now();
            chunks++;
            if (d.content) msg.content += d.content;
            if (d.reasoning_content) msg.thinking += d.reasoning_content;
            if (!raf) raf = requestAnimationFrame(paint);
          }
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") msg.error = (e.message || "Something went wrong.") + (msg.content ? "" : "");
    } finally {
      msg.pending = false;
      if (!msg.error && !msg.content && !msg.thinking) msg.error = ctrl.signal.aborted ? "Stopped." : "The model sent an empty answer.";
      const secs = (performance.now() - (first || t0)) / 1000, tps = timings && timings.predicted_per_second ? timings.predicted_per_second : (chunks > 1 && secs > 0 ? (chunks - 1) / secs : 0);
      if (!msg.error || msg.content) msg.meta = [tps ? tps.toFixed(1) + " tokens/s" : "", (timings && timings.predicted_n ? timings.predicted_n : chunks) + " tokens", ((performance.now() - t0) / 1000).toFixed(1) + " s", t.name].filter(Boolean).join(" · ");
      if (msg.error === "Stopped." && msg.content) msg.error = "";
      A.stream = null;
      paintSend();
      saveChats(); paintLast(); refreshChat();
      setTimeout(() => autoRun(c, msg), 50);
    }
  }

  // ------------------------------------------------------------------ models
  // loading progress: the % from the server, an ETA from how fast it has been rising
  const loadHist = [];
  function loadBlock(ld) {
    if (!ld || ld.phase === "ready") { loadHist.length = 0; return ""; }
    if (ld.phase === "waiting") {
      const sp = S.d.ai.split, dl = A.disk && (A.disk.downloads || []).find((d) => d.file === sp.model && d.state !== "done");
      if (ld.why === "downloading" && dl) return html`<div class="mt"><div class="row"><b>Downloading</b><span class="grow"></span>
        <span class="muted small">${dl.size ? Math.min(100, (100 * dl.got) / dl.size).toFixed(0) + "%" : ""}${dl.rate ? " · " + fmt.rate(dl.rate) : ""}${dl.eta != null ? " · about " + etaText(dl.eta) + " left" : ""}</span></div>
        <div class="bar"><i style="width:${dl.size ? Math.min(100, (100 * dl.got) / dl.size).toFixed(1) : 0}%"></i></div><div class="muted small" style="margin-top:6px">Then it loads into memory on each machine.</div></div>`;
      return html`<div class="mt"><div class="row"><b>Waiting</b><span class="grow"></span><span class="muted small">${ld.why === "downloading" ? "for the download to finish" : "getting llama.cpp"}</span></div></div>`;
    }
    if (ld.phase === "starting") return html`<div class="mt muted small">Starting the model server (${ld.why})…</div>`;
    const now = Date.now() / 1000;
    if (!loadHist.length || loadHist[loadHist.length - 1][1] !== ld.pct) loadHist.push([now, ld.pct]);
    while (loadHist.length > 2 && now - loadHist[0][0] > 120) loadHist.shift();
    let eta = ld.eta != null ? ld.eta : null;  // the server keeps the history (survives a reload)
    if (eta == null && loadHist.length >= 2) { const [t0, p0] = loadHist[0], rate = (ld.pct - p0) / (now - t0); if (rate > 0.001) eta = (100 - ld.pct) / rate; }
    return html`<div class="mt"><div class="row"><b>${ld.phase === "warming up" ? "Almost ready: warming up" : "Loading into memory"}</b><span class="grow"></span>
        <span class="muted small">${ld.pct.toFixed(0)}%${eta != null && ld.phase === "loading" ? " · about " + etaText(eta) + " left" : ""}</span></div>
      <div class="bar"><i style="width:${ld.pct}%"></i></div>
      <div class="row wrap muted small" style="gap:4px 16px;margin-top:6px">${(ld.nodes || []).map((n) => html`<span>${n.node}: ${fmt.bytes(n.got_mib * 1048576, 1)} of ${fmt.bytes(n.share_mib * 1048576, 1)}</span>`)}
        ${ld.file_pct != null ? html`<span>model file read: ${Math.min(100, ld.file_pct).toFixed(0)}%</span>` : ""}</div></div>`;
  }
  function splitCard() {
    const sp = S.d.ai && S.d.ai.split;
    if (!sp) return card("Split model", empty("No model is running across your machines", html`Pick one under <a href="#" data-ai-tab-link="search">Find models</a>; nodeyard downloads it and shares it out over your nodes.`));
    const ld = sp.load;
    const state = sp.loaded === false ? ["Unloaded", "warn"] : sp.ready ? ["Serving", "good"] : sp.download === "running" ? ["Downloading", "warn"] : ["Loading" + (ld && ld.phase === "loading" ? " " + ld.pct.toFixed(0) + "%" : ""), "warn"];
    const busy = A.targets && A.targets.busy;
    return card("Split model", html`${U.aiSummary(sp)}${loadBlock(ld)}
      <div class="row wrap" style="margin-top:16px;gap:8px">
        ${sp.ready ? html`<button class="btn primary" data-ai="goto-chat" data-target="split">Chat</button>` : ""}
        ${sp.loaded === false ? html`<button class="btn" data-ai="split-load" ${busy ? raw("disabled") : ""}>Load into memory</button>` : html`<button class="btn" data-ai="split-unload" ${busy ? raw("disabled") : ""}>Unload (free the memory)</button>`}
        <button class="btn" data-ai="split-status">Check progress</button>${sp.ready ? html`<button class="btn" data-ai="split-test">Speed test</button>` : ""}
        <button class="btn danger" data-ai="split-remove" ${busy ? raw("disabled") : ""}>Remove…</button></div>
      <p class="muted small" style="margin:12px 0 0">${sp.loaded === false ? "The files are still on disk, so loading again takes a minute or two." : "Unloading frees the memory on every machine but keeps the downloaded file."}</p>`, chip(state[0], state[1]));
  }
  function ollamaCard() {
    const has = S.d.pods.some((p) => p.namespace === "ai-inference" && p.name.startsWith("ollama"));
    if (!has) return card("Ollama (one model per machine)", empty("Ollama isn't set up on the cluster", html`<button class="btn primary" data-ai="ollama-deploy" style="margin-top:12px">Set up Ollama on every node</button>`));
    const pods = A.ollama || [];
    const rows = [];
    pods.forEach((p) => p.models.forEach((m) => rows.push({ pod: p.pod, node: p.node, m })));
    const cols = [
      { k: "name", t: "Model", v: (r) => r.m.name, r: (r) => html`<b>${r.m.name}</b><div class="sub">${[r.m.params, r.m.quant].filter(Boolean).join(" · ")}</div>` },
      { k: "node", t: "On", v: (r) => r.node, r: (r) => r.node },
      { k: "size", t: "Size", cls: "num", v: (r) => r.m.size, r: (r) => fmt.bytes(r.m.size) },
      { k: "state", t: "State", v: (r) => (r.m.loaded ? 1 : 0), r: (r) => (r.m.loaded ? chip("In memory · " + fmt.bytes(r.m.memory), "good") : chip("On disk")) },
      { k: "act", t: "", cls: "right nowrap", r: (r) => raw('<button class="btn small" data-ai="o-chat" data-pod="' + esc(r.pod) + '" data-model="' + esc(r.m.name) + '">Chat</button> ' +
        (r.m.loaded ? '<button class="btn small" data-ai="o-unload" data-pod="' + esc(r.pod) + '" data-model="' + esc(r.m.name) + '">Unload</button>' : '<button class="btn small" data-ai="o-load" data-pod="' + esc(r.pod) + '" data-model="' + esc(r.m.name) + '">Load</button>') +
        ' <button class="btn small danger" data-ai="o-rm" data-model="' + esc(r.m.name) + '" title="Delete from every node">Delete</button>') },
    ];
    return card("Ollama (one model per machine)", html`${A.ollama == null ? html`<div class="empty">Asking Ollama…</div>` : U.table("ollama", cols, rows, { k: "name", empty: "No models downloaded yet", emptySub: "Download one below." })}
      ${pods.filter((p) => p.error).map((p) => html`<p class="small" style="color:var(--warn)">${p.node}: ${p.error}</p>`)}
      <div class="row" style="margin-top:14px;gap:8px"><input class="input grow" id="o-pull" placeholder="Download a model, e.g. llama3.2:3b or hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M" aria-label="Model to download" spellcheck="false">
        <button class="btn primary" data-ai="o-pull">Download to every node</button></div>
      <p class="muted small" style="margin:8px 0 0">“Load” keeps the model in memory (30 minutes) so the first answer is quick; “Unload” frees it right away.</p>`, html`${plural(pods.length, "node")}`);
  }
  // Downloaded split models, unfinished downloads and weight caches on every node, with free disk.
  const cacheKey = (f) => f.replace(/\.gguf$/i, "").toLowerCase().replace(/[^a-z0-9-]/g, "-").slice(0, 40);
  const etaText = (t) => (t < 90 ? Math.round(t) + " s" : t < 3600 ? Math.round(t / 60) + " min" : Math.floor(t / 3600) + " h " + Math.round((t % 3600) / 60) + " min");
  function diskCard() {
    const D = A.disk;
    if (!D) return card("Downloaded models", A.diskErr ? html`<div class="empty"><b>Couldn't look at the disks</b>${A.diskErr}</div>` : html`<div class="empty"><div class="spin"></div>Looking at each machine's disk…</div>`);
    const kindName = { model: "Model file", partial: "Unfinished download", cache: "Weight cache" };
    const rows = [];
    let reclaim = 0;
    D.nodes.forEach((n) => n.items.forEach((it) => {
      if (it.kind === "disk") return;
      const base = it.kind === "partial" ? it.name.replace(/\.(part\d*|joining|copying)$/, "") : it.name;
      const busy = (D.downloads || []).some((d) => d.state === "running" && d.file === base);
      const inUse = it.kind === "model" ? it.name === D.in_use : it.kind === "cache" ? !!D.in_use && it.name === cacheKey(D.in_use) : busy;
      if (!inUse && it.kind !== "model") reclaim += it.bytes;
      rows.push({ node: n.node, it, base, inUse, busy });
    }));
    const disks = D.nodes.map((n) => { const d = n.items.find((x) => x.kind === "disk"); return d ? { node: n.node, free: d.free, cap: d.capacity } : null; }).filter(Boolean);
    const cols = [
      { k: "name", t: "File", cls: "wrap mono", v: (r) => r.it.name, r: (r) => html`${r.it.name}<div class="sub">${kindName[r.it.kind]}</div>` },
      { k: "node", t: "On", v: (r) => r.node, r: (r) => r.node },
      { k: "size", t: "Size", cls: "num", v: (r) => r.it.bytes, r: (r) => fmt.bytes(r.it.bytes) },
      { k: "state", t: "", v: (r) => (r.inUse ? 1 : 0), r: (r) => (r.inUse ? chip(r.busy ? "downloading" : "in use", r.busy ? "warn" : "good") : r.it.kind === "model" ? chip("on disk") : chip("can go", "warn")) },
      { k: "act", t: "", cls: "right nowrap", r: (r) => ((r.inUse && !r.busy) || r.it.kind === "cache" ? "" :
        raw('<button class="btn small danger" data-ai="m-rm" data-file="' + esc(r.base) + '">' + (r.busy ? "Stop + delete" : "Delete") + "</button>")) },
    ];
    const dls = (D.downloads || []).filter((d) => d.state !== "done");
    return card("Downloaded models", html`<div class="disks">${disks.map((d) => { const used = 1 - d.free / Math.max(1, d.cap); return html`<div><div class="row"><b>${d.node}</b><span class="grow"></span><span class="muted small">${fmt.bytes(d.free)} free of ${fmt.bytes(d.cap)}</span></div><div class="bar ${used > 0.9 ? "bad" : used > 0.8 ? "warn" : ""}"><i style="width:${(used * 100).toFixed(1)}%"></i></div></div>`; })}</div>
      ${dls.map((d) => html`<div class="mt"><div class="row"><span class="mono small grow" style="word-break:break-all">${d.file}</span><span class="muted small">${d.state === "stuck" ? "stuck: not enough disk" : d.state === "failed" ? "failed" : d.note ? d.note : fmt.bytes(d.got) + " of " + fmt.bytes(d.size) + (d.rate ? " · " + fmt.rate(d.rate) : "") + (d.eta != null ? " · about " + etaText(d.eta) + " left" : "")} · ${d.node}</span></div><div class="bar ${d.state === "running" ? "" : "bad"}"><i style="width:${d.size ? Math.min(100, (100 * d.got) / d.size).toFixed(1) : 0}%"></i></div></div>`)}
      <div class="mt">${U.table("disk-models", cols, rows, { k: "size", empty: "Nothing downloaded", emptySub: "Download a model from Find models." })}</div>
      <div class="row wrap" style="margin-top:14px;gap:8px">
        <button class="btn primary" data-ai="clean">Free up space${reclaim ? " (" + fmt.bytes(reclaim) + ")" : ""}</button>
        <button class="btn" data-ai="clean-models">…including unused models</button>
        <button class="btn" data-ai-tab-link="search">Download another</button>
        <button class="btn" data-ai="disk-refresh">Refresh</button></div>
      <p class="muted small" style="margin:10px 0 0">Every machine keeps a weight cache (its share of a model) so loading is quick. “Free up space” deletes caches of models that aren't running and unfinished downloads nothing is working on. The running model is never touched.</p>`, html`${plural(D.nodes.length, "machine")}`);
  }
  function renderModels() {
    if (!$("#ai-pane")) return;
    const recent = (A.targets && A.targets.recent) || [];
    setHTML($("#ai-pane"), html`${splitCard()}<div class="mt">${diskCard()}</div><div class="mt">${ollamaCard()}</div>
      ${recent.length ? html`<div class="mt">${card("Recent tasks", html`<div class="tasks">${recent.slice(0, 6).map((j) => html`<div class="row task" data-ai="open-job" data-id="${j.id}"><b>${j.title}</b><span class="grow"></span>${j.status === "running" ? chip("running…", "warn") : j.status === "ok" ? chip("done", "good") : chip("failed", "bad")}<span class="faint small">${ago(j.started)} ago</span></div>`)}</div>`)}</div>` : ""}`);
  }

  // ------------------------------------------------------------------ search
  const fitFor = (size) => {
    const d = S.d, splitPods = d.pods.filter((p) => p.namespace === "ai-split");
    let free = 0, big = 0;
    d.nodes.forEach((n) => {
      if (!n.ready) return;
      const avail = n.hw && n.hw.mem_available != null ? n.hw.mem_available : Math.max(0, (n.mem_total || 0) - (n.mem_used || 0));
      const mine = splitPods.filter((p) => p.node === n.name).reduce((s, p) => s + (p.mem || 0), 0);
      const f = Math.max(0, avail + mine - GiB);
      free += f; big = Math.max(big, f);
    });
    const need = size * 1.07 + GiB;
    return { free, big, need, split: free >= need * 1.05 ? "fits" : free >= need ? "tight" : "no", single: big >= size * 1.1 + 0.5 * GiB ? "fits" : "no" };
  };
  const verdict = (v) => (v === "fits" ? chip("fits", "good") : v === "tight" ? chip("tight", "warn") : chip("too big", "bad"));
  function buildSearch() {
    setHTML($("#ai-pane"), html`<div class="toolbar"><input class="input grow" id="s-q" placeholder="Search Hugging Face for GGUF models: qwen coder, llama 3.2, gemma…" value="${A.search.q}" aria-label="Search models" spellcheck="false" style="min-width:280px">
      <span class="seg" id="s-sort">${[["downloads", "Most downloaded"], ["likes", "Most liked"], ["trending", "Trending"], ["recent", "Recent"]].map(([v, l]) => raw('<button data-ai-sort="' + v + '">' + l + "</button>"))}</span></div>
      <div class="row wrap" id="s-chips" style="gap:6px;margin-bottom:14px">${["qwen coder", "llama 3.2", "gemma 3", "mistral", "deepseek", "phi", "abliterated"].map((s) => raw('<button class="chip btnlike" data-ai="s-chip" data-q="' + esc(s) + '">' + esc(s) + "</button>"))}</div>
      <div id="s-results"></div>`);
    if (!A.search.results && !A.search.loading) runSearch();
    refreshSearch();
  }
  async function runSearch() {
    A.search.loading = true; A.search.error = ""; refreshSearch();
    try {
      const r = await getJSON("/api/ai/search?q=" + encodeURIComponent(A.search.q) + "&sort=" + encodeURIComponent(A.search.sort) + "&limit=24");
      if (r.ok) A.search.results = r.results; else { A.search.error = r.error || "Search failed."; A.search.results = []; }
    } catch (e) { A.search.error = "Couldn't reach the dashboard server."; }
    A.search.loading = false; refreshSearch();
  }
  function refreshSearch() {
    const el = $("#s-results"); if (!el) return;
    $$("#s-sort button").forEach((b) => b.classList.toggle("on", b.dataset.aiSort === A.search.sort));
    const s = A.search;
    if (s.loading && !s.results) return setHTML(el, html`<div class="empty"><div class="spin"></div>Searching Hugging Face…</div>`);
    if (s.error) return setHTML(el, html`<div class="card"><div class="empty"><b>Couldn't search</b>${s.error}</div></div>`);
    if (!s.results || !s.results.length) return setHTML(el, html`<div class="card"><div class="empty"><b>No GGUF models found</b>Try a shorter search.</div></div>`);
    setHTML(el, s.results.map((m) => html`<div class="card result"><div class="row" style="gap:10px;align-items:flex-start"><div class="grow" style="min-width:0"><div style="font-weight:650;word-break:break-all">${m.id}</div>
      <div class="row wrap muted small" style="gap:6px 14px;margin-top:4px"><span>↓ ${num(m.downloads)}</span><span>♥ ${num(m.likes)}</span>${m.updated ? html`<span>updated ${m.updated.slice(0, 10)}</span>` : ""}</div>
      <div class="row wrap" style="gap:5px;margin-top:8px">${m.tags.map((t) => chip(t))}</div></div>
      <button class="btn small" data-ai="s-files" data-repo="${m.id}">${s.open === m.id ? "Hide files" : "Show files"}</button></div>${s.open === m.id ? filesPanel(m.id) : ""}</div>`));
  }
  function filesPanel(repo) {
    const f = A.search.files[repo];
    if (!f) return html`<div class="muted" style="padding:12px 0"><div class="spin" style="margin:0 8px 0 0;display:inline-block;vertical-align:middle;width:18px;height:18px"></div>Reading the file list…</div>`;
    if (f.error) return html`<div class="small" style="color:var(--bad);padding:12px 0">${f.error}</div>`;
    if (!f.files.length) return html`<div class="muted" style="padding:12px 0">This repository has no GGUF files.</div>`;
    const cols = [
      { k: "file", t: "File", cls: "wrap mono", v: (x) => x.file, r: (x) => x.file },
      { k: "quant", t: "Quality", v: (x) => x.quant, r: (x) => (x.quant ? chip(x.quant, "violet") : "") },
      { k: "size", t: "Size", cls: "num", v: (x) => x.size, r: (x) => fmt.bytes(x.size) + (x.parts > 1 ? " · " + x.parts + " parts" : "") },
      { k: "fit", t: "Fits on your cluster?", v: (x) => x.size, r: (x) => { const fit = fitFor(x.size); return html`<span class="nowrap">${verdict(fit.split)} <span class="faint small">split</span></span> <span class="nowrap">${verdict(fit.single)} <span class="faint small">one machine</span></span>`; } },
      { k: "act", t: "", cls: "right nowrap", r: (x) => raw((x.split_ok ? '<button class="btn small primary" data-ai="s-run" data-repo="' + esc(repo) + '" data-file="' + esc(x.file) + '" data-size="' + x.size + '">Run split</button> ' : "") +
        (x.split_ok ? '<button class="btn small" data-ai="s-download" data-repo="' + esc(repo) + '" data-file="' + esc(x.file) + '" title="Download now, run it later">Download</button> ' : "") +
        (x.ollama ? '<button class="btn small" data-ai="s-ollama" data-name="' + esc(x.ollama) + '">Run on Ollama</button> ' : "") + (x.split_ok ? '<button class="btn small" data-ai="s-plan" data-repo="' + esc(repo) + '" data-file="' + esc(x.file) + '" title="Show which machines it would use and its estimated speed">Check speed</button> ' : "") + '<button class="btn small" data-ai="s-copy" data-repo="' + esc(repo) + '" data-file="' + esc(x.file) + '">Copy command</button>') },
    ];
    return html`<div class="card" style="padding:4px;margin-top:12px;background:var(--panel2)">${U.table("files-" + repo, cols, f.files, { k: "size", empty: "No files" })}</div>
      <p class="muted small" style="margin:8px 4px 0">“Run split” shares the model over your machines (needs one file, not several parts). “Run on Ollama” downloads it into Ollama on every node. Sizes are checked against free memory right now.</p>`;
  }
  async function showFiles(repo) {
    const s = A.search; s.open = s.open === repo ? "" : repo; refreshSearch();
    if (s.open === repo && !s.files[repo]) {
      try { const r = await getJSON("/api/ai/files?repo=" + encodeURIComponent(repo)); s.files[repo] = r.ok ? { files: r.files } : { error: r.error || "Couldn't read the file list." }; } catch (e) { s.files[repo] = { error: "Couldn't reach the dashboard server." }; }
      refreshSearch();
    }
  }
  // the old model's file size on disk (what deleting it frees, caches not counted)
  const fileBytes = (file) => { let b = 0; ((A.disk && A.disk.nodes) || []).forEach((n) => n.items.forEach((it) => { if (it.kind === "model" && it.name === file) b += it.bytes; })); return b; };
  async function runModel(repo, file, size, localNode) {
    const d = S.d, sp = d.ai && d.ai.split, nodes = d.nodes.filter((n) => n.ready && n.mem_total >= 1.5 * GiB);
    const fit = size ? fitFor(size) : null, old = sp && sp.model && sp.model !== file ? sp.model : "", oldBytes = old ? fileBytes(old) : 0, have = !!localNode || installed().some((m) => m.file === file);
    const alias = file.replace(/\.gguf$/i, "").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").slice(0, 40);
    const vals = await dialog(old ? "Switch the AI model" : "Run this model across your machines", html`<p style="margin-top:0">${repo ? html`<b>${repo}</b><br>` : ""}<span class="mono small">${file}</span>${size ? " · " + fmt.bytes(size) : ""}${localNode ? " · on " + localNode : ""}</p>
      ${fit ? html`<p>${verdict(fit.split)} It needs about ${fmt.bytes(fit.need)} of your ${fmt.bytes(fit.free)} of free memory${old ? " (counting what the old model frees)" : ""}.</p>` : ""}
      <p class="muted small">${localNode ? "It's already downloaded on " + localNode + ", so it runs from there (" + localNode + " coordinates) and starts in a few minutes." : have ? "It's already downloaded, so it starts in a few minutes." : "It downloads first" + (size ? " (" + fmt.bytes(size, 1) + ")" : "") + ", then loads."}</p>
      ${old ? html`<div class="note"><b>First, ${sp.alias || old} is cleared away:</b><ol style="margin:6px 0 8px;padding-left:20px"><li>it is unloaded: its servers stop and every machine gets its memory back;</li>
        <li>its file and weight caches are deleted from every machine${oldBytes ? html` (frees ${fmt.bytes(oldBytes, 1)} and more)` : ""}.</li></ol>
        <label class="check"><input type="checkbox" data-v="keep"> <span>Keep its files on disk instead (switching back is quicker, but they take the space)</span></label></div>` : ""}
      ${sp && !old ? html`<p style="color:var(--warn)">This restarts the running model with these settings.</p>` : ""}
      <label class="field-l">Name for the API<input class="input" data-v="alias" value="${alias}" spellcheck="false"></label>
      <label class="field-l">Context length (how much text it can remember: attached files count)<input class="input" data-v="ctx" type="number" min="512" max="131072" step="512" value="${(sp && +sp.ctx) || 8192}"></label>
      <div class="field-l">Machines to use
        <label class="check" style="margin-top:8px"><input type="checkbox" data-v="auto" checked> <span><b>Pick the fastest machines automatically</b> (fewer, faster machines usually win: every extra one adds a network hop per word)</span></label>
        <div class="checklist">${nodes.map((n) => html`<label class="check"><input type="checkbox" data-v="n-${n.name}" checked> <span>${n.name}</span> <span class="faint small">${fmt.bytes(n.mem_total, 0)}</span></label>`)}</div>
        <div class="faint small" style="margin-top:6px">The ticks only count when automatic is off.</div></div>
      <p class="muted small">The file (${fmt.bytes(size, 1)}) goes to the machine with the most free disk, then each machine loads its share. Use “Check speed” in the file list to see the plan and its estimated speed first. You can close the progress window and watch on the Models tab.</p>`,
    old ? "Switch" : "Run it");
    if (!vals) return;
    const chosen = vals.auto ? [] : nodes.filter((n) => vals["n-" + n.name]).map((n) => n.name);
    if (!vals.auto && !chosen.length) { toast("Pick at least one machine."); return; }
    startJob(sp ? "switch" : "deploy", Object.assign(localNode ? { local: true } : { repo }, { file, ctx: +vals.ctx || 8192, alias: vals.alias, nodes: chosen, keep_old: !!vals.keep }),
      () => { loadTargets(true); loadDisk(true); });
  }

  // ------------------------------------------------------------------ API examples
  function renderApi() {
    const d = S.d, sp = d.ai && d.ai.split, host = location.hostname, port = sp && (sp.node_port || (sp.gate && sp.gate.port));
    const base = port ? "http://" + host + ":" + port + "/v1" : "http://NODE-IP:31435/v1", model = (sp && (sp.alias || "model")) || "your-model", key = A.key || "YOUR_API_KEY", keyless = !!(sp && sp.gate) && !A.useKey;
    const hdr = (pad) => (keyless ? "" : pad + "-H 'Authorization: Bearer " + key + "' \\\n");
    const ips = d.nodes.map((n) => n.internal_ip);
    const k2 = keyless ? "not-needed" : key;
    const ex = {
      curl: `curl ${base}/chat/completions \\\n  -H 'Content-Type: application/json' \\\n${hdr("  ")}  -d '{"model":"${model}","messages":[{"role":"user","content":"Hello!"}]}'`,
      stream: `curl -N ${base}/chat/completions \\\n  -H 'Content-Type: application/json' \\\n${hdr("  ")}  -d '{"model":"${model}","stream":true,"messages":[{"role":"user","content":"Write a haiku about clusters."}]}'`,
      models: keyless ? `curl ${base}/models` : `curl ${base}/models -H 'Authorization: Bearer ${key}'`,
      python: `from openai import OpenAI   # pip install openai\n\nclient = OpenAI(base_url="${base}", api_key="${k2}")\n\nreply = client.chat.completions.create(\n    model="${model}",\n    messages=[{"role": "user", "content": "Explain recursion in one sentence."}],\n)\nprint(reply.choices[0].message.content)\n\n# streaming\nfor chunk in client.chat.completions.create(model="${model}", stream=True,\n        messages=[{"role": "user", "content": "Count to five."}]):\n    print(chunk.choices[0].delta.content or "", end="", flush=True)`,
      requests: `import requests   # pip install requests\n\nr = requests.post(\n    "${base}/chat/completions",${keyless ? "" : `\n    headers={"Authorization": "Bearer ${key}"},`}\n    json={"model": "${model}", "messages": [{"role": "user", "content": "Hello!"}]},\n    timeout=600,\n)\nprint(r.json()["choices"][0]["message"]["content"])`,
      js: `// Node 18+ or any browser\nconst res = await fetch("${base}/chat/completions", {\n  method: "POST",\n  headers: { "Content-Type": "application/json"${keyless ? "" : `, Authorization: "Bearer ${key}"`} },\n  body: JSON.stringify({ model: "${model}", messages: [{ role: "user", content: "Hello!" }] }),\n});\nconsole.log((await res.json()).choices[0].message.content);`,
      editor: `# Continue (VS Code / JetBrains) config.yaml\nmodels:\n  - name: ${model}\n    provider: openai\n    model: ${model}\n    apiBase: ${base}\n    apiKey: ${k2}\n    roles: [chat, edit, apply]\n\n# Open WebUI / LibreChat / most chat apps: add an "OpenAI-compatible" connection\n#   URL: ${base}\n#   Key: ${k2}`,
    };
    const block = (title, text) => html`<div class="mt"><div class="muted small" style="margin-bottom:6px;font-weight:600">${title}</div><div class="cmd">${text}<button class="btn small" data-copy="${text}"><svg class="icon" style="width:14px;height:14px"><use href="#i-copy"/></svg>Copy</button></div></div>`;
    const oll = d.pods.some((p) => p.namespace === "ai-inference" && p.name.startsWith("ollama"));
    const osvc = d.services.find((s) => s.namespace === "ai-inference" && s.name === "ollama"), obase = osvc ? "http://" + (osvc.node_ports[0] ? host + ":" + osvc.node_ports[0] : osvc.cluster_ip + ":11434") : "http://NODE-IP:11434";
    setHTML($("#ai-pane"), html`<div class="grid g-2">
      ${card("Connect to the split model", sp ? html`<div class="kvl"><span class="muted">Base URL</span><span class="chip btnlike" data-copy="${base}"><span class="mono">${base}</span></span></div>
        <div class="kvl"><span class="muted">Model name</span><span class="chip btnlike" data-copy="${model}"><span class="mono">${model}</span></span></div>
        <div class="kvl"><span class="muted">API key</span><span>${sp.gate ? chip("not needed on your network", "good") : ""}${sp.auth ? (A.key ? html` <span class="chip btnlike" data-copy="${A.key}"><span class="mono">${A.key}</span></span>` : html` <button class="btn small" data-ai="reveal-key">Reveal the key</button>`) : (sp.gate ? "" : chip("none needed", "warn"))}</span></div>
        ${sp.gate ? html`<p class="muted small" style="margin:10px 0 0"><b>No key is needed from</b> ${sp.gate.trusted.map((n) => html`<span class="mono">${n}</span> `)}(your machines, LAN and Tailscale). <b>From anywhere else</b>, such as a router port-forward from the internet, send the key.</p>
          <label class="check" style="margin-top:10px"><input type="checkbox" data-ai="use-key" ${A.useKey ? raw("checked") : ""}> Show the examples for access from outside (with the key)</label>` : ""}
        <p class="muted small" style="margin:12px 0 0">The model's API key is <b>not</b> the dashboard password. It lives on the server in <span class="mono">/etc/nodeyard/secrets/ai-split-api-key</span>. It works on any machine address: ${ips.slice(0, 5).map((ip) => html`<span class="mono copy" data-copy="http://${ip}:${port || 31435}/v1">${ip}</span> `)}</p>` : empty("No split model is running", "Run one from the Models or Find models tabs to get its API address."))}
      ${NY.publicAccess && NY.publicAccess.api ? card("From the internet", html`<p class="muted small" style="margin-top:0">Public through Tailscale Funnel: works from any device, no Tailscale needed. The API key is always required here.</p>
        <div class="kvl"><span class="muted">Base URL</span><span class="chip btnlike" data-copy="${NY.publicAccess.api}"><span class="mono">${NY.publicAccess.api}</span></span></div>
        <div class="cmd mt">${"curl " + NY.publicAccess.api + "/chat/completions -H 'Content-Type: application/json' -H 'Authorization: Bearer " + (A.key || "YOUR_API_KEY") + "' -d '{\"model\":\"" + model + "\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}]}'"}<button class="btn small" data-copy="${"curl " + NY.publicAccess.api + "/chat/completions -H 'Content-Type: application/json' -H 'Authorization: Bearer " + (A.key || "YOUR_API_KEY") + "' -d '{\"model\":\"" + model + "\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}]}'"}">Copy</button></div>`) : ""}
      ${card("What the API understands", html`<p class="muted small" style="margin-top:0">It speaks the OpenAI API, so most tools just work: set the base URL and key and pick the model name.</p>
        <div class="tablewrap"><table class="tbl"><tbody>
          <tr><td class="mono">POST /v1/chat/completions</td><td class="muted">chat; add <span class="mono">"stream": true</span> for live tokens</td></tr>
          <tr><td class="mono">POST /v1/completions</td><td class="muted">plain text completion</td></tr>
          <tr><td class="mono">GET /v1/models</td><td class="muted">the model's name</td></tr>
          <tr><td class="mono">GET /health</td><td class="muted">is it ready? (no key needed)</td></tr>
          <tr><td class="mono">GET /</td><td class="muted">llama.cpp's own chat page</td></tr></tbody></table></div>
        <p class="muted small" style="margin-bottom:0">Reasoning models may spend part of <span class="mono">max_tokens</span> thinking: raise it if answers get cut off.</p>`)}</div>
      ${block("curl", ex.curl)}${block("curl: stream the answer as it's written", ex.stream)}${block("curl: list models", ex.models)}${block("Python: OpenAI library", ex.python)}${block("Python: requests only", ex.requests)}${block("JavaScript", ex.js)}${block("Editors and chat apps", ex.editor)}
      ${oll ? html`<div class="section-title">Ollama</div>${block("Ollama chat (its own API)", `curl ${obase}/api/chat -d '{"model":"llama3.2:3b","messages":[{"role":"user","content":"Hello!"}],"stream":false}'`)}${block("Ollama, OpenAI-compatible", `curl ${obase}/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"llama3.2:3b","messages":[{"role":"user","content":"Hello!"}]}'`)}<p class="muted small">Ollama has no API key; it is only reachable from inside the cluster${osvc && osvc.node_ports[0] ? "" : " (use kubectl port-forward, or deploy it with --nodeport)"}.</p>` : ""}`);
  }

  // ------------------------------------------------------------------ events
  document.addEventListener("click", async (e) => {
    const t = e.target, tabLink = t.closest("[data-ai-tab-link]");
    if (tabLink) { e.preventDefault(); selectTab(tabLink.dataset.aiTabLink); return; }
    const tab = t.closest("[data-ai-tab]");
    if (tab) { selectTab(tab.dataset.aiTab); return; }
    const sortBtn = t.closest("[data-ai-sort]");
    if (sortBtn) { A.search.sort = sortBtn.dataset.aiSort; A.search.results = null; runSearch(); return; }
    const el = t.closest("[data-ai]"); if (!el) return;
    const a = el.dataset.ai, c = curChat();
    if (a === "new-chat") { newChat(); buildChat(); }
    else if (a === "open-chat") { A.cur = el.dataset.id; saveChats(); buildChat(); }
    else if (a === "del-chat") { e.stopPropagation(); A.chats = A.chats.filter((x) => x.id !== el.dataset.id); if (A.cur === el.dataset.id) A.cur = (A.chats[0] || {}).id || ""; saveChats(); buildChat(); }
    else if (a === "clear-chat" && c) { if (A.stream) A.stream.ctrl.abort(); postJSON("/api/ai/agent/reset", { chat: c.id }).catch(() => {}); c.messages = []; c.summary = ""; c.summaryUpTo = 0; c.title = "New chat"; saveChats(); renderThread(); renderChatList(); refreshChat(); }
    else if (a === "compress-now" && c) { compressChat(c, { manual: true }); }
    else if (a === "agent-allow" || a === "agent-always" || a === "agent-deny") { agentReply(c, a); }
    else if (a === "stop-compress") { if (A.sumCtrl) A.sumCtrl.abort(); }
    else if (a === "chat-settings") settingsPanel();
    else if (a === "suggest") { send(el.textContent); }
    else if (a === "stop") stopStream();
    else if (a === "slash-pick") { slashPick(el.dataset.cmd); }
    else if (a === "run-code") { runCodeBlock(el); }
    else if (a === "fix-code") { fixCode(el); }
    else if (a === "copy-code") { const pre = el.closest(".code").querySelector("pre"); copyText(pre.textContent); }
    else if (a === "dl-code") { const box = el.closest(".code"), text = box.querySelector("pre").textContent; saveText(text.endsWith("\n") ? text : text + "\n", codeName(box.querySelector(".code-head span").textContent, text)); }
    else if (a === "file-dl" || a === "file-copy") {
      e.preventDefault();  // (the buttons sit in the card's summary: don't open/close it)
      const card = el.closest(".filecard"), f = msgFiles(card.dataset.mi).files[+card.dataset.fi];
      if (!f) return;
      if (a === "file-dl") saveText(f.content, baseName(f.path)); else copyText(f.content);
    } else if (a === "files-zip") {
      const pf = msgFiles(el.dataset.mi);
      saveBlob(makeZip(pf.files.map((f) => ({ path: f.path, text: f.content })).concat(pf.folders.map((p) => ({ path: p, dir: true })))), zipName(pf));
    } else if (a === "attach") { const inp = $("#attach-input"); if (inp) inp.click(); }
    else if (a === "att-rm") { A.attach.splice(+el.dataset.i, 1); renderAttach(); }
    else if (a === "att-dl" && c) { const f = (c.messages[+el.dataset.mi].files || [])[+el.dataset.fi]; if (f && f.text != null) saveText(f.text, baseName(f.name)); else toast("That file wasn't kept by this browser."); }
    else if (a === "copy-msg" && c) copyText(c.messages[+el.dataset.i].content);
    else if (a === "regen" && c && !A.stream) { while (c.messages.length && c.messages[c.messages.length - 1].role !== "user") c.messages.pop(); send("", true); }
    else if (a === "retry" && c && !A.stream) { while (c.messages.length && c.messages[c.messages.length - 1].role !== "user") c.messages.pop(); send("", true); }
    else if (a === "goto-chat") { newChat(el.dataset.target); selectTab("chat"); }
    else if (a === "o-chat") { newChat("ollama:" + el.dataset.pod + ":" + el.dataset.model); selectTab("chat"); }
    else if (a === "split-unload") { const v = await dialog("Unload the split model?", html`Every machine gets its memory back. The downloaded file stays on disk, so loading again takes a minute or two. Chat stops working until you load it.`, "Unload"); if (v) startJob("split-unload", {}, () => loadTargets(true)); }
    else if (a === "split-load") startJob("split-load", {}, () => loadTargets(true));
    else if (a === "split-status") startJob("status");
    else if (a === "split-test") startJob("test");
    else if (a === "split-remove") {
      const sp = S.d.ai && S.d.ai.split, file = sp && sp.model, bytes = file ? fileBytes(file) : 0;
      const v = await dialog("Remove the split model?", html`<p style="margin-top:0">It is <b>unloaded first</b> (its servers stop and every machine gets its memory back), then removed.</p>
        ${file ? html`<label class="check"><input type="checkbox" data-v="files"> <span>Also delete its downloaded file and weight caches from every machine${bytes ? " (frees " + fmt.bytes(bytes, 1) + " and more)" : ""}. Otherwise they stay, so running it again is quick.</span></label>` : ""}`, "Remove it", { danger: true });
      if (v) startJob("undeploy", {}, (j) => { loadTargets(true); if (v.files && file && j.status === "ok") startJob("split-rm", { file }, () => loadDisk(true)); else loadDisk(true); });
    }
    else if (a === "ollama-deploy") startJob("ollama-deploy", {}, () => { loadOllama(true); loadTargets(true); });
    else if (a === "o-pull") { const name = ($("#o-pull") || {}).value; if (!name || !name.trim()) { toast("Type a model name first."); return; } startJob("pull", { name: name.trim() }, () => loadOllama(true)); }
    else if (a === "o-load" || a === "o-unload") {
      el.disabled = true; el.textContent = a === "o-load" ? "Loading…" : "Unloading…";
      try { const r = await postJSON("/api/ai/ollama-load", { pod: el.dataset.pod, model: el.dataset.model, load: a === "o-load" }); if (!r.ok) toast(r.error || "That didn't work."); } catch (err) { /* signed out */ }
      loadOllama(true); loadTargets(true);
    } else if (a === "open-job") openJob(el.dataset.id);
    else if (a === "o-rm") { const v = await dialog("Delete this Ollama model?", html`<span class="mono">${el.dataset.model}</span> is deleted from every machine that runs Ollama. You can download it again later.`, "Delete", { danger: true }); if (v) startJob("ollama-rm", { name: el.dataset.model }, () => loadOllama(true)); }
    else if (a === "m-rm") { const v = await dialog("Delete this model?", html`<span class="mono">${el.dataset.file}</span>, any unfinished parts of it and its weight caches are deleted from every machine. A download of it that is still running is stopped.`, "Delete", { danger: true }); if (v) startJob("split-rm", { file: el.dataset.file }, () => loadDisk(true)); }
    else if (a === "clean") startJob("clean", {}, () => loadDisk(true));
    else if (a === "clean-models") { const v = await dialog("Free up space, including models?", html`This also deletes every downloaded model file that isn't running now. The running model stays.`, "Delete them", { danger: true }); if (v) startJob("clean", { models: true }, () => loadDisk(true)); }
    else if (a === "disk-refresh") loadDisk(true);
    else if (a === "s-download") startJob("download", { repo: el.dataset.repo, file: el.dataset.file }, () => { loadDisk(true); });
    else if (a === "s-plan") startJob("plan", { repo: el.dataset.repo, file: el.dataset.file });
    else if (a === "s-chip") { A.search.q = el.dataset.q; const i = $("#s-q"); if (i) i.value = A.search.q; A.search.results = null; runSearch(); }
    else if (a === "s-files") showFiles(el.dataset.repo);
    else if (a === "s-run") runModel(el.dataset.repo, el.dataset.file, +el.dataset.size);
    else if (a === "s-ollama") { if (!S.d.pods.some((p) => p.namespace === "ai-inference" && p.name.startsWith("ollama"))) { toast("Set up Ollama first (Models tab)."); selectTab("models"); return; } const v = await dialog("Download into Ollama?", html`<span class="mono">${el.dataset.name}</span> is downloaded onto every machine that runs Ollama. Large models can take a while.`, "Download"); if (v) startJob("pull", { name: el.dataset.name }, () => loadOllama(true)); }
    else if (a === "s-copy") copyText("sudo nodeyard ai split deploy --model " + el.dataset.repo + ":" + el.dataset.file);
    else if (a === "reveal-key") { try { const r = await postJSON("/api/ai/reveal-key", {}); if (r.ok) { A.key = r.key; A.useKey = true; renderApi(); } else toast(r.error || "Couldn't read the key."); } catch (err) { /* signed out */ } }
    else if (a === "use-key") { A.useKey = el.checked; renderApi(); }
  });
  document.addEventListener("submit", (e) => {
    if (e.target.id !== "composer") return;
    e.preventDefault();
    const ta = $("#prompt"), text = ta.value; ta.value = ""; ta.style.height = "auto";
    const mm = $("#slash-menu"); if (mm) mm.remove();
    if (/^\/[a-z?]*(\s|$)/i.test(text.trim()) && !/^\/\//.test(text.trim())) { slash(text.trim()); return; }
    send(text.replace(/^\/\//, "/"));
  });
  document.addEventListener("input", (e) => { if (e.target.id === "prompt") slashMenu(); });
  document.addEventListener("keydown", (e) => {
    const sm = $("#slash-menu");
    if (sm && e.target.id === "prompt") {
      const names = (sm.dataset.items || "").split(","), n = names.length, sel = +sm.dataset.sel || 0;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); sm.dataset.sel = (sel + (e.key === "ArrowDown" ? 1 : n - 1)) % n; slashMenuRedraw(); return; }
      if (e.key === "Tab" || (e.key === "Enter" && $("#prompt").value.slice(1).toLowerCase() !== names[sel])) { e.preventDefault(); slashPick(names[sel]); return; }
      if (e.key === "Escape") { sm.remove(); return; }
    }
    if (e.target.id === "prompt" && e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
    if (e.target.id === "s-q" && e.key === "Enter") { A.search.q = e.target.value; A.search.results = null; runSearch(); }
    if (e.key === "Escape" && $("#confirmm").classList.contains("on")) $("#confirmcancel").click();
    else if (e.key === "Escape" && A.stream && S.view === "ai" && A.tab === "chat") stopStream();
  });
  // attach by dropping files on the chat, or pasting them into the message box
  let dragDepth = 0;
  const dz = (on) => { const z = $("#dropzone"); if (z) z.classList.toggle("on", on); };
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  document.addEventListener("dragenter", (e) => { if (!hasFiles(e) || !e.target.closest || !e.target.closest(".chat-main")) return; dragDepth++; dz(true); });
  document.addEventListener("dragleave", (e) => { if (!hasFiles(e) || !e.target.closest || !e.target.closest(".chat-main")) return; if (--dragDepth <= 0) { dragDepth = 0; dz(false); } });
  document.addEventListener("dragover", (e) => { if (hasFiles(e) && e.target.closest && e.target.closest(".chat-main")) e.preventDefault(); });
  document.addEventListener("drop", (e) => {
    if (!hasFiles(e) || !e.target.closest || !e.target.closest(".chat-main")) return;
    e.preventDefault(); dragDepth = 0; dz(false); addFiles(e.dataTransfer.files);
  });
  document.addEventListener("paste", (e) => { if (e.target.id === "prompt" && e.clipboardData && e.clipboardData.files && e.clipboardData.files.length) { e.preventDefault(); addFiles(e.clipboardData.files); } });
  document.addEventListener("input", (e) => {
    const t = e.target, c = curChat();
    if (t.id === "prompt") { t.style.height = "auto"; t.style.height = Math.min(220, t.scrollHeight) + "px"; }
    else if (t.id === "cs-system" && c) { c.system = t.value; saveChats(); }
    else if (t.id === "cs-temp" && c) { c.temperature = +t.value; $("#cs-t-v").textContent = t.value; saveChats(); }
    else if (t.id === "cs-max" && c) { c.max_tokens = Math.max(16, Math.min(65536, +t.value || 1024)); saveChats(); }
    else if (t.id === "cs-nolimit" && c) { c.max_tokens = t.checked ? 0 : (+($("#cs-max").value) || 1024); $("#cs-max").disabled = t.checked; saveChats(); }
    else if (t.id === "cs-compress" && c) { c.compress = t.value; saveChats(); refreshChat(); }
    else if (t.id === "cs-web" && c) { c.web = t.checked; saveChats(); }
    else if (t.id === "cs-autofix" && c) { c.autofix = t.checked; saveChats(); }
    else if (t.id && t.id.startsWith("cp-") && c) { const id = t.id.slice(3); c.plugins = (c.plugins || []).filter((x) => x !== id).concat(t.checked ? [id] : []); saveChats(); postJSON("/api/ai/agent/reset", { chat: c.id }).catch(() => {}); }
  });
  document.addEventListener("change", (e) => {
    const c = curChat();
    if (e.target.id === "chat-target") { if (c) { c.target = e.target.value; saveChats(); refreshChat(true); } }
    else if (e.target.id === "ai-model") pickModel(e.target);
    else if (e.target.id === "attach-input") { addFiles(e.target.files).then(() => { e.target.value = ""; }); }
    else if (e.target.id === "cs-files" && c) { c.files = e.target.checked; saveChats(); }
  });
})();
