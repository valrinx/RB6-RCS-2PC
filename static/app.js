document.addEventListener('DOMContentLoaded', async () => {
const $ = id => document.getElementById(id);

// ── Toast ─────────────────────────────────────────────────────────────────────
let _toastTimer;
function toast(msg, color='var(--ac)') {
  const t=$('toast'); t.textContent=msg; t.style.color=color;
  t.classList.add('show'); clearTimeout(_toastTimer);
  _toastTimer = setTimeout(()=>t.classList.remove('show'), 2200);
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
let ws;
function connectWs() {
  const p = location.protocol==='https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(p+'//'+location.host+'/ws');
  ws.onopen = sendAll;
  ws.onclose = ()=>setTimeout(connectWs, 2000);
}
connectWs();

function sendAll() {
  if (ws && ws.readyState===1) ws.send(JSON.stringify({
    pull_down:              +$('sv').value  ||0,
    horizontal:             +$('hv').value  ||0,
    horizontal_delay_ms:    +$('dv').value  ||0,
    horizontal_duration_ms: +$('uv').value  ||0,
    vertical_delay_ms:      +$('vdv').value ||0,
    vertical_duration_ms:   +$('vduv').value||0,
    jitter_strength:        +$('js').value  ||0,
    smooth_factor:          +$('ss').value  ||0,
  }));
}

function safeNum(v, fb=0) { const n=parseFloat(v); return isNaN(n)?fb:n; }

function sync(r, i, cb) {
  r.oninput = ()=>{ i.value=r.value; if(cb)cb(); sendAll(); };
  i.oninput = ()=>{ const v=safeNum(i.value); r.value=v; i.value=v; if(cb)cb(); sendAll(); };
}
sync($('sl'),$('sv'),drawCurve); sync($('hs'),$('hv'));
sync($('ds'),$('dv')); sync($('us'),$('uv'));
sync($('vds'),$('vdv')); sync($('vdus'),$('vduv'));
$('js').oninput=()=>{ $('jv').textContent=parseFloat($('js').value).toFixed(2); sendAll(); };
$('ss').oninput=()=>{ $('sv2').textContent=parseFloat($('ss').value).toFixed(2); sendAll(); };

// ── BUG FIX: rapid fire slider/input sync — send interval as soon as value changes ──
const rfMs=$('rf-ms'), rfSl=$('rf-sl');

function sendRFInterval() {
  // Always send interval to server (whether rfEnabled is true or false)
  // so the server has the latest value when RF is enabled
  fetch('/rapid-fire',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:rfEnabled, interval_ms:safeNum(rfMs.value,100)})
  }).catch(()=>{});
}

// BUG FIX: use 'input' not 'change' — 'change' fires only after blur
rfSl.oninput = ()=>{
  rfMs.value = rfSl.value;
  sendRFInterval();
};
rfMs.oninput = ()=>{
  const v = safeNum(rfMs.value, 100);
  rfSl.value = Math.min(v, 500);
  sendRFInterval();
};

// ── Tabs ──────────────────────────────────────────────────────────────────────
const TABS=['recoil','humanize','settings'];
function switchTab(name) {
  document.querySelectorAll('.tab,.tc').forEach(e=>e.classList.remove('active'));
  document.querySelector(`.tab[data-tab="${name}"]`).classList.add('active');
  $('tab-'+name).classList.add('active');
}
document.querySelectorAll('.tab').forEach(t=>{ t.onclick=()=>switchTab(t.dataset.tab); });
document.addEventListener('keydown', e=>{
  if(e.altKey && e.key>='1' && e.key<='3'){ e.preventDefault(); switchTab(TABS[+e.key-1]); }
});

// ── Conn dot ──────────────────────────────────────────────────────────────────
function setConnDot(ok) {
  [$('conn-dot'),$('conn-dot2')].forEach(d=>{
    d.classList.toggle('ok',ok); d.classList.toggle('bad',!ok);
    d.title = ok ? 'Connected' : 'Disconnected';
  });
}

// ── Status ────────────────────────────────────────────────────────────────────
function setBtn(on) {
  $('toggle-btn').textContent = on ? '■ ON' : '○ OFF';
  $('toggle-btn').className   = on ? 'enabled' : 'disabled';
}

$('toggle-btn').onclick = ()=>
  fetch('/toggle',{method:'POST'}).then(r=>r.json()).then(d=>setBtn(d.is_enabled));

$('tbs').onchange = ()=>
  fetch('/toggle-button',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({button:$('tbs').value})});

// ── Beep toggle ───────────────────────────────────────────────────────────────
let beepEnabled = true;
function updateBeepBtn() {
  const b = $('beep-btn');
  b.textContent  = beepEnabled ? '🔔' : '🔕';
  b.style.color  = beepEnabled ? 'var(--ac)' : 'var(--mu)';
  b.style.borderColor = beepEnabled ? '#1a5535' : 'var(--bd2)';
}
$('beep-btn').onclick = ()=>{
  beepEnabled = !beepEnabled;
  updateBeepBtn();
  fetch('/beep',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:beepEnabled})});
};

// ── BUG FIX: Rapid Fire pill — send interval_ms on every pill click ───────────────
let rfEnabled = false;
function syncRF() {
  rfEnabled = !rfEnabled;
  $('rf-pill').classList.toggle('on', rfEnabled);
  $('rf-lbl').textContent = rfEnabled ? 'ON' : 'OFF';
  // BUG FIX: send interval_ms from current input every time (not default 100)
  fetch('/rapid-fire',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:rfEnabled, interval_ms:safeNum(rfMs.value,100)})
  }).then(r=>r.json()).then(d=>{
    if(d.rapid_fire_enabled) toast('⚡ Rapid Fire ON  '+d.rapid_fire_interval_ms+'ms','var(--or)');
    else toast('Rapid Fire OFF','var(--mu)');
  });
}
$('rf-pill').onclick = syncRF;

// ── BUG FIX: Hip Fire — always send values even when pill is off (persist) ───────
let hfEnabled = false;
function sendHF() {
  // BUG FIX: always send; do not gate on hfEnabled
  fetch('/hip-fire',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:hfEnabled, pull_down:safeNum($('hf-pd').value), horizontal:safeNum($('hf-hz').value)})
  }).catch(()=>{});
}
$('hf-pill').onclick = ()=>{
  hfEnabled = !hfEnabled;
  $('hf-pill').classList.toggle('on', hfEnabled);
  $('hf-lbl').textContent = hfEnabled ? 'ON' : 'OFF';
  sendHF();
  toast(hfEnabled ? '🎯 Hip Fire ON' : 'Hip Fire OFF', hfEnabled ? 'var(--vi)' : 'var(--mu)');
};
// BUG FIX: send on every change (enabled or not)
$('hf-pd').oninput = sendHF;
$('hf-hz').oninput = sendHF;

// BUG FIX: stop getStatus() overwriting fields the user is editing
// and sync pull_down/horizontal into UI on first init only
let _statusInitDone = false;
let _lastFocusedInput = null;
let _lastConfigLoad = 0;  // timestamp of last config load from browse
let _statusBusy = false;
document.querySelectorAll('input').forEach(el=>{
  el.addEventListener('focus', ()=>{ _lastFocusedInput = el.id; });
  el.addEventListener('blur',  ()=>{ setTimeout(()=>{ if(_lastFocusedInput===el.id) _lastFocusedInput=null; }, 300); });
});

