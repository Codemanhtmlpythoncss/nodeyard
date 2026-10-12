/* nodeyard: the controls panel for the galaxy screens. Tab opens and closes it (not while typing), Esc or a click
   outside closes it. It drives the screens' own controls (their keys still work too): the modes (camera, layout,
   panels, display), which screen, full screen, how it looks, and the way back to nodeyard. Only what this page has
   is shown, and the camera, layout and panel choices are remembered by the screens themselves. */
(function () {
  "use strict";
  var page = /\/nexus/.test(location.pathname) ? "nexus" : /\/desk/.test(location.pathname) ? "desk" : "wall";
  var MODES = [["wall", "Wall"], ["showcase", "Showcase"], ["ops", "Ops"], ["night", "Night"], ["alert", "Alert"]];
  var panel = null, mode = "";
  var windowTransparency = (function () {
    try { var saved = localStorage.getItem("galaxy.windowTransparency"); return saved == null ? 45 : Math.max(0, Math.min(85, Number(saved) || 0)); }
    catch (e) { return 45; }
  })();
  var allWindowsTransparent = (function () { try { return localStorage.getItem("galaxy.allWindowsTransparent") === "1"; } catch (e) { return false; } })();

  function applyWindowAppearance() {
    document.documentElement.style.setProperty("--ny-window-opacity", String(1 - windowTransparency / 100));
    if (document.body) document.body.classList.toggle("ny-all-windows-transparent", allWindowsTransparent);
  }

  function css() {
    var s = document.createElement("style");
    s.textContent =
      "#ny-ctl{position:fixed;left:18px;top:50%;transform:translateY(-50%);z-index:10000;width:min(400px,calc(100vw - 36px));max-height:calc(100vh - 40px);overflow:auto;" +
      "padding:14px 16px;border-radius:12px;background:rgba(5,9,22,.9);border:1px solid rgba(110,170,255,.28);box-shadow:0 18px 60px rgba(0,0,0,.55);" +
      "backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#cfe0ff}" +
      "#ny-ctl h2{margin:0 0 4px;font-size:12px;letter-spacing:.25em;color:#fff;display:flex;justify-content:space-between;align-items:baseline}" +
      "#ny-ctl h2 span{font-size:10px;letter-spacing:.08em;color:#6f86b3}" +
      "#ny-ctl .k{font-size:10px;letter-spacing:.16em;color:#7f9cd0;margin:12px 0 6px;text-transform:uppercase}" +
      "#ny-ctl .row{display:grid;grid-template-columns:64px 1fr;gap:6px 8px;align-items:center;margin:5px 0}" +
      "#ny-ctl .row>span{color:#9db3dc;font-size:11px}" +
      "#ny-ctl .r{display:flex;flex-wrap:wrap;gap:6px}" +
      "#ny-ctl .seg{display:inline-flex;flex-wrap:wrap;border:1px solid rgba(120,170,255,.25);border-radius:8px;overflow:hidden}" +
      "#ny-ctl .seg button{border:0;border-radius:0;border-right:1px solid rgba(120,170,255,.18)}" +
      "#ny-ctl .seg button:last-child{border-right:0}" +
      "#ny-ctl button,#ny-ctl a.b{font:inherit;font-size:11.5px;color:#dbe8ff;background:rgba(120,170,255,.08);border:1px solid rgba(120,170,255,.25);border-radius:7px;" +
      "padding:5px 9px;cursor:pointer;text-decoration:none;display:inline-flex;gap:6px;align-items:center}" +
      "#ny-ctl button:hover,#ny-ctl a.b:hover{background:rgba(120,170,255,.18)}" +
      "#ny-ctl .on{background:rgba(90,215,255,.22)!important;color:#fff;box-shadow:inset 0 0 0 1px #5ad7ff}" +
      "#ny-ctl kbd{font:inherit;font-size:10px;color:#8fa7d2;border:1px solid rgba(143,167,210,.35);border-radius:4px;padding:0 4px}" +
      "#ny-ctl .keys{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;color:#9db3dc;font-size:11px}" +
      "#ny-ctl .window-opacity{display:flex;align-items:center;gap:8px}#ny-ctl .window-opacity input{width:100%;accent-color:#5ad7ff}#ny-ctl .window-opacity b{width:36px;text-align:right;color:#dbe8ff;font-size:10px}" +
      "#ny-ctl .checkrow{display:flex;align-items:center;gap:8px;color:#cfe0ff;font-size:11px;margin:8px 0 0;cursor:pointer}#ny-ctl .checkrow input{accent-color:#5ad7ff}" +
      "body.ny-all-windows-transparent .panel,body.ny-all-windows-transparent .card{background:rgba(5,10,22,var(--ny-window-opacity,.55))!important}" +
      "#ny-ctl .note{color:#6f86b3;font-size:10.5px;margin-top:4px}" +
      "#ny-hint{position:fixed;left:96px;bottom:8px;z-index:9999;font:11px ui-monospace,Menlo,monospace;color:#7fa3c8;opacity:.5}";
    document.head.appendChild(s);
    var hint = document.createElement("div"); hint.id = "ny-hint"; hint.textContent = "Tab: controls"; document.body.appendChild(hint);
  }
  var esc = function (t) { return String(t).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); };
  function press(key) { document.dispatchEvent(new KeyboardEvent("keydown", { key: key, bubbles: true })); setTimeout(paint, 60); }
  function btn(act, label, on, key) { return '<button data-ny="' + act + '"' + (on ? ' class="on"' : "") + ">" + esc(label) + (key ? " <kbd>" + key + "</kbd>" : "") + "</button>"; }
  function seg(items) { return '<div class="seg">' + items.join("") + "</div>"; }
  function row(label, html) { return '<div class="row"><span>' + esc(label) + "</span><div>" + html + "</div></div>"; }
  function cinematics() { var q = /[?&]cinematics=(on|off)/.exec(location.search); if (q) return q[1] === "on"; var c = window.CONFIG || {}; return !!((c.features || {}).cinematics); }

  function paint() {
    if (!panel) return;
    var C = window.CINEMA, H = window.HERO && window.HERO.state, N = window.NEXUS, NS = window.NETSCENE, full = !!document.fullscreenElement, h = "";
    h += '<h2>CONTROLS <span>Tab or Esc closes</span></h2>';

    /* the modes: what the camera does, what is on screen, and the display mode */
    var modeAt = h.length;
    if (NS && NS.setTour) {
      var tm = NS.tourMode || "auto";
      h += row("Camera", seg([btn("tour:auto", "Auto tour", tm === "auto"), btn("tour:overview", "Overview", tm === "overview"), btn("tour:manual", "Manual", tm === "manual")]));
    }
    if (N && N.pinFree) {
      var own = !!(N.D && N.D.pinFree);
      h += row("Camera", seg([btn("nx:follow", "Follow the wall", !own), btn("nx:own", "Own tour", own)]));
    }
    if (H) h += row("Layout", seg([btn("layout:full", "All panels", H.mode !== "hero"), btn("layout:hero", "Scene first", H.mode === "hero")]));
    if (C) h += row("Panels", seg([btn("hud:1", "Shown", C.hud), btn("hud:0", "Hidden", !C.hud)]));
    if (page !== "desk") h += row("Display", seg(MODES.map(function (m, i) { return btn("mode:" + m[0], m[1], mode === m[0], String(i + 1)); })));
    if (N && N.SC) h += row("Showcase", seg([btn("sc:auto", "Auto", N.SC.manual === null), btn("sc:on", "Fly through", N.SC.manual === true), btn("sc:off", "Off", N.SC.manual === false)]));
    if (h.length > modeAt) h = h.slice(0, modeAt) + '<div class="k">Mode</div>' + h.slice(modeAt);

    h += '<div class="k">Screen</div><div class="r">' +
      ['<a class="b' + (page === "wall" ? " on" : "") + '" href="/g3/">Wall</a>', '<a class="b' + (page === "nexus" ? " on" : "") + '" href="/g3/nexus">Nexus</a>',
       '<a class="b' + (page === "desk" ? " on" : "") + '" href="/g3/desk">Desk</a>'].join("") +
      btn("full", full ? "Exit full screen" : "Full screen", full) + '<a class="b" href="/">&larr; nodeyard</a></div>';

    var look = [];
    if (C) look.push(btn("key:c", "Cinema bars", C.cinema, "C"), btn("key:g", "Colour grade", C.on, "G"));
    if (H) look.push(btn("key:p", "Privacy", !!H.privacy, "P"));
    if (C) look.push(btn("tour-x", "Extra camera moves: " + (cinematics() ? "on" : "off"), cinematics()), btn("key:m", "Play one", false, "M"));
    if (look.length) h += '<div class="k">Look</div><div class="r">' + look.join("") + "</div>";

    h += '<div class="k">Window appearance</div>' +
      '<div class="row"><span>Transparency</span><div class="window-opacity"><input type="range" min="0" max="85" step="5" value="' + windowTransparency + '" data-ny-window-alpha aria-label="Window transparency"><b data-ny-window-value>' + windowTransparency + '%</b></div></div>' +
      '<label class="checkrow"><input type="checkbox" data-ny-all-transparent' + (allWindowsTransparent ? " checked" : "") + '> <span>All windows transparent</span></label>' +
      '<div class="note">The slider is applied when all windows are transparent.</div>';

    h += '<div class="k">Keys</div><div class="keys">' +
      "<kbd>Tab</kbd><span>this panel</span>" + (page !== "desk" ? "<kbd>1</kbd><span>to <kbd>5</kbd> display mode</span>" : "") +
      (C ? "<kbd>H</kbd><span>panels on/off</span><kbd>C</kbd><span>cinema bars</span><kbd>G</kbd><span>colour grading</span><kbd>M</kbd><span>an extra camera move</span>" : "") +
      (H ? "<kbd>F</kbd><span>layout</span><kbd>P</kbd><span>privacy (hide names)</span>" : "") +
      (N ? "<kbd>Space</kbd><span>free roam</span><kbd>P</kbd><span>showcase on/off</span>" : "") + "</div>";
    if (NS) h += '<div class="note">Dragging or scrolling the scene takes the camera for 25 s, then the chosen camera mode carries on.</div>';
    panel.innerHTML = h;
  }
  function loadMode() {
    fetch("/g3/api/mode", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (m) { mode = m.mode || ""; paint(); }).catch(function () {});
  }
  function open() {
    if (panel) return;
    panel = document.createElement("div"); panel.id = "ny-ctl"; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "Galaxy controls");
    /* a click in here is not the camera being grabbed: keep it from the scene's pointer and wheel listeners */
    ["pointerdown", "wheel", "mousedown"].forEach(function (ev) { panel.addEventListener(ev, function (e) { e.stopPropagation(); }); });
    document.body.appendChild(panel); paint(); loadMode();
  }
  function close() { if (panel) { panel.remove(); panel = null; } }

  function act(a) {
    var C = window.CINEMA, H = window.HERO, N = window.NEXUS, NS = window.NETSCENE;
    if (a === "full") {
      if (document.fullscreenElement) document.exitFullscreen(); else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(function () {});
      setTimeout(paint, 150);
    } else if (a.indexOf("mode:") === 0) {
      mode = a.slice(5); paint();
      fetch("/g3/api/mode", { method: "POST", headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify({ mode: mode }) }).then(loadMode).catch(function () {});
    } else if (a.indexOf("tour:") === 0) { if (NS && NS.setTour) NS.setTour(a.slice(5)); paint(); }
    else if (a === "nx:follow" || a === "nx:own") { if (N && N.pinFree) N.pinFree(a === "nx:own"); paint(); }
    else if (a.indexOf("layout:") === 0) { if (H && H.set) H.set(a.slice(7), true); paint(); }
    else if (a.indexOf("hud:") === 0) { if (C && C.hud !== (a === "hud:1")) press("h"); }
    else if (a.indexOf("sc:") === 0) { if (N && N.SC) N.SC.manual = a === "sc:auto" ? null : a === "sc:on"; paint(); }
    else if (a === "tour-x") { var u = new URL(location.href); u.searchParams.set("cinematics", cinematics() ? "off" : "on"); location.href = u.toString(); }
    else if (a.indexOf("key:") === 0) press(a.slice(4));
  }

  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); if (panel) close(); else open(); }
    else if (e.key === "Escape" && panel) { e.preventDefault(); close(); }
    else if (panel) setTimeout(/^[1-5]$/.test(e.key) ? loadMode : paint, 150);
  }, true);
  document.addEventListener("click", function (e) {
    if (!panel) return;
    var b = e.target.closest && e.target.closest("[data-ny]");
    if (b) act(b.getAttribute("data-ny"));
    else if (!panel.contains(e.target)) close();
  }, true);
  document.addEventListener("input", function (e) {
    if (!e.target.matches("[data-ny-window-alpha]")) return;
    windowTransparency = Math.max(0, Math.min(85, Number(e.target.value) || 0));
    try { localStorage.setItem("galaxy.windowTransparency", String(windowTransparency)); } catch (er) {}
    applyWindowAppearance();
    var value = document.querySelector("[data-ny-window-value]"); if (value) value.textContent = windowTransparency + "%";
  });
  document.addEventListener("change", function (e) {
    if (!e.target.matches("[data-ny-all-transparent]")) return;
    allWindowsTransparent = e.target.checked;
    try { localStorage.setItem("galaxy.allWindowsTransparent", allWindowsTransparent ? "1" : "0"); } catch (er) {}
    applyWindowAppearance();
  });
  document.addEventListener("fullscreenchange", paint);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { css(); applyWindowAppearance(); });
  else { css(); applyWindowAppearance(); }
})();
