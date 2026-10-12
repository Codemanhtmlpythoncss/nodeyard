// Apply the theme before the page paints (no flash). "auto" follows the system.
// ?theme=dark or ?theme=light overrides it for this visit only.
(function () {
  var root = document.documentElement;
  var transparency = 45;
  var allTransparent = false;
  function readAppearance() {
    try {
      var saved = localStorage.getItem("nodeyard.windowTransparency");
      if (saved == null) saved = localStorage.getItem("galaxy.windowTransparency");
      transparency = saved == null || saved === "" ? 45 : Math.max(0, Math.min(85, Number(saved) || 0));
      var enabled = localStorage.getItem("nodeyard.allWindowsTransparent");
      if (enabled == null) enabled = localStorage.getItem("galaxy.allWindowsTransparent");
      allTransparent = enabled === "1";
    } catch (e) { transparency = 45; allTransparent = false; }
  }
  function applyAppearance() {
    root.style.setProperty("--ny-window-alpha", (100 - transparency) + "%");
    root.classList.toggle("ny-all-windows-transparent", allTransparent);
  }
  readAppearance();
  applyAppearance();
  window.NodeyardWindowAppearance = {
    read: function () { readAppearance(); return { transparency: transparency, allTransparent: allTransparent }; },
    setTransparency: function (value) {
      transparency = Math.max(0, Math.min(85, Number(value) || 0));
      try { localStorage.setItem("nodeyard.windowTransparency", String(transparency)); } catch (e) {}
      applyAppearance();
    },
    setAllTransparent: function (enabled) {
      allTransparent = !!enabled;
      try { localStorage.setItem("nodeyard.allWindowsTransparent", allTransparent ? "1" : "0"); } catch (e) {}
      applyAppearance();
    },
    refresh: function () { readAppearance(); applyAppearance(); }
  };
  window.addEventListener("storage", function (event) {
    if (["nodeyard.windowTransparency", "nodeyard.allWindowsTransparent", "galaxy.windowTransparency", "galaxy.allWindowsTransparent"].includes(event.key)) {
      readAppearance(); applyAppearance();
    }
  });
  try {
    var q = /[?&]theme=([a-z-]+)\b/.exec(location.search);
    var t = q ? q[1] : localStorage.getItem("nodeyard.theme");
    if (t && t !== "auto" && /^[a-z-]{2,24}$/.test(t)) document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: stay on automatic */ }
  // ?embed=1: just the page itself, without the sidebar and header (the Mac app's terminal windows)
  if (/[?&]embed=1\b/.test(location.search)) document.documentElement.classList.add("embed");
})();