function getStatus() {
  if (_statusBusy) return;
  _statusBusy = true;
  fetch('/status').then(r=>r.json()).then(d=>{
    setBtn(d.is_enabled);
    if(d.toggle_button) $('tbs').value=d.toggle_button;
    if(d.trigger_mode)  $('trig').value=d.trigger_mode;
    if(d.controller_type){ $('ctrl').value=d.controller_type; ctrlUI(d.controller_type,d.ctrl_connected); }
    if(d.current_config_file) $('cfg-badge').textContent=d.current_config_file.replace('.json','');
    if(d.jitter_strength!==undefined){ $('js').value=d.jitter_strength; $('jv').textContent=(+d.jitter_strength).toFixed(2); }
    if(d.smooth_factor  !==undefined){ $('ss').value=d.smooth_factor;   $('sv2').textContent=(+d.smooth_factor).toFixed(2); }
    $('cpv').style.display = d.has_pull_curve  ? 'inline-flex':'none';
    $('cph').style.display = d.has_horiz_curve ? 'inline-flex':'none';
    if(d.kmbox_ip && !$('km-ip').value){ $('km-ip').value=d.kmbox_ip; $('km-port').value=d.kmbox_port; }
    setConnDot(!!d.ctrl_connected);
    onStatusUpdate(d);

    // BUG FIX: sync pull_down/horizontal on first init only
    // do not overwrite every poll or loaded configs get reset
    if(!_statusInitDone) {
      if(d.pull_down  !==undefined){ $('sv').value=d.pull_down;   $('sl').value=Math.round(d.pull_down); }
      if(d.horizontal !==undefined){ $('hv').value=d.horizontal;  $('hs').value=Math.round(d.horizontal); }
      if(d.horizontal_delay_ms   !==undefined){ $('dv').value=d.horizontal_delay_ms;   $('ds').value=d.horizontal_delay_ms; }
      if(d.horizontal_duration_ms!==undefined){ $('uv').value=d.horizontal_duration_ms;$('us').value=d.horizontal_duration_ms; }
      if(d.vertical_delay_ms     !==undefined){ $('vdv').value=d.vertical_delay_ms;    $('vds').value=d.vertical_delay_ms; }
      if(d.vertical_duration_ms  !==undefined){ $('vduv').value=d.vertical_duration_ms;$('vdus').value=d.vertical_duration_ms; }
      _statusInitDone = true;
    }

    // Rapid fire sync — only when state differs (not while user is editing)
    if(d.rapid_fire_enabled!==undefined && d.rapid_fire_enabled!==rfEnabled){
      rfEnabled=d.rapid_fire_enabled;
      $('rf-pill').classList.toggle('on',rfEnabled); $('rf-lbl').textContent=rfEnabled?'ON':'OFF';
    }
    // BUG FIX: sync interval on init or when rf-ms is not focused
    if(d.rapid_fire_interval_ms && _lastFocusedInput!=='rf-ms') {
      rfMs.value=d.rapid_fire_interval_ms;
      rfSl.value=Math.min(d.rapid_fire_interval_ms,500);
    }

    // Hip fire pill state sync
    if(d.hip_fire_enabled!==undefined && d.hip_fire_enabled!==hfEnabled){
      hfEnabled=d.hip_fire_enabled;
      $('hf-pill').classList.toggle('on',hfEnabled); $('hf-lbl').textContent=hfEnabled?'ON':'OFF';
    }
    // FIX BUG 2: do not overwrite hf-pd/hf-hz within 3s after loading a config
    // getStatus may return stale server state before WS/REST updates land
    const now2 = Date.now();
    if(now2 - _lastConfigLoad > 3000) {
      if(d.hip_pull_down  !==undefined && _lastFocusedInput!=='hf-pd') $('hf-pd').value=d.hip_pull_down;
      if(d.hip_horizontal !==undefined && _lastFocusedInput!=='hf-hz') $('hf-hz').value=d.hip_horizontal;
    }
    // beep sync
    if(d.beep_enabled!==undefined && d.beep_enabled!==beepEnabled){
      beepEnabled=d.beep_enabled; updateBeepBtn();
    }
    // Weapon slot state sync
    if(d.weapon_slot_enabled!==undefined && d.weapon_slot_enabled!==wsEnabled){
      wsEnabled=d.weapon_slot_enabled;
      $('ws-pill').classList.toggle('on',wsEnabled);
      $('ws-lbl').textContent=wsEnabled?'ON':'OFF';
    }
    if(d.active_slot!==undefined){
      const prev=$('ws-active-num').textContent;
      const next=d.active_slot>0?String(d.active_slot):'—';
      $('ws-active-num').textContent=next;
      // If slot changed, sync recoil UI values from app_state (server already applied them)
      if(prev!==next && d.active_slot>0){
        if(d.pull_down  !==undefined){ $('sv').value=d.pull_down;   $('sl').value=Math.round(d.pull_down); }
        if(d.horizontal !==undefined){ $('hv').value=d.horizontal;  $('hs').value=Math.round(d.horizontal); }
        if(d.horizontal_delay_ms   !==undefined){ $('dv').value=d.horizontal_delay_ms;   $('ds').value=d.horizontal_delay_ms; }
        if(d.horizontal_duration_ms!==undefined){ $('uv').value=d.horizontal_duration_ms;$('us').value=d.horizontal_duration_ms; }
        if(d.vertical_delay_ms     !==undefined){ $('vdv').value=d.vertical_delay_ms;    $('vds').value=d.vertical_delay_ms; }
        if(d.vertical_duration_ms  !==undefined){ $('vduv').value=d.vertical_duration_ms;$('vdus').value=d.vertical_duration_ms; }
        $('cpv').style.display=d.has_pull_curve ?'inline-flex':'none';
        $('cph').style.display=d.has_horiz_curve?'inline-flex':'none';
        // Highlight active slot config in Browse list
        if(d.active_slot_config){ $('cfgdd').value=d.active_slot_config; updBrowseLbl(); }
        drawCurve();
      }
    }
  }).catch(()=>{}).finally(()=>{ _statusBusy = false; });
}
getStatus();
// OPT v8.3: adaptive polling — 800ms when visible, paused when tab hidden
let _statusTimer = setInterval(getStatus, 800);
document.addEventListener('visibilitychange', ()=>{
  clearInterval(_statusTimer);
  if (!document.hidden) {
    getStatus();  // immediate refresh on tab focus
    _statusTimer = setInterval(getStatus, 800);
  }
});

// ── Weapon Slots ──────────────────────────────────────────────────────────────
let wsEnabled = false;
let wsSlots   = {1:null,2:null,3:null,4:null,5:null};
let wsSlotRf  = {1:null,2:null,3:null,4:null,5:null};

