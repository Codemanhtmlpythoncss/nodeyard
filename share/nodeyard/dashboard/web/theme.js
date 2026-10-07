// Apply the theme before the page paints (no flash). "auto" follows the system.
// ?theme=dark or ?theme=light overrides it for this visit only.
(function () {
  try {
    var q = /[?&]theme=([a-z-]+)\b/.exec(location.search);
    var t = q ? q[1] : localStorage.getItem("nodeyard.theme");
    if (t && t !== "auto" && /^[a-z-]{2,24}$/.test(t)) document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: stay on automatic */ }
})();
