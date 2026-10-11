
"use strict";
/* ================= state ================= */
var S = { all:null, gpus:null, lastOk:0, hist:{ net:null, gpu:{} } };
var $ = function(id){ return document.getElementById(id); };

function esc(s){
  return String(s==null?"":s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function num(v,d){ v = Number(v); return isFinite(v) ? v.toFixed(d==null?0:d) : "?"; }
function gb(bytes){ return (bytes/1.074e9).toFixed(1); }
function pct(a,b){ return Math.round(a/Math.max(b,1)*100); }
function ph(msg){ return '<div class="ph">'+esc(msg||"no data")+'</div>'; }
function upfmt(sec){
  sec = Number(sec)||0;
  var d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600);
  if(d>0) return d+"d "+h+"h";
  var m = Math.floor(sec%3600/60);
  return h>0 ? h+"h "+m+"m" : m+"m";
}
function agefmt(sec){
  sec = Number(sec)||0;
  if(sec<90) return sec+"s";
  if(sec<5400) return Math.round(sec/60)+"m";
  if(sec<172800) return Math.round(sec/3600)+"h";
  return Math.round(sec/86400)+"d";
}
function barCls(p){ return p>=92 ? "b" : p>=80 ? "w" : "g"; }
function bar(p,cls){
  p = Math.max(0,Math.min(100,Number(p)||0));
  return '<div class="bar"><i class="'+(cls||barCls(p))+'" style="width:'+p+'%"></i></div>';
}

/* ---- inline SVG sparkline, no libraries ---- */
function spark(vals,w,h,color){
  if(!vals || vals.length<2) return "";
  var mn = Infinity, mx = -Infinity, i;
  for(i=0;i<vals.length;i++){ var v=Number(vals[i])||0; if(v<mn)mn=v; if(v>mx)mx=v; }
  var rg = (mx-mn)||1, pts = [];
  for(i=0;i<vals.length;i++){
    var x = (i/(vals.length-1)*w).toFixed(1);
    var y = (h-1.5-((Number(vals[i])||0)-mn)/rg*(h-3)).toFixed(1);
    pts.push(x+","+y);
  }
  return '<svg class="spk" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none">'
       + '<polyline points="'+pts.join(" ")+'" fill="none" stroke="'+color+'" stroke-width="1.3" stroke-linejoin="round"/></svg>';
}
function spark2(seriesA,seriesB,w,h,ca,cb){  /* two overlaid lines, shared scale */
  var all = (seriesA||[]).concat(seriesB||[]);
  if(all.length<2) return "";
  var mn=Infinity,mx=-Infinity,i;
  for(i=0;i<all.length;i++){ var v=Number(all[i])||0; if(v<mn)mn=v; if(v>mx)mx=v; }
  var rg=(mx-mn)||1;
  function line(vals,color){
    if(!vals||vals.length<2) return "";
    var pts=[];
    for(var j=0;j<vals.length;j++){
      pts.push((j/(vals.length-1)*w).toFixed(1)+","+(h-1.5-((Number(vals[j])||0)-mn)/rg*(h-3)).toFixed(1));
    }
    return '<polyline points="'+pts.join(" ")+'" fill="none" stroke="'+color+'" stroke-width="1.3"/>';
  }
  return '<svg class="spk" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none">'
       + line(seriesA,ca)+line(seriesB,cb)+'</svg>';
}

/* ================= issue collection -> status strip ================= */
function collectIssues(a){
  var red=[], amber=[];
  try{
    (a.health||[]).forEach(function(h){ if(h && h.up===false) red.push("SVC DOWN: "+esc(h.name)); });
  }catch(e){}
  try{
    (a.zfs||[]).forEach(function(p){
      if(p && p.bad) red.push("ZFS "+esc(p.name)+": "+esc(p.state));
      else if(p && p.cap>=90) amber.push("ZFS "+esc(p.name)+" "+num(p.cap)+"% full");
    });
  }catch(e){}
  try{
    var u=a.unifi||{};
    if(u.wan_up && String(u.wan_up).toLowerCase()!=="ok" && u.wan_up!=="?") red.push("WAN: "+esc(u.wan_up));
  }catch(e){}
  try{
    var z=a.zabbix||{};
    if(z.problems>0){
      var hi=0; Object.keys(z.sev||{}).forEach(function(k){ if(Number(k)>=4) hi+=z.sev[k]; });
      (hi>0?red:amber).push("ZBX: "+z.problems+" problem"+(z.problems>1?"s":""));
    }
  }catch(e){}
  try{
    var b=a.backups||{};
    if(b.missing>0) red.push("BACKUP NEVER: "+b.missing);
    if(b.stale7>0) amber.push("BACKUP STALE: "+b.stale7);
  }catch(e){}
  try{
    var c=a.certs||{};
    (c.hosts||[]).forEach(function(h){
      if(h.state==="crit") red.push("CERT "+esc(h.host)+" "+num(h.days)+"d");
      else if(h.state==="warn"||h.state==="err") amber.push("CERT "+esc(h.host)+(h.days!=null?" "+num(h.days)+"d":" err"));
    });
  }catch(e){}
  try{
    (a.storage||[]).forEach(function(s){ if(s.pct>=92) amber.push(esc(s.name)+" "+num(s.pct)+"% full"); });
  }catch(e){}
  try{
    (S.gpus||[]).forEach(function(g){ if(g.temp>=85) amber.push("GPU "+esc(g.name)+" "+g.temp+"&deg;C"); });
  }catch(e){}
  return { red:red, amber:amber };
}

function renderStrip(a){
  var light=$("light"), issues=$("issues"), kpi=$("stripkpi");
  var iz = collectIssues(a), n = iz.red.length+iz.amber.length;
  if(iz.red.length){ light.className="bad"; light.innerHTML='<div class="dot"></div><b>'+iz.red.length+' CRITICAL &middot; '+n+' TOTAL</b>'; }
  else if(iz.amber.length){ light.className="warn"; light.innerHTML='<div class="dot"></div><b>'+iz.amber.length+' WARNING'+(iz.amber.length>1?"S":"")+'</b>'; }
  else { light.className=""; light.innerHTML='<div class="dot"></div><b>ALL SYSTEMS NOMINAL</b>'; }
  var chips="";
  iz.red.slice(0,6).forEach(function(t){ chips+='<span class="ichip">'+t+'</span>'; });
  iz.amber.slice(0,Math.max(0,8-iz.red.length)).forEach(function(t){ chips+='<span class="ichip w">'+t+'</span>'; });
  var left = n-Math.min(n,8);
  if(left>0) chips+='<span class="ichip w">+'+left+' more</span>';
  issues.innerHTML=chips;

  var u=a.unifi||{}, z=a.zabbix||{};
  kpi.innerHTML =
    '<div><div class="n ok">'+(a.running!=null?a.running:"?")+'</div><div class="l">ONLINE</div></div>'
  + '<div><div class="n">'+(a.total!=null?a.total:"?")+'</div><div class="l">GUESTS</div></div>'
  + '<div><div class="n">'+(u.clients!=null?u.clients:"?")+'</div><div class="l">CLIENTS</div></div>'
  + '<div><div class="n '+(z.problems>0?"warn":"ok")+'">'+(z.problems!=null?z.problems:"?")+'</div><div class="l">ALERTS</div></div>';
}

/* ================= panels ================= */
function renderFleet(a){
  var gs=a.guests;
  if(!gs || !gs.length){ $("fleetgrid").innerHTML=ph("no guests reported"); $("fleetcats").innerHTML=""; return; }
  var cats={};
  gs.forEach(function(g){ var c=g.cat||"infra"; cats[c]=cats[c]||{n:0,run:0}; cats[c].n++; if(g.status==="running")cats[c].run++; });
  var ch="";
  Object.keys(cats).sort().forEach(function(c){ ch+='<span class="tag">'+esc(c)+'<b>'+cats[c].run+'/'+cats[c].n+'</b></span>'; });
  $("fleetcats").innerHTML=ch;
  $("fleetsub").textContent=(a.running!=null?a.running:"?")+" / "+(a.total!=null?a.total:gs.length)+" running";
  var html="";
  gs.forEach(function(g,i){
    var run=g.status==="running", hot=run&&g.cpu>=60;
    html+='<div class="gchip '+(run?(hot?"run hot":"run"):"off")+'" data-gi="'+i+'" title="'+esc(g.name)+' #'+esc(g.id)+' @ '+esc(g.node)+'">'
        + '<span class="d"></span><span class="nm">'+esc(g.name)+'</span>'
        + '<span class="v">'+(run?num(g.cpu)+"%":"&#183;")+'</span></div>';
  });
  $("fleetgrid").innerHTML=html;
}

function renderNodes(a){
  var ns = a.nodes2 && a.nodes2.length ? a.nodes2 : null;
  if(!ns){
    var basic=a.nodes;
    if(!basic || !basic.length){ $("nodegrid").innerHTML=ph("no node data"); return; }
    var h="";
    basic.forEach(function(n){
      var mp=pct(n.mem_used,n.mem_total);
      h+='<div class="node"><div class="hd"><b>'+esc(n.node)+'</b><span>up '+upfmt(n.uptime)+'</span></div>'
       + '<div class="kv"><span>cpu &middot; '+n.cores+' cores</span><b>'+num(n.cpu,1)+'%</b></div>'+bar(n.cpu)
       + '<div class="kv"><span>ram</span><b>'+gb(n.mem_used)+' / '+gb(n.mem_total)+' G</b></div>'+bar(mp)
       + '<div class="kv"><span>load</span><b>'+num(n.load,2)+'</b></div></div>';
    });
    $("nodegrid").innerHTML=h; $("nodesub").textContent=basic.length+" nodes"; return;
  }
  var html="";
  ns.forEach(function(n){
    html+='<div class="node"><div class="hd"><b>'+esc(n.node)+'</b>'
        + '<span>up '+upfmt(n.uptime)+' &middot; '+esc(n.kernel||"")+(n.temp!=null?' &middot; <b class="'+(n.temp>=80?"bad":n.temp>=70?"warn":"mut")+'">'+num(n.temp)+'&deg;C</b>':"")+'</span></div>'
        + '<div class="kv"><span>cpu &middot; '+n.cores+' cores &middot; load '+(n.load||[]).map(function(x){return num(x,2);}).join(" ")+'</span><b>'+num(n.cpu,1)+'%</b></div>'+bar(n.cpu)
        + '<div class="kv"><span>ram</span><b>'+gb(n.mem_used)+' / '+gb(n.mem_total)+' G &middot; '+num(n.mem_pct,1)+'%</b></div>'+bar(n.mem_pct)
        + '<div class="kv"><span>rootfs</span><b>'+gb(n.rootfs_used)+' / '+gb(n.rootfs_total)+' G &middot; '+num(n.rootfs_pct,1)+'%</b></div>'+bar(n.rootfs_pct)
        + '</div>';
  });
  $("nodegrid").innerHTML=html;
  $("nodesub").textContent=ns.length+" hypervisors";
}

function renderSvc(a){
  var hs=a.health;
  if(!hs || !hs.length){ $("svclist").innerHTML=ph("no health checks"); return; }
  var lat={};
  (a.latency||[]).forEach(function(l){ lat[l.name]=l; });
  var up=0, html="";
  hs.forEach(function(h,i){
    if(h.up) up++;
    var l=lat[h.name], sp=l&&l.spark ? spark(l.spark,70,16,h.up?"#5ad7ff":"#ff5a6a") : "";
    html+='<div class="svcrow'+(h.up?"":" dn")+'" data-si="'+i+'"><span class="d"></span>'
        + '<span class="nm">'+esc(h.name)+(h.note?' <span class="dim">'+esc(h.note)+'</span>':"")+'</span>'
        + sp
        + '<span class="ms">'+(h.up?(l?l.last:h.ms)+"ms":"DOWN")+'</span></div>';
  });
  $("svclist").innerHTML=html;
  var el=$("svcsub"); el.textContent=up+" / "+hs.length+" up";
  el.className = up===hs.length ? "ok" : "bad";
}

function renderGpu(){
  var gs=S.gpus;
  if(!gs || !gs.length){ $("gpulist").innerHTML=ph("no GPU telemetry"); return; }
  var html="";
  gs.forEach(function(g){
    var mp=pct(g.mem_used,g.mem_total);
    var hist=S.hist.gpu[g.index];
    var sp = hist && hist.length>2 ? spark(hist.map(function(r){return r.util;}),120,18,"#ff4fa3") : "";
    html+='<div class="gpucard"><div class="hd"><b>'+esc(g.name)+'</b><span>'+esc(g.host)+'</span></div>'
        + '<div class="kv"><span>util</span><b>'+num(g.util)+'%</b></div>'+bar(g.util,"")
        + '<div class="kv"><span>vram</span><b>'+(g.mem_used/1024).toFixed(1)+' / '+(g.mem_total/1024).toFixed(1)+' G</b></div>'+bar(mp)
        + '<div class="st"><span class="'+(g.temp>=85?"bad":g.temp>=75?"warn":"")+'">'+num(g.temp)+'&deg;C</span><span>'+num(g.power,1)+' W</span>'
        + (sp?'<span style="margin-left:auto">'+sp+'</span>':"")+'</div></div>';
  });
  $("gpulist").innerHTML=html;
  $("gpusub").textContent=gs.length+" GPU"+(gs.length>1?"s":"");
}

function renderZfs(a){
  var ps = a.zfs && a.zfs.length ? a.zfs : null;
  if(!ps){
    var basic=a.pools;
    if(!basic || !basic.length){ $("zfslist").innerHTML=ph("no pool data"); return; }
    var h="";
    basic.forEach(function(p){
      var cap=pct(p.alloc,p.size), ok=p.health==="ONLINE";
      h+='<div class="kv"><span><b class="'+(ok?"ok":"bad")+'">'+esc(p.health)+'</b> '+esc(p.node)+'/'+esc(p.name)+'</span><b>'+cap+'% &middot; frag '+num(p.frag)+'%</b></div>'+bar(cap);
    });
    $("zfslist").innerHTML=h; return;
  }
  var badn=0, html="";
  ps.forEach(function(p){
    if(p.bad) badn++;
    var e=p.errs||{}, errs=(e.r||0)+(e.w||0)+(e.c||0);
    html+='<div style="margin-bottom:6px">'
        + '<div class="kv"><span><b class="'+(p.bad?"bad":"ok")+'">'+esc(p.state)+'</b> '+esc(p.node)+'/'+esc(p.name)+'</span>'
        + '<b>'+num(p.cap,1)+'%</b></div>'
        + bar(p.cap)
        + '<div class="kv" style="font-size:10px"><span>frag '+num(p.frag)+'% &middot; scrub '+esc(p.scrub||"?")
        + (p.scrub_age_d!=null?" ("+num(p.scrub_age_d,1)+"d ago)":"")+'</span>'
        + '<b class="'+(errs?"bad":"dim")+'">'+(errs? "ERRS r"+(e.r||0)+" w"+(e.w||0)+" c"+(e.c||0) : "0 errs")+'</b></div></div>';
  });
  $("zfslist").innerHTML=html;
  var el=$("zfssub");
  el.textContent = badn? badn+" POOL"+(badn>1?"S":"")+" BAD" : "all ONLINE";
  el.className = badn? "bad" : "ok";
}

function renderBackups(a){
  var b=a.backups;
  if(!b || b.guests==null){ $("bkbody").innerHTML=ph("no backup data yet"); $("bksub").textContent=""; return; }
  var covered=pct(b.backed,b.guests);
  var html='<div class="kv"><span>coverage</span><b>'+b.backed+' / '+b.guests+' guests</b></div>'+bar(covered)
    + '<div class="row" style="gap:6px;margin:4px 0 6px;flex-wrap:wrap">'
    + (b.missing>0?'<span class="badge b">NEVER: '+b.missing+'</span>':'<span class="badge g">NONE MISSING</span>')
    + (b.stale7>0?'<span class="badge w">STALE &gt;7d: '+b.stale7+'</span>':'<span class="badge g">NONE STALE</span>')
    + (b.newest_age_h!=null?'<span class="tag">newest '+num(b.newest_age_h,1)+'h ago</span>':"")
    + '</div>';
  var worst=b.worst||[];
  if(worst.length){
    html+='<hr class="sep"/>';
    worst.forEach(function(w){
      var never=w.age_d==null;
      html+='<div class="lrow"><span class="sv '+(never?"bad":"warn")+'"></span>'
          + '<span class="h">'+esc(w.name)+'</span><span class="m dim">#'+esc(w.id)+'</span>'
          + '<span class="t '+(never?"bad":"warn")+'">'+(never?"NEVER":num(w.age_d,1)+"d")+'</span></div>';
    });
  }
  html+='<div class="kv" style="margin-top:5px;font-size:10px"><span>'+(b.storages||[]).map(esc).join(" &middot; ")+'</span><b class="dim">'+(b.archives!=null?b.archives+" archives":"")+'</b></div>';
  $("bkbody").innerHTML=html;
  var el=$("bksub");
  el.textContent = (b.missing||b.stale7) ? (b.missing||0)+" never / "+(b.stale7||0)+" stale" : "healthy";
  el.className = b.missing? "bad" : b.stale7? "warn" : "ok";
}

var SEVC=["#5f7da0","#5ad7ff","#ffb347","#ff8c5a","#ff5a6a","#ff5a6a"];
function renderZbx(a){
  var z=a.zabbix||{}, z2=a.zbx2||{};
  if(z.error && !z2.recent){ $("zbxlist").innerHTML=ph("zabbix unreachable: "+z.error); return; }
  var html='<div class="row" style="gap:6px;margin-bottom:6px;flex-wrap:wrap">'
    + '<span class="tag">'+(z.hosts!=null?z.hosts:"?")+' hosts</span>'
    + '<span class="badge '+(z.problems>0?"w":"g")+'">'+(z.problems!=null?z.problems:"?")+' PROBLEMS</span>';
  var sev=z.sev||{};
  Object.keys(sev).sort().reverse().forEach(function(k){
    html+='<span class="tag" style="color:'+SEVC[Math.min(5,Number(k))]+'">sev'+k+' &times;'+sev[k]+'</span>';
  });
  html+='</div>';
  var rec=z2.recent||[];
  if(rec.length){
    rec.forEach(function(p){
      var c=SEVC[Math.min(5,Number(p.sev)||0)];
      html+='<div class="lrow"><span class="sv" style="color:'+c+'"></span>'
          + '<span class="h">'+esc(p.host)+'</span><span class="m">'+esc(p.name)+'</span>'
          + '<span class="t">'+agefmt(p.age)+'</span></div>';
    });
  } else if(!z.problems){
    html+='<div class="ph">no active problems</div>';
  }
  $("zbxlist").innerHTML=html;
  var el=$("zbxsub");
  el.textContent = z2.version ? "v"+z2.version : "";
  el.className="dim";
}

function renderNet(a){
  var u=a.unifi;
  if(!u || (u.error && u.clients==null)){ $("netbody").innerHTML=ph("unifi unreachable"+(u&&u.error?": "+u.error:"")); return; }
  var wanOk = String(u.wan_up||"").toLowerCase()==="ok";
  var html='<div class="row" style="gap:6px;margin-bottom:5px;flex-wrap:wrap">'
    + '<span class="badge '+(wanOk?"g":"b")+'">WAN '+esc(String(u.wan_up||"?").toUpperCase())+'</span>'
    + (u.isp?'<span class="tag">'+esc(u.isp)+'</span>':"")
    + (u.wan_ip?'<span class="tag">'+esc(u.wan_ip)+'</span>':"")
    + '</div>'
    + '<div class="kv"><span>throughput &darr; / &uarr;</span><b>'+num(u.rx,1)+' / '+num(u.tx,1)+' Mbps</b></div>'
    + '<div class="kv"><span>latency</span><b class="'+(u.latency>60?"warn":"")+'">'+num(u.latency)+' ms</b></div>'
    + '<div class="kv"><span>clients (wifi)</span><b>'+(u.clients!=null?u.clients:"?")+' ('+(u.wifi!=null?u.wifi:"?")+')</b></div>'
    + '<div class="kv"><span>unifi devices</span><b>'+(u.devices!=null?u.devices:"?")+'</b></div>';
  var h=S.hist.net;
  if(h && h.length>3){
    var rx=h.map(function(r){return r.rx;}), tx=h.map(function(r){return r.tx;});
    html+='<div style="margin-top:6px"><div class="kv" style="font-size:9px"><span class="dim">WAN 2h &middot; <b style="color:#5ad7ff">rx</b> / <b style="color:#ff4fa3">tx</b> Mbps</span></div>'
        + spark2(rx,tx,260,26,"#5ad7ff","#ff4fa3")+'</div>';
  }
  $("netbody").innerHTML=html;
  var el=$("netsub");
  el.textContent = wanOk ? "wan ok" : "wan "+String(u.wan_up||"?");
  el.className = wanOk ? "ok" : "bad";
}

function renderCerts(a){
  var c=a.certs;
  if(!c || !c.hosts || !c.hosts.length){ $("certlist").innerHTML=ph("no cert data yet"); return; }
  var html="";
  c.hosts.forEach(function(h){
    var cls = h.state==="crit"||h.state==="err" ? "bad" : h.state==="warn" ? "warn" : "ok";
    html+='<div class="lrow"><span class="sv '+cls+'"></span>'
        + '<span class="h" style="max-width:60%">'+esc(h.host)+'</span>'
        + '<span class="m dim">'+esc(h.issuer||h.err||"")+'</span>'
        + '<span class="t '+cls+'">'+(h.days!=null?num(h.days)+"d":"ERR")+'</span></div>';
  });
  $("certlist").innerHTML=html;
  var el=$("certsub");
  el.textContent = c.warn? c.warn+" need attention" : "min "+num(c.min_days)+"d";
  el.className = c.warn? "warn" : "ok";
}

function renderTop(a){
  var t=a.top;
  if(!t){ $("topbody").innerHTML=ph("no data yet"); return; }
  var html='<div class="kv"><span class="dim" style="letter-spacing:1.5px">CPU</span></div>';
  (t.cpu||[]).forEach(function(g){
    html+='<div class="kv"><span>'+esc(g.name)+' <span class="dim">'+esc(g.node)+'</span></span><b>'+num(g.v,1)+'%</b></div>'+bar(g.v,"");
  });
  if(!(t.cpu||[]).length) html+=ph("idle");
  html+='<hr class="sep"/><div class="kv"><span class="dim" style="letter-spacing:1.5px">RAM</span></div>';
  (t.mem||[]).forEach(function(g){
    html+='<div class="kv"><span>'+esc(g.name)+' <span class="dim">'+esc(g.node)+'</span></span><b>'+num(g.gb,1)+'G &middot; '+num(g.pct)+'%</b></div>';
  });
  if((t.full||[]).length){
    html+='<hr class="sep"/><div class="kv"><span class="dim" style="letter-spacing:1.5px">NEARING FULL</span></div>';
    t.full.forEach(function(f){
      html+='<div class="kv"><span class="warn">'+esc(f.name)+' <span class="dim">'+esc(f.node)+'</span></span><b class="'+(f.pct>=92?"bad":"warn")+'">'+num(f.pct,1)+'%</b></div>'+bar(f.pct);
    });
  }
  $("topbody").innerHTML=html;
}

function renderStor(a){
  var st=a.storage;
  if(!st || !st.length){ $("storlist").innerHTML=ph("no storage data"); return; }
  var html="", tot=0, used=0;
  st.forEach(function(s){
    tot+=s.total; used+=s.used;
    html+='<div class="kv"><span>'+esc(s.name)+' <span class="dim">'+esc(s.node)+' &middot; '+esc(s.type||"")+'</span></span>'
        + '<b class="'+(s.pct>=92?"bad":s.pct>=80?"warn":"")+'">'+gb(s.used)+' / '+gb(s.total)+'G</b></div>'+bar(s.pct);
  });
  $("storlist").innerHTML=html;
  $("storsub").textContent=(used/1.1e12).toFixed(1)+" / "+(tot/1.1e12).toFixed(1)+" TB";
}

function renderFrig(a){
  var f=a.frigate;
  if(!f){ $("frigbody").innerHTML=ph("no frigate data yet"); return; }
  if(f.up===false){ $("frigbody").innerHTML=ph("frigate down"+(f.err?": "+f.err:"")); $("frigsub").textContent="DOWN"; $("frigsub").className="bad"; return; }
  var html="";
  var dets=f.det_ms||{};
  var dk=Object.keys(dets);
  if(dk.length){
    html+='<div class="row" style="gap:6px;margin-bottom:5px;flex-wrap:wrap">';
    dk.forEach(function(k){ html+='<span class="tag">'+esc(k)+' <b style="color:#dcefff">'+num(dets[k],1)+'ms</b></span>'; });
    if(f.version) html+='<span class="tag">v'+esc(f.version)+'</span>';
    html+='</div>';
  }
  (f.cams||[]).forEach(function(c){
    html+='<div class="kv"><span>'+esc(c.name)+'</span><b>'+num(c.fps,1)+' fps &middot; det '+num(c.dfps,1)
        + (c.skip>0.5?' &middot; <span class="warn">skip '+num(c.skip,1)+'</span>':"")+'</b></div>';
  });
  var r=f.rec||{};
  if(r.total_gb){
    html+='<hr class="sep"/><div class="kv"><span>recordings</span><b>'+num(r.used_gb,1)+' / '+num(r.total_gb,1)+'G</b></div>'+bar(r.pct)
        + '<div class="kv" style="font-size:10px"><span class="dim">est retention</span><b class="'+(r.est_days!=null&&r.est_days<3?"warn":"dim")+'">'+(r.est_days!=null?num(r.est_days,1)+"d":"calculating&#8230;")+'</b></div>';
  }
  $("frigbody").innerHTML=html||ph("no camera stats");
  $("frigsub").textContent=(f.cams||[]).length+" cams";
  $("frigsub").className="ok";
}

function renderAi(a){
  var s=a.sources||{}, nb=a.fleet||{}, ai=a.ai_servers||[];
  var html="";
  function srcRow(name,o,extra){
    var up=o&&o.up;
    return '<div class="kv"><span><b class="'+(up?"ok":"bad")+'">&#9679;</b> '+name+'</span><b class="'+(up?"":"bad")+'">'+(up?(extra||"up"):"down")+'</b></div>';
  }
  html+=srcRow("zabbix",s.zabbix,s.zabbix&&s.zabbix.items!=null?s.zabbix.items+" items":null);
  html+=srcRow("grafana",s.grafana,s.grafana&&s.grafana.dashboards!=null?s.grafana.dashboards+" dashboards":null);
  html+=srcRow("prometheus",s.prometheus,s.prometheus&&s.prometheus.targets!=null?(s.prometheus.healthy!=null?s.prometheus.healthy+"/":"")+s.prometheus.targets+" targets":null);
  if((nb.services||[]).length){
    html+='<hr class="sep"/>';
    (nb.services||[]).forEach(function(sv){
      html+='<div class="kv" style="font-size:10.5px"><span class="dim">&nbsp;&nbsp;'+esc(sv.name)+'</span><b class="'+(sv.up?"ok":"bad")+'">'+(sv.up?"up":"down")+'</b></div>';
    });
  }
  if(ai.length){
    html+='<hr class="sep"/>';
    ai.forEach(function(sv){
      html+='<div class="kv"><span><b class="'+(sv.up?"ok":"bad")+'">&#9679;</b> '+esc(sv.name)+'</span>'
          + '<b>'+(sv.ollama_up?sv.models+" models":"ollama down")+(sv.gpu_count?' &middot; '+sv.gpu_count+' gpu '+num(sv.gpu_util,1)+'%':"")+'</b></div>';
    });
  }
  $("aibody").innerHTML=html||ph("no data");
  var nUp=(nb.services||[]).filter(function(x){return x.up;}).length;
  $("aisub").textContent=(nb.services||[]).length? nUp+"/"+(nb.services||[]).length+" fleet up" : "";
}

var camsBuilt="";
function renderCams(a){
  var cams=a.cameras;
  if(!cams || !cams.length){ $("camgrid").innerHTML=ph("no cameras reported"); return; }
  var key=cams.join(",");
  if(key!==camsBuilt){
    var html="";
    cams.forEach(function(c){
      html+='<div class="cam"><img data-cam="'+esc(c)+'" src="/g3/cam/'+encodeURIComponent(c)+'.jpg" alt="'+esc(c)+'"/>'
          + '<span>'+esc(c)+'</span><span class="live">LIVE</span></div>';
    });
    $("camgrid").innerHTML=html;
    camsBuilt=key;
  }
  $("camsub").textContent=cams.length+" feeds · 10s refresh";
}
function refreshCamImgs(){
  try{
    document.querySelectorAll("img[data-cam]").forEach(function(img){
      img.src="/g3/cam/"+encodeURIComponent(img.getAttribute("data-cam"))+".jpg?t="+Date.now();
    });
  }catch(e){}
}

/* ================= popover ================= */
var pop=$("pop");
function hidePop(){ pop.style.display="none"; pop.innerHTML=""; }
function showPop(html,x,y){
  pop.innerHTML=html;
  pop.style.display="block";
  pop.style.left="0px"; pop.style.top="0px";
  var r=pop.getBoundingClientRect();
  var px=Math.min(Math.max(8,x),window.innerWidth-r.width-8);
  var py=Math.min(Math.max(8,y),window.innerHeight-r.height-8);
  pop.style.left=px+"px"; pop.style.top=py+"px";
}
function guestPop(g,x,y){
  var run=g.status==="running";
  var html='<div class="tt"><span>'+esc(g.name)+' <span class="dim">#'+esc(g.id)+'</span></span><i data-close="1">&times;</i></div>'
    + '<div class="kv"><span>node / type</span><b>'+esc(g.node)+' &middot; '+esc(g.type||"?")+'</b></div>'
    + '<div class="kv"><span>category</span><b>'+esc(g.cat)+'</b></div>'
    + '<div class="kv"><span>status</span><b class="'+(run?"ok":"dim")+'">'+esc(g.status)+'</b></div>'
    + '<div class="kv"><span>cpu</span><b>'+num(g.cpu,1)+'%</b></div>'+bar(g.cpu,"")
    + '<div class="kv"><span>ram</span><b>'+gb(g.mem)+' / '+gb(g.maxmem)+' G</b></div>'+bar(pct(g.mem,g.maxmem))
    + '<div class="hsp" id="pophist"><div class="l">2h history</div><div class="ph">loading&#8230;</div></div>';
  showPop(html,x,y);
  fetch("/g3/api/history?guest="+encodeURIComponent(g.id)+"&mins=120")
    .then(function(r){return r.json();})
    .then(function(rows){
      var el=$("pophist");
      if(!el) return;
      if(!rows || rows.length<3){ el.innerHTML='<div class="l">2h history</div><div class="ph">no samples</div>'; return; }
      var cpu=rows.map(function(r){return r.cpu;});
      var mem=rows.map(function(r){return r.mem/1.074e9;});
      el.innerHTML='<div class="l">2h &middot; <b style="color:#5ad7ff">cpu%</b></div>'+spark(cpu,270,26,"#5ad7ff")
        + '<div class="l" style="margin-top:4px"><b style="color:#ff4fa3">ram GB</b></div>'+spark(mem,270,26,"#ff4fa3");
    }).catch(function(){
      var el=$("pophist");
      if(el) el.innerHTML='<div class="l">2h history</div><div class="ph">history unavailable</div>';
    });
}
function svcPop(h,x,y){
  var l=null;
  ((S.all&&S.all.latency)||[]).forEach(function(r){ if(r.name===h.name) l=r; });
  var html='<div class="tt"><span>'+esc(h.name)+'</span><i data-close="1">&times;</i></div>'
    + '<div class="kv"><span>status</span><b class="'+(h.up?"ok":"bad")+'">'+(h.up?"UP":"DOWN")+' &middot; http '+esc(h.code)+'</b></div>'
    + '<div class="kv"><span>category</span><b>'+esc(h.cat)+'</b></div>'
    + (h.note?'<div class="kv"><span>note</span><b>'+esc(h.note)+'</b></div>':"");
  if(l){
    html+='<div class="kv"><span>latency last/avg</span><b>'+l.last+' / '+l.avg+' ms</b></div>'
        + '<div class="kv"><span>min / max</span><b>'+l.min+' / '+l.max+' ms</b></div>'
        + '<div class="hsp"><div class="l">recent checks (ms)</div>'+spark(l.spark,270,28,h.up?"#5ad7ff":"#ff5a6a")+'</div>';
  } else {
    html+='<div class="kv"><span>latency</span><b>'+esc(h.ms)+' ms</b></div>';
  }
  showPop(html,x,y);
}
document.addEventListener("click",function(ev){
  var t=ev.target;
  if(t.getAttribute && t.getAttribute("data-close")){ hidePop(); return; }
  if(pop.contains(t)) return;
  var chip=t.closest ? t.closest(".gchip") : null;
  if(chip && S.all && S.all.guests){
    var g=S.all.guests[Number(chip.getAttribute("data-gi"))];
    if(g){ guestPop(g,ev.clientX+12,ev.clientY+12); return; }
  }
  var row=t.closest ? t.closest(".svcrow") : null;
  if(row && S.all && S.all.health){
    var h=S.all.health[Number(row.getAttribute("data-si"))];
    if(h){ svcPop(h,ev.clientX+12,ev.clientY+12); return; }
  }
  hidePop();
});
document.addEventListener("keydown",function(ev){ if(ev.key==="Escape") hidePop(); });

/* ================= render orchestration ================= */
var RENDERERS=[renderStrip,renderFleet,renderNodes,renderSvc,renderZfs,renderBackups,
               renderZbx,renderNet,renderCerts,renderTop,renderStor,renderFrig,renderAi,renderCams];
function renderAll(){
  var a=S.all;
  if(!a) return;
  RENDERERS.forEach(function(fn){
    try{ fn(a); }catch(e){ /* one broken panel never kills the page */ }
  });
}

function tickClock(){
  var d=new Date();
  function p(n){ return (n<10?"0":"")+n; }
  $("clk").textContent=p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());
  var age=$("age");
  if(S.lastOk){
    var s=Math.round((Date.now()-S.lastOk)/1000);
    age.textContent="data "+s+"s ago";
    age.className = s>25 ? "stale" : "";
    if(s>25) age.textContent="STALE · data "+s+"s ago";
  }
}
setInterval(tickClock,1000); tickClock();