function buildWsGrid() {
  const grid = $('ws-slots-grid');
  grid.innerHTML = '';

  // Remove old datalist if any
  let dl = document.getElementById('ws-datalist');
  if(dl) dl.remove();

  // Inject custom dropdown styles once
  if(!document.getElementById('ws-dd-style')){
    const st = document.createElement('style');
    st.id = 'ws-dd-style';
    st.textContent = `
      .ws-dd-wrap { position:relative; margin-bottom:7px; }
      .ws-dd-input {
        width:100%; box-sizing:border-box;
        font-size:.72rem; padding:5px 26px 5px 8px;
        background:var(--bg); border:1px solid var(--bd2);
        border-radius:6px; color:var(--tx);
        font-family:var(--sa); outline:none;
        cursor:pointer; white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis;
        transition:border-color .15s;
      }
      .ws-dd-input:focus { border-color:var(--ac); }
      .ws-dd-arrow {
        position:absolute; right:7px; top:50%;
        transform:translateY(-50%);
        pointer-events:none; color:var(--mu);
        font-size:.6rem; line-height:1;
      }
      .ws-dd-clr {
        position:absolute; right:20px; top:50%;
        transform:translateY(-50%);
        background:transparent; border:none;
        color:var(--mu); font-size:.85rem;
        cursor:pointer; padding:0 2px; line-height:1;
        display:none;
      }
      .ws-dd-clr.visible { display:block; }
      .ws-dd-list {
        position:absolute; top:calc(100% + 3px); left:0; right:0;
        background:#1a1a1f; border:1px solid var(--ac);
        border-radius:7px; z-index:9999;
        max-height:220px; overflow-y:auto;
        box-shadow:0 6px 24px rgba(0,0,0,.6);
        display:none; flex-direction:column;
        scrollbar-width:thin; scrollbar-color:var(--bd2) transparent;
      }
      .ws-dd-list.open { display:flex; }
      .ws-dd-search {
        padding:6px 8px; font-size:.7rem;
        background:transparent; border:none;
        border-bottom:1px solid var(--bd2);
        color:var(--tx); font-family:var(--sa);
        outline:none; position:sticky; top:0;
        background:#1a1a1f;
      }
      .ws-dd-items { overflow-y:auto; flex:1; }
      .ws-dd-item {
        padding:5px 10px; font-size:.72rem;
        font-family:var(--sa); color:var(--tx);
        cursor:pointer; white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis;
        transition:background .1s;
      }
      .ws-dd-item:hover, .ws-dd-item.active { background:rgba(255,255,255,.07); color:var(--ac); }
      .ws-dd-item.selected { color:var(--ac); font-weight:600; }
      .ws-dd-empty { padding:8px 10px; font-size:.68rem; color:var(--mu); font-family:var(--sa); }
    `;
    document.head.appendChild(st);
  }

  // Helper: resolve display name → key
  function nameToKey(name){
    if(!name) return null;
    const lower = name.toLowerCase();
    if(typeof allKeys!=='undefined'){
      for(const k of allKeys){
        const cfg=cache[k];
        const n=typeof cfg==='object'&&cfg.name ? cfg.name : k;
        if(n.toLowerCase()===lower) return k;
      }
      if(allKeys.includes(name)) return name;
    }
    return null;
  }

  // Helper: build weapon name list
  function getWeaponList(filter=''){
    if(typeof allKeys==='undefined') return [];
    const q = filter.toLowerCase();
    return allKeys
      .map(k=>{ const cfg=cache[k]; return {key:k, name:typeof cfg==='object'&&cfg.name?cfg.name:k}; })
      .filter(w=>!q||w.name.toLowerCase().includes(q));
  }

  // Close all open dropdowns
  function closeAllDd(){
    document.querySelectorAll('.ws-dd-list.open').forEach(el=>el.classList.remove('open'));
  }
  document.removeEventListener('click', window._wsDdClose||null);
  window._wsDdClose = (e)=>{ if(!e.target.closest('.ws-dd-wrap')) closeAllDd(); };
  document.addEventListener('click', window._wsDdClose);

  for(let s=1;s<=5;s++){
    const wrap = document.createElement('div');
    wrap.style.cssText='background:var(--bg);border:1px solid var(--bd2);border-radius:7px;padding:8px 10px;';

    // Slot label
    const lbl = document.createElement('div');
    lbl.style.cssText='font-family:var(--mo);font-size:.58rem;color:var(--mu);margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px;';
    lbl.textContent='Slot '+s+' [key '+s+']';

    // ── Custom dropdown ───────────────────────────────────────────────────────
    const ddWrap = document.createElement('div');
    ddWrap.className = 'ws-dd-wrap';

    const inp = document.createElement('div');
    inp.id = 'ws-inp-'+s;
    inp.className = 'ws-dd-input';
    inp.tabIndex = 0;

    const arrow = document.createElement('span');
    arrow.className = 'ws-dd-arrow';
    arrow.textContent = '▾';

    const clrBtn = document.createElement('button');
    clrBtn.className = 'ws-dd-clr';
    clrBtn.textContent = '×';
    clrBtn.title = 'Clear slot';

    // Dropdown panel
    const ddList = document.createElement('div');
    ddList.className = 'ws-dd-list';

    const ddSearch = document.createElement('input');
    ddSearch.type = 'text';
    ddSearch.className = 'ws-dd-search';
    ddSearch.placeholder = '🔍  search…';
    ddSearch.autocomplete = 'off';

    const ddItems = document.createElement('div');
    ddItems.className = 'ws-dd-items';

    ddList.appendChild(ddSearch);
    ddList.appendChild(ddItems);

    // Pre-fill
    const assignedKey = wsSlots[s];
    let currentKey = assignedKey || null;
    if(assignedKey){
      const cfg=cache[assignedKey];
      inp.textContent = typeof cfg==='object'&&cfg.name ? cfg.name : assignedKey;
      inp.style.color = 'var(--tx)';
      clrBtn.classList.add('visible');
    } else {
      inp.textContent = 'Search weapon name…';
      inp.style.color = 'var(--mu)';
    }

    function renderItems(filter=''){
      ddItems.innerHTML='';
      const list = getWeaponList(filter);
      if(!list.length){
        const empty = document.createElement('div');
        empty.className='ws-dd-empty';
        empty.textContent='No weapons found';
        ddItems.appendChild(empty);
        return;
      }
      list.forEach(w=>{
        const item = document.createElement('div');
        item.className='ws-dd-item'+(w.key===currentKey?' selected':'');
        item.textContent = w.name;
        item.title = w.name;
        item.onmousedown=(e)=>{
          e.preventDefault();
          currentKey = w.key;
          inp.textContent = w.name;
          inp.style.color = 'var(--tx)';
          clrBtn.classList.add('visible');
          ddList.classList.remove('open');
          assignSlot(s, w.key);
        };
        ddItems.appendChild(item);
      });
    }

    inp.onclick=(e)=>{
      e.stopPropagation();
      const isOpen = ddList.classList.contains('open');
      closeAllDd();
      if(!isOpen){
        ddSearch.value='';
        renderItems('');
        ddList.classList.add('open');
        setTimeout(()=>ddSearch.focus(),30);
      }
    };
    inp.onkeydown=(e)=>{ if(e.key==='Enter'||e.key===' '){ inp.onclick(e); } };

    ddSearch.oninput=()=>renderItems(ddSearch.value);
    ddSearch.onkeydown=(e)=>{
      if(e.key==='Escape'){ ddList.classList.remove('open'); inp.focus(); }
    };

    clrBtn.onclick=(e)=>{
      e.stopPropagation();
      currentKey=null;
      inp.textContent='Search weapon name…';
      inp.style.color='var(--mu)';
      clrBtn.classList.remove('visible');
      closeAllDd();
      assignSlot(s,null);
    };

    ddWrap.appendChild(inp);
    ddWrap.appendChild(clrBtn);
    ddWrap.appendChild(arrow);
    ddWrap.appendChild(ddList);

    // Rapid Fire row
    const rfRow = document.createElement('div');
    rfRow.style.cssText='display:flex;align-items:center;gap:6px;margin-top:2px;';

    const rfChk = document.createElement('input');
    rfChk.type='checkbox';
    rfChk.id='ws-rf-en-'+s;
    const slotRf = wsSlotRf[s];
    rfChk.checked = slotRf ? slotRf.enabled : false;
    rfChk.title = 'Enable Rapid Fire for this slot';

    const rfLbl = document.createElement('label');
    rfLbl.htmlFor='ws-rf-en-'+s;
    rfLbl.style.cssText='font-family:var(--mo);font-size:.6rem;color:var(--or);cursor:pointer;user-select:none;';
    rfLbl.textContent='⚡ RF';

    const rfMs = document.createElement('input');
    rfMs.type='number';
    rfMs.id='ws-rf-ms-'+s;
    rfMs.min=30; rfMs.max=2000;
    rfMs.value = slotRf ? slotRf.interval_ms : 100;
    rfMs.style.cssText='width:58px;font-size:.68rem;padding:2px 5px;font-family:var(--mo);background:var(--sf);border:1px solid var(--bd2);border-radius:4px;color:var(--tx);';
    rfMs.title='Rapid Fire interval (ms)';

    const rfUnit = document.createElement('span');
    rfUnit.style.cssText='font-family:var(--mo);font-size:.6rem;color:var(--mu);';
    rfUnit.textContent='ms';

    const rfClear = document.createElement('button');
    rfClear.style.cssText='margin-left:auto;font-family:var(--mo);font-size:.58rem;color:var(--mu);background:transparent;border:1px solid var(--bd2);border-radius:4px;padding:2px 6px;cursor:pointer;';
    rfClear.textContent='inherit';
    rfClear.title='Inherit global Rapid Fire setting';
    rfClear.onclick=()=>{ rfChk.checked=false; assignSlotRf(s,null,null); };

    const saveRf=()=>assignSlotRf(s, rfChk.checked, parseInt(rfMs.value)||100);
    rfChk.onchange=saveRf;
    rfMs.onchange=saveRf;

    rfRow.appendChild(rfChk);
    rfRow.appendChild(rfLbl);
    rfRow.appendChild(rfMs);
    rfRow.appendChild(rfUnit);
    rfRow.appendChild(rfClear);

    wrap.appendChild(lbl);
    wrap.appendChild(ddWrap);
    wrap.appendChild(rfRow);
    grid.appendChild(wrap);
  }
}

function assignSlot(slot, configName){
  fetch('/weapon-slots/assign',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({slot,config_name:configName||null})})
  .then(r=>r.json()).then(d=>{
    wsSlots=d.slots;
    toast('Slot '+slot+': '+(configName||'cleared'),'var(--ac)');
  }).catch(()=>{});
}

function assignSlotRf(slot, enabled, interval_ms){
  fetch('/weapon-slots/assign-rf',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({slot, enabled, interval_ms})})
  .then(r=>r.json()).then(d=>{
    wsSlotRf=d.slot_rf;
    if(enabled===null) toast('Slot '+slot+' RF: inherit global','var(--mu)');
    else toast('Slot '+slot+' RF: '+(enabled?'ON '+interval_ms+'ms':'OFF'),'var(--or)');
  }).catch(()=>{});
}

function fetchWsSlots(){
  fetch('/weapon-slots').then(r=>r.json()).then(d=>{
    wsEnabled=d.enabled;
    wsSlots=d.slots;
    wsSlotRf=d.slot_rf||{1:null,2:null,3:null,4:null,5:null};
    $('ws-pill').classList.toggle('on',wsEnabled);
    $('ws-lbl').textContent=wsEnabled?'ON':'OFF';
    $('ws-active-num').textContent=d.active_slot>0?d.active_slot:'—';
    buildWsGrid();
  }).catch(()=>{});
}

