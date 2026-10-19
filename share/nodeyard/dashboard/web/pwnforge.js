// The PwnForge page: host the PwnForge training platform from this machine if you want it (pwnforge.py does the work).
// Off and not installed, it only explains what it would do. Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML, fmt } = NY;
  const U = NY.ui;
  const { S, V, $, card, chip, toast, getJSON, postJSON, ago, empty } = U;
  const PF = { data: null, timer: 0, busy: "", cleanup: null };

  async function load() {
    clearTimeout(PF.timer);
    try { const r = await getJSON("/api/pwnforge"); PF.data = r.ok ? r : { error: r.error }; } catch (e) { PF.data = { error: "Couldn't reach the dashboard." }; }
    paint();
    if (S.view === "pwnforge") PF.timer = setTimeout(load, PF.data && PF.data.job && PF.data.job.running ? 1500 : 8000);
  }
  async function post(url, body, busy) {
    PF.busy = busy || "…"; paint();
    try { const r = await postJSON(url, body); if (!r.ok) { toast(r.error || "That didn't work."); return null; } return r; }
    catch (e) { toast(e.message || "That didn't work."); return null; }
    finally { PF.busy = ""; setTimeout(load, 300); }
  }

  const stateChip = (d) => {
    if (!d.installed) return chip("not installed");
    const a = d.unit.active, h = d.health;
    if (a === "active") return h && h.ok ? chip("running", "good") : chip("unhealthy", "bad");
    if (a === "activating" || a === "reloading") return chip("starting", "warn");
    if (a === "failed") return chip("failed", "bad");
    return chip("stopped");
  };
  const btn = (act, label, cls, title) => raw('<button class="btn small ' + (cls || "") + '" data-pf="' + act + '"' + (PF.busy ? " disabled" : "") + (title ? ' title="' + esc(title) + '"' : "") + ">" + esc(label) + "</button>");

  function hostingCard(d) {
    const u = d.unit || {}, h = d.health, c = d.config, job = d.job;
    const lines = [];
    if (d.installed) {
      lines.push(html`<div class="row wrap" style="gap:6px">${stateChip(d)} ${c.enabled ? chip("hosting on", "good") : chip("hosting off")} ${u.enabled ? chip("starts at boot") : ""}
        ${h && h.version ? chip("v" + h.version) : ""} ${h && h.ok ? html`<span class="muted small">answers in ${h.ms} ms</span>` : ""}
        ${u.since ? html`<span class="muted small">up since ${ago(u.since)}</span>` : ""}${u.restarts ? html` <span class="muted small">· restarted ${u.restarts}×</span>` : ""}</div>`);
      if (h && !h.ok && u.active === "active") lines.push(html`<div class="err mt small">The service runs, but PwnForge doesn't answer: ${h.error || "no reply"}. Open Logs or Diagnostics.</div>`);
      if (u.active === "failed") lines.push(html`<div class="err mt small">The service failed (${u.result || "unknown"}). Its log says why: open Logs.</div>`);
      if (h && h.ok && h.initialised === false) lines.push(html`<div class="banner warn mt"><svg class="icon"><use href="#i-warn"/></svg><span>Open PwnForge now and create its <b>administrator account</b>: until you do, whoever opens it first could. Keep it on <b>This machine only</b> or <b>Your tailnet</b> until then.</span></div>`);
      if (u.active === "active" && (d.urls || []).length) lines.push(html`<div class="mt">${d.urls.map((x) => html`<div><a href="${x.url}" target="_blank" rel="noopener">${x.url}</a> <span class="muted small">${x.note}</span></div>`)}</div>`);
      const v = d.virt || {};
      lines.push(html`<div class="mt small row wrap" style="gap:6px">${v.ready ? chip("real machines", "good") : chip("real VM setup needed", "warn")}<span class="muted">${v.ready
        ? v.kvm ? "Labs boot real virtual machines with KVM acceleration. The first start of each kind of machine prepares its image once."
          : "Labs boot real virtual machines with QEMU software emulation. They work without KVM but run slower."
        : "Real VMs are unavailable until QEMU and libvirt are set up. PwnForge will not create simulated guests."}</span></div>`);
    } else {
      lines.push(html`<p class="muted" style="margin-top:0">PwnForge is a self-hosted cybersecurity training platform: courses and real practice machines on isolated virtual networks (QEMU/libvirt; KVM acceleration when available).
        Install puts it in <span class="mono">${c.path}</span> with its data in <span class="mono">${c.data_dir}</span>, as its own service running as the unprivileged user <span class="mono">pwnforge</span>.
        It downloads its code and packages (git, pip, npm) but never any OS images. The dashboard works the same with or without it.</p>`);
    }
    if (job) lines.push(jobBlock(job));
    const acts = [];
    if (!d.installed) acts.push(btn("install", job && job.running ? "Installing…" : "Install PwnForge", "primary"));
    else {
      acts.push(c.enabled ? btn("hosting-off", "Turn hosting off", "", "Stops it and stops it starting at boot") : btn("hosting-on", "Turn hosting on", "primary", "Starts it (and at boot, if that's on)"));
      if (u.active === "active") acts.push(btn("restart", "Restart"), btn("stop", "Stop"));
      else acts.push(btn("start", "Start"));
      acts.push(u.enabled ? btn("disable", "Don't start at boot") : btn("enable", "Start at boot"));
      acts.push(btn("logs", "Logs"), btn("doctor", "Diagnostics"), btn("install", "Update", "", "Gets the newest PwnForge and rebuilds it; your users, progress and machines are kept"));
      const v = d.virt || {};
      if (v.linux && !v.ready) acts.push(btn("virt", job && job.running && job.what === "real machines setup" ? "Setting up…" : "Set up real machines", "primary", "Installs QEMU and libvirt so labs boot real virtual machines; without KVM they use slower software emulation"));
    }
    return card("PwnForge", html`${lines}<div class="row wrap mt" style="gap:6px">${acts}</div>`, d.installed ? html`<span class="muted small">${u.active || ""}</span>` : "");
  }
  function jobBlock(job) {
    const title = job.running ? html`<span class="spin small"></span> ${job.what}…` : job.result === "ok" ? html`${chip("done", "good")} ${job.what}` : html`${chip("failed", "bad")} ${job.what}`;
    return html`<div class="mt"><div class="row" style="gap:6px">${title}<span class="grow"></span><button class="btn small" data-pf="logs">Full log</button></div><pre class="hl-pre small" id="pf-joblog" style="max-height:180px;overflow:auto;margin-top:6px">${PF.jobLog || ""}</pre></div>`;
  }

  function addressCard(d) {
    const c = d.config;
    const opt = (k, label, desc) => html`<label class="perm-opt ${c.bind === k ? "on" : ""}" style="cursor:pointer"><span class="row" style="gap:8px"><input type="radio" name="pf-bind" value="${k}" ${c.bind === k ? "checked" : ""}> <b>${label}</b></span><div class="muted small">${desc}</div></label>`;
    return card("Address", html`<div class="perm-grid">
        ${opt("local", "This machine only", "127.0.0.1. The safest: reach it from elsewhere with an SSH tunnel.")}
        ${opt("tailnet", "Your tailnet", "This machine's Tailscale address: only your own devices on Tailscale can open it.")}
        ${opt("https", "Your tailnet, HTTPS", "The same, at an https:// address with a real certificate (Tailscale Serve). Needs MagicDNS and HTTPS certificates on for your tailnet.")}
        ${opt("lan", "Every network", "0.0.0.0: anyone on the networks this machine is on. Only once its administrator account exists. Never use this for a machine that's on the internet.")}
      </div>
      <div class="row wrap mt" style="gap:8px"><label class="row small" style="gap:6px">Port <input id="pf-port" type="number" min="1024" max="65535" value="${c.port}" style="width:100px"></label>
        <button class="btn small primary" data-pf="save-config" ${PF.busy ? "disabled" : ""}>Save</button>
        <span class="muted small">This setting wins over PwnForge's own "LAN access" setting. PwnForge is never published to the internet from here.</span></div>`);
  }

  function labAccessCard(d) {
    if (!d.installed) return "";
    return card("Direct lab access", html`<p class="muted small" style="margin-top:0">Route directly to running lab machines by their private IP addresses from this LAN or your Tailscale network. The host keeps lab-to-internet and lab-to-lab forwarding blocked. Tailscale route approval is still required in its admin console.</p>
      <div class="row wrap" style="gap:8px">
        <label class="row small" style="gap:6px">Lab subnet <input id="pf-lab-cidr" value="10.200.0.0/16" spellcheck="false" autocapitalize="off" style="width:180px"></label>
        <button class="btn small" data-pf="lab-access-status">Check routing</button>
        <button class="btn small primary" data-pf="lab-access-on">Enable direct access</button>
        <button class="btn small" data-pf="lab-access-off">Disable direct access</button>
        <a class="btn small" href="/pwnforge-install-macos.sh" download="pwnforge-install-macos.sh">Install macOS CLI</a>
        <a class="btn small" href="/pwnforge-install-linux.sh" download="pwnforge-install-linux.sh">Install Linux CLI</a>
      </div>
      <div class="muted small mt">The installer adds a <span class="mono">pwnforge</span> command. Run <span class="mono">pwnforge install 10.200.0.0/16 &lt;server-LAN-IP&gt;</span> once; then use <span class="mono">pwnforge status</span> or <span class="mono">pwnforge test &lt;lab-IP&gt; 22</span>. macOS and Linux use a persistent LAN fallback and leave approved Tailscale routes in control.</div>`);
  }

  function envCard(d) {
    if (!d.installed) return "";
    if (d.environments_error) return card("Training environments", html`<div class="muted">${d.environments_error}</div>`);
    const E = d.environments;
    if (!E) return card("Training environments", html`<div class="muted"><span class="spin small"></span> Asking PwnForge…</div>`);
    const vms = E.vms || [], labs = E.labs || [];
    const net = (n) => (!n ? html`<span class="faint">none</span>` : n.mode === "isolated" && !n.external_access ? chip("isolated", "good")
      : html`${n.mode}${n.external_access ? html` ${chip("outside access", "warn")}` : ""}`);
    const st = (s) => chip(s, s === "running" ? "good" : s === "error" ? "bad" : s === "stopped" || s === "defined" ? "" : "warn");
    const acts = (kind, x) => html`<span class="row" style="gap:4px;justify-content:flex-end">${x.status === "running" ? raw('<button class="btn small" data-pf-env="stop" data-kind="' + kind + '" data-id="' + esc(x.id) + '">Stop</button>')
      : raw('<button class="btn small" data-pf-env="start" data-kind="' + kind + '" data-id="' + esc(x.id) + '">Start</button>')}${raw('<button class="btn small danger" data-pf-env="delete" data-kind="' + kind + '" data-id="' + esc(x.id) + '" data-name="' + esc(x.name || x.title || x.id) + '">Delete</button>')}</span>`;
    const running = vms.filter((v) => v.status === "running");
    const rows = vms.map((v) => html`<tr><td class="wrap"><b>${v.name}</b>${v.lab_instance_id ? html` <span class="faint small">lab</span>` : ""}<div class="sub">${v.owner || ""}${v.driver === "simulation" ? " · simulated" : ""}${v.ip_address ? " · " + v.ip_address : ""}</div>${v.last_error ? html`<div class="err small">${v.last_error}</div>` : ""}</td>
      <td>${st(v.status)}</td><td class="wrap small">${net(v.network)}</td>
      <td class="small">${v.vcpu} vCPU · ${fmt.bytes(v.memory_mb * 1048576)}${v.usage ? html`<div class="sub">${v.usage.cpu != null ? v.usage.cpu + "% CPU · " : ""}${fmt.bytes(v.usage.rss)} in use</div>` : ""}</td>
      <td class="small">${v.disk_bytes ? fmt.bytes(v.disk_bytes) : "–"}</td><td class="small">${v.started_at && v.status === "running" ? ago(Date.parse(v.started_at) / 1000) : ""}</td><td>${acts("vm", v)}</td></tr>`);
    const labRows = labs.map((l) => html`<tr><td class="wrap"><b>${l.title}</b> <span class="faint small">lab · ${l.machines.length} machine${l.machines.length === 1 ? "" : "s"}</span>${l.last_error ? html`<div class="err small">${l.last_error}</div>` : ""}</td><td>${st(l.status)}</td><td class="small">its own networks</td><td></td><td></td><td></td><td>${acts("lab", l)}</td></tr>`);
    const stg = d.storage;
    const sum = html`<div class="row wrap" style="gap:12px;margin-bottom:8px"><span><b>${running.length}</b> <span class="muted">running</span></span><span><b>${vms.length}</b> <span class="muted">machines</span></span><span><b>${labs.length}</b> <span class="muted">labs</span></span>
      ${running.length ? html`<span class="muted">${running.reduce((n, v) => n + v.vcpu, 0)} vCPU · ${fmt.bytes(running.reduce((n, v) => n + v.memory_mb, 0) * 1048576)} reserved</span>` : ""}
      ${stg && stg.physical ? html`<span class="muted">disk: ${stg.physical.free_gb} GB free of ${stg.physical.total_gb} GB${stg.overlays ? " · machine disks " + stg.overlays.actual_gb + " GB" : ""}</span>` : ""}
      <span class="grow"></span><button class="btn small" data-pf-env="cleanup">Clean up unused disks</button></div>`;
    const table = vms.length || labs.length ? html`<div class="tablewrap"><table class="tbl"><thead><tr><th>Environment</th><th>State</th><th>Network</th><th>Size</th><th>Disk</th><th>Up</th><th></th></tr></thead><tbody>${rows}${labRows}</tbody></table></div>`
      : empty("No training environments", html`<div class="muted small">Learners start them from PwnForge's website; they show up here.</div>`);
    return card("Training environments", html`${sum}${table}${PF.cleanup ? cleanupBlock(PF.cleanup) : ""}`);
  }
  function cleanupBlock(r) {
    if (!r.candidates.length && !r.removed.length) return html`<div class="muted small mt">No unused machine disks.</div>`;
    if (!r.dry_run) return html`<div class="mt small">${chip("done", "good")} Removed ${r.removed.length} disk${r.removed.length === 1 ? "" : "s"}, ${fmt.bytes(r.freed_bytes)} freed.</div>`;
    const total = r.candidates.reduce((n, x) => n + x.size_bytes, 0);
    return html`<div class="mt"><b>Unused machine disks</b> <span class="muted small">(${fmt.bytes(total)}; base images and machines in use are kept)</span>
      <ul class="small">${r.candidates.map((x) => html`<li><span class="mono">${x.path}</span> · ${fmt.bytes(x.size_bytes)} · ${x.reason}</li>`)}</ul>
      <button class="btn small danger" data-pf-env="cleanup-apply">Delete these ${r.candidates.length}</button></div>`;
  }
  const safetyCard = () => card("Isolation and safety", html`<ul class="small" style="margin:0;padding-left:18px">
    <li>Practice machines run on PwnForge's own <b>isolated</b> virtual networks; one that has outside access says so above. This page never changes that.</li>
    <li>PwnForge runs as the unprivileged user <span class="mono">pwnforge</span> (in the libvirt and kvm groups), with its unit's own hardening. No container or service here is privileged.</li>
    <li>Nothing is published to the internet: the address is this machine, your tailnet, or (after the admin account exists, and only when you say so) your local networks.</li>
    <li>Logs shown here hide passwords, tokens, cookies and captured flags. The dashboard never reads PwnForge's secret key or users' passwords.</li>
    <li>Only use it for training you're authorised to do: on these lab machines, not other people's systems.</li></ul>`);

  function paint() {
    const root = $("#pf-root"); if (!root) return;
    const d = PF.data;
    if (!d) { setHTML(root, html`<div class="muted"><span class="spin small"></span> Loading…</div>`); return; }
    if (d.error) { setHTML(root, card("PwnForge", html`<div class="err">${d.error}</div>`)); return; }
    if (d.problem) { setHTML(root, card("PwnForge", empty("Not available on this machine", html`<div class="muted small">${d.problem}</div>`))); return; }
    setHTML(root, html`${d.demo ? html`<div class="banner info"><svg class="icon"><use href="#i-help"/></svg><span>Demo: nothing is installed or started; this shows what the page does.</span></div>` : ""}
      <div class="grid g-2">${hostingCard(d)}${addressCard(d)}</div>${labAccessCard(d)}${envCard(d)}${safetyCard()}`);
    if (d.job) refreshJobLog();
  }
  async function refreshJobLog() {
    try { const r = await getJSON("/api/pwnforge/logs"); if (r.ok) { PF.jobLog = r.job; const el = $("#pf-joblog"); if (el) { el.textContent = r.job || "(starting…)"; el.scrollTop = el.scrollHeight; } } } catch (e) { /* next time */ }
  }
  function showDialog(title, body) {
    let dlg = document.getElementById("pf-dialog");
    if (!dlg) { dlg = document.createElement("div"); dlg.id = "pf-dialog"; document.body.appendChild(dlg); }
    setHTML(dlg, html`<div class="tu-back" data-pf-close></div><div class="tu-box" role="dialog" aria-modal="true" aria-label="${title}" style="width:min(900px,96vw)"><h3 style="margin-top:0">${title}</h3>${body}<div class="row mt"><button class="btn" data-pf-close>Close</button></div></div>`);
    dlg.hidden = false;
  }
  async function confirmDlg(title, body, okLabel, danger) {
    return new Promise((resolve) => {
      showDialog(title, html`${body}<div class="row mt" style="gap:6px"><button class="btn ${danger ? "danger" : "primary"}" data-pf-ok>${okLabel}</button></div>`);
      const dlg = document.getElementById("pf-dialog");
      dlg.onclick = (e) => { if (e.target.closest("[data-pf-ok]")) { dlg.hidden = true; dlg.onclick = null; resolve(true); } else if (e.target.closest("[data-pf-close]")) { dlg.hidden = true; dlg.onclick = null; resolve(false); } };
    });
  }

  document.addEventListener("click", async (e) => {
    const t = e.target; if (!t.closest) return;
    if (t.closest("#pf-dialog [data-pf-close]") && !document.getElementById("pf-dialog").onclick) { document.getElementById("pf-dialog").hidden = true; return; }
    if (!t.closest("#pf-root")) return;
    let b;
    if ((b = t.closest("[data-pf]"))) {
      const a = b.dataset.pf;
      if (a === "logs") {
        try { const r = await getJSON("/api/pwnforge/logs"); if (!r.ok) return toast(r.error || "No log.");
          showDialog("PwnForge log", html`${r.job ? html`<h4>Last install / task</h4><pre class="hl-pre small" style="max-height:30vh;overflow:auto">${r.job}</pre>` : ""}<h4>Service</h4><pre class="hl-pre small" style="max-height:50vh;overflow:auto">${r.service || "(empty)"}</pre>`);
        } catch (err) { toast("Couldn't reach the dashboard."); }
      } else if (a === "doctor") {
        const r = await post("/api/pwnforge/action", { action: "doctor" }, "doctor");
        if (r) showDialog("PwnForge diagnostics", html`${chip(r.exit === 0 ? "OK" : "problems found", r.exit === 0 ? "good" : "warn")}<pre class="hl-pre small" style="max-height:60vh;overflow:auto">${r.output}</pre>`);
      } else if (a === "lab-access-status") {
        const cidr = (($('#pf-lab-cidr') || {}).value || "10.200.0.0/16").trim();
        const r = await post("/api/pwnforge/action", { action: a, cidr }, a);
        if (r) showDialog("PwnForge lab routing", html`${chip(r.exit === 0 ? "status checked" : "check failed", r.exit === 0 ? "good" : "warn")}<pre class="hl-pre small" style="max-height:60vh;overflow:auto">${r.output}</pre><p class="muted small">The advertised Tailscale route also needs approval in the Tailscale admin console before other devices can use it.</p>`);
      } else if (a === "lab-access-on" || a === "lab-access-off") {
        const cidr = (($('#pf-lab-cidr') || {}).value || "10.200.0.0/16").trim();
        const enabling = a === "lab-access-on";
        const title = enabling ? "Enable direct lab access?" : "Disable direct lab access?";
        const body = enabling
          ? html`<p style="margin-top:0">Makes running lab machines in <span class="mono">${cidr}</span> reachable by IP from devices on this LAN and your Tailscale network. Other tailnet devices can reach these lab targets too. Tailscale route approval is required after setup.</p><p class="muted small">Lab machines remain blocked from starting connections to the internet or other lab networks.</p>`
          : html`<p style="margin-top:0">Stops routing <span class="mono">${cidr}</span> through this host, withdraws its Tailscale route, and restores the previous IPv4 forwarding setting.</p>`;
        if (!(await confirmDlg(title, body, enabling ? "Enable access" : "Disable access", !enabling))) return;
        PF.jobLog = "";
        await post("/api/pwnforge/action", { action: a, cidr }, a);
      } else if (a === "install") {
        const d = PF.data, upd = d.installed;
        if (!(await confirmDlg(upd ? "Update PwnForge?" : "Install PwnForge?", html`<p style="margin-top:0">${upd ? "Gets the newest PwnForge from" : "Downloads PwnForge from"} <span class="mono">${d.repo}</span> into <span class="mono">${d.config.path}</span>, installs its Python packages and builds its website, then installs its service (user <span class="mono">pwnforge</span>, data in <span class="mono">${d.config.data_dir}</span>).</p>
          <p class="muted small">If git, python3-venv, nodejs or npm are missing it installs them with apt. On an x86_64 or arm64 Linux host it then sets up real machines automatically — QEMU, libvirt and the cloud images for this CPU (about 1 GB, downloaded once) — so labs boot real VMs, not simulations. An update keeps your users, progress and machines, and doesn't re-download images. It takes a few minutes.</p>`, upd ? "Update" : "Install"))) return;
        PF.jobLog = ""; await post("/api/pwnforge/action", { action: "install" }, "install");
      } else if (a === "virt") {
        if (!(await confirmDlg("Set up real machines?", html`<p style="margin-top:0">Installs QEMU, libvirt and the firmware virtual machines need (with apt), lets PwnForge's user use libvirt, and downloads the Debian, Alpine and Ubuntu cloud images for this CPU (about 1 GB). If this host has no KVM device, guests still run as real VMs using slower QEMU software emulation.</p>
          <ul class="small"><li>Lab machines then boot for real, each on its own isolated virtual network; you log in with the username and password the lab page shows.</li>
          <li>The first start of each kind of machine prepares its image once (installs its packages, which needs the internet), so it takes a few minutes. Later starts take seconds.</li>
          <li>Windows labs can't run on an ARM machine such as a Raspberry Pi.</li></ul>`, "Set up"))) return;
        PF.jobLog = ""; await post("/api/pwnforge/action", { action: "virt" }, a);
      } else if (a === "hosting-off") {
        if (!(await confirmDlg("Turn PwnForge hosting off?", html`<p style="margin-top:0">Stops PwnForge and stops it starting at boot. Running practice machines are managed by libvirt and keep running; stop them first below if you want them off too. Nothing is deleted.</p>`, "Turn off"))) return;
        await post("/api/pwnforge/action", { action: "hosting-off" }, a);
      } else if (a === "save-config") {
        const bind = (document.querySelector('input[name="pf-bind"]:checked') || {}).value, port = +$("#pf-port").value;
        const body = { bind, port };
        if (bind === "lan" && PF.data.config.bind !== "lan") {
          if (!(await confirmDlg("Open PwnForge to every network?", html`<p style="margin-top:0">Anyone on the networks this machine is on (your LAN, and your tailnet) could open PwnForge's sign-in page on port ${port}.</p><ul class="small"><li>Make sure its administrator account exists and has a strong password.</li><li>Don't use this on a machine with a public address.</li><li>The practice machines stay on their isolated networks either way.</li></ul>`, "Open it", true))) return;
          body.confirm_lan = true;
        }
        const r = await post("/api/pwnforge/config", body, a);
        if (r) toast(r.applied ? "PwnForge now listens on the new address." : "Saved. It applies when PwnForge is installed.");
      } else await post("/api/pwnforge/action", { action: a }, a);
      return;
    }
    if ((b = t.closest("[data-pf-env]"))) {
      const a = b.dataset.pfEnv;
      if (a === "cleanup" || a === "cleanup-apply") {
        if (a === "cleanup-apply" && !(await confirmDlg("Delete the unused disks?", html`<p style="margin-top:0">Deletes the listed machine disks that no machine uses. Base images and machines in use are kept.</p>`, "Delete", true))) return;
        const r = await post("/api/pwnforge/env", { action: "cleanup", apply: a === "cleanup-apply" }, a);
        if (r) { PF.cleanup = r.result; paint(); }
        return;
      }
      const body = { action: a, kind: b.dataset.kind, id: b.dataset.id };
      if (a === "delete") {
        if (!(await confirmDlg("Delete " + (b.dataset.name || "it") + "?", html`<p style="margin-top:0">Stops it and deletes its disk${b.dataset.kind === "lab" ? "s and its networks" : ""}. Course progress isn't touched, and base images are kept.</p>`, "Delete", true))) return;
        body.confirm = true;
      }
      const r = await post("/api/pwnforge/env", body, a);
      if (r && a === "start") toast("Starting it; this can take a minute.");
    }
  });
  document.addEventListener("change", (e) => { if (e.target.name === "pf-bind") document.querySelectorAll("#pf-root .perm-opt").forEach((el) => el.classList.toggle("on", el.querySelector("input").checked)); });

  V.pwnforge = {
    build() { NY.freshHTML($("#view"), html`<div id="pf-root"></div>`); paint(); load(); },
    update() { },
  };
})();
