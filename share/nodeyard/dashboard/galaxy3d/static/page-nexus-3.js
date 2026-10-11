
/* Identity first: nexus.js reads window.CONFIG synchronously at load, so the
   scene modules are only appended once GET /api/config has answered. */
(function () {
  function esc(x){return String(x==null?'':x).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  window.loadConfig().then(function (C) {
    document.title = C.brand + ' · nexus';
    var b = document.getElementById('brand');
    if (b) b.textContent = C.brand + ' · ' + (C.domain || 'homelab') + ' from orbit';
    /* the legend is the configured category palette, plus the stopped colour */
    var lg = document.getElementById('legend');
    if (lg) {
      lg.innerHTML = C.categories.map(function (k) {
        var h = C.palette[k] || '#8fb0d0';
        return '<span><i style="color:' + esc(h) + ';background:' + esc(h) + '"></i>' + esc(k) + '</span>';
      }).join('') +
      '<span><i style="color:' + esc(C.palette.stopped) + ';background:' + esc(C.palette.stopped) + '"></i>stopped</span>';
    }
    document.body.classList.toggle('no-cameras', !C.features.cameras);
    document.body.classList.toggle('no-audience', !C.features.audience);
    window.loadScripts([
      '/g3/static/nexus.js?v=1',
      '/g3/static/lightsync.js?v=1',
      '/g3/static/showtime.js?v=1'
    ]);
  });
})();