$('ws-pill').onclick=()=>{
  wsEnabled=!wsEnabled;
  $('ws-pill').classList.toggle('on',wsEnabled);
  $('ws-lbl').textContent=wsEnabled?'ON':'OFF';
  fetch('/weapon-slots/enabled',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:wsEnabled})})
  .then(()=>toast(wsEnabled?'🔫 Weapon Slots ON':'Weapon Slots OFF',wsEnabled?'var(--ac)':'var(--mu)'));
};

// ── Controller ────────────────────────────────────────────────────────────────
function ctrlUI(ct, connected) {
  $('kmbox-card').style.display = ct==='kmbox'    ? 'block':'none';
  $('sw-card').style.display    = ct==='software' ? 'block':'none';
  const M={makcu:['mk','MAKCU 2-PC'],kmbox:['km','KMBox / kmNet'],software:['sw','Software 1-PC']};
  const [cls,txt]=M[ct]||['mk','—'];
  $('cbw').innerHTML=`<span class="cbadge ${cls}">${txt}</span>`;
  if(ct==='software'){
    $('sw-status').textContent=connected?'✓ Ready — SendInput active':'✗ Not Ready (Windows only)';
    $('sw-status').style.color=connected?'var(--ac)':'var(--rd)';
  }
}
$('ctrl').onchange=()=>{
  const ct=$('ctrl').value;
  fetch('/controller-type',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({controller:ct})})
    .then(()=>ctrlUI(ct,false));
};
$('trig').onchange=()=>
  fetch('/trigger-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:$('trig').value})});

const kmsg=(t,c)=>{$('km-msg').textContent=t;$('km-msg').style.color=c||'var(--mu)';};
$('km-save').onclick=()=>{
  const ip=$('km-ip').value.trim(),port=+$('km-port').value||57856,uuid=$('km-uuid').value.trim().replace(/-/g,'').replace(/ /g,'');
  if(!ip){kmsg('Enter IP','var(--rd)');return;}
  if(uuid.length<8){kmsg('Enter UUID (e.g. 4BD95C53)','var(--rd)');return;}
  fetch('/kmbox-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ip,port,uuid})})
    .then(()=>kmsg('✓ Saved','var(--ac)')).catch(()=>kmsg('Failed','var(--rd)'));
};
$('km-conn').onclick=()=>{
  kmsg('Connecting...');
  fetch('/kmbox-connect',{method:'POST'}).then(r=>r.json())
    .then(d=>kmsg(d.connected?'✓ Connected':'✗ '+d.message,d.connected?'var(--ac)':'var(--rd)'))
    .catch(()=>kmsg('Connection failed','var(--rd)'));
};

// ── Curve editor ──────────────────────────────────────────────────────────────
const cv=$('curve-canvas'),ctx=cv.getContext('2d');
let pts=[],drawing=false;
function resize(){
  const r=cv.getBoundingClientRect();
  const w=Math.floor(r.width)||cv.offsetWidth||320;
  if(cv.width!==w){cv.width=w;cv.height=120;drawCurve();}
}
new ResizeObserver(resize).observe(cv);
requestAnimationFrame(resize);

function drawCurve(){
  const W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#080b0f';ctx.fillRect(0,0,W,H);
  ctx.strokeStyle='#191e28';ctx.lineWidth=1;
  [1,2,3].forEach(i=>{
    ctx.beginPath();ctx.moveTo(0,H*i/4);ctx.lineTo(W,H*i/4);ctx.stroke();
    ctx.beginPath();ctx.moveTo(W*i/4,0);ctx.lineTo(W*i/4,H);ctx.stroke();
  });
  const refY=1-Math.min((safeNum($('sv').value)/300),1);
  ctx.strokeStyle='rgba(50,160,80,.5)';ctx.lineWidth=1.5;ctx.setLineDash([5,5]);
  ctx.beginPath();ctx.moveTo(0,refY*H);ctx.lineTo(W,refY*H);ctx.stroke();
  ctx.setLineDash([]);
  if(pts.length<2){$('curve-pts').textContent='0 pts';return;}
  const sorted=[...pts].sort((a,b)=>a.x-b.x);
  ctx.beginPath();ctx.moveTo(sorted[0].x*W,H);
  sorted.forEach(p=>ctx.lineTo(p.x*W,p.y*H));
  ctx.lineTo(sorted[sorted.length-1].x*W,H);ctx.closePath();
  ctx.fillStyle='rgba(91,240,160,.05)';ctx.fill();
  ctx.beginPath();ctx.strokeStyle='#5bf0a0';ctx.lineWidth=2;
  sorted.forEach((p,i)=>{const x=p.x*W,y=p.y*H;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
  ctx.stroke();
  $('curve-pts').textContent=pts.length+' pts';
  // Keep LIVE PREVIEW in sync with editor changes while idle.
  if(!vizFiring && typeof scheduleVizStatic==='function') scheduleVizStatic();
}

function normM(e){const r=cv.getBoundingClientRect();return{x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))};}
function normT(e){const r=cv.getBoundingClientRect(),t=e.touches[0];return{x:Math.max(0,Math.min(1,(t.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(t.clientY-r.top)/r.height))};}
cv.addEventListener('mousedown',e=>{e.preventDefault();drawing=true;pts=[normM(e)];drawCurve();});
cv.addEventListener('mousemove',e=>{if(!drawing)return;const p=normM(e),l=pts[pts.length-1];if(Math.abs(p.x-l.x)>.007||Math.abs(p.y-l.y)>.007){pts.push(p);drawCurve();}});
cv.addEventListener('mouseup',()=>{drawing=false;});
cv.addEventListener('mouseleave',()=>{drawing=false;});
cv.addEventListener('touchstart',e=>{e.preventDefault();drawing=true;pts=[normT(e)];drawCurve();},{passive:false});
cv.addEventListener('touchmove',e=>{e.preventDefault();if(!drawing)return;const p=normT(e),l=pts[pts.length-1];if(Math.abs(p.x-l.x)>.007||Math.abs(p.y-l.y)>.007){pts.push(p);drawCurve();}},{passive:false});
cv.addEventListener('touchend',()=>{drawing=false;});

$('curve-load-btn').onclick=()=>{
  const v=Math.max(0,Math.min(300,safeNum($('sv').value))),N=40;
  pts=Array.from({length:N},(_,i)=>{const t=i/(N-1);const decay=Math.exp(-t*1.6)*0.45+0.55;return{x:t,y:1-Math.min(v*decay/300,1)};});
  drawCurve();
};
// Flat — horizontal curve at same level as constant (dashed ref), apply immediately
$('curve-flat-btn').onclick=()=>{
  const v=Math.max(0,Math.min(300,safeNum($('sv').value)));
  const flatY=1-Math.min(v/300,1);
  pts=[{x:0,y:flatY},{x:0.25,y:flatY},{x:0.5,y:flatY},{x:0.75,y:flatY},{x:1,y:flatY}];
  drawCurve();
  const c=getCurve();
  if(c&&ws&&ws.readyState===1)ws.send(JSON.stringify({pull_down_curve:c}));
  $('cpv').style.display='inline-flex';
  toast('✓ Flat curve applied');
};
$('curve-apply-btn').onclick=()=>{
  const c=getCurve();if(!c){showWarn('Draw a curve first');return;}
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({pull_down_curve:c}));
  $('cpv').style.display='inline-flex';toast('✓ Curve applied');
  const b=$('curve-apply-btn');b.textContent='Applied ✓';setTimeout(()=>{b.textContent='Apply';},1600);
};
$('curve-clear-btn').onclick=()=>{
  pts=[];drawCurve();$('cpv').style.display='none';
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({pull_down_curve:[]}));
  toast('Curve cleared','var(--mu)');
};
function restoreCurve(arr){
  if(!arr||arr.length<2){pts=[];drawCurve();return;}
  const N=arr.length;pts=arr.map((v,i)=>({x:i/(N-1),y:1-Math.min(v/300,1)}));drawCurve();
}
function getCurve(){
  if(pts.length<2)return null;
  return[...pts].sort((a,b)=>a.x-b.x).map(p=>parseFloat(((1-p.y)*300).toFixed(2)));
}

// ── Preset tag buttons ────────────────────────────────────────────────────────
let currentTags = {};

document.querySelectorAll('.pbtn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const k=btn.dataset.k, v=btn.dataset.v;
    currentTags[k]=v;
    btn.closest('.preset-row').querySelectorAll('.pbtn').forEach(b=>{
      b.style.opacity = (b.dataset.v===v) ? '1' : '0.45';
      b.style.borderWidth = (b.dataset.v===v) ? '2px' : '1px';
    });
    renderTagChips(); updSavePreview();
  });
});

