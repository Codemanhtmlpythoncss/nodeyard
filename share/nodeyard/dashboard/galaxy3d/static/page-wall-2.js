
/* ---- shared helpers for the inline HUD code below ---- */
function nbEsc(x){return String(x==null?'':x).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function cfg(){return window.CONFIG||{features:{},palette:{},nodes:[],categories:[]};}
/* ADD-ONS panel: one block per data.addons[id] = {title,status,rows,ts,error}
   (contract in docs/contracts.md). Hidden when there are none. */
function renderAddons(d){
  var panel=document.getElementById('addons'),body=document.getElementById('addonbody'),stat=document.getElementById('s-addons');
  var A=(d&&d.addons)||{},ids=Object.keys(A);
  if(!ids.length){panel.style.display='none';if(stat){stat.textContent='–';stat.className='n ok';}return;}
  panel.style.display='';
  var up=0,now=Date.now()/1000;
  body.innerHTML=ids.map(function(id){
    var a=A[id]||{},st=String(a.status||'unknown').toLowerCase();
    if(st==='ok')st='up';
    if(st==='up')up++;
    var age=a.ts?Math.max(0,now-a.ts):null,ageS=age==null?'':age<90?Math.round(age)+'s':age<5400?Math.round(age/60)+'m':Math.round(age/3600)+'h';
    var rows=(a.rows||[]).map(function(r){var c=r.cls==='ok'?'':r.cls==='warn'?' warn':r.cls==='bad'?' bad':'';
      return '<div class="kv"><span>'+nbEsc(r.k)+'</span><b class="'+c.trim()+'">'+nbEsc(r.v)+'</b></div>';}).join('');
    return '<div class="addon"><div class="ah"><span class="pip '+nbEsc(st)+'"></span><b>'+nbEsc(a.title||id)+'</b><em>'+ageS+'</em></div>'+
      rows+(a.error?'<div class="err">'+nbEsc(a.error)+'</div>':'')+'</div>';
  }).join('');
  if(stat){stat.textContent=up+'/'+ids.length;stat.className='n '+(up===ids.length?'ok':up?'warn':'bad');}
}
function startHudFallback(){
  if(window.HUD_STARTED||window.SCENE_OK)return; window.HUD_STARTED=true;
  if(window.__hudFallbackStarted)return;
  window.__hudFallbackStarted=true;
  const text=(v,fb='–')=>(v===undefined||v===null||v==='')?fb:v;
  const pct=(used,total)=>Math.min(100,Math.max(0,total?used/total*100:0));
  async function pollFallback(){
    let d={},gpu=[];
    try{d=await (await fetch('/g3/api/all')).json();}catch(e){}if(window.renderNewPanels){try{window.renderNewPanels(d);}catch(e){}}
    try{const g=await (await fetch('/g3/api/gpu')).json();gpu=g.gpus||g||[];}catch(e){}
    const byHost={};gpu.forEach(g=>{const h=g.host||'gpu';(byHost[h]=byHost[h]||[]).push(g);});
    document.getElementById('s-run').textContent=text((d.running||0)+(d.ai_servers||[]).filter(s=>s.up).length);
    document.getElementById('s-tot').textContent=text((d.guests||[]).length+(d.ai_servers||[]).reduce((a,s)=>a+1+(s.gpus||[]).length,0));
    document.getElementById('s-cli').textContent=text(d.unifi&&d.unifi.clients);
    document.getElementById('s-gpu').textContent=gpu.length?Math.round(gpu.reduce((a,g)=>a+Number(g.util||0),0)/gpu.length)+'%':'–';
    document.getElementById('s-net').textContent=d.netmap?`${d.netmap.up}/${d.netmap.total}`:'–';
    try{renderAddons(d);}catch(e){}
    document.getElementById('gpus').innerHTML=Object.entries(byHost).map(([host,arr])=>{
      const util=arr.reduce((a,g)=>a+Number(g.util||0),0)/Math.max(arr.length,1);
      const memUsed=arr.reduce((a,g)=>a+Number(g.mem_used||0),0),memTotal=arr.reduce((a,g)=>a+Number(g.mem_total||0),0);
      return `<div class="hostblock"><div class="hosttitle"><span>${nbEsc(host)}</span><b>${arr.length} GPU · ${Math.round(util)}%</b></div><div class="bar"><i style="width:${util}%"></i></div><div class="gpuchips">`+
        arr.map(g=>`<div class="gpuchip"><b>${nbEsc((g.name||'GPU').replace('NVIDIA GeForce ',''))}</b><span><em>${g.util||0}%</em><em>${g.temp||0}°C</em><em>${(Number(g.mem_used||0)/1024).toFixed(1)}G</em></span></div>`).join('')+
        `</div><div class="sub"><span>VRAM</span><span>${(memUsed/1024).toFixed(1)}/${(memTotal/1024).toFixed(0)}GB</span></div></div>`;
    }).join('');
    const nodes=d.nodes||[],health=d.health||[],upHealth=health.filter(h=>h.up).length,ai=d.ai_servers||[],u=d.unifi||{},z=d.zabbix||{};
    const memTotal=nodes.reduce((a,n)=>a+Number(n.mem_total||0),0),memUsed=nodes.reduce((a,n)=>a+Number(n.mem_used||0),0);
    const gpuMem=gpu.reduce((a,g)=>a+Number(g.mem_total||0),0),gpuUsed=gpu.reduce((a,g)=>a+Number(g.mem_used||0),0);
    const ops=document.getElementById('opsgrid');
    if(ops)ops.innerHTML=
      `<div class="op"><span>Compute</span><b>${nodes.length} nodes · ${gpu.length} GPU</b><small>${(memUsed/1.07e9).toFixed(0)}/${(memTotal/1.07e9).toFixed(0)}G RAM</small><div class="meter"><i style="width:${pct(memUsed,memTotal)}%"></i></div></div>`+
      `<div class="op"><span>AI Fabric</span><b>${ai.filter(s=>s.up).length}/${ai.length||1} online</b><small>${(gpuUsed/1024).toFixed(1)}/${(gpuMem/1024).toFixed(0)}GB VRAM</small><div class="meter"><i style="width:${pct(gpuUsed,gpuMem)}%"></i></div></div>`+
      `<div class="op"><span>Network</span><b>${text(u.clients)} clients</b><small>${text(u.rx)} / ${text(u.tx)} Mbps WAN</small><div class="meter"><i style="width:${Math.min(100,Number(u.rx||0)*2)}%"></i></div></div>`+
      `<div class="op"><span>Health</span><b style="color:${z.problems?'#ff5a6a':'#37f5a0'}">${z.problems||0} alerts</b><small>${upHealth}/${health.length||0} checks up</small><div class="meter"><i style="width:${pct(upHealth,health.length)}%"></i></div></div>`;
    const storage=d.storage||[],pools=d.pools||[],storTotal=storage.reduce((a,s)=>a+Number(s.total||0),0),storUsed=storage.reduce((a,s)=>a+Number(s.used||0),0);
    const aiLine=ai.map(s=>`${nbEsc(s.name)} ${s.ollama_up?'ready':'down'} · ${s.models||0} models`).join(' · ')||'none';
    document.getElementById('kvs').innerHTML=
      `<div class="kv"><span>UniFi clients / devices</span><b>${text(u.clients)} / ${text(u.devices)} (${text(u.wifi,'?')} wifi)</b></div>`+
      `<div class="kv"><span>WAN down / up</span><b>${text(u.rx)} / ${text(u.tx)} Mbps</b></div>`+
      `<div class="kv"><span>Zabbix hosts / alerts</span><b style="color:${z.problems?'#ff5a6a':'#37f5a0'}">${text(z.hosts)} / ${z.problems||0}</b></div>`+
      `<div class="kv"><span>k3s nodes</span><b>${nodes.length} · ${(memUsed/1.07e9).toFixed(0)}/${(memTotal/1.07e9).toFixed(0)}G RAM</b></div>`+
      `<div class="kv"><span>AI GPU server</span><b style="color:${ai.length?'#37f5a0':'#ff5a6a'}">${aiLine}</b></div>`+
      `<div class="kv"><span>ZFS pools</span><b>${pools.length} pools</b></div>`+
      `<div class="kv"><span>Storage</span><b>${storage.length} stores · ${(storUsed/1e12).toFixed(1)}/${(storTotal/1e12).toFixed(1)}TB</b></div>`;
    const cams=cfg().features.cameras?(d.cameras||[]).slice(0,4):[];
    document.getElementById('cams').innerHTML=cams.map(c=>`<div class="cam"><img src="/g3/cam/${encodeURIComponent(c)}.jpg?${Date.now()}" id="hud-${nbEsc(c)}"/><span>${nbEsc(c)}</span></div>`).join('');
    const ent=Object.entries(z.alert_hosts||{}).sort((a,b)=>b[1].sev-a[1].sev),shown=ent.slice(0,6);
    const ap=document.getElementById('alerts'),al=document.getElementById('alertlist'),SEVC={5:'#ff2b2b',4:'#ff5a6a',3:'#ffa033',2:'#ffd24a'};
    if(ent.length){ap.style.display='block';al.innerHTML=shown.map(([h,v])=>`<div class="a"><span class="sv" style="color:${SEVC[v.sev]||'#ffd24a'}"></span><span class="h">${nbEsc(h)}</span><span class="m">${nbEsc((v.names&&v.names[0])||'')}${v.n>1?' +'+(v.n-1):''}</span></div>`).join('')+(ent.length>shown.length?`<div class="more">+${ent.length-shown.length} more alerts in Zabbix</div>`:'');}
    else ap.style.display='none';
  }
  pollFallback();setInterval(pollFallback,6000);setInterval(()=>document.querySelectorAll('#cams img').forEach(im=>im.src='/g3/cam/'+im.id.replace('hud-','')+'.jpg?'+Date.now()),1800);
}
window.addEventListener('error',()=>startHudFallback(),{once:true});
setTimeout(()=>{if(!window.SCENE_OK&&document.getElementById('s-run').textContent==='–')startHudFallback();},6500);

/* ---- brand / features from /api/config, applied before the scene boots ---- */
function applyConfig(C){
  document.title=C.brand+' · wall';
  var b=document.getElementById('brand');if(b)b.innerHTML=C.brandHtml();
  var tl=document.getElementById('tagline');if(tl)tl.textContent=C.tagline;
  document.body.classList.toggle('no-cameras',!C.features.cameras);
  document.body.classList.toggle('no-chat',!C.features.chat);
  var nh=document.getElementById('nexus-hint');if(nh)nh.style.display=C.features.nexus?'':'none';
  var CAT={},CATNAME={};
  C.categories.forEach(function(c){CAT[c]=C.catHex(c);CATNAME[c]=C.catName(c);});
  window.CAT=CAT;window.CATNAME=CATNAME;
  document.getElementById('legend').innerHTML=Object.keys(CATNAME).map(k=>`<div class="r"><span class="d" style="color:${C.hexStr(CAT[k])}"></span>${nbEsc(CATNAME[k])}</div>`).join('');
}

/* ---- the 3D scene: renderer, composer, HUD pollers. netscene.js and the
   other modules (loaded afterwards, in order) build everything visible. ---- */
function bootScene(){
if(!(window.THREE&&THREE.OrbitControls&&THREE.EffectComposer)){startHudFallback();return;}
window.SCENE_OK=true;
const CAT=window.CAT||{},CATNAME=window.CATNAME||{};
const R=150;

const scene=new THREE.Scene();scene.fog=new THREE.FogExp2(0x01020a,0.00018);
const cam=new THREE.PerspectiveCamera(58,innerWidth/innerHeight,0.5,18000);cam.position.set(0,330,1080);
const rndr=new THREE.WebGLRenderer({canvas:c,antialias:true,powerPreference:'high-performance'});rndr.setSize(innerWidth,innerHeight);rndr.setPixelRatio(Math.min(devicePixelRatio,2));
rndr.toneMapping=THREE.ACESFilmicToneMapping;rndr.toneMappingExposure=1.2;
const ctrl=new THREE.OrbitControls(cam,rndr.domElement);ctrl.enableDamping=true;ctrl.maxDistance=6200;ctrl.minDistance=25;
scene.add(new THREE.AmbientLight(0x44557a,.6));
const composer=new THREE.EffectComposer(rndr);composer.addPass(new THREE.RenderPass(scene,cam));
const bloom=new THREE.UnrealBloomPass(new THREE.Vector2(innerWidth,innerHeight),0.95,0.6,0.78);composer.addPass(bloom);
window.scene=scene;window.cam=cam;window.rndr=rndr;window.composer=composer;window.bloom=bloom;window.ctrl=ctrl;

// ---- netscene.js renders the live network topology + flows ----
const GAL={};let PLANETS=[],NETMESHES=[],netNodes=[],camScreens=[],storageWraps=[],
    satellites=[],TOUR=[],tIdx=-1;
function updateGpuBlades(){}function buildGuests(){}function buildCameras(){}
function buildNetmap(){}function refreshCams(){}function buildTour(){}
function showStopCard(){}function buildStorage(){}function spawnMeteor(){}

// ---- HUD (same data feeds) ----
let GPU=[];
async function pollGpu(){try{const j=await (await fetch('/g3/api/gpu')).json();GPU=j.gpus||[];updateGpuBlades(GPU);
  const grouped={};GPU.forEach(g=>{const h=g.host||'gpu';(grouped[h]=grouped[h]||[]).push(g);});
  document.getElementById('gpus').innerHTML=Object.entries(grouped).map(([host,arr])=>{
    const util=arr.reduce((a,g)=>a+Number(g.util||0),0)/arr.length;
    const memUsed=arr.reduce((a,g)=>a+Number(g.mem_used||0),0),memTotal=arr.reduce((a,g)=>a+Number(g.mem_total||0),0);
    const watts=arr.reduce((a,g)=>a+Number(g.power||0),0),temp=Math.max(...arr.map(g=>Number(g.temp||0)));
    return `<div class="hostblock"><div class="hosttitle"><span>${nbEsc(cfg().nodeLabel?cfg().nodeLabel(host):host)}</span><b>${arr.length} GPU · ${Math.round(util)}%</b></div><div class="bar"><i style="width:${util}%"></i></div><div class="gpuchips">`+
      arr.map(g=>`<div class="gpuchip"><b>${nbEsc((g.name||'GPU').replace('NVIDIA GeForce ',''))}</b><span><em>${g.util}%</em><em>${g.temp}°C</em><em>${(g.mem_used/1024).toFixed(1)}G</em></span></div>`).join('')+
      `</div>`+
      `<div class="sub"><span>host total</span><span>${(memUsed/1024).toFixed(1)}/${(memTotal/1024).toFixed(0)}GB</span><span>${temp}°C max</span><span>${watts.toFixed(0)}W</span></div></div>`;
  }).join('');
  document.getElementById('s-gpu').textContent=(GPU.length?Math.round(GPU.reduce((a,g)=>a+Number(g.util||0),0)/GPU.length):0)+'%';
}catch(e){}}
function aiServerPlanets(servers){
  const out=[];
  (servers||[]).forEach((s,si)=>{
    out.push({id:9000+si,name:`${s.name} · ${s.gpu_count||0} GPUs`,type:'host',status:s.up?'running':'stopped',node:s.name,cat:'ai',cpu:s.gpu_util||0,mem:(s.gpu_mem_used||0)*1048576,maxmem:Math.max((s.gpu_mem_total||1)*1048576,1)});
    (s.gpus||[]).forEach((g,gi)=>out.push({id:9100+si*20+gi,name:(g.name||`GPU ${gi}`).replace('NVIDIA GeForce ',''),type:'gpu',status:'running',node:s.name,cat:'ai',cpu:g.util||0,mem:(g.mem_used||0)*1048576,maxmem:Math.max((g.mem_total||1)*1048576,1)}));
  });
  return out;
}
async function pollAll(){try{const d=await (await fetch('/g3/api/all')).json();if(d.topology&&window.netSceneUpdate){try{window.netSceneUpdate(d.topology);}catch(e){}}if(d.topology){try{renderFlows(d.topology);}catch(e){}}if(window.renderNewPanels){try{window.renderNewPanels(d);}catch(e){}}const allGuests=(d.guests||[]).concat(aiServerPlanets(d.ai_servers));window.GMAP={};allGuests.forEach(g=>GMAP[g.id]=g);document.getElementById('s-run').textContent=(d.running||0)+(d.ai_servers||[]).filter(s=>s.up).length;document.getElementById('s-tot').textContent=allGuests.length;document.getElementById('s-cli').textContent=(d.unifi&&d.unifi.clients)??'–';
  if(PLANETS.filter(p=>p.userData.g).length!==allGuests.length){buildGuests(allGuests);}
  const camList0=cfg().features.cameras?(d.cameras||[]):[];
  if(camScreens.length!==camList0.length)buildCameras(camList0);
  if(d.netmap&&d.netmap.devices){if(d.netmap.devices.length!==netNodes.length)buildNetmap(d.netmap.devices);
    else d.netmap.devices.forEach((dv,i)=>{const nn=netNodes[i];if(nn){nn.up=dv.up;nn.m.material.emissive.setHex(dv.up?0x37f5a0:0xff5a6a);nn.m.userData.netdev=dv;}});
    window.NETMAP=d.netmap;document.getElementById('s-net').textContent=d.netmap.up+'/'+d.netmap.total;}
  try{renderAddons(d);}catch(e){}
  const u=d.unifi||{},z=d.zabbix||{};window.ALERTS=z.alert_hosts||{};
  const _nodes=d.nodes||[],_pools=d.pools||[];
  const hl=d.health||[],upHealth=hl.filter(h=>h.up).length;
  const memTotal=_nodes.reduce((a,n)=>a+Number(n.mem_total||0),0),memUsed=_nodes.reduce((a,n)=>a+Number(n.mem_used||0),0);
  const gpuMem=GPU.reduce((a,g)=>a+Number(g.mem_total||0),0),gpuUsed=GPU.reduce((a,g)=>a+Number(g.mem_used||0),0);
  const aiUp=(d.ai_servers||[]).filter(s=>s.up).length;
  const ops=document.getElementById('opsgrid');
  if(ops)ops.innerHTML=
    `<div class="op"><span>Compute</span><b>${_nodes.length} nodes · ${GPU.length} GPU</b><small>${(memUsed/1.07e9).toFixed(0)}/${(memTotal/1.07e9).toFixed(0)}G RAM</small><div class="meter"><i style="width:${Math.min(100,memUsed/Math.max(memTotal,1)*100)}%"></i></div></div>`+
    `<div class="op"><span>AI Fabric</span><b>${aiUp}/${(d.ai_servers||[]).length||1} online</b><small>${(gpuUsed/1024).toFixed(1)}/${(gpuMem/1024).toFixed(0)}GB VRAM</small><div class="meter"><i style="width:${Math.min(100,gpuUsed/Math.max(gpuMem,1)*100)}%"></i></div></div>`+
    `<div class="op"><span>Network</span><b>${u.clients??'–'} clients</b><small>${u.rx??'–'} / ${u.tx??'–'} Mbps WAN</small><div class="meter"><i style="width:${Math.min(100,Number(u.rx||0)*2)}%"></i></div></div>`+
    `<div class="op"><span>Health</span><b style="color:${z.problems?'#ff5a6a':'#37f5a0'}">${z.problems??0} alerts</b><small>${upHealth}/${hl.length||0} checks up</small><div class="meter"><i style="width:${hl.length?upHealth/hl.length*100:0}%"></i></div></div>`+
    opsTelemetry(d.telemetry||{});
  const storage=d.storage||[],storTotal=storage.reduce((a,s)=>a+Number(s.total||0),0),storUsed=storage.reduce((a,s)=>a+Number(s.used||0),0);
  const pveCpu=_nodes.length?(_nodes.reduce((a,n)=>a+Number(n.cpu||0),0)/_nodes.length):0;
  const zfsBad=_pools.filter(p=>p.health!=='ONLINE'),zfsPct=_pools.length?Math.round(_pools.reduce((a,p)=>a+(Number(p.alloc||0)/Math.max(Number(p.size||1),1)*100),0)/_pools.length):0;
  const aiLine=(d.ai_servers||[]).map(s=>`${nbEsc(s.name)} ${s.ollama_up?'ready':'down'} · ${s.models||0} models`).join(' · ')||'none';
  const sourceBits=[['Zabbix',d.sources&&d.sources.zabbix],['Prom',d.sources&&d.sources.prometheus],['Grafana',d.sources&&d.sources.grafana]]
    .map(([n,o])=>`<span style="color:${o&&o.up?'#37f5a0':'#ff5a6a'}">${o&&o.up?'●':'○'} ${n}</span>`).join(' ');
  document.getElementById('kvs').innerHTML=
    `<div class="kv"><span>UniFi clients / devices</span><b>${u.clients??'–'} / ${u.devices??'–'} (${u.wifi??'?'} wifi)</b></div>`+
    `<div class="kv"><span>WAN down / up</span><b>${u.rx??'–'} / ${u.tx??'–'} Mbps</b></div>`+
    `<div class="kv"><span>WAN link</span><b style="color:${u.wan_up==='ok'?'#37f5a0':'#ff5a6a'}">${(u.wan_up||'?').toUpperCase()} · ${nbEsc(u.isp||cfg().wan_label||'–')} · ${u.latency??'–'}ms</b></div>`+
    `<div class="kv"><span>Zabbix hosts / alerts</span><b style="color:${z.problems?'#ff5a6a':'#37f5a0'}">${z.hosts??'–'} / ${z.problems??0}</b></div>`+
    `<div class="kv"><span>k3s nodes</span><b>${_nodes.length} · avg ${pveCpu.toFixed(1)}% CPU · ${(memUsed/1.07e9).toFixed(0)}/${(memTotal/1.07e9).toFixed(0)}G</b></div>`+
    `<div class="kv"><span>AI GPU server</span><b style="color:${aiUp?'#37f5a0':'#ff5a6a'}">${aiLine}</b></div>`+
    `<div class="kv"><span>ZFS pools</span><b style="color:${zfsBad.length?'#ff5a6a':'#dcefff'}">${_pools.length} pools · ${zfsBad.length?zfsBad.length+' degraded':'ONLINE'} · ${zfsPct}% avg</b></div>`+
    `<div class="kv"><span>Storage</span><b>${storage.length} stores · ${(storUsed/1e12).toFixed(1)}/${(storTotal/1e12).toFixed(1)}TB</b></div>`+
    `<div class="kv" style="grid-column:1/-1;margin-top:2px"><span style="letter-spacing:1px">DATA SOURCES</span><b style="font-weight:400">${sourceBits}</b></div>`;
  const camList=camList0.slice(0,4);
  document.getElementById('cams').innerHTML=camList.map(c=>`<div class="cam"><img src="/g3/cam/${encodeURIComponent(c)}.jpg" id="hud-${nbEsc(c)}"/><span>${nbEsc(c)}</span></div>`).join('');
  const se=document.getElementById('srcs');
  if(se)se.innerHTML='';
  const he=document.getElementById('health');
  if(he&&hl.length){const upn=hl.filter(h=>h.up).length;
    const down=hl.filter(h=>!h.up);
    he.innerHTML=`<div style="color:#7e93ab;font-size:10px;letter-spacing:1px;margin-top:4px">SERVICE HEALTH</div>`+
      `<div class="kv"><span>${upn}/${hl.length} checks up</span><b style="color:${down.length?'#ff5a6a':'#37f5a0'}">${down.length?nbEsc(down.map(h=>h.name).slice(0,2).join(', ')):'ALL GREEN'}</b></div>`;
    // flare planets red for any down service (merge into Zabbix alert map)
    hl.filter(h=>!h.up).forEach(h=>{window.ALERTS[h.name]=window.ALERTS[h.name]||{sev:4,n:1,names:['service down ('+h.code+')']};});}
  const SEVC={5:'#ff2b2b',4:'#ff5a6a',3:'#ffa033',2:'#ffd24a'};const ent=Object.entries(z.alert_hosts||{});
  const ap=document.getElementById('alerts'),al=document.getElementById('alertlist');
  if(ent.length){const sorted=ent.sort((a,b)=>b[1].sev-a[1].sev),shown=sorted.slice(0,6);
    ap.style.display='block';al.innerHTML=shown.map(([h,v])=>`<div class="a"><span class="sv" style="color:${SEVC[v.sev]||'#ffd24a'}"></span><span class="h">${nbEsc(h)}</span><span class="m">${nbEsc((v.names&&v.names[0])||'')}${v.n>1?' +'+(v.n-1):''}</span></div>`).join('')+
      (sorted.length>shown.length?`<div class="more">+${sorted.length-shown.length} more alerts in Zabbix</div>`:'');}
  else{ap.style.display='none';}
}catch(e){}}
function refreshHud(){document.querySelectorAll('#cams img').forEach(im=>im.src='/g3/cam/'+im.id.replace('hud-','')+'.jpg?'+Date.now());}
async function gpuHist(){try{const a=await (await fetch('/g3/api/history?metric=gpu&idx=0&mins=120')).json();const b=await (await fetch('/g3/api/history?metric=gpu&idx=1&mins=120')).json();
  const el=document.getElementById('gpuhist');if(el)el.innerHTML='<div style="color:#7e93ab;font-size:9px;letter-spacing:1px;margin-bottom:2px">UTIL HISTORY · 2h</div>'+spark(a.map(x=>x.util),232,22,'#ff4fa3')+spark(b.map(x=>x.util),232,22,'#5ad7ff');}catch(e){}}
async function netHist(){try{const n=await (await fetch('/g3/api/history?metric=net&mins=120')).json();const el=document.getElementById('nethist');
  if(el&&n.length)el.innerHTML='<div style="color:#7e93ab;font-size:9px;letter-spacing:1px;margin:4px 0 2px">WAN ↓ Mbps · CLIENTS · 2h</div>'+spark(n.map(x=>x.rx),232,18,'#37f5a0')+spark(n.map(x=>x.clients),232,16,'#9b8cff');}catch(e){}}
pollGpu();pollAll();gpuHist();netHist();setInterval(pollGpu,2500);setInterval(pollAll,6000);setInterval(refreshCams,1300);setInterval(refreshHud,1600);setInterval(gpuHist,20000);setInterval(netHist,20000);

function spark(vals,w,h,color){if(!vals||vals.length<2)return '<div style="color:#6d84a0;font-size:9px">collecting…</div>';
  const mn=Math.min(...vals),mx=Math.max(...vals,mn+1),rng=(mx-mn)||1;
  const pts=vals.map((v,i)=>`${(i/(vals.length-1)*w).toFixed(1)},${(h-((v-mn)/rng)*h).toFixed(1)}`).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="display:block;overflow:visible;width:100%;height:${h}px"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>`;}
window.ask=async function(){const i=document.getElementById('msg'),o=document.getElementById('cout');if(!i.value.trim())return;const q=i.value;i.value='';o.textContent='thinking…';try{const r=await (await fetch('/g3/api/chat',{method:'POST',headers:{'Content-Type':'application/json', 'X-Nodeyard': '1'},body:JSON.stringify({message:q})})).json();o.textContent=r.reply;}catch(e){o.textContent='(error)';}};
document.getElementById('msg').addEventListener('keydown',e=>{if(e.key==='Enter')ask();});
const clock=new THREE.Clock();
{const fc=document.getElementById('focuscard');if(fc)fc.style.display='none';
 const te=document.getElementById('tour');if(te)te.style.display='none';}
// This is a monitoring dashboard, not a game, so an uncapped render loop at
// full quality was real waste — but the fix is NOT an arbitrary fps cap. A cap
// that isn't a clean divisor of the display's refresh rate (45fps on a 60Hz
// panel) makes requestAnimationFrame skip a frame every 2-3 ticks unevenly,
// which *looks* like stutter even though the average fps sounds fine. rAF is
// already vsync-locked by the browser, so just render every tick: smooth by
// construction, and per-frame cost (not frame count) is the real lever.
let RENDER_ACTIVE=true,_animRunning=true;
// data-doom is a compound string ("phase=off booted=true capture=live ..."), so it
// is truthy and !== 'off' even when idle — comparing it to 'off' pinned the wall
// rendering forever after the first takeover. Read the phase token instead.
function doomActive(){const d=document.body.dataset.doom;return !!d&&!/(^|\s)phase=off(\s|$)/.test(d);}
function animate(now){
  // Room is empty: stop the rAF chain instead of scheduling another frame.
  // A presence SSE event flips RENDER_ACTIVE back and restarts it. Never
  // pause mid-DOOM-takeover even if presence lags the last keypress.
  if(!RENDER_ACTIVE&&!doomActive()){_animRunning=false;return;}
  requestAnimationFrame(animate);
  const dt=Math.min(clock.getDelta(),.05),t=clock.getElapsedTime();
  if(window.netSceneTick){try{window.netSceneTick(t,dt);}catch(e){console.error(e);window.netSceneTick=null;}}
  ctrl.update();
  // AFTER ctrl.update(): only now is the camera in its final orientation for
  // this frame, so this is the only point at which label collisions and the
  // hover raycast can be decided against what will actually be drawn.
  // a throw here used to vanish silently and leave one frame half-judged (the
  // rank-5 flow labels, processed last, stayed up over a hot callout - seen as a
  // 1-in-60 sample in a label audit). Say so, at most once every 5 s.
  if(window.NETSCENE&&NETSCENE.labelPass){try{NETSCENE.labelPass();}catch(e){NETSCENE._lpExternal=false;if(!window.__lpWarnAt||now-window.__lpWarnAt>5000){window.__lpWarnAt=now;console.warn('labelPass threw:',e&&e.message,e&&e.stack&&e.stack.split('\n')[1]);}}}
  // Shared heartbeat with the nexus (big-screen) view — same beat, same
  // instant, both screens flash together since /api/pulse/stream derives
  // its beat number from wall-clock time, not per-connection state.
  // (bloom itself is owned by cinema.js; the beat is handed over below
  // with CINEMA.beat, and only a fallback touches it here)
  if(!window.CINEMA){beatFlash=Math.max(0,beatFlash-dt*0.8);bloom.strength=BLOOM_BASE+beatFlash*0.9+(beatSev>=3?0.4:0);}
  composer.render();}
let beatFlash=0,beatSev=0;const BLOOM_BASE=bloom.strength;
/* SSE budget: Chrome allows 6 connections per host and every EventSource holds
   one. This page opens pulse (here + showtime.js), presence, takeover (doom.js)
   and mode (showtime.js) = five. Never add a sixth; poll instead. */
(function connectPulse(){
  if(typeof window.EventSource!=='function')return;
  const es=new EventSource('/g3/api/pulse/stream');
  es.onmessage=(ev)=>{try{const d=JSON.parse(ev.data);beatFlash=1;beatSev=d.sev||0;if(window.CINEMA&&CINEMA.beat)CINEMA.beat(beatSev);}catch(e){}};
})();
// Resume where we left off. getDelta() must be flushed or the first frame gets a
// dt of the whole pause, but it also ADDS that gap to Clock.elapsedTime — and the
// tour/shaders key off elapsed time, so the scene fast-forwarded the moment the
// room was occupied again. Restoring elapsedTime makes a pause invisible.
function wakeRender(){if(!_animRunning){_animRunning=true;const keep=clock.elapsedTime;clock.getDelta();clock.elapsedTime=keep;requestAnimationFrame(animate);}}
(function connectPresence(){
  if(typeof window.EventSource!=='function')return;
  const es=new EventSource('/g3/api/presence/stream');
  es.onmessage=(ev)=>{try{
    const d=JSON.parse(ev.data);
    RENDER_ACTIVE=('effective' in d)?d.effective:d.active;
    if(RENDER_ACTIVE)wakeRender();
  }catch(e){}};
})();
// A takeover fired at an empty room finds the loop already stopped, so the
// RENDER_ACTIVE guard in animate() cannot help — it only prevents pausing.
// DOOM's tear/leak entry and unswallow/unleak/untear exit are composited over
// the live scene, so wake it here. 1 Hz, no GPU work while idle.
setInterval(()=>{if(doomActive())wakeRender();},1000);
animate();
addEventListener('resize',()=>{cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();rndr.setSize(innerWidth,innerHeight);composer.setSize(innerWidth,innerHeight);bloom.setSize(innerWidth,innerHeight);});
}

/* ---- boot: config first, then the scene, then the modules in load order ---- */
window.loadConfig().then(function(C){
  applyConfig(C);
  bootScene();
  window.loadScripts([
    '/g3/static/netscene.js?v=1',
    '/g3/static/netscene_flows.js?v=1',
    '/g3/static/netscene_exo.js?v=1',
    '/g3/static/galaxy.js?v=1',
    '/g3/static/lightsync.js?v=1',
    '/g3/static/cinema.js?v=1',
    '/g3/static/doom.js?v=1',
    '/g3/static/hero.js?v=1',
    '/g3/static/showtime.js?v=1'
  ]);
});

document.getElementById("chat-send").addEventListener("click", function () { ask(); });
