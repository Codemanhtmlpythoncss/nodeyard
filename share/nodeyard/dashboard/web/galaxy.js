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
  const G = { scene: null, poll: 0, prev: null, rate: 0, log: [], last: {}, act: null };
  const say = (text, tone) => { G.log.unshift({ t: Date.now() / 1000, text, tone }); G.log.length = Math.min(G.log.length, 9); };
  function nodesFor(d) {
    const sp = (d && d.ai && d.ai.split) || {}, shares = sp.shares || [], total = shares.reduce((a, s) => a + (s.mib || 0), 0) || 1;
    return ((d && d.nodes) || []).map((n) => {
      const mine = shares.filter((s) => s.node.replace(/ GPU$/, "") === n.name), mib = mine.reduce((a, s) => a + (s.mib || 0), 0);
      return { name: n.name, ready: n.ready, cpu: n.cpu_cores ? (n.cpu_used || 0) / n.cpu_cores : 0, mem: n.mem_total ? (n.mem_used || 0) / n.mem_total : 0,
        temp: n.hw && n.hw.temp_c, model: mine.length > 0, gpu: mine.some((s) => s.gpu), share: mib / total };
    });
  }
  function track(d) {   // what changed since last time, for the activity log
    const sp = (d && d.ai && d.ai.split) || null;
    ((d && d.nodes) || []).forEach((n) => { const was = G.last["n:" + n.name]; if (was !== undefined && was !== n.ready) say(n.name + (n.ready ? " is back" : " dropped out"), n.ready ? "good" : "bad"); G.last["n:" + n.name] = n.ready; });
    const phase = sp ? (sp.loaded === false ? "unloaded" : sp.ready ? "ready" : "loading") : "none";
    if (G.last.phase !== undefined && G.last.phase !== phase) say("Model " + (phase === "ready" ? "ready" : phase === "loading" ? "loading" : phase === "unloaded" ? "unloaded" : "gone"), phase === "ready" ? "good" : "");
    G.last.phase = phase;
  }
  async function pollActivity() {
    let a; try { a = await getJSON("/api/ai/activity"); } catch (e) { return; }
    if (!a || !a.ok) return;
    const now = a.time || Date.now() / 1000, p = G.prev;
    if (a.processing && p && p.processing && p.task === a.task && a.decoded >= p.decoded && now > p.time) {
      const r = (a.decoded - p.decoded) / (now - p.time); G.rate = G.rate ? G.rate * 0.6 + r * 0.4 : r;
    }
    if (a.processing && !(p && p.processing)) { G.started = now; G.rate = 0; say("Answering started", ""); }
    if (!a.processing && p && p.processing) say("Answer finished: " + (p.decoded || 0) + " tokens in " + Math.max(1, Math.round(now - (G.started || now))) + " s", "good");
    G.prev = a; G.act = a;
    if (G.scene) G.scene.set({ busy: !!a.processing, reading: a.processing && !a.decoded, rate: G.rate });
    paintHud();
  }
  function paintHud() {
    const d = S.d; if (!d || !$("#gx")) return;
    const sp = (d.ai && d.ai.split) || null, a = G.act || {}, nodes = nodesFor(d), ready = nodes.filter((n) => n.ready).length;
    const state = !sp ? "No model running" : sp.loaded === false ? "Unloaded" : !sp.ready ? "Loading" + (sp.load && sp.load.pct != null ? " " + Math.round(sp.load.pct) + "%" : "") : a.processing ? (a.decoded ? "Answering" : "Reading the prompt") : "Ready";
    const tone = state === "Answering" || state === "Reading the prompt" ? "live" : state === "Ready" ? "good" : "warn";
    NY.freshHTML($("#gx-model"), html`<div class="gx-k">Model</div><div class="gx-big">${sp ? sp.alias || sp.model : "none"}</div>
      <div class="gx-row gx-state"><span class="gx-dot ${tone}"></span>${state}</div>
      <div class="gx-row"><span>speed</span><b>${a.processing && G.rate ? G.rate.toFixed(1) + " tok/s" : "–"}</b></div>
      <div class="gx-row"><span>this answer</span><b>${a.processing ? (a.decoded || 0) + " tokens" : "–"}</b></div>
      <div class="gx-row"><span>context</span><b>${a.n_ctx || (sp && sp.ctx) || "–"}</b></div>
      <div class="gx-row"><span>runs on</span><b>${sp && sp.shares ? Array.from(new Set(sp.shares.map((s) => s.node.replace(/ GPU$/, "")))).join(", ") : "–"}</b></div>`);
    const cpu = nodes.length ? nodes.reduce((x, n) => x + n.cpu, 0) / nodes.length : 0, t = S.t || {};
    NY.freshHTML($("#gx-cluster"), html`<div class="gx-k">Cluster</div><div class="gx-big">${(d.cluster && d.cluster.name) || "cluster"}</div>
      <div class="gx-row"><span>nodes ready</span><b>${ready} / ${nodes.length}</b></div>
      <div class="gx-row"><span>cpu</span><b>${Math.round(cpu * 100)}%</b></div>
      <div class="gx-row"><span>memory</span><b>${t.mem_total ? fmt.bytes(t.mem_used, 0) + " / " + fmt.bytes(t.mem_total, 0) : "–"}</b></div>
      <div class="gx-row"><span>pods running</span><b>${t.pods_running != null ? t.pods_running : ((d.pods || []).filter((p) => p.status === "Running").length)}</b></div>`);
    NY.freshHTML($("#gx-nodes"), html`<div class="gx-k">Machines</div>${nodes.map((n) => html`<div class="gx-node"><span class="gx-dot ${n.ready ? (n.model ? "live" : "good") : "bad"}"></span><b>${n.name}</b>
      <span class="gx-bar"><i style="width:${Math.round(n.cpu * 100)}%"></i></span><span class="gx-num">${n.ready ? Math.round(n.cpu * 100) + "%" : "down"}</span></div>`)}`);
    NY.freshHTML($("#gx-log"), html`<div class="gx-k">Activity</div>${G.log.length ? G.log.map((e) => html`<div class="gx-ev ${e.tone || ""}"><span>${new Date(e.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>${e.text}</div>`) : html`<div class="gx-ev faint">Waiting for something to happen…</div>`}`);
    if (G.scene) G.scene.set({ nodes, loaded: !sp || (sp.loaded !== false && sp.ready) });
  }

  V.galaxy = {
    build() {
      NY.freshHTML($("#view"), raw('<div class="gx" id="gx"><canvas id="gx-c" aria-label="Your cluster and its AI, drawn as a galaxy"></canvas>' +
        '<div class="gx-hud tl" id="gx-model"></div><div class="gx-hud tr" id="gx-cluster"></div><div class="gx-hud bl" id="gx-nodes"></div><div class="gx-hud br" id="gx-log"></div>' +
        '<button class="btn small gx-full" data-gx="full" title="Full screen (for a spare screen, like a rack display)">Full screen</button></div>'));
      if (G.scene) G.scene.destroy();
      G.scene = new Galaxy($("#gx-c"), { hud: true, particles: 2600 }).start();
      if (S.d) track(S.d);
      paintHud();
      clearInterval(G.poll); pollActivity();
      G.poll = setInterval(() => { if (!$("#gx")) { clearInterval(G.poll); G.poll = 0; return; } if (!document.hidden) pollActivity(); }, 1000);
    },
    update() { if (S.d) track(S.d); paintHud(); },
  };
  document.addEventListener("click", (e) => {
    const b = e.target.closest('[data-gx="full"]'); if (!b) return;
    const el = $("#gx"); if (!el) return;
    if (document.fullscreenElement) document.exitFullscreen(); else if (el.requestFullscreen) el.requestFullscreen().then(() => { if (G.scene) { G.scene.fit(); G.scene.reset(); } }).catch(() => {});
  });
  document.addEventListener("fullscreenchange", () => { if (G.scene && $("#gx")) setTimeout(() => { G.scene.fit(); G.scene.reset(); }, 60); });
  NY.Galaxy = Galaxy;
  NY.galaxyScene = () => G.scene;   // (for tests)
})();