function renderTagChips(){
  const c=$('tag-chips');c.innerHTML='';
  Object.entries(currentTags).forEach(([k,v])=>{
    const chip=document.createElement('span');
    chip.className='tag-chip';
    chip.innerHTML=`<span style="color:var(--bl)">${k}:</span><span>${v}</span><span class="rm" data-k="${k}">×</span>`;
    chip.querySelector('.rm').onclick=e=>{
      delete currentTags[e.target.dataset.k];
      document.querySelectorAll(`.pbtn[data-k="${e.target.dataset.k}"]`).forEach(b=>{ b.style.opacity='1'; b.style.borderWidth='1px'; });
      renderTagChips(); updSavePreview();
    };
    c.appendChild(chip);
  });
}

$('add-tag-btn').onclick=()=>{
  const k=$('tag-key').value.trim(),v=$('tag-val').value.trim();
  if(!k||!v){showWarn('Enter both key and value');return;}
  currentTags[k]=v;renderTagChips();updSavePreview();
  $('tag-key').value='';$('tag-val').value='';
};
$('tag-key').addEventListener('keydown',e=>{if(e.key==='Tab'&&$('tag-key').value.trim()){e.preventDefault();$('tag-val').focus();}});
$('tag-val').addEventListener('keydown',e=>{if(e.key==='Enter')$('add-tag-btn').click();});

function updSavePreview(){
  const name=$('cfg-name').value.trim(),pre=$('sp-preview');
  if(!name){pre.textContent='Enter a name first…';pre.className='sp-preview';return;}
  let txt='"'+name+'"';
  if(Object.keys(currentTags).length>0)txt+='  '+Object.entries(currentTags).map(([k,v])=>`[${k}:${v}]`).join(' ');
  const vv=safeNum($('sv').value),hh=safeNum($('hv').value);
  txt+=`  ↓${vv}`;if(hh!==0)txt+=`  ←→${hh}`;
  pre.textContent=txt;pre.className='sp-preview ready';
}
$('cfg-name').oninput=updSavePreview;

// ── Config system ─────────────────────────────────────────────────────────────
let cache={},allKeys=[],activeTagFilters=new Set();

function fetchConfigs(){
  return fetch('/configs').then(r=>r.json()).then(d=>{
    cache=d;allKeys=Object.keys(d);buildTagFilters();filterBrowse();
    buildWsGrid();  // rebuild weapon slot dropdowns with fresh config list
  }).catch(()=>{});
}

function buildTagFilters(){
  const tagSet=new Set();
  Object.values(cache).forEach(cfg=>{
    if(typeof cfg==='object'&&cfg.tags)
      Object.entries(cfg.tags).forEach(([k,v])=>tagSet.add(`${k}:${v}`));
  });
  const c=$('tag-filters');c.innerHTML='';
  [...tagSet].sort().forEach(tag=>{
    const el=document.createElement('span');
    el.className='tfchip'+(activeTagFilters.has(tag)?' active':'');
    el.textContent=tag;
    el.onclick=()=>{ activeTagFilters.has(tag)?activeTagFilters.delete(tag):activeTagFilters.add(tag); el.classList.toggle('active',activeTagFilters.has(tag)); filterBrowse(); };
    c.appendChild(el);
  });
  if(tagSet.size===0)c.innerHTML='<span style="font-size:.65rem;color:var(--mu);">No tags yet</span>';
}

function filterBrowse(){
  const q=$('search').value.toLowerCase(),prev=$('cfgdd').value;
  $('cfgdd').innerHTML='<option value="">-- Select config --</option>';
  for(const key of allKeys){
    const cfg=cache[key];
    const name=typeof cfg==='object'?(cfg.name||key):key;
    const tags=typeof cfg==='object'?(cfg.tags||{}):{};
    const pd=typeof cfg==='object'?(cfg.pull_down??0):0;
    const haystack=(name+' '+Object.entries(tags).map(([k,v])=>k+':'+v).join(' ')).toLowerCase();
    if(q&&!haystack.includes(q))continue;
    if(activeTagFilters.size>0){
      const ts=new Set(Object.entries(tags).map(([k,v])=>`${k}:${v}`));
      if(![...activeTagFilters].every(t=>ts.has(t)))continue;
    }
    const tagStr=Object.entries(tags).map(([k,v])=>`[${k}:${v}]`).join(' ');
    const o=document.createElement('option');
    o.value=key;
    o.textContent=name+`  ↓${pd}`;
    if(tagStr) o.title=tagStr;
    $('cfgdd').appendChild(o);
  }
  $('cfgdd').value=prev;
  updBrowseLbl();
}

function updBrowseLbl(){
  const key=$('cfgdd').value,lbl=$('browse-lbl');
  if(!key){lbl.textContent='Browse Configs';return;}
  const cfg=cache[key];
  const name=typeof cfg==='object'?(cfg.name||key):key;
  const tags=typeof cfg==='object'?(cfg.tags||{}):{}
  const tagStr=Object.entries(tags).map(([k,v])=>`<span style="color:var(--mu);font-size:.6rem">[${k}:${v}]</span>`).join(' ');
  lbl.innerHTML='Selected — <span style="color:var(--ac);font-family:var(--mo)">'+name+'</span>'+(tagStr?' '+tagStr:'');
}

$('cfgdd').onchange=()=>{
  const key=$('cfgdd').value;updBrowseLbl();if(!key)return;
  const cfg=cache[key];if(cfg==null)return;
  const pd=typeof cfg==='object'?(cfg.pull_down??0):(cfg??0);
  const hz=typeof cfg==='object'?(cfg.horizontal??0):0;
  const dl=typeof cfg==='object'?(cfg.horizontal_delay_ms??500):500;
  const du=typeof cfg==='object'?(cfg.horizontal_duration_ms??2000):2000;
  const vdl=typeof cfg==='object'?(cfg.vertical_delay_ms??0):0;
  const vdu=typeof cfg==='object'?(cfg.vertical_duration_ms??0):0;
  $('sv').value=pd;$('sl').value=Math.round(pd);
  $('hv').value=hz;$('hs').value=Math.round(hz);
  $('dv').value=dl;$('ds').value=dl;
  $('uv').value=du;$('us').value=du;
  $('vdv').value=vdl;$('vds').value=vdl;
  $('vduv').value=vdu;$('vdus').value=vdu;
  // FIX BUG 2a: set hip fire inputs then immediately push to server via REST
  // (WS alone is too slow — getStatus() poll at 1s can overwrite before WS arrives)
  if(cfg.hip_pull_down !==undefined) $('hf-pd').value=cfg.hip_pull_down;
  if(cfg.hip_horizontal!==undefined) $('hf-hz').value=cfg.hip_horizontal;
  // FIX BUG 2b: always push hip fire values to server when loading config
  _lastConfigLoad = Date.now();  // suppress getStatus overwrite for 3s
  sendHF();
  const hasCurve=typeof cfg==='object'&&cfg.pull_down_curve;
  $('cpv').style.display=hasCurve?'inline-flex':'none';
  restoreCurve(hasCurve?cfg.pull_down_curve:null);
  // FIX BUG 2c: send all recoil values immediately via WS
  // Use setTimeout(0) to ensure DOM values are committed before sendAll reads them
  setTimeout(()=>{
    sendAll();
    if(ws&&ws.readyState===1) ws.send(JSON.stringify({pull_down_curve:hasCurve?cfg.pull_down_curve:[]}));
  }, 0);
  const name=typeof cfg==='object'?(cfg.name||key):key;
  $('cfg-name').value=name;
  currentTags=typeof cfg==='object'&&cfg.tags?{...cfg.tags}:{};
  document.querySelectorAll('.pbtn').forEach(b=>{ b.style.opacity='1'; b.style.borderWidth='1px'; });
  Object.entries(currentTags).forEach(([k,v])=>{
    document.querySelectorAll(`.pbtn[data-k="${k}"][data-v="${v}"]`).forEach(b=>{ b.style.opacity='1'; b.style.borderWidth='2px'; });
    document.querySelectorAll(`.pbtn[data-k="${k}"]:not([data-v="${v}"])`).forEach(b=>{ b.style.opacity='0.45'; });
  });
  renderTagChips();updSavePreview();
  toast('✓ Loaded: '+name);
};