/* ================= polling ================= */
function pollAll(){
  fetch("/g3/api/all").then(function(r){ return r.json(); }).then(function(j){
    S.all=j; S.lastOk=Date.now(); renderAll();
  }).catch(function(){ /* keep last-good data on screen */ });
}
function pollGpu(){
  fetch("/g3/api/gpu").then(function(r){ return r.json(); }).then(function(j){
    S.gpus=(j&&j.gpus)||[];
    try{ renderGpu(); }catch(e){}
  }).catch(function(){});
}
function pollHist(){
  fetch("/g3/api/history?metric=net&mins=120").then(function(r){ return r.json(); })
    .then(function(rows){ S.hist.net=rows; try{ if(S.all) renderNet(S.all); }catch(e){} }).catch(function(){});
  (S.gpus||[]).slice(0,4).forEach(function(g){
    if(g.index>=100) return;  /* remote exporters share history idx space; keep to local */
    fetch("/g3/api/history?metric=gpu&idx="+g.index+"&mins=120").then(function(r){ return r.json(); })
      .then(function(rows){ S.hist.gpu[g.index]=rows; try{ renderGpu(); }catch(e){} }).catch(function(){});
  });
}
pollAll(); pollGpu();
setTimeout(pollHist,1500);
setInterval(pollAll,6000);
setInterval(pollGpu,3000);
setInterval(pollHist,60000);
setInterval(refreshCamImgs,10000);

/* identity from /api/config, applied once (config.js has already normalised it) */
if(window.loadConfig) window.loadConfig().then(function(C){
  try{
    document.title=C.brand+" \u00b7 desk";
    var b=document.getElementById("deskbrand");
    if(b) b.innerHTML=C.brandHtml()+" <span>DESK</span>";
    var t=document.getElementById("desktag"); if(t) t.textContent=C.tagline;
    /* no NVR configured: the camera panels would sit empty forever */
    if(!C.features.cameras){
      ["cams","frig"].forEach(function(id){var e=document.getElementById(id); if(e) e.style.display="none";});
    }
  }catch(e){}
});
