
/* ---- live clock, WAN/ISP/CAM chips, alert-state styling ---- */
(function(){
  function tick(){
    var d=new Date(), p=function(n){return String(n).padStart(2,'0');};
    var e=document.getElementById('clk');
    if(e) e.textContent=p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
  }
  tick(); setInterval(tick,1000);

  async function chips(){
    try{
      var d = await (await fetch('/g3/api/all')).json();
      var C = cfg();
      var u = d.unifi||{};
      var w = document.getElementById('chip-wan');
      if(w){ var ok = u.wan_up==='ok';
        w.className='chip '+(ok?'good':'bad');
        w.textContent='WAN '+(ok?'UP':'DOWN')+(u.latency!=null?' · '+u.latency+'ms':''); }
      var i = document.getElementById('chip-isp');
      if(i) i.textContent=(u.isp||C.wan_label||'ISP')+(u.wan_ip?' · '+u.wan_ip:'');
      var c = document.getElementById('chip-cam');
      if(c){ var n=C.features.cameras?(d.cameras||[]).length:0;
        c.className='chip '+(n?'good':'bad');
        c.textContent=n+' CAMS LIVE'; }
      /* the edge: Cloudflare tunnel + CrowdSec, from d.edge. Blocks
         that are not up leave the chip empty rather than guessing. */
      var e=d.edge||{}, cf=e.cloudflare||{}, cs=e.crowdsec||{};
      var fc=document.getElementById('chip-cf');
      if(fc){ if(cf.up){ /* PoPs in one metro collapse to 'SEA ×3' so the chip fits its panel */
                var locs=(cf.locations||[]).map(function(l){return String(l).toUpperCase();}), metro=locs.length>1&&locs.every(function(l){return l.slice(0,3)===locs[0].slice(0,3);})?locs[0].slice(0,3)+' ×'+locs.length:locs.join(' ');
                fc.textContent='CF EDGE · '+(cf.ha_connections!=null?cf.ha_connections+' conns':'?')+' · '+Math.round(cf.req_per_min||0)+' req/min · '+metro;
                fc.className='chip cf'+((cf.err_per_min||0)>0?' warn':''); }
              else { fc.textContent=''; fc.className='chip cf'; } }
      var sc=document.getElementById('chip-cs');
      if(sc){ if(cs.up){ var la=null;(cs.top_attackers||[]).forEach(function(a){if(a&&a.ts&&(!la||a.ts>la.ts))la=a;});
                var ag=la?Math.max(0,Date.now()/1000-la.ts):0, agS=ag<3600?Math.round(ag/60)+'m':ag<86400?Math.round(ag/3600)+'h':Math.round(ag/86400)+'d';
                sc.textContent='CROWDSEC · '+(cs.alerts_24h||0)+' atk/24h · '+(cs.banned||0)+' banned'+(la?' · '+agS+' ago · '+(la.cn||'??')+' · '+String(la.scenario||'').replace(/^crowdsecurity\//,'').replace(/-attempts$|-probing$/,''):'');
                sc.className='chip'+((cs.alerts_1h||0)>0?' warn':''); }
              else { sc.textContent=''; sc.className='chip'; } }
      /* alert panel goes "hot" when Zabbix has problems */
      var ap=document.getElementById('alerts');
      if(ap){ var pr=(d.zabbix||{}).problems||0; ap.classList.toggle('hot', pr>0); }
      /* colour the headline ONLINE/alert counters */
      var sn=document.getElementById('s-net');
      if(sn&&d.netmap){ var frac=d.netmap.up/Math.max(d.netmap.total,1);
        sn.className='n '+(frac>0.9?'ok':frac>0.7?'warn':'bad'); }
    }catch(e){}
  }
  chips(); setInterval(chips,6000);
})();