$('search').oninput=filterBrowse;

function showWarn(msg){ $('warn-txt').textContent=msg; $('warn').classList.add('show'); setTimeout(()=>$('warn').classList.remove('show'),3000); }

function buildPayload(name){
  const c=getCurve();
  return{
    name,
    tags:Object.keys(currentTags).length>0?currentTags:null,
    pull_down_value:        safeNum($('sv').value),
    vertical_delay_ms:      safeNum($('vdv').value),
    vertical_duration_ms:   safeNum($('vduv').value),
    horizontal_value:       safeNum($('hv').value),
    horizontal_delay_ms:    safeNum($('dv').value),
    horizontal_duration_ms: safeNum($('uv').value),
    hip_pull_down:          safeNum($('hf-pd').value),
    hip_horizontal:         safeNum($('hf-hz').value),
    ...(c?{pull_down_curve:c}:{})
  };
}

$('save-btn').onclick=()=>{
  const name=$('cfg-name').value.trim();if(!name){showWarn('Enter a name');return;}
  fetch('/configs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(buildPayload(name))})
    .then(r=>r.json()).then(d=>{ if(d.detail)showWarn(d.detail); else{fetchConfigs();toast('✓ Saved: '+name);} }).catch(()=>showWarn('Save failed'));
};
$('overwrite-btn').onclick=()=>{
  const key=$('cfgdd').value,name=$('cfg-name').value.trim()||key;
  if(!key){showWarn('Select a config to overwrite');return;}
  if(!confirm('Overwrite "'+name+'" with current values?'))return;
  fetch('/configs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(buildPayload(name))})
    .then(r=>r.json()).then(d=>{
      if(d.detail)showWarn(d.detail);
      else{
        if(key!==name) fetch('/configs/'+encodeURIComponent(key),{method:'DELETE'}).finally(fetchConfigs);
        else fetchConfigs();
        toast('✓ Overwritten: '+name);
      }
    }).catch(()=>showWarn('Overwrite failed'));
};
$('delete-btn').onclick=()=>{
  const key=$('cfgdd').value;if(!key){showWarn('Select a config to delete');return;}
  if(!confirm('Delete "'+key+'"?'))return;
  fetch('/configs/'+encodeURIComponent(key),{method:'DELETE'})
    .then(()=>{fetchConfigs();toast('Deleted: '+key,'var(--rd)');}).catch(()=>{});
};

// ── Profile management ────────────────────────────────────────────────────────
function fetchCfgFiles(){
  fetch('/config-files').then(r=>r.json()).then(d=>{
    const dd=$('cfgfd');dd.innerHTML='';
    d.files.forEach(f=>{ const o=document.createElement('option');o.value=f;o.textContent=f.replace('.json','');dd.appendChild(o); });
    dd.value=d.current;
    $('cfg-badge').textContent=d.current.replace('.json','');
  }).catch(()=>{});
}
$('cfgfd').onchange=()=>{
  fetch('/config-files/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:$('cfgfd').value})})
    .then(r=>r.json()).then(d=>{
      $('cfg-badge').textContent=d.current_config_file.replace('.json','');
      cache=d.guns;allKeys=Object.keys(d.guns);
      buildTagFilters();filterBrowse();
      buildWsGrid();  // weapon slots follow the active profile
      toast('Profile: '+d.current_config_file.replace('.json',''));
    }).catch(()=>{});
};
$('create-cfg').onclick=()=>{
  const n=$('new-cfg-name').value.trim();if(!n)return;
  fetch('/config-files',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:n})})
    .then(r=>r.json()).then(()=>{fetchCfgFiles();$('new-cfg-name').value='';toast('✓ Profile created: '+n);}).catch(()=>{});
};
$('delete-cfg').onclick=()=>{
  const f=$('cfgfd').value;if(!f||f==='default.json')return;
  if(confirm('Delete profile "'+f+'"?'))
    fetch('/config-files/'+encodeURIComponent(f),{method:'DELETE'})
      .then(()=>{fetchCfgFiles();toast('Profile deleted','var(--rd)');}).catch(()=>{});
};

// ══════════════════════════════════════════════════════════════════════════
//  v8.0 — Curve Visualizer  (lightweight, event-driven)
//
//  Rules:
//  1. Static draw (drawVizStatic) — called ONCE whenever values change.
//     No animation loop. Costs one canvas repaint.
//  2. Tick animation — starts ONLY when server reports is_enabled=true.
//     Driven by setInterval at 50ms (20fps) — matches TICK_S granularity,
//     NOT requestAnimationFrame (no need for 60fps on a status indicator).
//     Stops immediately when firing stops OR tab goes hidden.
//  3. No duplicate /status poll — vizFiring is updated inside the existing
//     getStatus() cycle already polling every 1 s.
// ══════════════════════════════════════════════════════════════════════════
const vizCanvas = $('curve-viz');
const vizCtx    = vizCanvas ? vizCanvas.getContext('2d') : null;
let _vizIntervalId = null;   // setInterval handle — null when idle
let _vizTick       = 0;
let vizFiring      = false;
let _vizStaticRaf  = null;

function scheduleVizStatic() {
  if (vizFiring || _vizStaticRaf !== null) return;
  _vizStaticRaf = requestAnimationFrame(()=>{
    _vizStaticRaf = null;
    drawVizStatic();
  });
}

function _drawViz(tick, firing) {
  if (!vizCtx) return;
  const W = vizCanvas.width, H = vizCanvas.height;
  vizCtx.clearRect(0, 0, W, H);

  // grid — draw once using a single path per axis
  vizCtx.strokeStyle = '#1a1a2a'; vizCtx.lineWidth = 1;
  vizCtx.beginPath();
  for (let x = 0; x <= W; x += W/6) { vizCtx.moveTo(x,0); vizCtx.lineTo(x,H); }
  for (let y = 0; y <= H; y += H/4) { vizCtx.moveTo(0,y); vizCtx.lineTo(W,y); }
  vizCtx.stroke();

  const curve = getCurve();
  const pd    = parseFloat($('sv').value) || 0;
  const pts   = (curve && curve.length > 1) ? curve : Array.from({length:30}, ()=>pd);
  const maxV  = Math.max(...pts, 1);
  const N     = pts.length;
  const px    = i => (i / (N - 1)) * W;
  const py    = v => H - (v / maxV) * (H * 0.85) - H * 0.075;

  // filled area
  vizCtx.beginPath();
  pts.forEach((v,i) => i===0 ? vizCtx.moveTo(px(i),py(v)) : vizCtx.lineTo(px(i),py(v)));
  vizCtx.lineTo(W,H); vizCtx.lineTo(0,H); vizCtx.closePath();
  vizCtx.fillStyle = 'rgba(30,100,60,0.10)'; vizCtx.fill();

  // curve line
  vizCtx.beginPath();
  pts.forEach((v,i) => i===0 ? vizCtx.moveTo(px(i),py(v)) : vizCtx.lineTo(px(i),py(v)));
  vizCtx.strokeStyle = firing ? '#1a7050' : '#252535';
  vizCtx.lineWidth = 1.5; vizCtx.stroke();

  // position indicator — only when firing
  if (firing) {
    const t  = tick % N;
    const cx = px(t), cy = py(pts[t]);
    vizCtx.beginPath();
    vizCtx.strokeStyle = 'rgba(91,240,160,0.5)';
    vizCtx.lineWidth = 1; vizCtx.setLineDash([3,4]);
    vizCtx.moveTo(cx,0); vizCtx.lineTo(cx,H); vizCtx.stroke();
    vizCtx.setLineDash([]);
    vizCtx.beginPath();
    vizCtx.arc(cx, cy, 4, 0, Math.PI*2);
    vizCtx.fillStyle = '#5bf0a0'; vizCtx.fill();
    if ($('viz-tick-lbl')) $('viz-tick-lbl').textContent = `t=${tick % N}/${N}`;
  } else {
    if ($('viz-tick-lbl')) $('viz-tick-lbl').textContent = '';
  }
}

// Static snapshot — called on value changes, no loop
function drawVizStatic() { _drawViz(0, false); }

// Start 20fps interval only while actively firing
function _vizStart() {
  if (_vizIntervalId) return;
  _vizTick = 0;
  _vizIntervalId = setInterval(()=>{
    // Stop if tab hidden (saves CPU when alt-tabbed mid-game)
    if (document.hidden || !vizFiring) { _vizStop(); return; }
    _drawViz(_vizTick++, true);
  }, 50);  // 20fps — enough for a tick indicator
}

