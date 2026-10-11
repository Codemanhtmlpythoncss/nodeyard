
function nbBps(b){b=+b||0;
  if(b<1e3)return b.toFixed(0)+' b/s';
  if(b<1e6)return (b/1e3).toFixed(1)+' kb/s';
  if(b<1e9)return (b/1e6).toFixed(2)+' Mb/s';
  return (b/1e9).toFixed(2)+' Gb/s';}
function renderFlows(topo){
  var el=document.getElementById('flowlist'); if(!el||!topo)return;
  var hd=document.getElementById('flowhd');
  var meta=topo.flow_meta||{}, fl=(topo.flows||[]).slice().sort(function(a,b){return (b.bps||0)-(a.bps||0);});
  if(hd) hd.textContent = meta.up ? ((meta.flow_count||fl.length)+' ACTIVE') : 'SOURCE DOWN';
  if(!meta.up){ el.innerHTML='<div class="fnone">ntopng unreachable — no flow data</div>'; return; }
  if(!fl.length){ el.innerHTML='<div class="fnone">no measurable conversations</div>'; return; }
  var max=0; for(var i=0;i<fl.length;i++) max=Math.max(max, fl[i].bps||0);
  if(max<=0) max=1;
  el.innerHTML = fl.slice(0,6).map(function(f){
    var a=f.src_label||f.src_ip||'?', b=f.dst_label||f.dst_ip||'?';
    var w=Math.max(3,Math.round(100*(f.bps||0)/max));
    return '<div class="fl'+(f.internal?'':' ext')+'">'+
      '<div class="fp"><span class="fa">'+nbEsc(a)+'</span><span class="fx">&#8594;</span>'+
        '<span class="fb">'+nbEsc(b)+'</span></div>'+
      '<div class="fr">'+nbBps(f.bps)+'</div>'+
      '<div class="fbar"><i style="width:'+w+'%"></i></div>'+
      '<div class="fpr">'+nbEsc(f.proto||'')+'</div></div>';
  }).join('');
}

/* exporter fleet (ipmi / smartctl / pbs / gatus / crowdsec / akvorado) via DATA.telemetry.
   Per-node maps (smart_hot, smart_worn, arc_hit ...) are keyed by node id; the
   tiles show the worst value across nodes. */
function opsTelemetry(t){
  if(!t||!t.up)return '';
  const n=(v,d=0)=>(v==null||isNaN(v))?'–':Number(v).toFixed(d);
  const mx=o=>{const v=Object.values(o||{}).map(Number).filter(x=>!isNaN(x));return v.length?Math.max(...v):null;};
  const hotV=mx(t.smart_hot),wornV=mx(t.smart_worn),arcV=mx(t.arc_hit);
  const nDrives=Object.values(t.smart_n||{}).reduce((a,b)=>a+b,0);
  const slo=t.slo_total?`${t.slo_ok}/${t.slo_total}`:'–',sloBad=t.slo_total&&t.slo_ok<t.slo_total;
  const cs=t.cs_alerts_h||0,flows=t.flows_fps||0;
  const domain=cfg().domain||'WAN';
  return `<div class="op"><span>Iron · IPMI</span><b>${n(t.watts)} W</b><small>fans ${n(t.fan_min)}–${n(t.fan_max)} rpm · inlet ${n(t.inlet_c)}°C</small><div class="meter"><i style="width:${Math.min(100,(t.watts||0)/6)}%"></i></div></div>`+
    `<div class="op"><span>Drives</span><b style="color:${t.smart_bad?'#ff5a6a':'#37f5a0'}">${nDrives||'–'} · ${t.smart_bad?t.smart_bad+' FAILING':'SMART ok'}</b><small>hottest ${n(hotV)}°C · worn ${n(wornV)}% · ARC ${n(arcV)}%</small><div class="meter"><i style="width:${Math.min(100,wornV||0)}%"></i></div></div>`+
    `<div class="op"><span>Backups · PBS</span><b style="color:${t.pbs_stale?'#ffb347':(t.pbs_up?'#37f5a0':'#ff5a6a')}">${t.pbs_up?n(t.pbs_vms)+' guests':'PBS DOWN'}</b><small>oldest ${n(t.pbs_oldest_h)}h · ${n(t.pbs_stale)} stale · ${n(t.pbs_used_pct)}% full</small><div class="meter"><i style="width:${Math.min(100,t.pbs_used_pct||0)}%"></i></div></div>`+
    `<div class="op"><span>Edge · ${nbEsc(domain)}</span><b style="color:${sloBad?'#ff5a6a':'#37f5a0'}">${slo} up · ${n(t.slo_24h,1)}% 24h</b><small>${n(cs)} attacks/h · ${n(t.cs_banned)} banned · ${n(flows,1)} flows/s</small><div class="meter"><i style="width:${Math.min(100,cs*5)}%"></i></div></div>`;
}
