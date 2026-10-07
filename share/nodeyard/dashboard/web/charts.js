// Helpers shared by the dashboard: safe HTML templates, number formatting and
// small hand-made SVG charts (no libraries, nothing loaded from the internet).
(function () {
  "use strict";
  const NY = (window.NY = window.NY || {});

  // ---- safe HTML: values are escaped unless wrapped by html`` or raw() ----
  class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]);
  const part = (v) => (v instanceof Raw ? v.s : Array.isArray(v) ? v.map(part).join("") : v == null || v === false ? "" : esc(v));
  const html = (strings, ...vals) => { let o = strings[0]; vals.forEach((v, i) => { o += part(v) + strings[i + 1]; }); return new Raw(o); };
  const raw = (s) => new Raw(s);
  // setHTML patches the page in place instead of replacing it: elements that
  // are still there keep their scroll position, focus, hover, open menus,
  // typed text and running animations, and only changed text/attributes are
  // touched. That is what stops the page jolting on every refresh.
  // freshHTML replaces everything (for a view's first paint).
  const sameNode = (a, b) => a.nodeType === b.nodeType && a.nodeName === b.nodeName &&
    (a.nodeType !== 1 || ((a.id || "") === (b.id || "") && (a.getAttribute("data-k") || "") === (b.getAttribute("data-k") || "")));
  function morphAttrs(a, b) {
    const keepOpen = a.nodeName === "DETAILS" ? a.open : null;
    for (const at of Array.from(b.attributes)) if (a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value);
    for (const at of Array.from(a.attributes)) if (!b.hasAttribute(at.name) && at.name !== "data-touched") a.removeAttribute(at.name);
    if (keepOpen !== null) a.open = keepOpen; // the user opened or closed it
    const tag = a.nodeName;
    if ((tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") && a !== document.activeElement) {
      if (a.type === "checkbox" || a.type === "radio") { if (!a.dataset.touched) a.checked = b.hasAttribute("checked"); }
      else if (tag !== "SELECT" && !a.dataset.touched && a.value !== (b.getAttribute("value") || (tag === "TEXTAREA" ? b.textContent : ""))) a.value = tag === "TEXTAREA" ? b.textContent : (b.getAttribute("value") || "");
    }
  }
  function morph(a, b) {
    if (a.nodeType === 3 || a.nodeType === 8) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
    morphAttrs(a, b);
    if (a.hasAttribute("data-keep")) return; // managed by its own code (e.g. the chat thread while streaming)
    if (a.nodeName === "TEXTAREA") return;
    morphChildren(a, b);
  }
  function morphChildren(from, to) {
    let a = from.firstChild, b = to.firstChild;
    while (b) {
      const next = b.nextSibling;
      if (a && sameNode(a, b)) { morph(a, b); a = a.nextSibling; }
      else {
        // look a little ahead for a match (a row was added or removed above)
        let m = a, steps = 0;
        while (m && steps < 4 && !sameNode(m, b)) { m = m.nextSibling; steps++; }
        if (m && steps < 4 && sameNode(m, b)) {
          while (a !== m) { const dead = a; a = a.nextSibling; from.removeChild(dead); }
          morph(a, b); a = a.nextSibling;
        } else from.insertBefore(b, a);
      }
      b = next;
    }
    while (a) { const dead = a; a = a.nextSibling; from.removeChild(dead); }
  }
  const setHTML = (el, r) => {
    if (!el) return;
    const s = part(r);
    if (!el.firstChild) { el.innerHTML = s; return; }
    const tpl = document.createElement("template");
    tpl.innerHTML = s;
    morphChildren(el, tpl.content);
  };
  const freshHTML = (el, r) => { if (el) el.innerHTML = part(r); };
  // typed-in form fields keep what the user typed until the page rebuilds the form
  document.addEventListener("input", (e) => { if (e.target && e.target.dataset) e.target.dataset.touched = "1"; }, true);
  Object.assign(NY, { esc, html, raw, setHTML, freshHTML, Raw });

  // ---- formatting ----
  const UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  function bytes(n, d) {
    if (n == null || isNaN(n)) return "–";
    let i = 0, v = Math.abs(n);
    while (v >= 1024 && i < UNITS.length - 1) { v /= 1024; i++; }
    const dec = d != null ? d : v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2;
    return (n < 0 ? "-" : "") + v.toFixed(dec) + " " + UNITS[i];
  }
  const rate = (n) => (n == null || isNaN(n) ? "–" : bytes(n) + "/s");
  const cores = (c) => (c == null || isNaN(c) ? "–" : c < 1 ? Math.round(c * 1000) + "m" : c.toFixed(c >= 10 ? 1 : 2));
  const num = (n) => (n == null || isNaN(n) ? "–" : Math.round(n).toLocaleString());
  const pct = (f) => (f == null || isNaN(f) ? "–" : Math.round(f * 100) + "%");
  function dur(sec) {
    if (!isFinite(sec) || sec < 0) return "–";
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + (s && m < 10 ? s + "s" : "");
    return s + "s";
  }
  const ago = (ts, now) => (ts ? dur((now || Date.now() / 1000) - ts).trim() : "–");
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const shortClock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const mhz = (v) => (v == null || isNaN(v) ? "–" : v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 2).replace(/0$/, "") + " GHz" : Math.round(v) + " MHz");
  const temp = (v) => (v == null || isNaN(v) ? "–" : Math.round(v) + " °C");
  NY.fmt = { bytes, rate, cores, num, pct, dur, ago, clock, shortClock, mhz, temp };

  const level = (f) => (f >= 0.9 ? "bad" : f >= 0.75 ? "warn" : "");
  NY.level = level;
  NY.bar = (f, cls, thick) => {
    const w = Math.max(0, Math.min(1, isNaN(f) || f == null ? 0 : f)) * 100;
    return html`<div class="bar ${cls == null ? level(f) : cls} ${thick ? "thick" : ""}"><i style="width:${w.toFixed(1)}%"></i></div>`;
  };

  // ---- donut ring ----
  NY.donut = function (frac, o) {
    o = o || {};
    const size = o.size || 84, f = Math.max(0, Math.min(1, frac || 0)), r = 38, c = 2 * Math.PI * r;
    const col = o.color || (f >= 0.9 ? "var(--bad)" : f >= 0.75 ? "var(--warn)" : "var(--accent)");
    return html`<svg class="ring" width="${size}" height="${size}" viewBox="0 0 100 100" role="img" aria-label="${o.label || ""}">
      <circle cx="50" cy="50" r="${r}" fill="none" style="stroke:var(--panel2)" stroke-width="10"/>
      <circle cx="50" cy="50" r="${r}" fill="none" style="stroke:${col}" stroke-width="10" stroke-linecap="round"
        stroke-dasharray="${(c * f).toFixed(2)} ${c.toFixed(2)}" transform="rotate(-90 50 50)"/>
      <text x="50" y="${o.sub ? 49 : 56}" text-anchor="middle" style="fill:var(--text);font:700 21px var(--sans)">${o.label == null ? pct(f) : o.label}</text>
      ${o.sub ? html`<text x="50" y="66" text-anchor="middle" style="fill:var(--muted);font:500 10.5px var(--sans)">${o.sub}</text>` : ""}
    </svg>`;
  };

  // ---- sparkline ----
  NY.spark = function (values, o) {
    o = o || {};
    const v = values.map((x) => (x == null ? 0 : x));
    if (v.length < 2) return html``;
    const W = 200, H = 40, max = o.max || Math.max(...v, 1e-9), min = 0;
    const xs = (i) => (i / (v.length - 1)) * W, ys = (y) => H - 2 - ((y - min) / (max - min || 1)) * (H - 6);
    const line = v.map((y, i) => (i ? "L" : "M") + xs(i).toFixed(1) + " " + ys(y).toFixed(1)).join(" ");
    const col = o.color || "var(--accent)";
    return html`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" width="100%" height="100%" aria-hidden="true">
      <path d="${line} L${W} ${H} L0 ${H}Z" style="fill:${col};opacity:.22"/><path d="${line}" fill="none" style="stroke:${col}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
  };

  // ---- area/line chart with hover tooltip ----
  const registry = {};
  const nice = (v) => {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
  };
  NY.PALETTE = ["#7c8cff", "#22d3ee", "#34d399", "#fbbf24", "#f472b6", "#a78bfa", "#fb923c", "#60a5fa", "#f87171", "#2dd4bf"];

  // area(id, ts, series, {stacked, max, fmt, height}): series = [{name, color, values}]
  NY.area = function (id, ts, series, o) {
    o = o || {};
    const W = 640, H = o.height || 190, L = o.left || 52, R = 10, T = 10, B = 22;
    if (ts.length > 260) { // keep the page light: at most ~260 points per chart
      const k = Math.ceil(ts.length / 260), keep = ts.map((_, i) => i).filter((i) => i % k === 0 || i === ts.length - 1);
      series = series.map((s) => Object.assign({}, s, { values: keep.map((i) => s.values[i]) }));
      ts = keep.map((i) => ts[i]);
    }
    const fmt = o.fmt || ((v) => v.toFixed(1));
    if (ts.length < 2) return html`<div class="empty" style="padding:30px"><b>Collecting data…</b>Charts fill in as the dashboard keeps watching.</div>`;
    const n = ts.length, stacked = !!o.stacked;
    const cum = series.map(() => new Array(n).fill(0));
    for (let i = 0; i < n; i++) {
      let acc = 0;
      series.forEach((s, k) => { const v = s.values[i] == null ? 0 : s.values[i]; acc = stacked ? acc + v : v; cum[k][i] = acc; });
    }
    let top = 0;
    cum.forEach((a) => a.forEach((v) => { if (v > top) top = v; }));
    const max = o.max || nice(top * 1.08 || 1);
    const x0 = ts[0], x1 = ts[n - 1] || x0 + 1;
    const X = (t) => L + ((t - x0) / (x1 - x0 || 1)) * (W - L - R);
    const Y = (v) => T + (1 - Math.min(v, max) / max) * (H - T - B);
    registry[id] = { ts, series, stacked, cum, fmt, W, H, L, R, X, name: o.title || "" };
    let g = "";
    for (let i = 0; i <= 4; i++) {
      const v = (max * i) / 4, y = Y(v);
      g += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/><text class="axis" x="${L - 7}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${esc(o.axis ? o.axis(v) : fmt(v))}</text>`;
    }
    for (let i = 0; i <= 3; i++) {
      const t = x0 + ((x1 - x0) * i) / 3;
      g += `<text class="axis" x="${X(t).toFixed(1)}" y="${H - 5}" text-anchor="${i === 0 ? "start" : i === 3 ? "end" : "middle"}">${esc(shortClock(t))}</text>`;
    }
    let paths = "";
    for (let k = series.length - 1; k >= 0; k--) {
      const s = series[k], col = s.color;
      const top = cum[k].map((v, i) => (i ? "L" : "M") + X(ts[i]).toFixed(1) + " " + Y(v).toFixed(1)).join(" ");
      if (stacked || k === 0 || o.fillAll) {
        const base = stacked && k > 0 ? cum[k - 1].map((v, i) => "L" + X(ts[n - 1 - i]).toFixed(1) + " " + Y(cum[k - 1][n - 1 - i]).toFixed(1)).join(" ") : `L${X(ts[n - 1]).toFixed(1)} ${Y(0)} L${X(ts[0]).toFixed(1)} ${Y(0)}`;
        paths += `<path d="${top} ${base}Z" style="fill:${col};opacity:${stacked ? 0.55 : 0.16}"/>`;
      }
      paths += `<path d="${top}" fill="none" style="stroke:${col}" stroke-width="1.7" stroke-linejoin="round"/>`;
    }
    return html`<div class="chart" data-chart="${id}"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${o.title || "Chart"}">${raw(g)}${raw(paths)}<line class="cursor" y1="${T}" y2="${H - B}"/></svg></div>`;
  };

  const tip = () => document.getElementById("tip");
  function onMove(e) {
    const el = e.target.closest ? e.target.closest(".chart[data-chart]") : null;
    if (!el) return hideTip();
    const c = registry[el.dataset.chart], svg = el.querySelector("svg");
    if (!c || !svg) return hideTip();
    const rect = svg.getBoundingClientRect(), vx = ((e.clientX - rect.left) / rect.width) * c.W;
    let best = 0, bd = Infinity;
    for (let i = 0; i < c.ts.length; i++) { const d = Math.abs(c.X(c.ts[i]) - vx); if (d < bd) { bd = d; best = i; } }
    const cur = svg.querySelector(".cursor"), x = c.X(c.ts[best]);
    cur.setAttribute("x1", x); cur.setAttribute("x2", x); cur.style.display = "block";
    let rows = "", total = 0;
    c.series.forEach((s) => { const v = s.values[best]; if (v != null) total += v; rows += `<div class="r"><span><i style="background:${s.color}"></i>${esc(s.name)}</span><b>${esc(c.fmt(v == null ? 0 : v))}</b></div>`; });
    if (c.stacked && c.series.length > 1) rows += `<div class="r" style="border-top:1px solid var(--line);margin-top:5px;padding-top:4px"><span>Total</span><b>${esc(c.fmt(total))}</b></div>`;
    const t = tip();
    t.innerHTML = `<div class="t">${esc(clock(c.ts[best]))}</div>${rows}`;
    t.style.display = "block";
    const w = t.offsetWidth, h = t.offsetHeight;
    t.style.left = Math.min(window.innerWidth - w - 10, e.clientX + 16) + "px";
    t.style.top = Math.min(window.innerHeight - h - 10, e.clientY + 14) + "px";
  }
  function hideTip() {
    const t = tip();
    if (t) t.style.display = "none";
    document.querySelectorAll(".chart .cursor").forEach((c) => { c.style.display = "none"; });
  }
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseleave", hideTip);
  document.addEventListener("scroll", hideTip, true);
})();