function _vizStop() {
  if (_vizIntervalId) { clearInterval(_vizIntervalId); _vizIntervalId = null; }
  if (_vizStaticRaf !== null) { cancelAnimationFrame(_vizStaticRaf); _vizStaticRaf = null; }
  _vizTick = 0;
  drawVizStatic();  // restore clean static view
}

// Pause/resume on tab visibility change
document.addEventListener('visibilitychange', ()=>{ if (document.hidden) _vizStop(); });

// Redraw static preview whenever curve/slider values change (input events only)
['sv','sl','hv','hs'].forEach(id=>{
  const el=$(id);
  if (el) el.addEventListener('input', scheduleVizStatic);
});

// Called from getStatus handler below — zero overhead when state hasn't changed
function vizUpdate(isFiring) {
  if (isFiring === vizFiring) return;  // no state change → do nothing
  vizFiring = isFiring;
  const stateLbl = $('viz-state-lbl');
  if (stateLbl) {
    stateLbl.textContent = isFiring ? 'Firing' : 'Idle';
    stateLbl.classList.toggle('on', isFiring);
  }
  isFiring ? _vizStart() : _vizStop();
}

// Draw initial static preview after DOM settles
setTimeout(drawVizStatic, 300);

// ══════════════════════════════════════════════════════════════════════════
//  v8.0 — Sensitivity Scaling
// ══════════════════════════════════════════════════════════════════════════
// ══════════════════════════════════════════════════════════════════════════
// ══════════════════════════════════════════════════════════════════════════
//  v8.2 — Macros UI
// ══════════════════════════════════════════════════════════════════════════
let macRecording    = false;
let macRecPollTimer = null;

function fetchMacros() {
  fetch('/macros').then(r=>r.json()).then(d=>{
    renderMacros(d.macros, d.playing, d.recording);
    macRecording = d.recording;
    updateRecBtn();
  }).catch(()=>{});
}

function stepSummary(steps) {
  if (!steps || !steps.length) return '0 steps';
  const moves   = steps.filter(s=>s.type==='move').length;
  const clicks  = steps.filter(s=>s.type==='click'&&s.state==='down').length;
  const keys    = steps.filter(s=>s.type==='kdown').length;
  const delays  = steps.filter(s=>s.type==='delay').length;
  const parts   = [];
  if (moves)  parts.push(`${moves} moves`);
  if (clicks) parts.push(`${clicks} clicks`);
  if (keys)   parts.push(`${keys} keys`);
  if (delays) parts.push(`${delays} delays`);
  return parts.join(', ') || `${steps.length} steps`;
}

function renderMacros(macros, playing, recording) {
  const el = $('mac-list');
  if (!el) return;
  const keys = Object.keys(macros||{});
  if (!keys.length) {
    el.innerHTML='<div style="font-size:.68rem;color:var(--mu);padding:6px 0;">No macros saved yet.</div>';
    return;
  }
  el.innerHTML = keys.map(name=>{
    const m        = macros[name];
    const keyBadge = m.key ? `<span class="macro-key">${m.key}</span>` : '';
    const loopBadge= m.loop ? '<span style="color:var(--vi);font-size:.6rem;margin-left:4px;">LOOP</span>' : '';
    const isPlaying= (playing||[]).includes(name);
    const playLbl  = isPlaying ? '⏹ Stop' : '▶ Play';
    const playCls  = isPlaying ? 'btn-d'  : 'btn-g';
    const enc      = encodeURIComponent(name);
    return `<div class="macro-item" style="flex-wrap:wrap;gap:5px;">
      <span class="macro-name ${isPlaying?'macro-playing':''}" style="flex:1;min-width:80px;">${name}</span>
      ${keyBadge}${loopBadge}
      <span class="macro-steps">${stepSummary(m.steps)}</span>
      <button class="btn ${playCls} mac-play-btn" style="font-size:.6rem;padding:4px 8px;"
              data-name="${enc}" data-loop="${!!m.loop}" data-playing="${isPlaying}">${playLbl}</button>
      <button class="btn btn-s mac-edit-btn"  style="font-size:.6rem;padding:4px 8px;"
              data-name="${enc}">⏱ Delays</button>
      <button class="btn btn-d mac-del-btn"   style="font-size:.6rem;padding:4px 8px;"
              data-name="${enc}">✕</button>
    </div>`;
  }).join('');

  el.querySelectorAll('.mac-play-btn').forEach(btn=>{
    btn.onclick = ()=>{
      const name    = decodeURIComponent(btn.dataset.name);
      const loop    = btn.dataset.loop === 'true';
      const playing = btn.dataset.playing === 'true';
      toggleMacro(name, loop, playing);
    };
  });
  el.querySelectorAll('.mac-edit-btn').forEach(btn=>{
    btn.onclick = ()=> openDelayEditor(decodeURIComponent(btn.dataset.name));
  });
  el.querySelectorAll('.mac-del-btn').forEach(btn=>{
    btn.onclick = ()=> deleteMacro(decodeURIComponent(btn.dataset.name));
  });
}

function toggleMacro(name, loop, playing) {
  const ep   = playing ? '/macros/stop' : '/macros/play';
  const body = playing ? {name} : {name, loop};
  fetch(ep, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(r=>{ if(!r.ok) r.json().then(e=>showWarn(e.detail||'Failed')); else fetchMacros(); })
    .catch(()=>showWarn('Request failed'));
}

function deleteMacro(name) {
  if (!confirm(`Delete macro "${name}"?`)) return;
  fetch('/macros/'+encodeURIComponent(name), {method:'DELETE'})
    .then(r=>{ if(!r.ok) showWarn('Delete failed'); else fetchMacros(); })
    .catch(()=>showWarn('Delete failed'));
}

function updateRecBtn() {
  const btn     = $('mac-rec-btn');
  const st      = $('mac-rec-status');
  const discard = $('mac-discard-btn');
  if (!btn) return;
  if (macRecording) {
    btn.textContent = '⏹ Stop & Save';
    btn.style.cssText = 'flex:1;background:#200404;border-color:#7a0808;color:#ff9090;';
    if (discard) discard.style.display = '';
  } else {
    btn.textContent = '⏺ Record';
    btn.style.cssText = 'flex:1;background:#0d0404;border-color:#3a0808;color:#ff6060;';
    if (st) st.textContent = '';
    if (discard) discard.style.display = 'none';
    stopRecPoll();
  }
}

function startRecPoll() {
  if (macRecPollTimer) return;
  macRecPollTimer = setInterval(()=>{
    fetch('/macros/record/status').then(r=>r.json()).then(d=>{
      if (!d.recording) { stopRecPoll(); return; }
      const st = $('mac-rec-status');
      if (st)
        st.innerHTML = `● Recording&hellip; &nbsp;<span style="color:var(--ac);">${d.steps} events captured</span>`;
    }).catch(()=>{});
  }, 200);   // 200ms — slightly snappier counter, still cheap
}

function stopRecPoll() {
  if (macRecPollTimer) { clearInterval(macRecPollTimer); macRecPollTimer = null; }
}

$('mac-rec-btn').onclick = ()=>{
  if (!macRecording) {
    fetch('/macros/record/start', {method:'POST'})
      .then(()=>{
        macRecording = true;
        updateRecBtn();
        startRecPoll();
        const st = $('mac-rec-status');
        if (st) { st.textContent='● Recording…'; st.style.color='#ff6060'; }
      }).catch(()=>showWarn('Could not start recording'));
  } else {
    const name = ($('mac-name').value||'').trim();
    if (!name) { showWarn('Enter a macro name first'); return; }
    const key  = ($('mac-rec-trigger-key').value) || null;
    const loop = $('mac-rec-loop').checked;
    fetch('/macros/record/stop', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, key, loop, steps:[]})
    }).then(r=>r.json()).then(d=>{
      macRecording = false;
      updateRecBtn();
      fetchMacros();
      toast(`✓ Saved "${d.saved}" — ${d.steps} events`);
      $('mac-name').value='';
    }).catch(()=>showWarn('Save failed'));
  }
};

$('mac-discard-btn').onclick = ()=>{
  if (!confirm('Discard current recording?')) return;
  fetch('/macros/record/discard', {method:'POST'})
    .then(()=>{ macRecording=false; updateRecBtn(); fetchMacros(); })
    .catch(()=>{ macRecording=false; updateRecBtn(); });
};

