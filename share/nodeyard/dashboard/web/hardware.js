// The Hardware page: what every machine is (CPU, clocks, caches, memory, disks, network, GPU)
// from the node agents, and how fast it really is from `nodeyard hw bench`. Loaded after app.js.
(function () {
  "use strict";
  const { html, raw, setHTML, fmt } = NY;
  const U = NY.ui;
  const { S, V, $, chip, card, empty, startJob } = U;

  const gbs = (v) => (v == null ? "–" : v.toFixed(v >= 10 ? 1 : 2) + " GB/s");
  const mbs = (v) => (v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(2) + " GB/s" : Math.round(v) + " MB/s");
  const mbps = (v) => (v == null ? "–" : v >= 1000 ? v / 1000 + " Gbit/s" : v + " Mbit/s");
  const cacheText = (cs) => (cs || []).filter((c) => c.type !== "Instruction").map((c) => "L" + c.level + " " + fmt.bytes(c.size, 0) + (c.shared && /[-,]/.test(c.shared) && c.level >= 2 ? " shared" : "")).join(" · ");
  const FLAG_NAMES = { sse4_2: "SSE4.2", avx: "AVX", avx2: "AVX2", fma: "FMA", f16c: "F16C", avx512f: "AVX-512", avx512_vnni: "AVX-512 VNNI", avx_vnni: "AVX-VNNI",
    amx_tile: "AMX", aes: "AES", sha_ni: "SHA", asimd: "NEON", neon: "NEON", asimddp: "dot product", sve: "SVE", sve2: "SVE2", i8mm: "int8 matmul", bf16: "BF16", fp16: "FP16", asimdhp: "half-float" };
  const AI_FLAGS = new Set(["avx2", "avx512f", "avx512_vnni", "avx_vnni", "amx_tile", "asimddp", "sve", "sve2", "i8mm", "bf16"]);

  function rows() {
    const d = S.d, bench = d.bench || {};
    return d.nodes.map((n) => ({ n, hw: (n.hw && n.hw.hardware) || null, b: bench[n.name] || null }));
  }

  function summaryTable(list) {
    const cols = [
      { k: "name", t: "Machine", v: (r) => r.n.name, r: (r) => html`<b>${r.n.name}</b><div class="sub">${(r.hw && r.hw.board.model) || r.n.os}</div>` },
      { k: "cpu", t: "CPU", cls: "wrap", v: (r) => (r.hw ? r.hw.cpu.model : ""), r: (r) => (r.hw ? html`${r.hw.cpu.model}${r.hw.cpu.core ? html`<div class="sub">${r.hw.cpu.core}</div>` : ""}` : html`<span class="faint">needs the node agent</span>`) },
      { k: "cores", t: "Cores", cls: "num", v: (r) => (r.hw ? r.hw.cpu.threads : r.n.cpu_capacity), r: (r) => (r.hw ? r.hw.cpu.cores + (r.hw.cpu.threads !== r.hw.cpu.cores ? " / " + r.hw.cpu.threads : "") : "–") },
      { k: "clock", t: "Max clock", cls: "num", v: (r) => (r.hw ? r.hw.cpu.max_mhz : null), r: (r) => (r.hw && r.hw.cpu.max_mhz ? fmt.mhz(r.hw.cpu.max_mhz) : "–") },
      { k: "ram", t: "RAM", cls: "num", v: (r) => r.n.mem_total, r: (r) => fmt.bytes(r.n.mem_total, 1) },
      { k: "memspeed", t: "Memory speed", cls: "num", v: (r) => (r.b ? r.b.mem_copy_all_gbs : null), r: (r) => (r.b ? gbs(r.b.mem_copy_all_gbs) : "–") },
      { k: "cpuscore", t: "CPU score", cls: "num", v: (r) => (r.b ? r.b.cpu_all : null), r: (r) => (r.b ? html`${r.b.cpu_all}<div class="sub">${r.b.cpu_1core} one core</div>` : "–") },
      { k: "disk", t: "Disk read / write", cls: "num", v: (r) => (r.b && r.b.disk ? r.b.disk.read_mbs : null), r: (r) => (r.b && r.b.disk && r.b.disk.read_mbs ? html`${mbs(r.b.disk.read_mbs)}<div class="sub">${mbs(r.b.disk.write_mbs)} write</div>` : "–") },
      { k: "gpu", t: "GPU", cls: "wrap", v: (r) => (r.hw ? (r.hw.gpus || []).filter((g) => !g.boot).length : null), r: (r) => { const g = r.hw && (r.hw.gpus || []); if (!g || !g.length) return "–"; const extra = g.filter((x) => !x.boot); return (extra.length ? extra : g).map((x) => (x.name_full || x.vendor).replace(/^(NVIDIA|AMD|Intel) .*?\[(.+)\]$/, "$1 $2")).join(", "); } },
      { k: "net", t: "Network", cls: "num", v: (r) => (r.hw ? Math.max(0, ...r.hw.nics.map((x) => x.speed_mbps || 0)) : null), r: (r) => { const c = r.hw && r.hw.nics.find((x) => x.cluster); if (c) return c.wireless ? "Wi-Fi" : c.speed_mbps && c.speed_mbps < 1000 ? chip(mbps(c.speed_mbps), "warn") : mbps(c.speed_mbps); const w = r.hw && r.hw.nics.filter((x) => x.up && !x.name.startsWith("tailscale")); return w && w.length ? w.map((x) => (x.wireless ? "Wi-Fi" : mbps(x.speed_mbps))).join(" · ") : "–"; } },
    ];
    return U.table("hw-sum", cols, list, { k: "cpuscore", empty: "No nodes" });
  }

  function nodeCard(r) {
    const { n, hw, b } = r, cpu = hw && hw.cpu;
    const kv = (k, v) => html`<div class="kvl"><span class="muted">${k}</span><span>${v}</span></div>`;
    const tested = b ? html`<span class="faint small">tested ${fmt.ago(b.time)} ago</span>` : "";
    const body = hw ? html`
      <div class="grid g-2" style="gap:6px 28px">
        <div><div class="section-title" style="margin-top:0">Processor</div>
          ${kv("Model", cpu.model + (cpu.core ? " (" + cpu.core + ")" : ""))}
          ${kv("Cores", cpu.cores + " cores" + (cpu.threads !== cpu.cores ? ", " + cpu.threads + " threads" : ""))}
          ${kv("Clock", (cpu.min_mhz ? fmt.mhz(cpu.min_mhz) + " – " : "") + (cpu.max_mhz ? fmt.mhz(cpu.max_mhz) + " max" : "–") + (n.hw.freq_mhz ? " · now " + fmt.mhz(n.hw.freq_mhz) : ""))}
          ${kv("Cache", cacheText(cpu.caches) || "–")}
          ${kv("Architecture", cpu.arch + (cpu.vendor ? " · " + cpu.vendor : ""))}
          <div class="row wrap" style="gap:5px;margin-top:8px">${cpu.flags.map((f) => chip(FLAG_NAMES[f] || f, AI_FLAGS.has(f) ? "violet" : ""))}</div>
          ${b ? html`<div class="section-title">Speed test ${tested}</div>
            ${kv("CPU score", b.cpu_all + " all cores · " + b.cpu_1core + " one core")}
            ${kv("Memory copy", gbs(b.mem_copy_all_gbs) + " all cores · " + gbs(b.mem_copy_1core_gbs) + " one core")}
            ${b.disk && b.disk.read_mbs ? html`${kv("Disk read", mbs(b.disk.read_mbs))}${kv("Disk write", mbs(b.disk.write_mbs))}${kv("Random reads", fmt.num(b.disk.rand_read_iops) + " IOPS (4 KiB)")}` : kv("Disk", (b.disk && (b.disk.skipped || b.disk.error)) || "–")}` : ""}
        </div>
        <div><div class="section-title" style="margin-top:0">Memory</div>
          ${kv("RAM", fmt.bytes(hw.memory.total || n.mem_total, 1))}${kv("Swap", hw.memory.swap ? fmt.bytes(hw.memory.swap, 1) : "none")}
          <div class="section-title">Disks</div>
          ${hw.disks.length ? hw.disks.map((d) => kv(d.name, html`${d.model || d.vendor || "disk"} · ${fmt.bytes(d.size, 0)} ${chip(d.kind, d.kind.startsWith("NVMe") ? "good" : d.kind.startsWith("HDD") || d.kind.startsWith("SD") ? "warn" : "")}`)) : kv("Disks", "–")}
          <div class="section-title">Network</div>
          ${hw.nics.length ? hw.nics.map((x) => kv(x.name, html`${x.wireless ? "Wi-Fi" : x.name.startsWith("tailscale") ? "Tailscale" : mbps(x.speed_mbps)} ${x.up ? "" : chip("down")}${x.cluster ? chip("cluster link", x.speed_mbps && x.speed_mbps < 1000 && !x.wireless ? "warn" : "accent") : ""}`)) : kv("Network", "–")}
          ${hw.gpus.length ? html`<div class="section-title">Graphics</div>${hw.gpus.map((g) => kv(g.boot ? "built in" : "card", html`${g.name_full || g.model || g.vendor}${g.vram ? " · " + fmt.bytes(g.vram, 0) : ""} ${g.driver ? chip(g.driver + (g.driver_version ? " " + g.driver_version : ""), g.vendor === "0x10de" && g.driver === "nvidia" ? "good" : "") : chip("no driver", "warn")}`))}` : ""}
          <div class="section-title">System</div>
          ${hw.board.board ? kv("Board", hw.board.board) : ""}${hw.board.bios ? kv("Firmware", hw.board.bios + (hw.board.bios_date ? " (" + hw.board.bios_date + ")" : "")) : ""}
          ${kv("Kernel", hw.kernel || n.kernel)}${kv("OS", n.os)}
        </div>
      </div>` : empty("No hardware details yet", "Install the node agents (Settings → Node agents) to see this machine's CPU, disks and network.");
    return card(n.name + (hw && hw.board.model ? " · " + hw.board.model : ""), html`${body}
      <div class="row wrap" style="gap:8px;margin-top:14px"><button class="btn small" data-hw="bench" data-node="${n.name}">${b ? "Test again" : "Run speed test"}</button>${tested}</div>`);
  }

  V.hardware = {
    build() { NY.freshHTML($("#view"), html`<div id="hw-body"></div>`); },
    update() {
      const list = rows();
      setHTML($("#hw-body"), html`<div class="card"><div class="row wrap" style="gap:10px">
          <span class="muted small grow">RAM type and MT/s need root-only firmware tables, so memory is <b>measured</b> instead (how fast each machine copies memory, which is what limits AI speed).</span>
          <button class="btn primary" data-hw="bench">Run speed test on every machine</button></div>
          <div class="mt">${summaryTable(list)}</div></div>
        ${list.map((r) => html`<div class="mt">${nodeCard(r)}</div>`)}`);
    },
  };

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-hw]"); if (!el) return;
    if (el.dataset.hw === "bench") startJob("hw-bench", el.dataset.node ? { node: el.dataset.node } : {}, () => U.load(true));
  });
  void raw;
})();
