
/* alert flash */
// The alerts panel used to sit open permanently and eat rail space. Now it slides in
// for a few seconds whenever the alert set CHANGES (or every ~90s while any alert is
// live), then fades out again - noticeable without being a permanent obstruction.
(function(){
  var el=document.getElementById('alerts'); if(!el) return;
  var lastSig='', lastShow=0, hideAt=0;
  function show(ms){ el.style.display='block'; el.style.opacity='1';
    el.style.transform='translateX(0)'; document.body.classList.add('has-alerts');
    hideAt=Date.now()+ms; lastShow=Date.now(); }
  function hide(){ el.style.opacity='0'; el.style.transform='translateX(-14px)';
    document.body.classList.remove('has-alerts');
    setTimeout(function(){ if(parseFloat(el.style.opacity||'0')===0) el.style.display='none'; }, 650); }
  hide();
  setInterval(function(){
    var d=window.ALERTS||{}; var keys=Object.keys(d);
    var sig=keys.sort().map(function(k){return k+':'+(d[k]&&d[k].n);}).join('|');
    var now=Date.now();
    if(keys.length===0){ lastSig=''; if(hideAt) { hideAt=0; hide(); } return; }
    if(sig!==lastSig){ lastSig=sig; show(9000); return; }        // changed -> show 9s
    if(now-lastShow>90000){ show(6000); return; }                 // otherwise re-flash every 90s
    if(hideAt && now>hideAt){ hideAt=0; hide(); }
  }, 1000);
})();