fetchMacros();
// Reduced from 2000ms → 1200ms for snappier macro list refresh
// but paused while macro tab is hidden (saves CPU when not looking at macros)
let _macPollTimer = setInterval(fetchMacros, 1200);
document.querySelectorAll('.tab').forEach(t=>{
  t.addEventListener('click', ()=>{
    // Slow down poll when not on macros tab
    clearInterval(_macPollTimer);
    _macPollTimer = setInterval(fetchMacros, t.dataset.tab === 'macros' ? 1200 : 4000);
  });
});

// ── Macro Record Key ──────────────────────────────────────────────────────────
function fetchRecordKey() {
  fetch('/macros/record-key').then(r=>r.json()).then(d=>{
    if ($('mac-rec-key'))         $('mac-rec-key').value         = d.record_key  || '';
    if ($('mac-rec-trigger-key')) $('mac-rec-trigger-key').value = d.trigger_key || '';
    if ($('mac-rec-loop'))        $('mac-rec-loop').checked      = !!d.loop;
    const st = $('mac-rec-key-status');
    if (st) _updateRecKeyStatus(d.record_key, d.trigger_key, d.loop);
  }).catch(()=>{});
}
fetchRecordKey();

function _updateRecKeyStatus(recKey, triggerKey, loop) {
  const st = $('mac-rec-key-status');
  if (!st) return;
  if (!recKey) { st.textContent = ''; return; }
  let msg = `✓ ${recKey} → toggles recording`;
  if (triggerKey) msg += ` · saves with hotkey ${triggerKey}`;
  if (loop)       msg += ' · LOOP';
  st.textContent = msg;
  st.style.color = 'var(--vi)';
}

$('mac-rec-key-save').onclick = ()=>{
  const key        = $('mac-rec-key').value        || '';
  const triggerKey = $('mac-rec-trigger-key').value || '';
  const loop       = $('mac-rec-loop').checked;
  fetch('/macros/record-key', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key, trigger_key: triggerKey || null, loop})
  }).then(r=>r.json()).then(d=>{
    _updateRecKeyStatus(d.record_key, d.trigger_key, d.loop);
    if (d.record_key) {
      let msg = `⏺ Record key: ${d.record_key}`;
      if (d.trigger_key) msg += `  →  trigger: ${d.trigger_key}`;
      toast(msg, 'var(--vi)');
    } else {
      toast('Record key disabled', 'var(--mu)');
    }
  }).catch(()=>showWarn('Failed to set record key'));
};

// ══════════════════════════════════════════════════════════════════════════
//  Delay Editor
// ══════════════════════════════════════════════════════════════════════════
let _delayMacroName  = '';
let _delaySteps      = [];

function openDelayEditor(name) {
  _delayMacroName = name;
  fetch('/macros').then(r=>r.json()).then(d=>{
    const m = (d.macros||{})[name];
    if (!m) { showWarn('Macro not found'); return; }
    _delaySteps = JSON.parse(JSON.stringify(m.steps||[]));
    $('delay-macro-name').textContent = name;
    renderDelaySteps();
    $('delay-editor').style.display = 'flex';
  });
}

function stepLabel(s) {
  if (!s) return '?';
  switch(s.type) {
    case 'move':  return `Move (${s.dx>=0?'+':''}${s.dx}, ${s.dy>=0?'+':''}${s.dy})`;
    case 'click': return `Click ${s.btn} ${s.state}`;
    case 'kdown': return `Key ${s.key} down`;
    case 'kup':   return `Key ${s.key} up`;
    case 'delay': return `Delay`;
    default:      return s.type||'step';
  }
}

function renderDelaySteps() {
  const el = $('delay-step-list');
  if (!el) return;
  if (!_delaySteps.length) { el.innerHTML='<div style="color:var(--mu);padding:6px 0;">No steps.</div>'; return; }

  el.innerHTML = _delaySteps.map((s,i)=>{
    const lbl   = stepLabel(s);
    const dtVal = s.dt_ms != null ? s.dt_ms : 0;
    const isDelay = s.type === 'delay';
    // Color-code delay steps differently so they're easy to spot
    const lblColor = isDelay ? 'var(--yl)' : 'var(--tx)';
    return `<div style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid var(--bd);">
      <span style="min-width:18px;font-family:var(--mo);font-size:.58rem;color:var(--mu);">${i+1}</span>
      <span style="flex:1;color:${lblColor};font-size:.72rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${lbl}</span>
      <span style="color:var(--mu);font-size:.58rem;white-space:nowrap;">wait:</span>
      <input type="number" value="${dtVal}" min="0" max="60000" step="10"
             data-idx="${i}"
             style="width:65px;font-size:.68rem;padding:2px 4px;text-align:right;"
             class="delay-ms-inp">
      <span style="color:var(--mu);font-size:.58rem;">ms</span>
      <button class="btn btn-d delay-del-btn" data-idx="${i}"
              style="font-size:.55rem;padding:2px 6px;flex-shrink:0;" title="Remove this step">✕</button>
    </div>`;
  }).join('');

  // Bind events via delegation — avoids inline onclick index bugs
  el.querySelectorAll('.delay-ms-inp').forEach(inp=>{
    inp.addEventListener('change', ()=>{
      const idx = parseInt(inp.dataset.idx);
      if (_delaySteps[idx] != null)
        _delaySteps[idx].dt_ms = Math.max(0, parseInt(inp.value)||0);
    });
    // Also update on blur so typing then clicking ✕ immediately captures the value
    inp.addEventListener('blur', ()=>{
      const idx = parseInt(inp.dataset.idx);
      if (_delaySteps[idx] != null)
        _delaySteps[idx].dt_ms = Math.max(0, parseInt(inp.value)||0);
    });
  });

  el.querySelectorAll('.delay-del-btn').forEach(btn=>{
    btn.addEventListener('click', ()=>{
      const idx = parseInt(btn.dataset.idx);
      _delaySteps.splice(idx, 1);
      renderDelaySteps();
    });
  });
}

$('delay-add-btn').onclick = ()=>{
  const ms = Math.max(1, parseInt($('delay-add-ms').value)||500);
  _delaySteps.push({type:'delay', dt_ms: ms});
  renderDelaySteps();
};

$('delay-save-btn').onclick = ()=>{
  // Flush any input values still in-focus before saving — use data-idx, not forEach index
  $('delay-step-list').querySelectorAll('input.delay-ms-inp').forEach(inp=>{
    const idx = parseInt(inp.dataset.idx);
    if (_delaySteps[idx] != null)
      _delaySteps[idx].dt_ms = Math.max(0, parseInt(inp.value)||0);
  });
  fetch(`/macros/${encodeURIComponent(_delayMacroName)}/steps`, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({steps: _delaySteps})
  }).then(r=>r.json()).then(d=>{
    $('delay-editor').style.display = 'none';
    fetchMacros();
    toast(`✓ Delays saved — ${d.steps} steps`);
  }).catch(()=>showWarn('Save failed'));
};

$('delay-close-btn').onclick = ()=>{ $('delay-editor').style.display='none'; };

// Close on backdrop click
$('delay-editor').onclick = e=>{ if(e.target===$('delay-editor')) $('delay-editor').style.display='none'; };

// ══════════════════════════════════════════════════════════════════════════
//  v8.0 — Export / Import
// ══════════════════════════════════════════════════════════════════════════
$('import-file').onchange=function(){
  const file=this.files[0]; if(!file)return;
  const merge=$('import-merge').checked;
  const st=$('import-status');
  st.textContent='Uploading…'; st.style.color='var(--mu)';
  const reader=new FileReader();
  reader.onload=e=>{
    const b64=btoa(String.fromCharCode(...new Uint8Array(e.target.result)));
    fetch('/import',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({data:b64,merge})})
      .then(r=>r.json()).then(d=>{
        const imp=d.imported;
        st.textContent=`✓ Imported: ${imp.profiles.join(', ')||'—'}${imp.macros?' + macros':''}`;
        st.style.color='var(--ac)';
        fetchConfigs();fetchCfgFiles();fetchMacros();
        toast('✓ Import complete');
      }).catch(()=>{st.textContent='Import failed';st.style.color='var(--rd)';});
  };
  reader.readAsArrayBuffer(file);
  this.value='';
};

// ══════════════════════════════════════════════════════════════════════════
//  v8.0 — Visualizer status hook (driven by getStatus poll)
// ══════════════════════════════════════════════════════════════════════════
function onStatusUpdate(s) {
  // Drive visualizer — only triggers canvas work on state change
  vizUpdate(!!s.is_enabled && !!s.ctrl_connected);
}

// Init
fetchConfigs().then(()=>fetchWsSlots());fetchCfgFiles();updSavePreview();
});
