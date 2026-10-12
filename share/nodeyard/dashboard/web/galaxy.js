// The AI Galaxy page, and the animated backgrounds (Galaxy, Warp, Constellation).
// The cluster drawn as a spiral galaxy: the core is the model (it brightens while it works, pulls light in while it
// reads the prompt and streams light out along the arms at its real writing speed); the machines are stars orbiting
// it (brighter with CPU use, red when they drop out), linked to the core while they run part of the model.
// Plain 2D canvas, ~30 frames a second, paused while the tab is hidden, a still frame for reduced motion.
// Loaded after app.js and before settings.js (which mounts the animated backgrounds).
(function () {
  "use strict";
  const { html, raw, fmt } = NY;
  const U = NY.ui;
  const { S, V, $, getJSON } = U;

  const TAU = Math.PI * 2;
  const rnd = (a, b) => a + Math.random() * (b - a);
  const still = () => !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  // radius 0 (core) .. 1 (rim): warm white, gold, cyan, blue, violet
  const PAL = ["255,244,214", "255,214,150", "255,190,120", "150,230,255", "100,200,255", "110,160,255", "150,130,255", "190,120,255"];

  // ---------------------------------------------------------------- the drawing loop every animation shares
  class Scene {
    constructor(canvas, opts) {
      this.c = canvas; this.ctx = canvas.getContext("2d");
      this.o = Object.assign({ fps: 30, dim: 1 }, opts || {});
      this.t = 0; this.prev = 0; this.raf = 0; this.on = false;
      this.fit();
      this._resize = () => { this.fit(); this.reset(); if (!this.on) this.frame(0); };
      this._vis = () => { if (document.hidden) this.halt(); else if (this.on) this.go(); };
      window.addEventListener("resize", this._resize);
      document.addEventListener("visibilitychange", this._vis);
    }
    fit() {
      const dpr = Math.min(1.5, window.devicePixelRatio || 1), r = this.c.getBoundingClientRect();
      this.w = Math.max(40, r.width || this.c.clientWidth || 300); this.h = Math.max(40, r.height || this.c.clientHeight || 200);
      this.c.width = Math.round(this.w * dpr); this.c.height = Math.round(this.h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    reset() {}
    start() { this.on = true; this.reset(); this.frame(0); if (!still()) this.go(); return this; }
    go() { if (this.raf || document.hidden) return; this.prev = performance.now(); const tick = (now) => { this.raf = 0; if (!this.c.isConnected) { this.destroy(); return; } const dt = Math.min(0.1, (now - this.prev) / 1000); if (dt >= 1 / this.o.fps - 0.004) { this.prev = now; this.t += dt; this.frame(dt); } this.raf = requestAnimationFrame(tick); }; this.raf = requestAnimationFrame(tick); }
    halt() { if (this.raf) cancelAnimationFrame(this.raf); this.raf = 0; }
    destroy() { this.on = false; this.halt(); window.removeEventListener("resize", this._resize); document.removeEventListener("visibilitychange", this._vis); }
    backdrop() {
      const ctx = this.ctx, g = ctx.createRadialGradient(this.w * 0.5, this.h * 0.45, 0, this.w * 0.5, this.h * 0.5, Math.max(this.w, this.h) * 0.75);
      g.addColorStop(0, "#0b1030"); g.addColorStop(1, "#03040b");
      ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = 1; ctx.fillStyle = g; ctx.fillRect(0, 0, this.w, this.h);
      if (!this.sky || this.sky.width !== Math.round(this.w)) {   // a fixed field of faint stars, drawn once
        this.sky = document.createElement("canvas"); this.sky.width = Math.round(this.w); this.sky.height = Math.round(this.h);
        const s = this.sky.getContext("2d"), n = Math.round((this.w * this.h) / 2600);
        for (let i = 0; i < n; i++) { s.globalAlpha = rnd(0.15, 0.7); s.fillStyle = Math.random() < 0.15 ? "#bcd4ff" : "#ffffff"; const z = Math.random() < 0.06 ? 1.6 : 1; s.fillRect(Math.random() * this.w, Math.random() * this.h, z, z); }
      }
      ctx.drawImage(this.sky, 0, 0, this.w, this.h);
    }
  }

  // ---------------------------------------------------------------- the galaxy
  class Galaxy extends Scene {
    constructor(canvas, opts) {
      super(canvas, Object.assign({ particles: 1500, arms: 3, tilt: 0.42, spin: 1, hud: false }, opts));
      this.st = { nodes: [], busy: 0, writing: 0, reading: 0, rate: 0, loaded: true };
      this.lit = 0; this.comets = []; this.emit = 0; this.rot = rnd(0, TAU);
    }
    reset() {
      const scale = Math.min(1.6, Math.max(0.45, (this.w * this.h) / (1280 * 720)));
      const n = Math.round(this.o.particles * scale), arms = this.o.arms;
      this.p = [];
      for (let i = 0; i < n; i++) {
        const r = Math.pow(Math.random(), 0.7) * 0.98 + 0.02, arm = i % arms, spread = 0.18 + (1 - r) * 0.55;
        this.p.push({ r, a: (arm * TAU) / arms + r * 4.6 + rnd(-spread, spread), z: rnd(-1, 1) * (0.015 + (1 - r) * 0.05),
          s: Math.random() < 0.1 ? rnd(1.6, 2.6) : rnd(0.8, 1.6), glow: Math.random() < 0.05, c: Math.min(PAL.length - 1, Math.floor(r * PAL.length + rnd(-0.6, 0.6))) || 0, tw: rnd(0, TAU) });
      }
    }
    set(state) { Object.assign(this.st, state); if (this.on && !this.raf) this.frame(0); }   // a still frame stays current
    geom() {
      const hud = this.o.hud, R = Math.min(this.w * 0.5, this.h * (hud ? 1.2 : 1.3)) * (hud ? 0.94 : 1.05);
      return { cx: this.w / 2, cy: this.h * (hud ? 0.52 : 0.5), R, tilt: this.o.tilt };
    }
    proj(r, a, z, g) { return [g.cx + Math.cos(a) * r * g.R, g.cy + Math.sin(a) * r * g.R * g.tilt + z * g.R]; }
    frame(dt) {
      const ctx = this.ctx, st = this.st, g = this.geom();
      this.lit += ((st.busy ? 1 : 0) - this.lit) * Math.min(1, dt * 2.5);           // smooth 0..1: is the model working
      this.rot += dt * 0.045 * this.o.spin * (1 + this.lit * 1.2);
      this.backdrop();
      ctx.globalCompositeOperation = "lighter";
      // the arms
      const dim = this.o.dim * (st.loaded ? 1 : 0.45);
      for (const q of this.p) {
        const ang = q.a + this.rot / Math.max(0.2, Math.sqrt(q.r));                    // the middle turns faster
        const [x, y] = this.proj(q.r, ang, q.z, g);
        const al = Math.min(1, (0.38 + 0.62 * (1 - q.r)) * dim * (0.8 + 0.2 * Math.sin(this.t * 1.7 + q.tw)) * (1 + this.lit * 0.35));
        ctx.fillStyle = "rgb(" + PAL[q.c] + ")";
        if (q.glow) { ctx.globalAlpha = al * 0.18; ctx.fillRect(x - q.s * 1.5, y - q.s * 1.5, q.s * 4, q.s * 4); }
        ctx.globalAlpha = al;
        ctx.fillRect(x, y, q.s, q.s);
      }
      // the core: the model
      const pulse = 1 + this.lit * (0.28 + 0.12 * Math.sin(this.t * 5.5)), cr = g.R * 0.3 * pulse;
      ctx.save(); ctx.translate(g.cx, g.cy); ctx.scale(1, 0.62);
      const cg = ctx.createRadialGradient(0, 0, 0, 0, 0, cr);
      cg.addColorStop(0, "rgba(255,248,230," + (0.85 * dim) + ")"); cg.addColorStop(0.12, "rgba(255,206,130," + (0.55 * dim) + ")");
      cg.addColorStop(0.45, "rgba(120,170,255," + (0.16 + 0.12 * this.lit) * dim + ")"); cg.addColorStop(1, "rgba(0,0,0,0)");
      ctx.globalAlpha = 1; ctx.fillStyle = cg; ctx.fillRect(-cr, -cr, cr * 2, cr * 2); ctx.restore();
      // light streaming out (writing, at its speed) or in (reading the prompt)
      if (st.busy) {
        const per = st.reading ? 14 : Math.min(40, Math.max(3, (st.rate || 3) * 2.2));
        this.emit += dt * per;
        while (this.emit >= 1 && this.comets.length < 220) { this.emit -= 1; this.comets.push({ arm: Math.floor(rnd(0, this.o.arms)), off: rnd(-0.25, 0.25), u: st.reading ? 1 : 0, dir: st.reading ? -1 : 1, v: rnd(0.25, 0.42) }); }
      } else this.emit = 0;
      for (const k of this.comets) {
        k.u += k.dir * k.v * dt;
        const r = 0.04 + k.u * 0.94, a = (k.arm * TAU) / this.o.arms + r * 4.6 + k.off + this.rot / Math.max(0.2, Math.sqrt(r));
        const [x, y] = this.proj(r, a, 0, g), [x2, y2] = this.proj(Math.max(0.03, r - 0.045 * k.dir), a - 0.2 * k.dir, 0, g);
        const fade = Math.sin(Math.PI * Math.min(1, Math.max(0, k.u)));
        ctx.globalAlpha = 0.9 * fade * dim; ctx.strokeStyle = k.dir > 0 ? "rgb(255,226,160)" : "rgb(140,230,255)"; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.moveTo(x2, y2); ctx.lineTo(x, y); ctx.stroke();
      }
      this.comets = this.comets.filter((k) => k.u >= 0 && k.u <= 1);
      if (this.o.hud) this.drawNodes(g, dt);
      ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = 1;
    }
    drawNodes(g, dt) {
      const ctx = this.ctx, nodes = this.st.nodes || [], n = nodes.length; if (!n) return;
      this.nodeAt = [];
      nodes.forEach((nd, i) => {
        const a = (i / n) * TAU + this.rot * 0.35 + 0.6, r = 0.62 + (i % 2) * 0.14;
        const [x, y] = this.proj(r, a, 0, g);
        const bad = !nd.ready, cpu = Math.max(0, Math.min(1, nd.cpu || 0)), work = nd.model && this.lit > 0.05;
        // the link to the core while it runs part of the model
        if (nd.model) {
          ctx.globalAlpha = 0.12 + 0.35 * this.lit; ctx.strokeStyle = bad ? "rgb(255,110,110)" : "rgb(120,200,255)"; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(g.cx, g.cy); ctx.quadraticCurveTo((g.cx + x) / 2, (g.cy + y) / 2 - g.R * 0.12, x, y); ctx.stroke();
          if (work) for (let j = 0; j < 3; j++) {            // traffic running along it
            const u = (this.t * 0.6 + j / 3 + i * 0.17) % 1, mx = (g.cx + x) / 2, my = (g.cy + y) / 2 - g.R * 0.12;
            const px = (1 - u) * (1 - u) * g.cx + 2 * (1 - u) * u * mx + u * u * x, py = (1 - u) * (1 - u) * g.cy + 2 * (1 - u) * u * my + u * u * y;
            ctx.globalAlpha = 0.9 * this.lit; ctx.fillStyle = "rgb(200,240,255)"; ctx.fillRect(px - 1, py - 1, 2.4, 2.4);
          }
        }
        const size = 3 + (nd.share || 0) * 5 + cpu * 3;
        const sg = ctx.createRadialGradient(x, y, 0, x, y, size * 4);
        const col = bad ? "255,90,90" : nd.gpu ? "180,255,170" : "200,225,255";
        sg.addColorStop(0, "rgba(" + col + ",0.95)"); sg.addColorStop(0.25, "rgba(" + col + "," + (0.3 + cpu * 0.4) + ")"); sg.addColorStop(1, "rgba(" + col + ",0)");
        ctx.globalAlpha = bad ? 0.6 + 0.4 * Math.abs(Math.sin(this.t * 3)) : 1; ctx.fillStyle = sg; ctx.fillRect(x - size * 4, y - size * 4, size * 8, size * 8);
        ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = 0.9;
        ctx.font = "600 12px ui-sans-serif, system-ui, sans-serif"; ctx.fillStyle = bad ? "#ff9b9b" : "#dbe8ff"; ctx.textAlign = "center";
        ctx.fillText(nd.name, x, y - size - 8);
        ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace"; ctx.fillStyle = "rgba(200,215,255,.75)";
        ctx.fillText(bad ? "not ready" : Math.round(cpu * 100) + "% cpu" + (nd.temp != null ? " · " + Math.round(nd.temp) + "°" : ""), x, y + size + 16);
        ctx.globalCompositeOperation = "lighter";
        this.nodeAt.push({ x, y, nd });
      });
    }
  }

  // ---------------------------------------------------------------- warp: flying through stars
  class Warp extends Scene {
    reset() { const n = Math.round(Math.min(900, (this.w * this.h) / 1500)); this.s = []; for (let i = 0; i < n; i++) this.s.push(this.star(true)); }
    star(any) { return { x: rnd(-1, 1), y: rnd(-1, 1), z: any ? rnd(0.05, 1) : 1, c: Math.random() < 0.2 ? "170,200,255" : Math.random() < 0.1 ? "255,210,170" : "255,255,255" }; }
    frame(dt) {
      const ctx = this.ctx, cx = this.w / 2, cy = this.h / 2, f = Math.max(this.w, this.h) * 0.5;
      ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = 1; ctx.fillStyle = "#03040b"; ctx.fillRect(0, 0, this.w, this.h);
      const neb = ctx.createRadialGradient(cx, cy, 0, cx, cy, f * 1.2); neb.addColorStop(0, "rgba(60,80,200,.18)"); neb.addColorStop(0.5, "rgba(120,60,180,.08)"); neb.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = neb; ctx.fillRect(0, 0, this.w, this.h);
      ctx.globalCompositeOperation = "lighter"; ctx.lineCap = "round";
      for (const s of this.s) {
        const z0 = s.z; s.z -= dt * 0.12 * (this.o.speed || 1);
        if (s.z <= 0.03) { Object.assign(s, this.star(false)); continue; }
        const x = cx + (s.x / s.z) * f * 0.5, y = cy + (s.y / s.z) * f * 0.5, x0 = cx + (s.x / z0) * f * 0.5, y0 = cy + (s.y / z0) * f * 0.5;
        if (x < -20 || x > this.w + 20 || y < -20 || y > this.h + 20) { Object.assign(s, this.star(false)); continue; }
        ctx.globalAlpha = Math.min(1, (1 - s.z) * 1.2) * this.o.dim; ctx.strokeStyle = "rgb(" + s.c + ")"; ctx.lineWidth = Math.max(0.6, (1 - s.z) * 2.2);
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x, y); ctx.stroke();
      }
      ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = 1;
    }
  }

  // ---------------------------------------------------------------- constellation: a drifting network
  class Constellation extends Scene {
    reset() { const n = Math.round(Math.min(110, Math.max(40, (this.w * this.h) / 16000))); this.p = []; for (let i = 0; i < n; i++) this.p.push({ x: rnd(0, this.w), y: rnd(0, this.h), vx: rnd(-12, 12), vy: rnd(-12, 12), c: Math.random() < 0.5 ? "120,210,255" : "180,140,255" }); }
    frame(dt) {
      const ctx = this.ctx, d = Math.min(180, Math.max(110, this.w / 9));
      this.backdrop();
      for (const p of this.p) { p.x += p.vx * dt; p.y += p.vy * dt; if (p.x < 0 || p.x > this.w) p.vx *= -1; if (p.y < 0 || p.y > this.h) p.vy *= -1; }
      ctx.globalCompositeOperation = "lighter"; ctx.lineWidth = 1;
      for (let i = 0; i < this.p.length; i++) for (let j = i + 1; j < this.p.length; j++) {
        const a = this.p[i], b = this.p[j], dx = a.x - b.x, dy = a.y - b.y, dd = dx * dx + dy * dy;
        if (dd < d * d) { ctx.globalAlpha = (1 - Math.sqrt(dd) / d) * 0.45 * this.o.dim; ctx.strokeStyle = "rgb(" + a.c + ")"; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
      }
      for (const p of this.p) { ctx.globalAlpha = 0.9 * this.o.dim; ctx.fillStyle = "rgb(" + p.c + ")"; ctx.fillRect(p.x - 1.2, p.y - 1.2, 2.4, 2.4); }
      ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = 1;
    }
  }

  // animated backgrounds, for settings.js
  const ANIMATED = { galaxy: (c) => new Galaxy(c, { particles: 1100, dim: 0.85, spin: 0.7, fps: 24 }), warp: (c) => new Warp(c, { fps: 30, dim: 0.9 }),
    constellation: (c) => new Constellation(c, { fps: 24, dim: 0.9 }) };
  NY.anim = { kinds: Object.keys(ANIMATED), start(canvas, kind) { return ANIMATED[kind] ? ANIMATED[kind](canvas).start() : null; } };

  // ---------------------------------------------------------------- the AI Galaxy page
  // Every machine is a galaxy: its size from its memory, its core from its CPU, its pods as stars (gold AI, blue
  // system, teal nodeyard, violet apps; dim when stopped, red when failing). Lines run from each machine to the control
  // plane carrying its measured network traffic: light travels along them in proportion and they flash when it jumps.
  // The model's own lines (its main server to each machine holding layers) glow gold while it works. Click a galaxy to
  // fly to it, Tour visits them in turn, and the AI helper box asks the model about the cluster with a live summary.
  const HUES = [195, 280, 160, 32, 330, 120, 250, 55];
  const hashStr = (s) => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; };
  const prng = (seed) => () => { seed = (seed + 0x6D2B79F5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const speed = (b) => (b == null ? "–" : fmt.rate(b));
  const POD = { ai: "255,200,90", system: "110,170,255", nodeyard: "70,225,205", app: "195,145,255", bad: "255,90,90", wait: "240,210,120", off: "120,130,150" };
  function podKind(p) {
    if (/^ai-|^ollama/.test(p.namespace) || /llama|ollama|^rpc-/.test(p.name)) return "ai";
    if (/^kube-/.test(p.namespace) || /coredns|traefik|metrics|local-path|svclb/.test(p.name)) return "system";
    if (/^nodeyard/.test(p.namespace)) return "nodeyard";
    return "app";
  }
  function podColor(p) {
    if (/Error|BackOff|Failed|OOM|Err|Unknown/.test(p.status)) return POD.bad;
    if (p.status === "Running") return POD[podKind(p)];
    if (/Succeeded|Completed/.test(p.status)) return POD.off;
    return POD.wait;
  }
  function galaxySprite(name, hue, size) {   // drawn once per machine, then turned and squashed every frame
    const c = document.createElement("canvas"); c.width = c.height = size;
    const x = c.getContext("2d"), rand = prng(hashStr(name)), cx = size / 2, R = size * 0.47, arms = 2 + (hashStr(name) % 2);
    x.globalCompositeOperation = "lighter";
    for (let i = 0; i < 240; i++) {   // clouds along the arms
      const r = Math.pow(rand(), 0.6), a = ((i % arms) * TAU) / arms + r * 4.3 + (rand() - 0.5) * 0.7, br = R * (0.05 + 0.1 * (1 - r));
      const px = cx + Math.cos(a) * r * R, py = cx + Math.sin(a) * r * R, g = x.createRadialGradient(px, py, 0, px, py, br);
      g.addColorStop(0, "hsla(" + (r < 0.3 ? 40 : hue) + ",70%," + (68 - r * 20) + "%," + 0.09 * (1 - r * 0.5) + ")"); g.addColorStop(1, "hsla(" + hue + ",70%,50%,0)");
      x.fillStyle = g; x.fillRect(px - br, py - br, br * 2, br * 2);
    }
    for (let i = 0; i < 3200; i++) {  // stars
      const r = Math.pow(rand(), 0.8), spread = 0.1 + (1 - r) * 0.55, a = ((i % arms) * TAU) / arms + r * 4.3 + (rand() - 0.5) * spread * 1.6;
      const px = cx + Math.cos(a) * r * R + (rand() - 0.5) * R * 0.04, py = cx + Math.sin(a) * r * R + (rand() - 0.5) * R * 0.04;
      x.fillStyle = "hsla(" + (r < 0.22 ? 42 : hue) + "," + (r < 0.22 ? 85 : 65) + "%," + (90 - r * 35) + "%," + (0.35 + 0.6 * (1 - r)) + ")";
      const s = rand() < 0.05 ? 1.7 : 0.9; x.fillRect(px, py, s, s);
    }
    const g = x.createRadialGradient(cx, cx, 0, cx, cx, R * 0.3);   // the bright middle
    g.addColorStop(0, "rgba(255,250,235,0.95)"); g.addColorStop(0.18, "rgba(255,220,160,0.55)"); g.addColorStop(0.6, "hsla(" + hue + ",80%,60%,0.12)"); g.addColorStop(1, "rgba(0,0,0,0)");
    x.fillStyle = g; x.fillRect(0, 0, size, size);
    return c;
  }

  class Universe extends Scene {
    constructor(canvas) {
      super(canvas, { fps: 30 });
      this.N = new Map(); this.links = []; this.pulses = []; this.cam = null; this.focus = ""; this.tour = false; this.tourAt = 0;
      this.ai = { busy: false, reading: false, rate: 0, main: "", label: "" };
      canvas.addEventListener("click", (e) => { const n = this.hit(e); this.fly(n ? n.name : ""); });
      canvas.addEventListener("mousemove", (e) => { this.c.style.cursor = this.hit(e) ? "pointer" : "default"; });
    }
    reset() { this.neb = null; this.layout(); }
    toWorld(e) { const r = this.c.getBoundingClientRect(), cam = this.cam || { x: this.w / 2, y: this.h / 2, z: 1 }; return [(e.clientX - r.left - this.w / 2) / cam.z + cam.x, (e.clientY - r.top - this.h / 2) / cam.z + cam.y]; }
    toScreen(x, y) { const cam = this.cam; return [(x - cam.x) * cam.z + this.w / 2, (y - cam.y) * cam.z + this.h / 2]; }
    hit(e) { const [x, y] = this.toWorld(e); let best = null; for (const n of this.N.values()) { const d = Math.hypot(x - n.x, (y - n.y) / 0.55); if (d < n.size * 1.1 && (!best || d < best.d)) best = { n, d }; } return best && best.n; }
    fly(name) { this.focus = name || ""; if (this.onFocus) this.onFocus(this.focus); }
    set(state) {
      if (state.ai) Object.assign(this.ai, state.ai);
      if (!state.nodes) { if (this.on && !this.raf) this.frame(0); return; }   // (only the model's state changed)
      const seen = new Set();
      state.nodes.forEach((n) => {
        let e = this.N.get(n.name);
        if (!e) { e = { name: n.name, hue: HUES[hashStr(n.name) % HUES.length], rot: rnd(0, TAU), spin: rnd(0.02, 0.04) * (hashStr(n.name) % 2 ? 1 : -1), flash: 0, prev: null, fx: 0, fy: 0 }; this.N.set(n.name, e); }
        const total = (n.rx || 0) + (n.tx || 0);
        if (e.prev != null && total > Math.max(20480, e.prev * 1.4)) e.flash = 1;   // its traffic jumped: flash its line
        e.prev = total;
        Object.assign(e, n, { pods: (state.pods || []).filter((p) => p.node === n.name).map((p) => Object.assign({ seed: hashStr(p.namespace + "/" + p.name) }, p)) });
        seen.add(n.name);
      });
      for (const k of Array.from(this.N.keys())) if (!seen.has(k)) this.N.delete(k);
      this.layout();
      if (this.on && !this.raf) this.frame(0);
    }
    layout() {
      const list = Array.from(this.N.values()); if (!list.length) return;
      // fit inside the free space: clear of the cards down both sides (250 px, 196 on smaller screens, none on
      // phones), the clock and headline at the top and the AI helper at the bottom
      const sp = this.space ? this.space() : null;   // measured from the panels when the page provides it
      const side = sp ? sp.side : this.w > 1180 ? 282 : this.w > 820 ? 228 : 12, top = sp ? sp.top : 112, bottom = sp ? sp.bottom : this.w > 820 ? 196 : 170;
      const uw = Math.max(220, this.w - side * 2), uh = Math.max(160, this.h - top - bottom), cx = this.w / 2, cy = top + uh / 2;
      const maxMem = Math.max(1, ...list.map((n) => n.memTotal || 1)), base = Math.min(uw * 0.75, uh * 1.4) * (list.length > 6 ? 0.1 : 0.13);
      const hub = list.find((n) => n.hub) || list[0], rest = list.filter((n) => n !== hub);
      list.forEach((n) => { n.size = base * (0.7 + 0.45 * ((n.memTotal || 1) / maxMem)) * (n === hub ? 1.12 : 1); });
      const big = Math.max(...list.map((n) => n.size)), rx = Math.max(uw * 0.16, uw / 2 - big * 1.2), ry = Math.max(uh * 0.12, uh / 2 - big * 0.7 - 22);
      hub.x = cx; hub.y = cy;
      rest.forEach((n, i) => { const a = -Math.PI * 0.75 + (i * TAU) / Math.max(1, rest.length); n.x = cx + Math.cos(a) * rx; n.y = cy + Math.sin(a) * ry; });
      const keep = new Map(this.links.map((l) => [l.key, l]));
      this.links = rest.map((n) => Object.assign(keep.get("net:" + n.name) || { acc: 0 }, { a: hub, b: n, kind: "net", key: "net:" + n.name, bend: 1 }));
      const main = list.find((n) => n.main);
      if (main) list.filter((n) => n.model && n !== main).forEach((n) => this.links.push(Object.assign(keep.get("model:" + n.name) || { acc: 0 }, { a: main, b: n, kind: "model", key: "model:" + n.name, bend: -1 })));
    }
    curve(l) {   // a gentle arc between two galaxies
      const ax = l.a.x, ay = l.a.y, bx = l.b.x, by = l.b.y, mx = (ax + bx) / 2, my = (ay + by) / 2, dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1;
      return [ax, ay, mx - (dy / len) * len * 0.12 * l.bend, my + (dx / len) * len * 0.12 * l.bend, bx, by];
    }
    at(c, u) { const v = 1 - u; return [v * v * c[0] + 2 * v * u * c[2] + u * u * c[4], v * v * c[1] + 2 * v * u * c[3] + u * u * c[5]]; }
    frame(dt) {
      const ctx = this.ctx, list = Array.from(this.N.values()), ai = this.ai;
      // the camera: the whole estate, or flown to one galaxy
      if (this.tour && this.t - this.tourAt > 9 && list.length) { this.tourAt = this.t; const order = [""].concat(list.map((n) => n.name)); this.fly(order[(order.indexOf(this.focus) + 1) % order.length]); }
      const f = this.focus && this.N.get(this.focus), goal = f ? { x: f.x, y: f.y, z: Math.min(2.4, (Math.min(this.w, this.h) * 0.34) / f.size) } : { x: this.w / 2, y: this.h / 2, z: 1 };
      if (!this.cam) this.cam = Object.assign({}, goal);
      const k = 1 - Math.exp(-dt * 2.2); this.cam.x += (goal.x - this.cam.x) * k; this.cam.y += (goal.y - this.cam.y) * k; this.cam.z += (goal.z - this.cam.z) * k;
      this.backdrop();
      if (!this.neb) {   // faint nebula clouds, drawn once
        this.neb = document.createElement("canvas"); this.neb.width = Math.round(this.w); this.neb.height = Math.round(this.h);
        const nx = this.neb.getContext("2d"), r = prng(7);
        nx.globalCompositeOperation = "lighter";
        for (let i = 0; i < 26; i++) { const x = r() * this.w, y = r() * this.h, rad = (0.15 + r() * 0.3) * Math.max(this.w, this.h), g = nx.createRadialGradient(x, y, 0, x, y, rad); g.addColorStop(0, "hsla(" + (r() < 0.5 ? 200 : 275) + ",70%,45%,0.05)"); g.addColorStop(1, "rgba(0,0,0,0)"); nx.fillStyle = g; nx.fillRect(0, 0, this.w, this.h); }
      }
      ctx.globalCompositeOperation = "lighter"; ctx.globalAlpha = 1;
      ctx.drawImage(this.neb, (this.w / 2 - this.cam.x) * 0.15, (this.h / 2 - this.cam.y) * 0.15);
      ctx.save(); ctx.translate(this.w / 2, this.h / 2); ctx.scale(this.cam.z, this.cam.z); ctx.translate(-this.cam.x, -this.cam.y);
      // lines and the traffic on them
      for (const l of this.links) {
        const c = this.curve(l), model = l.kind === "model", busy = model && ai.busy, bytes = (l.b.rx || 0) + (l.b.tx || 0);
        let perSec = model ? (busy ? 3 + (ai.rate || 2) * 1.4 : 0) : Math.max(0, Math.min(8, (Math.log10(bytes + 1) - 2.6) * 1.7));
        if (!l.b.ready || !l.a.ready) perSec = 0;
        l.acc += dt * perSec;
        while (l.acc >= 1 && this.pulses.length < 400) { l.acc -= 1; const down = model ? Math.random() < 0.5 : Math.random() < ((l.b.rx || 0) / (bytes || 1)); this.pulses.push({ l, u: down ? 0 : 1, dir: down ? 1 : -1, v: rnd(0.35, 0.6), col: model ? "255,205,110" : down ? "120,220,255" : "190,150,255" }); }
        l.glow = Math.max((l.b.flash || 0) * (model ? 0 : 1), busy ? 0.75 + 0.25 * Math.sin(this.t * 6) : 0);
        const bad = !l.b.ready || !l.a.ready, base = bad ? "255,100,100" : model ? "255,200,110" : "120,180,255";
        ctx.lineCap = "round";
        if (l.glow > 0.02) { ctx.globalAlpha = 0.35 * l.glow; ctx.strokeStyle = "rgb(" + base + ")"; ctx.lineWidth = 6 / this.cam.z; ctx.beginPath(); ctx.moveTo(c[0], c[1]); ctx.quadraticCurveTo(c[2], c[3], c[4], c[5]); ctx.stroke(); }
        ctx.globalAlpha = bad ? 0.25 : (model ? 0.3 : 0.16) + 0.6 * l.glow; ctx.strokeStyle = "rgb(" + base + ")"; ctx.lineWidth = (model ? 1.6 : 1.1) / this.cam.z;
        ctx.setLineDash(bad ? [6, 6] : []); ctx.beginPath(); ctx.moveTo(c[0], c[1]); ctx.quadraticCurveTo(c[2], c[3], c[4], c[5]); ctx.stroke(); ctx.setLineDash([]);
      }
      for (const p of this.pulses) {
        p.u += p.dir * p.v * dt;
        const c = this.curve(p.l), [x, y] = this.at(c, Math.max(0, Math.min(1, p.u))), [x2, y2] = this.at(c, Math.max(0, Math.min(1, p.u - p.dir * 0.05)));
        ctx.globalAlpha = 0.95; ctx.strokeStyle = "rgb(" + p.col + ")"; ctx.lineWidth = 2.2 / this.cam.z; ctx.beginPath(); ctx.moveTo(x2, y2); ctx.lineTo(x, y); ctx.stroke();
        const g = ctx.createRadialGradient(x, y, 0, x, y, 6 / this.cam.z); g.addColorStop(0, "rgba(" + p.col + ",0.9)"); g.addColorStop(1, "rgba(" + p.col + ",0)");
        ctx.fillStyle = g; ctx.fillRect(x - 6 / this.cam.z, y - 6 / this.cam.z, 12 / this.cam.z, 12 / this.cam.z);
      }
      this.pulses = this.pulses.filter((p) => p.u >= 0 && p.u <= 1 && this.links.includes(p.l));
      // the galaxies, their pods, their cores
      for (const n of list) {
        n.rot += dt * n.spin * (1 + (n.cpu || 0) * 2);
        n.flash = Math.max(0, (n.flash || 0) - dt * 0.8);
        if (!n.sprite) n.sprite = galaxySprite(n.name, n.hue, 384);
        const s = n.size * 2.3;
        ctx.save(); ctx.translate(n.x, n.y); ctx.scale(1, 0.55); ctx.rotate(n.rot);
        ctx.globalAlpha = n.ready ? 0.95 : 0.3; ctx.drawImage(n.sprite, -s / 2, -s / 2, s, s);
        for (const p of n.pods || []) {
          if (/Succeeded|Completed/.test(p.status)) continue;
          const r = 0.2 + 0.7 * ((p.seed % 997) / 997), a = ((p.seed >>> 10) % 6283) / 1000, px = Math.cos(a) * r * s * 0.45, py = Math.sin(a) * r * s * 0.45;
          const col = podColor(p), big = 1.8 + Math.min(3, (p.mem || 0) / 5e8), hot = col === POD.bad;
          const g = ctx.createRadialGradient(px, py, 0, px, py, big * (hot ? 5 + 2 * Math.sin(this.t * 5) : 3.2));
          g.addColorStop(0, "rgba(" + col + ",0.95)"); g.addColorStop(0.35, "rgba(" + col + ",0.35)"); g.addColorStop(1, "rgba(" + col + ",0)");
          ctx.globalAlpha = p.status === "Running" || hot ? 1 : 0.55; ctx.fillStyle = g; const rr = big * 6; ctx.fillRect(px - rr, py - rr, rr * 2, rr * 2);
        }
        ctx.restore();
        const cpu = Math.max(0, Math.min(1, n.cpu || 0)), cr = n.size * (0.35 + 0.35 * cpu);
        const cg = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, cr);
        cg.addColorStop(0, n.ready ? "rgba(255,245,225," + (0.35 + 0.5 * cpu) + ")" : "rgba(255,90,90,0.6)"); cg.addColorStop(1, "rgba(0,0,0,0)");
        ctx.globalAlpha = n.ready ? 1 : 0.6 + 0.4 * Math.sin(this.t * 3); ctx.fillStyle = cg; ctx.fillRect(n.x - cr, n.y - cr, cr * 2, cr * 2);
        if (n.main) {   // the model lives here: a golden halo, brighter while it works
          const hr = n.size * (1.05 + (ai.busy ? 0.08 * Math.sin(this.t * 5) : 0));
          ctx.save(); ctx.translate(n.x, n.y); ctx.scale(1, 0.55);
          ctx.globalAlpha = ai.busy ? 0.8 : 0.35; ctx.strokeStyle = "rgb(255,205,110)"; ctx.lineWidth = (ai.busy ? 2.2 : 1.2) / this.cam.z;
          ctx.beginPath(); ctx.arc(0, 0, hr, 0, TAU); ctx.stroke();
          if (ai.busy) { ctx.globalAlpha = 0.25; ctx.lineWidth = 8 / this.cam.z; ctx.beginPath(); ctx.arc(0, 0, hr, this.t * 2, this.t * 2 + 1.2); ctx.stroke(); }
          ctx.restore();
        }
      }
      ctx.restore();
      // words, at screen size
      ctx.globalCompositeOperation = "source-over"; ctx.textAlign = "center";
      for (const l of this.links) {   // each line's measured traffic, nearer its machine (the middle is busy)
        if (l.kind === "model" && !ai.busy) continue;
        const c = this.curve(l), [mx, my] = this.at(c, l.kind === "model" ? 0.62 : 0.68), [sx, sy] = this.toScreen(mx, my);
        const txt = l.kind === "model" ? "model · " + (ai.rate ? ai.rate.toFixed(1) + " tok/s" : ai.reading ? "reading" : "working")
          : l.b.ready ? "↓ " + speed(l.b.rx) + "  ↑ " + speed(l.b.tx) : "no contact";
        ctx.globalAlpha = 0.85; ctx.font = "10.5px ui-monospace, SFMono-Regular, Menlo, monospace"; ctx.fillStyle = l.kind === "model" ? "#ffd58a" : l.b.ready ? "#9fc4ff" : "#ff9b9b";
        ctx.fillText(txt, sx, sy - 4);
      }
      for (const n of list) {
        const [sx, sy] = this.toScreen(n.x, n.y - n.size * 0.62), hue = n.ready ? "hsl(" + n.hue + ",85%,72%)" : "#ff8080";
        ctx.globalAlpha = 1; ctx.font = "600 13px ui-monospace, SFMono-Regular, Menlo, monospace"; ctx.fillStyle = hue;
        ctx.fillText(n.name.toUpperCase().split("").join(String.fromCharCode(8202)), sx, sy - 16);
        ctx.font = "10.5px ui-monospace, SFMono-Regular, Menlo, monospace"; ctx.fillStyle = "rgba(200,215,240,.8)";
        ctx.fillText(n.ready ? Math.round((n.cpu || 0) * 100) + "% cpu · " + (n.pods || []).filter((p) => p.status === "Running").length + " pods" + (n.temp != null ? " · " + Math.round(n.temp) + "°" : "") : "NOT READY", sx, sy - 2);
        if (n.main && ai.label) { ctx.fillStyle = "#ffd58a"; ctx.fillText("◆ " + ai.label, sx, sy + n.size * 1.4 * this.cam.z + 18); }
      }
      ctx.globalAlpha = 1;
    }
  }

  // ---- the page: the scene, the panels around it, the AI helper
  const G = { scene: null, poll: 0, prev: null, rate: 0, log: [], last: {}, act: null, target: "", ask: { busy: false, text: "", q: "", history: [] } };
  const say = (text, tone) => { G.log.unshift({ t: Date.now() / 1000, text, tone }); G.log.length = Math.min(G.log.length, 6); };
  const pct = (x) => (x == null || isNaN(x) ? "–" : Math.round(x * 100) + "%");
  function snapshotNodes(d) {
    const sp = (d.ai && d.ai.split) || {}, shares = sp.shares || [], pods = d.pods || [];
    const mainNode = ((sp.pods || []).find((p) => /^llama-main/.test(p.name)) || {}).node || "";
    return (d.nodes || []).map((n) => {
      const mine = shares.filter((s) => s.node.replace(/ GPU$/, "") === n.name), hw = n.hw || {};
      return { name: n.name, ready: n.ready, cores: n.cpu_cores, cpu: n.cpu_cores ? (n.cpu_used || 0) / n.cpu_cores : 0, memUsed: n.mem_used, memTotal: n.mem_total, mem: n.mem_total ? (n.mem_used || 0) / n.mem_total : 0,
        disk: n.disk_total ? (n.disk_used || 0) / n.disk_total : null, temp: hw.temp_c, rx: n.net_rx_rate, tx: n.net_tx_rate, gpus: hw.gpu_live || [], roles: n.roles || [],
        hub: (n.roles || []).some((r) => /control-plane|master/.test(r)), model: mine.length > 0, main: n.name === mainNode, pods: pods.filter((p) => p.node === n.name).length, reason: n.ready_reason };
    });
  }
  function track(d) {
    const sp = (d.ai && d.ai.split) || null;
    (d.nodes || []).forEach((n) => { const was = G.last["n:" + n.name]; if (was !== undefined && was !== n.ready) say(n.name + (n.ready ? " is back" : " dropped out"), n.ready ? "good" : "bad"); G.last["n:" + n.name] = n.ready; });
    const phase = sp ? (sp.loaded === false ? "unloaded" : sp.ready ? "ready" : "loading") : "none";
    if (G.last.phase !== undefined && G.last.phase !== phase) say("Model " + phase, phase === "ready" ? "good" : "");
    G.last.phase = phase;
  }
  async function pollActivity() {
    let a; try { a = await getJSON("/api/ai/activity"); } catch (e) { return; }
    if (!a || !a.ok) return;
    const now = a.time || Date.now() / 1000, p = G.prev;
    if (a.processing && p && p.processing && p.task === a.task && a.decoded >= p.decoded && now > p.time) { const r = (a.decoded - p.decoded) / (now - p.time); G.rate = G.rate ? G.rate * 0.6 + r * 0.4 : r; }
    if (a.processing && !(p && p.processing)) { G.started = now; G.rate = 0; say("The model started answering", ""); }
    if (!a.processing && p && p.processing) say("Answer done: " + (p.decoded || 0) + " tokens in " + Math.max(1, Math.round(now - (G.started || now))) + " s", "good");
    G.prev = a; G.act = a;
    paint();
  }
  function card(n) {
    const bar = (v, warn) => '<span class="ux-bar"><i style="width:' + Math.round(Math.max(0, Math.min(1, v || 0)) * 100) + "%" + (warn && v > warn ? ";background:#f87171" : "") + '"></i></span>';
    const row = (k, v, b) => '<div class="ux-row"><span>' + k + "</span>" + (b || "") + "<b>" + NY.esc(v) + "</b></div>";
    const g = n.gpus[0];
    return '<div class="ux-card' + (G.scene && G.scene.focus === n.name ? " on" : "") + (n.ready ? "" : " down") + '" data-gx-node="' + NY.esc(n.name) + '" style="--h:' + HUES[hashStr(n.name) % HUES.length] + '">' +
      '<div class="ux-card-h"><b>' + NY.esc(n.name) + "</b><span>" + (n.ready ? NY.esc((n.hub ? "control plane" : "worker") + (n.main ? " · model" : n.model ? " · model layers" : "")) : "NOT READY") + "</span></div>" +
      (n.ready ? row("CPU", pct(n.cpu) + (n.cores ? " of " + n.cores : ""), bar(n.cpu, 0.85)) + row("MEM", n.memTotal ? fmt.bytes(n.memUsed, 1) + " / " + fmt.bytes(n.memTotal, 0) : "–", bar(n.mem, 0.9)) +
        (n.disk != null ? row("DISK", pct(n.disk), bar(n.disk, 0.9)) : "") + (n.temp != null ? row("TEMP", Math.round(n.temp) + " °C", bar(n.temp / 95, 0.8)) : "") +
        row("NET", "↓ " + speed(n.rx) + "  ↑ " + speed(n.tx)) + (g ? row("GPU", pct(g.use) + " · vram " + pct(g.mem_use), bar(g.use)) : "") + row("PODS", String(n.pods))
        : '<div class="ux-row"><span>' + NY.esc(n.reason || "Kubelet stopped posting status") + "</span></div>") + "</div>";
  }
  function paint() {
    const d = S.d; if (!d || !$("#gx")) return;
    const sp = (d.ai && d.ai.split) || null, a = G.act || {}, nodes = snapshotNodes(d), up = nodes.filter((n) => n.ready).length, down = nodes.length - up;
    const busy = !!a.processing, state = !sp ? "no model" : sp.loaded === false ? "unloaded" : !sp.ready ? "loading" : busy ? (a.decoded ? "answering" : "reading the prompt") : "ready";
    const label = sp ? (sp.alias || sp.model || "model").replace(/\.gguf$/i, "").slice(0, 38) + " · " + state + (busy && G.rate ? " · " + G.rate.toFixed(1) + " tok/s" : "") : "";
    if (G.scene) G.scene.set({ nodes, pods: d.pods || [], ai: { busy, reading: busy && !a.decoded, rate: G.rate, label } });
    const head = $("#ux-head"); if (head) { head.textContent = down ? down + (down === 1 ? " NODE DOWN" : " NODES DOWN") : "ALL NODES UP"; head.className = "ux-head " + (down ? "bad" : "good"); }
    const running = (d.pods || []).filter((p) => p.status === "Running").length, failing = (d.pods || []).filter((p) => /Error|BackOff|Failed|OOM/.test(p.status)).length;
    const hs = $("#ux-headsub"); if (hs) hs.textContent = up + " / " + nodes.length + " nodes · " + running + " pods running" + (failing ? " · " + failing + " failing" : "") + " · " + ((S.res && S.res.alerts) || []).length + " alerts";
    const tk = $("#ux-ticker"); if (tk) NY.freshHTML(tk, raw(G.log.slice(0, 3).map((e) => '<div class="' + (e.tone || "") + '"><span>' + new Date(e.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + "</span> " + NY.esc(e.text) + "</div>").join("")));
    const cl = $("#ux-cluster"); if (cl) cl.textContent = ((d.cluster && d.cluster.name) || "cluster").toUpperCase() + " · " + (sp ? label : "no model running");
    const half = Math.ceil(nodes.length / 2);
    const L = $("#ux-left"), R = $("#ux-right");
    if (L) L.innerHTML = nodes.slice(0, half).map(card).join("");
    if (R) R.innerHTML = nodes.slice(half).map(card).join("");
    if (G.scene && !G.laid) { G.laid = true; requestAnimationFrame(() => { G.scene.layout(); if (!G.scene.raf) G.scene.frame(0); }); }
    const f = G.scene && G.scene.focus && nodes.find((n) => n.name === G.scene.focus);
    const t = $("#ux-title"), o = $("#ux-overview");
    if (t) t.textContent = f ? f.name.toUpperCase() : "OVERVIEW";
    if (o) o.textContent = f ? (f.ready ? pct(f.cpu) + " cpu · " + pct(f.mem) + " memory · " + f.pods + " pods · ↓ " + speed(f.rx) + " ↑ " + speed(f.tx) : "not ready: " + (f.reason || "no contact")) : up + " / " + nodes.length + " nodes up · " + running + " pods · model " + state;
  }
  function tick() {
    const c = $("#ux-clock"), dd = $("#ux-date"); if (!c) return;
    const now = new Date(); c.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (dd) dd.textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "short", year: "numeric" }).toUpperCase();
  }

  // the AI helper: a question about the cluster, answered by the model with a short live summary
  function brief() {
    const d = S.d || {}, nodes = snapshotNodes(d), sp = (d.ai && d.ai.split) || null;
    const lines = nodes.map((n) => "- " + n.name + ": " + (n.ready ? "Ready" : "NOT READY (" + (n.reason || "no contact") + ")") + (n.hub ? ", control plane" : "") + (n.main ? ", runs the model" : n.model ? ", holds model layers" : "") +
      (n.ready ? ", CPU " + pct(n.cpu) + (n.cores ? " of " + n.cores + " cores" : "") + ", memory " + (n.memTotal ? fmt.bytes(n.memUsed, 1) + "/" + fmt.bytes(n.memTotal, 1) : "?") + (n.temp != null ? ", " + Math.round(n.temp) + " °C" : "") +
        ", network down " + speed(n.rx) + " up " + speed(n.tx) + (n.gpus[0] ? ", GPU " + (n.gpus[0].name || "") + " " + pct(n.gpus[0].use) : "") + ", " + n.pods + " pods" : ""));
    const bad = (d.pods || []).filter((p) => p.status !== "Running" && !/Succeeded|Completed/.test(p.status)).slice(0, 8).map((p) => p.namespace + "/" + p.name + " " + p.status + " on " + (p.node || "no node"));
    const alerts = ((S.res && S.res.alerts) || []).slice(0, 6).map((x) => x.level + ": " + x.title);
    return "Now: " + new Date().toLocaleString() + ". Cluster " + ((d.cluster && d.cluster.name) || "") + ".\nNodes:\n" + lines.join("\n") +
      "\nModel: " + (sp ? (sp.alias || sp.model) + (sp.loaded === false ? " (unloaded)" : sp.ready ? " (ready)" : " (loading)") + " split over " + Array.from(new Set((sp.shares || []).map((s) => s.node))).join(", ") : "none") +
      (bad.length ? "\nPods not running: " + bad.join("; ") : "") + (alerts.length ? "\nAlerts: " + alerts.join("; ") : "");
  }
  // The instructions never change, so the model keeps them read (its prompt cache); the live numbers go with each question.
  const ASK_SYSTEM = "You are the AI helper on the AI galaxy display of the user's home Kubernetes (k3s) cluster, run with nodeyard. " +
    "Answer in 1 to 4 short, plain sentences. Each question comes with the cluster's live state; use it, and if the answer isn't in it, say so.";
  async function pickTarget() {
    if (G.target) return G.target;
    try { const r = await getJSON("/api/ai/targets"); const ts = (r.targets || []).filter((t) => t.ready); const t = ts.find((x) => x.id === "split") || ts[0]; G.target = t ? t.id : ""; } catch (e) { G.target = ""; }
    return G.target;
  }
  function paintAnswer() {
    const el = $("#ux-answer"), A = G.ask; if (!el) return;
    el.classList.toggle("hide", !A.q);
    el.innerHTML = A.q ? '<div class="ux-q">' + NY.esc(A.q) + '</div><div class="ux-a">' + (A.text ? NY.esc(A.text).replace(/\n/g, "<br>") : '<span class="faint">' + NY.esc(A.wait || "Thinking…") + "</span>") + (A.busy && A.text ? '<span class="caret"></span>' : "") + "</div>" : "";
    el.scrollTop = el.scrollHeight;
    const b = $("#ux-send"); if (b) { b.textContent = A.busy ? "Stop" : "Ask"; b.classList.toggle("danger", A.busy); }
  }
  async function ask(q) {
    const A = G.ask;
    if (A.busy) { if (A.ctrl) A.ctrl.abort(); return; }
    q = (q || "").trim(); if (!q) return;
    const target = await pickTarget();
    A.q = q; A.text = ""; A.wait = "Thinking…"; A.busy = true; A.ctrl = new AbortController(); paintAnswer();
    if (!target) { A.busy = false; A.text = "No model is ready to answer. Start one in AI › Models."; paintAnswer(); return; }
    // history moves in steps of two exchanges (not one), so the start of the prompt stays the same between questions
    const keep = A.history.length <= 6 ? A.history : A.history.slice(-4 - (A.history.length % 4));
    const messages = [{ role: "system", content: ASK_SYSTEM }].concat(keep, [{ role: "user", content: "Live cluster state:\n" + brief() + "\n\nQuestion: " + q }]);
    try {
      const r = await fetch("/api/ai/chat", { method: "POST", signal: A.ctrl.signal, headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify({ target, messages, temperature: 0.3, max_tokens: 400 }) });
      if (!r.ok) { let j = {}; try { j = await r.json(); } catch (e) { /* not JSON */ } throw new Error(j.error || "The model didn't answer (HTTP " + r.status + ")."); }
      const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await rd.read(); if (done) break;
        buf += dec.decode(value, { stream: true }); let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const l = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!l.startsWith("data:") || l.includes("[DONE]")) continue;
          let j; try { j = JSON.parse(l.slice(5)); } catch (e) { continue; }
          if (j.error) throw new Error(j.error.message || j.error);
          if (j.prompt_progress && !A.text) { const pp = j.prompt_progress; A.wait = "Reading the cluster summary… " + Math.round((100 * (pp.processed || 0)) / Math.max(1, pp.total || 1)) + "%"; paintAnswer(); }
          const dl = j.choices && j.choices[0] && j.choices[0].delta;
          if (dl && dl.content) { A.text += dl.content.replace(/<\/?think>/g, ""); paintAnswer(); }
        }
      }
      A.history.push({ role: "user", content: q }, { role: "assistant", content: A.text }); if (A.history.length > 10) A.history = A.history.slice(-4);
    } catch (e) {
      if (e.name !== "AbortError") A.text = (A.text ? A.text + "\n\n" : "") + "⚠ " + (e.message || "Something went wrong.");
    } finally { A.busy = false; paintAnswer(); }
  }

  V.galaxy = {
    build() {
      NY.freshHTML($("#view"), html`<div class="galaxy-embed"><iframe src="/g3/embed" title="AI Galaxy" allow="fullscreen"></iframe></div>`);
    },
    update() {},
  };
  document.addEventListener("submit", (e) => { if (e.target.id === "ux-ask") { e.preventDefault(); const q = $("#ux-q"); ask(q.value); if (!G.ask.busy) q.value = ""; else q.value = ""; } });
  document.addEventListener("click", (e) => {
    const nodeCard = e.target.closest("[data-gx-node]");
    if (nodeCard && G.scene) { G.scene.tour = false; G.scene.fly(G.scene.focus === nodeCard.dataset.gxNode ? "" : nodeCard.dataset.gxNode); return; }
    const b = e.target.closest("[data-gx]"); if (!b) return;
    if (b.dataset.gx === "tour" && G.scene) { G.scene.tour = !G.scene.tour; G.scene.tourAt = G.scene.t - 99; b.classList.toggle("primary", G.scene.tour); if (!G.scene.tour) G.scene.fly(""); return; }
    if (b.dataset.gx !== "full") return;
    const el = $("#gx"); if (!el) return;
    if (document.fullscreenElement) document.exitFullscreen(); else if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && G.scene && G.scene.focus && $("#gx") && !document.fullscreenElement) G.scene.fly(""); });
  document.addEventListener("fullscreenchange", () => { if (G.scene && $("#gx")) setTimeout(() => { G.scene.fit(); G.scene.reset(); }, 60); });
  NY.Galaxy = Galaxy;
  NY.galaxyScene = () => G.scene;   // (for tests)
})();
