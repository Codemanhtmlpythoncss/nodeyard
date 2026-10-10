// The Terminal page: shells on this server, on every cluster node (through Kubernetes) and on Tailscale devices
// (SSH), several at once in tabs, with a small built-in terminal emulator (no outside code).
// It understands what shells, kubectl, htop, nano and vim send: colours, cursor moves, clearing,
// scroll regions and the alternate screen. Loaded after app.js.
(function () {
  "use strict";
  const { html, raw, fmt } = NY;
  const U = NY.ui;
  const { S, V, $, chip, card, toast, getJSON, postJSON } = U;

  // ---------------------------------------------------------------- the emulator
  const DEF = 256; // "default colour"
  const pack = (fg, bg, bold, ul, inv, dim, it) => fg | (bg << 9) | (bold << 18) | (ul << 19) | (inv << 20) | (dim << 21) | (it << 22);
  const BASE = pack(DEF, DEF, 0, 0, 0, 0, 0);
  const PALETTE = ["#1d1f21", "#e0605f", "#5fbf73", "#d8b04a", "#5b8def", "#b57edc", "#45b8c8", "#c9ccd3",
    "#6b7280", "#ff7b7a", "#7ee08f", "#f0cc6a", "#82a8ff", "#d29cf5", "#6fd6e3", "#ffffff"];
  const color256 = (n) => {
    if (n < 16) return PALETTE[n];
    if (n < 232) { n -= 16; const v = [0, 95, 135, 175, 215, 255]; return "rgb(" + v[Math.floor(n / 36)] + "," + v[Math.floor(n / 6) % 6] + "," + v[n % 6] + ")"; }
    const g = 8 + (n - 232) * 10; return "rgb(" + g + "," + g + "," + g + ")";
  };
  const nearest256 = (r, g, b) => { const q = (v) => (v < 48 ? 0 : v < 115 ? 1 : Math.floor((v - 35) / 40)); return 16 + 36 * q(r) + 6 * q(g) + q(b); };

  class Term {
    constructor(cols, rows) { this.reset(cols, rows); }
    reset(cols, rows) {
      this.cols = cols; this.rows = rows; this.attr = BASE;
      this.main = this.blank(rows); this.alt = null; this.scr = this.main; this.back = []; this.pendingBack = [];
      this.x = 0; this.y = 0; this.top = 0; this.bot = rows - 1; this.wrapNext = false; this.saved = null;
      this.cursorOn = true; this.appKeys = false; this.state = 0; this.buf = ""; this.dirty = true; this.replies = [];
    }
    blankRow() { return { c: new Array(this.cols).fill(" "), a: new Array(this.cols).fill(BASE) }; }
    blank(n) { const r = []; for (let i = 0; i < n; i++) r.push(this.blankRow()); return r; }
    resize(cols, rows) {
      if (cols === this.cols && rows === this.rows) return;
      const fix = (scr) => {
        scr.forEach((r) => { while (r.c.length < cols) { r.c.push(" "); r.a.push(BASE); } r.c.length = cols; r.a.length = cols; });
        // rows below the cursor go first (usually empty), then the oldest ones into the scrollback
        while (scr.length > rows) {
          if (scr === this.scr && scr.length - 1 > this.y) scr.pop();
          else { const r = scr.shift(); if (scr === this.main) this.pushBack(r); if (scr === this.scr) this.y = Math.max(0, this.y - 1); }
        }
        while (scr.length < rows) scr.push({ c: new Array(cols).fill(" "), a: new Array(cols).fill(BASE) });
      };
      this.cols = cols; fix(this.main); if (this.alt) fix(this.alt); this.rows = rows;
      this.top = 0; this.bot = rows - 1; this.x = Math.min(this.x, cols - 1); this.y = Math.min(this.y, rows - 1); this.dirty = true;
    }
    pushBack(row) { this.back.push(row); this.pendingBack.push(row); if (this.back.length > 3000) this.back.shift(); if (this.pendingBack.length > 3000) this.pendingBack.shift(); }
    scrollUp(n) {
      for (let i = 0; i < n; i++) {
        const gone = this.scr.splice(this.top, 1)[0];
        if (this.scr === this.main && this.top === 0) this.pushBack(gone);
        this.scr.splice(this.bot, 0, this.blankRow());
      }
    }
    scrollDown(n) { for (let i = 0; i < n; i++) { this.scr.splice(this.bot, 1); this.scr.splice(this.top, 0, this.blankRow()); } }
    lineFeed() { if (this.y === this.bot) this.scrollUp(1); else if (this.y < this.rows - 1) this.y++; }
    put(ch) {
      if (this.wrapNext) { this.x = 0; this.lineFeed(); this.wrapNext = false; }
      const row = this.scr[this.y]; row.c[this.x] = ch; row.a[this.x] = this.attr;
      if (this.x === this.cols - 1) this.wrapNext = true; else this.x++;
    }
    // erased cells keep the current background colour (what "clear" in a coloured app expects)
    eraseCells(row, from, to) { const r = this.scr[row], a = (this.attr & (0x1ff << 9)) | DEF; for (let i = from; i < to; i++) { r.c[i] = " "; r.a[i] = a; } }
    write(s) {
      for (let i = 0; i < s.length; i++) {
        const ch = s[i], code = s.charCodeAt(i);
        if (this.state === 0) {
          if (code >= 32 && code !== 127) { this.put(ch); continue; }
          if (ch === "\x1b") { this.state = 1; continue; }
          if (ch === "\r") { this.x = 0; this.wrapNext = false; }
          else if (ch === "\n" || ch === "\x0b" || ch === "\x0c") { this.lineFeed(); this.wrapNext = false; }
          else if (ch === "\b") { if (this.x > 0) this.x--; this.wrapNext = false; }
          else if (ch === "\t") { this.x = Math.min(this.cols - 1, (Math.floor(this.x / 8) + 1) * 8); }
        } else if (this.state === 1) { // after ESC
          this.state = 0;
          if (ch === "[") { this.state = 2; this.buf = ""; }
          else if (ch === "]") { this.state = 3; this.buf = ""; }
          else if (ch === "(" || ch === ")" || ch === "#") { this.state = 4; }
          else if (ch === "7") this.saved = { x: this.x, y: this.y, a: this.attr };
          else if (ch === "8") { if (this.saved) { this.x = this.saved.x; this.y = this.saved.y; this.attr = this.saved.a; } }
          else if (ch === "D") this.lineFeed();
          else if (ch === "E") { this.x = 0; this.lineFeed(); }
          else if (ch === "M") { if (this.y === this.top) this.scrollDown(1); else if (this.y > 0) this.y--; }
          else if (ch === "c") this.reset(this.cols, this.rows);
        } else if (this.state === 2) { // CSI
          if ((code >= 0x30 && code <= 0x3f) || code === 0x20 || code === 0x22 || code === 0x27) { this.buf += ch; continue; }
          this.state = 0; this.csi(this.buf, ch);
        } else if (this.state === 3) { // OSC: titles etc., ignored
          if (ch === "\x07") this.state = 0; else if (ch === "\x1b") this.state = 5; else if (this.buf.length < 512) this.buf += ch;
        } else if (this.state === 5) { this.state = ch === "\\" ? 0 : 3; }
        else if (this.state === 4) { this.state = 0; }
      }
      this.dirty = true;
    }
    csi(p, f) {
      const priv = p.startsWith("?"), args = (priv ? p.slice(1) : p).split(";").map((x) => (x === "" ? NaN : parseInt(x, 10)));
      const n = (i, d) => (isNaN(args[i]) || args[i] === undefined ? d : args[i]);
      const clampY = () => { this.y = Math.max(0, Math.min(this.rows - 1, this.y)); }, clampX = () => { this.x = Math.max(0, Math.min(this.cols - 1, this.x)); };
      this.wrapNext = false;
      switch (f) {
        case "A": this.y = Math.max(this.top <= this.y ? this.top : 0, this.y - n(0, 1)); break;
        case "B": case "e": this.y = Math.min(this.y <= this.bot ? this.bot : this.rows - 1, this.y + n(0, 1)); break;
        case "C": case "a": this.x += n(0, 1); clampX(); break;
        case "D": this.x -= n(0, 1); clampX(); break;
        case "E": this.x = 0; this.y += n(0, 1); clampY(); break;
        case "F": this.x = 0; this.y -= n(0, 1); clampY(); break;
        case "G": case "`": this.x = n(0, 1) - 1; clampX(); break;
        case "d": this.y = n(0, 1) - 1; clampY(); break;
        case "H": case "f": this.y = n(0, 1) - 1; this.x = n(1, 1) - 1; clampY(); clampX(); break;
        case "J": { const m = n(0, 0);
          if (m === 0) { this.eraseCells(this.y, this.x, this.cols); for (let r = this.y + 1; r < this.rows; r++) this.eraseCells(r, 0, this.cols); }
          else if (m === 1) { this.eraseCells(this.y, 0, this.x + 1); for (let r = 0; r < this.y; r++) this.eraseCells(r, 0, this.cols); }
          else { for (let r = 0; r < this.rows; r++) this.eraseCells(r, 0, this.cols); if (m === 3) this.back = []; }
          break; }
        case "K": { const m = n(0, 0); if (m === 0) this.eraseCells(this.y, this.x, this.cols); else if (m === 1) this.eraseCells(this.y, 0, this.x + 1); else this.eraseCells(this.y, 0, this.cols); break; }
        case "X": this.eraseCells(this.y, this.x, Math.min(this.cols, this.x + n(0, 1))); break;
        case "L": if (this.y >= this.top && this.y <= this.bot) for (let i = 0; i < n(0, 1); i++) { this.scr.splice(this.bot, 1); this.scr.splice(this.y, 0, this.blankRow()); } break;
        case "M": if (this.y >= this.top && this.y <= this.bot) for (let i = 0; i < n(0, 1); i++) { this.scr.splice(this.y, 1); this.scr.splice(this.bot, 0, this.blankRow()); } break;
        case "P": { const r = this.scr[this.y], k = Math.min(n(0, 1), this.cols - this.x); r.c.splice(this.x, k); r.a.splice(this.x, k); for (let i = 0; i < k; i++) { r.c.push(" "); r.a.push(BASE); } break; }
        case "@": { const r = this.scr[this.y], k = Math.min(n(0, 1), this.cols - this.x); for (let i = 0; i < k; i++) { r.c.splice(this.x, 0, " "); r.a.splice(this.x, 0, BASE); } r.c.length = this.cols; r.a.length = this.cols; break; }
        case "S": this.scrollUp(n(0, 1)); break;
        case "T": if (!priv) this.scrollDown(n(0, 1)); break;
        case "r": this.top = Math.max(0, n(0, 1) - 1); this.bot = Math.min(this.rows - 1, n(1, this.rows) - 1); if (this.top >= this.bot) { this.top = 0; this.bot = this.rows - 1; } this.x = 0; this.y = 0; break;
        case "s": this.saved = { x: this.x, y: this.y, a: this.attr }; break;
        case "u": if (this.saved) { this.x = this.saved.x; this.y = this.saved.y; } break;
        case "m": this.sgr(args); break;
        case "n": if (n(0, 0) === 6) this.replies.push("\x1b[" + (this.y + 1) + ";" + (this.x + 1) + "R"); else if (n(0, 0) === 5) this.replies.push("\x1b[0n"); break;
        case "c": if (!p.startsWith(">")) this.replies.push("\x1b[?1;2c"); break;
        case "h": case "l": if (priv) args.forEach((m) => this.mode(m, f === "h")); break;
        default: break;
      }
    }
    mode(m, on) {
      if (m === 25) this.cursorOn = on;
      else if (m === 1) this.appKeys = on;
      else if (m === 1049 || m === 47 || m === 1047) {
        if (on && !this.alt) { if (m === 1049) this.saved = { x: this.x, y: this.y, a: this.attr }; this.alt = this.blank(this.rows); this.scr = this.alt; this.x = 0; this.y = 0; }
        else if (!on && this.alt) { this.alt = null; this.scr = this.main; if (m === 1049 && this.saved) { this.x = this.saved.x; this.y = this.saved.y; } }
        this.top = 0; this.bot = this.rows - 1;
      }
    }
    sgr(a) {
      if (!a.length || (a.length === 1 && isNaN(a[0]))) a = [0];
      let fg = this.attr & 0x1ff, bg = (this.attr >> 9) & 0x1ff, bold = (this.attr >> 18) & 1, ul = (this.attr >> 19) & 1, inv = (this.attr >> 20) & 1, dim = (this.attr >> 21) & 1, it = (this.attr >> 22) & 1;
      for (let i = 0; i < a.length; i++) {
        const v = isNaN(a[i]) ? 0 : a[i];
        if (v === 0) { fg = DEF; bg = DEF; bold = ul = inv = dim = it = 0; }
        else if (v === 1) bold = 1; else if (v === 2) dim = 1; else if (v === 3) it = 1; else if (v === 4) ul = 1; else if (v === 7) inv = 1;
        else if (v === 22) { bold = 0; dim = 0; } else if (v === 23) it = 0; else if (v === 24) ul = 0; else if (v === 27) inv = 0;
        else if (v >= 30 && v <= 37) fg = v - 30; else if (v === 39) fg = DEF; else if (v >= 40 && v <= 47) bg = v - 40; else if (v === 49) bg = DEF;
        else if (v >= 90 && v <= 97) fg = v - 82; else if (v >= 100 && v <= 107) bg = v - 92;
        else if (v === 38 || v === 48) {
          let c = null;
          if (a[i + 1] === 5) { c = a[i + 2]; i += 2; } else if (a[i + 1] === 2) { c = nearest256(a[i + 2] || 0, a[i + 3] || 0, a[i + 4] || 0); i += 4; }
          if (c != null && !isNaN(c)) { if (v === 38) fg = c & 255; else bg = c & 255; }
        }
      }
      this.attr = pack(fg, bg, bold, ul, inv, dim, it);
    }
  }

  const styleOf = (a) => {
    let fg = a & 0x1ff, bg = (a >> 9) & 0x1ff;
    const bold = (a >> 18) & 1, ul = (a >> 19) & 1, inv = (a >> 20) & 1, dim = (a >> 21) & 1, it = (a >> 22) & 1;
    if (bold && fg < 8) fg += 8;
    let f = fg === DEF ? "" : color256(fg), b = bg === DEF ? "" : color256(bg);
    if (inv) { const t = f || "var(--text)"; f = b || "var(--solid)"; b = t; }
    let st = (f ? "color:" + f + ";" : "") + (b ? "background:" + b + ";" : "") + (bold ? "font-weight:700;" : "") + (ul ? "text-decoration:underline;" : "") + (dim ? "opacity:.65;" : "") + (it ? "font-style:italic;" : "");
    return st;
  };
  const escText = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  function rowHTML(r, cursorX) {
    let out = "", run = "", cur = null;
    const flush = () => { if (run) { const st = styleOf(cur); out += st ? '<span style="' + st + '">' + escText(run) + "</span>" : escText(run); run = ""; } };
    for (let i = 0; i < r.c.length; i++) {
      if (i === cursorX) { flush(); const st = styleOf(r.a[i]); out += '<span class="tcur"' + (st ? ' style="' + st + '"' : "") + ">" + escText(r.c[i]) + "</span>"; cur = null; continue; }
      if (r.a[i] !== cur) { flush(); cur = r.a[i]; }
      run += r.c[i];
    }
    flush();
    return out;
  }

  // ---------------------------------------------------------------- the page
  // T.list: every open terminal (each keeps streaming while another tab is shown); T.sess: the one on screen,
  // or null for the device list.
  const T = { info: null, targets: null, list: [], sess: null, error: "", busy: "", pw: "", intent: null };
  // ?term=node:NAME&method=root (links from the Mac app and the Nodes page): offer that one first
  (() => { const q = new URLSearchParams(location.search), id = q.get("term"); if (id) T.intent = { id: id.slice(0, 300), method: q.get("method") || "" }; })();
  const HOW = { local: "Shell", kube: "Shell", root: "Root shell", diagnose: "Checks", ssh: "SSH" };
  const TIPS = {
    local: "A login shell on this server as your user (not root; use sudo as usual).",
    kube: "A shell on this node as your user, through a short-lived Kubernetes helper pod (no SSH keys needed).",
    root: "A root shell on this node through a short-lived Kubernetes helper pod.",
    diagnose: "Read-only checks for a node that drops out (agent errors, out-of-memory, network links, clock, temperature), then a root shell.",
    ssh: "SSH from the server as the user below (its keys, or type the device's password in the terminal).",
  };
  const logins = (() => { try { return JSON.parse(localStorage.getItem("ny-ssh-logins") || "{}") || {}; } catch (e) { return {}; } })();
  const saveLogin = (id, v) => { logins[id] = v; try { localStorage.setItem("ny-ssh-logins", JSON.stringify(logins)); } catch (e) { /* private mode */ } };

  function size() {
    const box = $("#tbox"); if (!box) return { cols: 100, rows: 30 };
    // a drawn row is the true line height (rows have a minimum height taller than the text itself)
    const probe = $("#tprobe"), row = $("#tscreen > div"), cw = probe.getBoundingClientRect().width / 10 || 8;
    const ch = Math.max((row && row.getBoundingClientRect().height) || 0, probe.getBoundingClientRect().height) || 17;
    return { cols: Math.max(20, Math.floor((box.clientWidth - 16) / cw)), rows: Math.max(6, Math.floor((box.clientHeight - 12) / ch)) };
  }
  function render() {
    const s = T.sess; if (!s || !$("#tscreen")) return;
    const t = s.term;
    if (t.pendingBack.length) {
      const back = $("#tback");
      back.insertAdjacentHTML("beforeend", t.pendingBack.map((r) => "<div>" + (rowHTML(r, -1) || " ") + "</div>").join(""));
      t.pendingBack = [];
      while (back.childElementCount > 3000) back.firstElementChild.remove();
    }
    if (!t.dirty) return;
    t.dirty = false;
    $("#tscreen").innerHTML = t.scr.map((r, y) => "<div>" + (rowHTML(r, y === t.y && t.cursorOn && s.alive ? t.x : -1) || " ") + "</div>").join("");
    $("#tback").style.display = t.alt ? "none" : "";
    if (s.stick) { const box = $("#tbox"); box.scrollTop = box.scrollHeight; }
  }
  let raf = 0;
  const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); }); setTimeout(() => { if (raf) { cancelAnimationFrame(raf); raf = 0; render(); } }, 60); };

  function send(data, s) {
    s = s || T.sess; if (!s || !s.alive) return;
    s.queue += data;
    if (s.sending) return;
    s.sending = true;
    (async () => {
      while (s.queue) {
        const chunk = s.queue; s.queue = "";
        try { const r = await postJSON("/api/term/input", { id: s.id, data: chunk }); if (!r.ok) { s.alive = false; toast(r.error || "The terminal closed."); paintTabs(); break; } } catch (e) { break; }
      }
      s.sending = false;
    })();
  }
  // Only the terminal on screen streams: a browser keeps at most 6 connections to one site, so one stream per tab
  // would soon block the rest of the dashboard. The others catch up from the server's buffer when shown.
  function attach() {
    T.list.forEach((x) => { if (x !== T.sess && x.es) { x.es.close(); x.es = null; } });
    const s = T.sess;
    if (s && !s.es && !s.done) connect(s);
  }
  async function poll() {
    if (!T.list.length) return;
    let r;
    try { r = await getJSON("/api/term/info"); } catch (e) { return; }
    if (!r || !r.ok) return;
    let changed = false;
    T.list.forEach((s) => {
      const x = (r.sessions || []).find((y) => y.id === s.id);
      if (!x) { if (!s.done) { s.done = true; s.alive = false; changed = true; } return; }   // gone (closed, or the dashboard restarted)
      if (!x.alive && s.alive && s !== T.sess) { s.alive = false; changed = true; }
      if (s !== T.sess && x.end > s.offset && !s.unseen) { s.unseen = true; changed = true; }
    });
    if (changed) { paintTabs(); paintBar(); }
  }
  function connect(s) {
    if (s.es) s.es.close();
    const es = new EventSource("/api/term/stream?id=" + encodeURIComponent(s.id) + "&from=" + s.offset);
    s.es = es;
    es.addEventListener("out", (ev) => {
      const m = JSON.parse(ev.data), bin = atob(m.d), bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      s.term.write(s.dec.decode(bytes, { stream: true }));
      s.offset = m.o;
      if (s.term.replies.length) { send(s.term.replies.join(""), s); s.term.replies = []; }
      if (T.sess === s) schedule(); else if (!s.unseen) { s.unseen = true; paintTabs(); }
    });
    es.addEventListener("exit", () => { s.alive = false; s.done = true; es.close(); s.es = null; s.term.dirty = true; if (T.sess === s) { schedule(); paintBar(); } paintTabs(); });
    es.onerror = () => {
      es.close(); if (s.es === es) s.es = null;
      if (!s.done && T.sess === s) setTimeout(() => { if (T.sess === s && !s.es && !s.done) { poll(); connect(s); } }, 1500);
    };
  }
  const KEYS = { Enter: "\r", Backspace: "\x7f", Tab: "\t", Escape: "\x1b", Delete: "\x1b[3~", Home: "\x1b[H", End: "\x1b[F", PageUp: "\x1b[5~", PageDown: "\x1b[6~", Insert: "\x1b[2~",
    F1: "\x1bOP", F2: "\x1bOQ", F3: "\x1bOR", F4: "\x1bOS", F5: "\x1b[15~", F6: "\x1b[17~", F7: "\x1b[18~", F8: "\x1b[19~", F9: "\x1b[20~", F10: "\x1b[21~", F11: "\x1b[23~", F12: "\x1b[24~" };
  function onKey(e) {
    const s = T.sess; if (!s) return;
    if ((e.metaKey || (e.ctrlKey && e.shiftKey)) && (e.key === "c" || e.key === "C" || e.key === "v" || e.key === "V")) return; // copy / paste stay with the browser
    let seq = null;
    if (e.key.startsWith("Arrow")) { const d = { ArrowUp: "A", ArrowDown: "B", ArrowRight: "C", ArrowLeft: "D" }[e.key]; seq = (s.term.appKeys ? "\x1bO" : "\x1b[") + d; }
    else if (KEYS[e.key]) seq = e.shiftKey && e.key === "Tab" ? "\x1b[Z" : KEYS[e.key];
    else if (e.ctrlKey && !e.altKey && e.key.length === 1) { const c = e.key.toUpperCase().charCodeAt(0); if (c >= 64 && c <= 95) seq = String.fromCharCode(c - 64); else if (e.key === " ") seq = "\x00"; }
    else if (e.altKey && !e.ctrlKey && e.key.length === 1) seq = "\x1b" + e.key;
    if (seq != null) { e.preventDefault(); s.stick = true; send(seq); }
  }
  function onInput(e) { const ta = e.target; if (ta.value) { send(ta.value.replace(/\n/g, "\r")); ta.value = ""; if (T.sess) T.sess.stick = true; } }
  function onPaste(e) { const t = (e.clipboardData || window.clipboardData).getData("text"); if (t) { e.preventDefault(); send(t.replace(/\r?\n/g, "\r")); } }

  function adopt(id, user, target, started) {
    const s = { id, user, target: target || { name: (S.res && S.res.host) || "server", method: "local" }, term: new Term(100, 30), dec: new TextDecoder(),
      offset: 0, alive: true, done: false, es: null, queue: "", sending: false, stick: true, started: started || Date.now() / 1000, unseen: false };
    T.list.push(s);
    return s;
  }
  async function openTerminal(targetId, method) {
    const pwEl = $("#t-pw");
    if (pwEl) T.pw = pwEl.value;
    const t = ((T.targets && T.targets.targets) || []).find((x) => x.id === targetId);
    const login = method === "ssh" ? ((($('[data-login="' + CSS.escape(targetId) + '"]') || {}).value || "").trim() || (T.targets && T.targets.user) || "") : "";
    if (method === "ssh" && login) saveLogin(targetId, login);
    T.error = ""; T.busy = targetId + "/" + method; paintDevices();
    let r;
    try { r = await postJSON("/api/term/open", { target: targetId, method, login, password: T.pw, cols: 100, rows: 30 }); } catch (e) { r = { ok: false, error: "The dashboard didn't answer." }; }
    T.busy = "";
    if (!r.ok) { T.error = (t ? t.name + ": " : "") + (r.error || "Couldn't open a terminal."); if (/password/i.test(r.error || "")) T.pw = ""; paintDevices(); return; }
    T.sess = adopt(r.id, r.user, r.target);
    if (T.intent && T.intent.id === targetId) T.intent = null;
    paintPage();
  }
  async function closeTerminal(s) {
    if (!s) return;
    try { await postJSON("/api/term/close", { id: s.id }); } catch (e) { /* gone */ }
    s.alive = false; s.done = true; if (s.es) { s.es.close(); s.es = null; }
    forget(s);
  }
  function forget(s) {
    const i = T.list.indexOf(s);
    if (i >= 0) T.list.splice(i, 1);
    if (T.sess === s) T.sess = T.list[Math.min(i, T.list.length - 1)] || null;
    paintPage();
  }
  function show(s) { T.sess = s; if (s) s.unseen = false; paintPage(); }

  function fit() {
    const s = T.sess; if (!s || !$("#tbox")) return;
    const { cols, rows } = size();
    if (cols !== s.term.cols || rows !== s.term.rows) { s.term.resize(cols, rows); schedule(); if (s.alive) postJSON("/api/term/resize", { id: s.id, cols, rows }).catch(() => {}); }
  }
  const label = (s) => s.target.name + (s.target.method && s.target.method !== "local" ? " · " + (HOW[s.target.method] || s.target.method) : "");
  function paintTabs() {
    const el = $("#ttabs"); if (!el) return;
    if (!T.list.length) { el.innerHTML = ""; return; }
    NY.freshHTML(el, html`<span class="seg term-tabs" role="tablist">${T.list.map((s, i) => raw('<button role="tab" data-term-tab="' + i + '" class="' + (s === T.sess ? "on" : "") + '" title="' +
      NY.esc(s.user + "@" + s.target.name + (s.alive ? "" : " (closed)")) + '"><span class="tdot ' + (s.alive ? (s.unseen ? "new" : "live") : "dead") + '"></span>' + NY.esc(label(s)) + "</button>"))}
      <button data-term="devices" class="${T.sess ? "" : "on"}" title="Open another terminal">+ New</button></span>`);
  }
  function paintBar() {
    const s = T.sess, el = $("#tbar"); if (!el || !s) return;
    const tg = s.target || {};
    NY.freshHTML(el, html`<b>${s.user}@${tg.name || "server"}</b><span class="faint small">${tg.how || ""}${tg.address ? " · " + tg.address : ""}</span>${s.alive ? chip("connected", "good") : chip("closed")}<span class="faint small">opened ${fmt.ago(s.started)} ago</span><span class="grow"></span>
      <button class="btn small" data-term="full">${document.body.classList.contains("term-full") ? "Exit full screen" : "Full screen"}</button>
      ${s.alive ? html`<button class="btn small danger" data-term="close">Close</button>` : html`<button class="btn small primary" data-term="again">Open again</button><button class="btn small" data-term="dismiss">Remove</button>`}`);
  }

  function statusChip(t) {
    if (t.kind === "server") return chip("this server", "good");
    if (t.kind === "node") return t.ready ? chip("Ready", "good") : chip(t.status || "NotReady", "bad");
    return t.online ? chip("online", "good") : chip(t.last_seen ? "offline · " + fmt.ago(t.last_seen) + " ago" : "offline");
  }
  function buttons(t) {
    if (!t.methods.length) return html`<span class="faint small">${t.note || "Can't be opened from here."}</span>`;
    return html`${t.methods.map((m) => {
      const busy = T.busy === t.id + "/" + m, cls = m === "diagnose" ? "btn small" : m === "root" ? "btn small warn" : "btn small primary";
      return raw('<button class="' + cls + '" data-topen="' + NY.esc(t.id) + '" data-method="' + m + '" title="' + NY.esc(TIPS[m] || "") + '"' + (T.busy ? " disabled" : "") + ">" +
        (busy ? "Opening…" : (m === "ssh" && t.kind === "node" ? "SSH" : HOW[m])) + "</button>");
    })}${t.methods.includes("ssh") ? raw('<input class="input small t-login" data-login="' + NY.esc(t.id) + '" placeholder="' + NY.esc((T.targets && T.targets.user) || "user") +
      '" value="' + NY.esc(logins[t.id] || "") + '" aria-label="SSH user for ' + NY.esc(t.name) + '" title="SSH user" autocapitalize="off" spellcheck="false">') : ""}`;
  }
  function deviceRow(t) {
    const where = t.kind === "node" ? [t.address && t.address !== "" ? t.address : "", t.tailnet && t.tailnet !== t.name ? "tailnet: " + t.tailnet : ""].filter(Boolean).join(" · ")
      : t.kind === "tailnet" ? [t.address, t.os].filter(Boolean).join(" · ") : "";
    const why = t.kind === "node" && !t.ready && t.reason ? t.reason : "";
    return html`<div class="trow ${t.online ? "" : "off"}"><div class="tname"><b>${t.name}</b>${statusChip(t)}${t.this_server ? chip("this server") : ""}
      <div class="faint small">${where}${why ? html` · <span style="color:var(--bad)">${why}</span>` : ""}</div></div><div class="tact">${buttons(t)}</div></div>`;
  }
  function intentBox(list) {
    const it = T.intent; if (!it) return "";
    const t = list.find((x) => x.id === it.id);
    if (!t) return html`<p class="dev-note">${it.id.replace(/^\w+:/, "")} isn't in the device list.</p>`;
    const m = t.methods.includes(it.method) ? it.method : t.methods[0];
    if (!m) return html`<p class="dev-note">${t.name}: ${t.note || "can't be opened from here."}</p>`;
    it.method = m;
    return html`<div class="dev-note tintent"><b>${HOW[m]} on ${t.name}</b> <span class="faint">${TIPS[m] || ""}</span>
      <div class="row wrap" style="gap:8px;margin-top:8px">${raw('<button class="btn small primary" data-topen="' + NY.esc(t.id) + '" data-method="' + m + '"' + (T.busy ? " disabled" : "") + ">" + (T.busy ? "Opening…" : "Open") + "</button>")}
      <span class="faint small">Type the dashboard password above, then press Return or Open.</span></div></div>`;
  }
  function paintDevices() {
    const host = $("#tdevices"); if (!host) return;
    const i = T.info, tg = T.targets;
    if (i && i.public) { NY.freshHTML(host, card("Terminal", html`<div class="empty"><b>Not over public access</b>Terminals only work over Tailscale or your own network.</div>`)); return; }
    if (!tg) { NY.freshHTML(host, card("Open a terminal", html`<div class="empty">${T.error || "Finding devices…"}</div>`)); return; }
    const list = tg.targets || [], nodes = list.filter((t) => t.kind === "node"), net = list.filter((t) => t.kind === "tailnet"), me = list.filter((t) => t.kind === "server");
    const group = (title, items, empty) => html`<div class="tgroup"><div class="tgh">${title}</div>${items.length ? items.map(deviceRow) : html`<div class="faint small" style="padding:8px 2px">${empty}</div>`}</div>`;
    NY.freshHTML(host, card("Open a terminal", html`
      <form id="t-open" class="row wrap" style="gap:8px;align-items:flex-end;margin-bottom:6px" onsubmit="return false"><label class="field-l grow" style="margin:0">Dashboard password, once more (asked for every terminal)
        <input class="input" type="password" id="t-pw" autocomplete="current-password" value="${T.pw}"></label>
        <button class="btn small" type="button" data-term="refresh">Refresh list</button></form>
      ${T.error ? html`<p class="small" style="color:var(--bad);margin:6px 0">${T.error}</p>` : ""}
      ${intentBox(list)}
      ${group("This server", me, "")}
      ${group("Cluster nodes", nodes, "No nodes yet.")}
      ${group("Tailscale devices", net, tg.tailnet_error || "No other devices on your tailnet.")}
      <p class="faint small" style="margin-bottom:0"><b>Shell</b> and <b>Root shell</b> on a node go through a short-lived Kubernetes helper pod, so they work without SSH keys while the node's kubelet runs; <b>Checks</b> runs read-only checks for a node that drops out, then leaves you in a root shell. <b>SSH</b> goes from the server as the user you type (default ${(tg && tg.user) || "your user"}), over Tailscale when the device has it: use it when a node isn't Ready. Idle terminals close after 30 minutes; copy and paste with your browser's usual shortcuts (Ctrl+Shift+C / V on Windows and Linux).</p>`));
  }
  function paintPage() {
    const host = $("#term-body"); if (!host) return;
    const i = T.info;
    if (i && !i.available && !T.list.length && !(T.targets && (T.targets.targets || []).some((t) => t.methods.length))) {
      NY.freshHTML(host, card("Terminal", html`<div class="empty"><b>Terminals aren't available</b>${i.why}</div>`));
      return;
    }
    if (!T.sess) {
      attach();
      NY.freshHTML(host, raw('<div id="ttabs" style="margin-bottom:10px"></div><div id="tdevices"></div>'));
      paintTabs(); paintDevices();
      const pw = $("#t-pw"); if (pw) { pw.addEventListener("input", () => { T.pw = pw.value; }); if (!T.pw) pw.focus(); }
      return;
    }
    NY.freshHTML(host, raw(`<div id="ttabs" style="margin-bottom:10px"></div><div class="card term-card"><div class="row wrap" id="tbar" style="gap:10px;margin-bottom:10px"></div>
      <div class="tbox" id="tbox" tabindex="0"><div id="tback"></div><div id="tscreen"></div><span id="tprobe" aria-hidden="true">MMMMMMMMMM</span>
      <textarea id="tin" aria-label="Terminal input" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false"></textarea></div></div>`));
    paintTabs(); paintBar(); attach();
    const box = $("#tbox"), ta = $("#tin");
    box.addEventListener("mouseup", () => { if (!String(window.getSelection())) ta.focus({ preventScroll: true }); });
    box.addEventListener("scroll", () => { T.sess && (T.sess.stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 4); });
    ta.addEventListener("keydown", onKey); ta.addEventListener("input", onInput); ta.addEventListener("paste", onPaste);
    // the terminal emulator keeps its own state: show what it has (also after coming back to this page or tab)
    const t = T.sess.term; t.pendingBack = t.back.slice(); t.dirty = true;
    requestAnimationFrame(() => { render(); fit(); render(); ta.focus({ preventScroll: true }); });
  }
  async function loadTargets() {
    try { const r = await getJSON("/api/term/targets"); if (r.ok) T.targets = r; else T.error = r.error || "Couldn't list devices."; } catch (e) { T.error = "Couldn't list devices."; }
    if (!T.sess) paintDevices();
  }

  V.terminal = {
    async build() {
      NY.freshHTML($("#view"), html`<div id="term-body"></div>`);
      try { const r = await getJSON("/api/term/info"); T.info = r.ok ? r : { available: false, why: r.error }; } catch (e) { T.info = null; }
      // terminals opened before a reload are still running on the server: pick them up again
      ((T.info && T.info.sessions) || []).forEach((x) => { if (x.alive && !T.list.some((s) => s.id === x.id)) adopt(x.id, (x.target && x.target.user) || "", x.target, x.started); });
      if (T.sess && !T.list.includes(T.sess)) T.sess = null;
      paintPage();
      await loadTargets();
      paintPage();
    },
    update() { if (T.sess) paintBar(); poll(); },
  };
  let resizeTimer = null;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(fit, 150); });
  document.addEventListener("submit", (e) => { if (e.target.id === "t-open") e.preventDefault(); });
  document.addEventListener("keydown", (e) => {
    if (e.target && e.target.classList && e.target.classList.contains("t-login") && e.key === "Enter") { e.preventDefault(); openTerminal(e.target.dataset.login, "ssh"); }
    if (e.target && e.target.id === "t-pw" && e.key === "Enter" && T.intent && T.intent.method && !T.busy) { e.preventDefault(); openTerminal(T.intent.id, T.intent.method); }
  });
  document.addEventListener("click", (e) => {
    const o = e.target.closest("[data-topen]");
    if (o) { if (!T.busy) openTerminal(o.dataset.topen, o.dataset.method); return; }
    const go = e.target.closest("[data-term-goto]");
    if (go) { e.preventDefault(); T.intent = { id: go.dataset.termGoto, method: go.dataset.method || "" }; T.sess = null; if (location.hash === "#terminal") paintPage(); else location.hash = "#terminal"; return; }
    const tab = e.target.closest("[data-term-tab]");
    if (tab) { show(T.list[+tab.dataset.termTab] || null); return; }
    const el = e.target.closest("[data-term]"); if (!el) return;
    const a = el.dataset.term;
    if (a === "close") closeTerminal(T.sess);
    else if (a === "dismiss") forget(T.sess);
    else if (a === "again") { const tg = T.sess.target || {}; forget(T.sess); show(null); openTerminal(tg.id || "local", tg.method || "local"); }
    else if (a === "devices") { show(null); loadTargets(); }
    else if (a === "refresh") { T.error = ""; loadTargets(); }
    else if (a === "full") { document.body.classList.toggle("term-full"); paintBar(); setTimeout(fit, 50); }
  });
  NY.Term = Term; // for tests
})();
