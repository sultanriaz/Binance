/* AegisTrade operator UI — no third-party scripts, no localStorage/sessionStorage.
 * Session tokens live only in memory. Never use response HTML or eval for user content.
 */
'use strict';
const $ = (q) => document.querySelector(q);
let bearer = null;
let timer = null;
let page = 'overview';
let chartBars = [];
let overviewBars = [];
let lastChartSource = 'imported';
let prevDepth = null;
const titles = {overview:'Overview',trading:'Trading Terminal',portfolio:'Paper Portfolio',
    sandbox:'Binance Sandbox',approvals:'Approval Center',research:'AI Research Lab',live:'Live Trading Controls',operations:'Infrastructure'};
const plain = (v) => v === null || v === undefined ? '—' : String(v);
const money = (v) => Number.isFinite(Number(v)) ? Number(v).toLocaleString('en-US',{maximumFractionDigits:4}) : '—';
const smallId = (v) => String(v || '—').slice(0,9);
const stamp = (v) => v ? new Date(v).toLocaleString() : '—';
const latestPrices = { BTCUSDT: 65000, ETHUSDT: 3500 };
function showToast(message, type = 'info', duration = 5000) {
  const container = $('#toastContainer');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;

  const textWrap = document.createElement('div');
  textWrap.style.flex = '1';
  textWrap.textContent = plain(message);
  toast.append(textWrap);

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'toast-close';
  closeBtn.setAttribute('aria-label', 'Close notification');
  closeBtn.innerHTML = '&times;';
  closeBtn.onclick = () => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(100%)';
    setTimeout(() => toast.remove(), 250);
  };
  toast.append(closeBtn);

  container.append(toast);
  if (duration > 0) {
    setTimeout(() => {
      if (toast.isConnected) {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        setTimeout(() => toast.remove(), 250);
      }
    }, duration);
  }
}
function announce(message, selector = '#globalMessage', type = null) {
  const text = plain(message);
  const target = $(selector);
  if (target) target.textContent = text;

  let toastType = type;
  if (!toastType) {
    const lower = text.toLowerCase();
    if (lower.includes('reject') || lower.includes('fail') || lower.includes('error') || lower.includes('invalid') || lower.includes('denied') || lower.includes('decaying') || lower.includes('halt') || lower.includes('disarm')) {
      toastType = 'danger';
    } else if (lower.includes('success') || lower.includes('created') || lower.includes('approved') || lower.includes('enqueued') || lower.includes('demoted') || lower.includes('armed') || lower.includes('active')) {
      toastType = 'success';
    } else if (lower.includes('warn') || lower.includes('review') || lower.includes('pending') || lower.includes('confirm') || lower.includes('standby')) {
      toastType = 'warning';
    } else {
      toastType = 'info';
    }
  }
  showToast(text, toastType);
}
function logout(message='Session ended') {bearer=null;clearInterval(timer);$('#app').classList.add('hidden');$('#auth').classList.remove('hidden');$('#password').value='';$('#otp').value='';announce(message,'#authNotice');}
async function api(path, options={}) {
  if (!bearer) throw new Error('Sign in first');
  const controller = new AbortController();
  const watchdog=setTimeout(()=>controller.abort(),12000);
  try {
    const response=await fetch(path,{...options,signal:controller.signal,headers:{Authorization:'Bearer '+bearer,...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}});
    let data;
    try {data=await response.json();} catch {data={detail:'Invalid API response'};}
    if(response.status===401){logout('Session expired. Please sign in again.');throw new Error('Session expired');}
    if(!response.ok){
      let msg = 'Request failed';
      if (typeof data.detail === 'string') msg = data.detail;
      else if (data.detail && typeof data.detail === 'object') {
        if (data.detail.paper_order_rejected) msg = 'Paper order rejected: ' + data.detail.paper_order_rejected;
        else msg = JSON.stringify(data.detail);
      } else if (data.paper_order_rejected) {
        msg = 'Paper order rejected: ' + data.paper_order_rejected;
      } else {
        msg = JSON.stringify(data);
      }
      throw new Error(msg);
    }
    return data;
  } finally {clearTimeout(watchdog);}
}
const post=(path,body={})=>api(path,{method:'POST',body:JSON.stringify(body)});
function td(tr,value){const el=document.createElement('td');el.textContent=plain(value);tr.append(el);return el;}
function tdForecast(tr,attr){
  const el=document.createElement('td');
  if(!attr||attr.forecast_p===undefined||attr.forecast_p===null){
    el.textContent='—';
  }else{
    const p=Number(attr.forecast_p);
    const span=document.createElement('span');
    span.className=p>=0.60?'chip safe':'chip danger';
    span.textContent=`P(Up): ${(p*100).toFixed(1)}%`;
    el.append(span);
  }
  tr.append(el);
  return el;
}
function tdAttribution(tr,attr){
  const el=document.createElement('td');
  if(!attr){
    el.textContent='—';
  }else{
    const top=attr.top_features||[];
    if(top.length===0){
      el.textContent='Canary Active';
    }else{
      el.textContent=top.map(f=>`${f[0]}: ${f[1]>0?'+':''}${Number(f[1]).toFixed(3)}`).join(' · ');
      el.title=`Model: ${attr.model_id||'Canary'} | Forecast P(Up): ${(Number(attr.forecast_p)*100).toFixed(1)}%`;
    }
  }
  tr.append(el);
  return el;
}
function table(selector,rows,render){const tbody=$(selector);tbody.replaceChildren();if(!rows||rows.length===0){const tr=document.createElement('tr');const c=td(tr,'No records yet');c.colSpan=10;tbody.append(tr);return;}for(const r of rows){const tr=document.createElement('tr');render(tr,r);tbody.append(tr);}}
function putButton(container,name,action,cls='secondary'){const b=document.createElement('button');b.type='button';b.className=cls;b.textContent=name;b.addEventListener('click',action);container.append(b);return b;}
function fieldData(form){return Object.fromEntries(new FormData(form).entries());}
function computeSMA(bars, period) {
  const sma = new Array(bars.length).fill(null);
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close;
    if (i >= period) sum -= bars[i - period].close;
    if (i >= period - 1) sma[i] = sum / period;
  }
  return sma;
}
function renderCandles(canvas, bars, hoverPos) {
  const width = canvas.clientWidth || 800, height = canvas.clientHeight || 350;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const cx = canvas.getContext('2d');
  cx.scale(dpr, dpr);
  cx.clearRect(0, 0, width, height);
  cx.fillStyle = '#0e1625';
  cx.fillRect(0, 0, width, height);
  if (bars.length < 2) {
    cx.fillStyle = '#98accb';
    cx.font = '13px system-ui';
    cx.fillText('Import closed OHLCV candles to render a chart.', 22, height / 2);
    return;
  }
  const fullSma8 = computeSMA(bars, 8);
  const fullSma30 = computeSMA(bars, 30);
  const startIdx = Math.max(0, bars.length - 110);
  const data = bars.slice(startIdx);
  const sma8 = fullSma8.slice(startIdx);
  const sma30 = fullSma30.slice(startIdx);

  const pad = {l: 10, r: 65, t: 20, b: 31};
  const top = height * 0.72;
  const chartH = top - pad.t;
  let lo = Math.min(...data.map(x => x.low));
  let hi = Math.max(...data.map(x => x.high));
  for (let i = 0; i < data.length; i++) {
    if (sma8[i] !== null) { lo = Math.min(lo, sma8[i]); hi = Math.max(hi, sma8[i]); }
    if (sma30[i] !== null) { lo = Math.min(lo, sma30[i]); hi = Math.max(hi, sma30[i]); }
  }
  const dy = Math.max((hi - lo) * 0.08, 1e-8);
  hi += dy;
  lo -= dy;
  const y = (p) => pad.t + (hi - p) / (hi - lo) * chartH;
  const cw = (width - pad.l - pad.r) / data.length;
  const maxVol = Math.max(...data.map(x => x.volume), 1);

  cx.strokeStyle = '#253348';
  cx.lineWidth = 1;
  cx.fillStyle = '#93a8c5';
  cx.font = '11px system-ui';
  for (let k = 0; k <= 4; k++) {
    const yy = pad.t + chartH * k / 4;
    cx.beginPath();
    cx.moveTo(pad.l, yy);
    cx.lineTo(width - pad.r, yy);
    cx.stroke();
    cx.fillText((hi - (hi - lo) * k / 4).toFixed(2), width - pad.r + 5, yy + 4);
  }

  data.forEach((b, i) => {
    const x = pad.l + (i + 0.5) * cw;
    const rise = b.close >= b.open;
    const color = rise ? '#42d4a4' : '#f4788d';
    cx.strokeStyle = color;
    cx.fillStyle = color;
    cx.beginPath();
    cx.moveTo(x, y(b.high));
    cx.lineTo(x, y(b.low));
    cx.stroke();
    const y1 = y(b.open), y2 = y(b.close);
    cx.fillRect(x - Math.max(1, cw * 0.31), Math.min(y1, y2), Math.max(1, cw * 0.62), Math.max(1, Math.abs(y2 - y1)));
    const vH = Math.max(1, (height - top - 38) * b.volume / maxVol);
    cx.globalAlpha = 0.45;
    cx.fillRect(x - Math.max(1, cw * 0.28), height - 30 - vH, Math.max(1, cw * 0.56), vH);
    cx.globalAlpha = 1;
  });

  // Fast SMA(8) curve overlay (#38bdf8 cyan)
  cx.strokeStyle = '#38bdf8';
  cx.lineWidth = 1.5;
  cx.beginPath();
  let s8Started = false;
  for (let i = 0; i < data.length; i++) {
    if (sma8[i] !== null) {
      const px = pad.l + (i + 0.5) * cw;
      const py = y(sma8[i]);
      if (!s8Started) { cx.moveTo(px, py); s8Started = true; }
      else { cx.lineTo(px, py); }
    }
  }
  cx.stroke();

  // Slow SMA(30) curve overlay (#fb923c amber)
  cx.strokeStyle = '#fb923c';
  cx.lineWidth = 1.5;
  cx.beginPath();
  let s30Started = false;
  for (let i = 0; i < data.length; i++) {
    if (sma30[i] !== null) {
      const px = pad.l + (i + 0.5) * cw;
      const py = y(sma30[i]);
      if (!s30Started) { cx.moveTo(px, py); s30Started = true; }
      else { cx.lineTo(px, py); }
    }
  }
  cx.stroke();

  cx.fillStyle = '#9aabc3';
  for (const i of [0, Math.floor(data.length / 2), data.length - 1]) {
    const x = pad.l + (i + 0.5) * cw;
    cx.fillText(new Date(data[i].t * 1000).toLocaleString(undefined, {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}), Math.min(width - 140, Math.max(4, x - 35)), height - 8);
  }

  // Interactive crosshair & hover HUD
  if (hoverPos && hoverPos.x >= pad.l && hoverPos.x <= width - pad.r) {
    const hoverIdx = Math.floor((hoverPos.x - pad.l) / cw);
    if (hoverIdx >= 0 && hoverIdx < data.length) {
      const b = data[hoverIdx];
      const hx = pad.l + (hoverIdx + 0.5) * cw;
      const hy = Math.max(pad.t, Math.min(pad.t + chartH, hoverPos.y));
      const hoveredPrice = hi - ((hy - pad.t) / chartH) * (hi - lo);

      cx.save();
      cx.setLineDash([3, 3]);
      cx.strokeStyle = 'rgba(237, 243, 255, 0.45)';
      cx.lineWidth = 1;
      cx.beginPath();
      cx.moveTo(hx, pad.t);
      cx.lineTo(hx, height - pad.b);
      cx.stroke();

      cx.beginPath();
      cx.moveTo(pad.l, hy);
      cx.lineTo(width - pad.r, hy);
      cx.stroke();
      cx.restore();

      cx.fillStyle = '#223452';
      cx.fillRect(width - pad.r + 2, hy - 9, pad.r - 4, 18);
      cx.fillStyle = '#edf3ff';
      cx.font = '10px monospace, system-ui';
      cx.fillText(hoveredPrice.toFixed(2), width - pad.r + 5, hy + 4);

      const timeStr = new Date(b.t * 1000).toLocaleString(undefined, {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'});
      cx.fillStyle = '#223452';
      const tw = cx.measureText(timeStr).width;
      const tx = Math.max(pad.l, Math.min(width - pad.r - tw - 8, hx - (tw + 8) / 2));
      cx.fillRect(tx, height - pad.b + 2, tw + 8, 16);
      cx.fillStyle = '#edf3ff';
      cx.fillText(timeStr, tx + 4, height - pad.b + 14);

      const s8Val = sma8[hoverIdx] !== null ? sma8[hoverIdx].toFixed(2) : '—';
      const s30Val = sma30[hoverIdx] !== null ? sma30[hoverIdx].toFixed(2) : '—';
      const isUp = b.close >= b.open;

      cx.fillStyle = 'rgba(14, 22, 37, 0.9)';
      cx.fillRect(pad.l + 4, pad.t + 2, Math.min(500, width - pad.r - pad.l - 8), 24);

      cx.font = '11px monospace, system-ui';
      cx.fillStyle = isUp ? '#42d4a4' : '#f4788d';
      const ohlcText = `O:${b.open.toFixed(2)} H:${b.high.toFixed(2)} L:${b.low.toFixed(2)} C:${b.close.toFixed(2)} V:${b.volume.toFixed(2)}`;
      cx.fillText(ohlcText, pad.l + 10, pad.t + 17);

      if (width > 550) {
        cx.fillStyle = '#38bdf8';
        cx.fillText(`SMA(8):${s8Val}`, pad.l + 310, pad.t + 17);
        cx.fillStyle = '#fb923c';
        cx.fillText(`SMA(30):${s30Val}`, pad.l + 410, pad.t + 17);
      }

      if (canvas.id === 'tradingChart') {
        $('#chartNote').textContent = `${timeStr} · O: ${b.open.toFixed(2)} · H: ${b.high.toFixed(2)} · L: ${b.low.toFixed(2)} · C: ${b.close.toFixed(2)} · Vol: ${b.volume.toFixed(2)} · SMA(8): ${s8Val} · SMA(30): ${s30Val}`;
      }
    }
  }
}
function attachChartEvents(canvas, getBars) {
  if (!canvas || canvas._chartEventsAttached) return;
  canvas._chartEventsAttached = true;
  canvas.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    canvas._hoverPos = { x, y };
    renderCandles(canvas, getBars(), canvas._hoverPos);
  });
  canvas.addEventListener('mouseleave', () => {
    canvas._hoverPos = null;
    renderCandles(canvas, getBars());
    if (canvas.id === 'tradingChart') {
      $('#chartNote').textContent = `${chartBars.length} imported closed candles · ${lastChartSource}`;
    }
  });
}
window.addEventListener('resize', () => {
  renderCandles($('#overviewChart'), overviewBars, $('#overviewChart')?._hoverPos);
  renderCandles($('#tradingChart'), chartBars, $('#tradingChart')?._hoverPos);
});
attachChartEvents($('#overviewChart'), () => overviewBars);
attachChartEvents($('#tradingChart'), () => chartBars);
function navigate(next){page=next;$('#pageTitle').textContent=titles[next];document.querySelectorAll('nav button').forEach(b=>b.setAttribute('aria-current',b.dataset.page===next?'page':'false'));document.querySelectorAll('.page').forEach(el=>el.classList.toggle('active',el.dataset.view===next));loadPage().catch(e=>announce(e.message));}
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>navigate(b.dataset.page)));
$('#loginForm').addEventListener('submit',async e=>{e.preventDefault();try{const r=await fetch('/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('#user').value,password:$('#password').value,totp:$('#otp').value})});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Sign in rejected');bearer=data.access_token;$('#session').textContent=data.role+' · MFA';$('#auth').classList.add('hidden');$('#app').classList.remove('hidden');$('#password').value='';$('#otp').value='';navigate('overview');clearInterval(timer);timer=setInterval(()=>{loadPage().catch(e=>announce(e.message));},15000);}catch(err){announce(err.message,'#authNotice');}});
$('#logout').addEventListener('click',()=>logout());$('#refresh').addEventListener('click',()=>loadPage().catch(e=>announce(e.message)));
async function candles(market='spot',symbol='BTCUSDT',interval='1m'){return api(`/v1/terminal/candles?market=${encodeURIComponent(market)}&symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=250`);}
async function loadOverview(){
  const [streamsRes,eventsRes,papersRes,approvalsRes,historyRes,sandboxRes,canaryRes]=await Promise.allSettled([
    api('/v1/market/streams'),
    api('/v1/market/latest'),
    api('/v1/paper/accounts'),
    api('/v1/governance/requests'),
    candles(),
    api('/v1/testnet/accounts'),
    api('/v1/research/models/active')
  ]);
  const streams = streamsRes.status === 'fulfilled' ? streamsRes.value : {streams: []};
  const events = eventsRes.status === 'fulfilled' ? eventsRes.value : {events: []};
  const papers = papersRes.status === 'fulfilled' ? papersRes.value : {accounts: []};
  const approvals = approvalsRes.status === 'fulfilled' ? approvalsRes.value : {requests: []};
  const history = historyRes.status === 'fulfilled' ? historyRes.value : {bars: [], regime: 'Unavailable', fresh: false};
  const sandbox = sandboxRes.status === 'fulfilled' ? sandboxRes.value : {accounts: []};
  const canary = canaryRes.status === 'fulfilled' ? canaryRes.value : {active_model: null};

  const status=(name)=>{const row=streams.streams.find(x=>x.market===name);return row&&row.connected&&(Date.now()-row.last_event_ts)<30000?'HEALTHY':'STALE';};
  $('#spotFeed').textContent=status('spot');
  $('#futureFeed').textContent=status('futures');
  updateFeedHeartbeat(streams);
  $('#paperCount').textContent=papers.accounts.length;
  $('#approvalCount').textContent=approvals.requests.filter(x=>x.status==='PENDING').length;
  $('#regime').textContent=history.regime||'Insufficient bars';

  if (canary.active_model) {
    const m = canary.active_model;
    const wr = m.realized_win_rate !== null && m.realized_win_rate !== undefined ? `${(Number(m.realized_win_rate)*100).toFixed(1)}%` : '—';
    const brier = m.rolling_brier !== null && m.rolling_brier !== undefined ? Number(m.rolling_brier).toFixed(4) : '—';
    $('#overviewCanaryStatus').textContent = `Model: ${smallId(m.id)} (${m.market}:${m.symbol})\nStatus: ${m.status} · Win Rate: ${wr}\nRolling Brier: ${brier} · Watchdog: ${m.win_rate_health}`;
  } else {
    $('#overviewCanaryStatus').textContent = 'No active canary model. Deterministic fallback active.';
  }

  overviewBars=history.bars||[];
  renderCandles($('#overviewChart'),overviewBars);
  $('#overviewChartNote').textContent=overviewBars.length+' closed imported bars · '+(history.fresh?'recent':'may be stale');
  $('#safetyState').textContent=sandbox.accounts.map(a=>`${a.code}: ${a.state} · ${a.operator_enabled?'ARMED':'DISARMED'}`).join('\n')||'No test accounts initialized';
  table('#marketRows',events.events,(tr,x)=>{const p=x.payload||{};[x.market,x.symbol,x.event_type,p.best_bid||p.close||p.mark_price||p.price||'—',Math.round(x.age_ms/1000)+'s',x.stale?'STALE':'OK'].forEach(z=>td(tr,z));});
  for (const x of events.events || []) {
    const p = x.payload || {};
    const price = parseFloat(p.best_bid || p.close || p.mark_price || p.price);
    if (price > 0 && x.symbol) {
      latestPrices[x.symbol.toUpperCase()] = price;
    }
  }
  updateAllTicketNotionals();
  $('#lastRefresh').textContent='Last updated '+new Date().toLocaleTimeString();
}
document.addEventListener('focusin', e => {
  if (e.target && (e.target.matches('input[type="text"], input[type="number"]') || (e.target.tagName === 'INPUT' && !e.target.type))) {
    e.target.select();
  }
});
async function loadChart() {
  const m = $('#chartMarket').value;
  const s = $('#chartSymbol').value;
  const inv = $('#chartInterval').value;
  const [candlesRes, marketRes] = await Promise.all([
    candles(m, s, inv),
    api(`/v1/market/latest?market=${encodeURIComponent(m)}&symbol=${encodeURIComponent(s)}`)
  ]);

  chartBars = candlesRes.bars || [];
  lastChartSource = candlesRes.source || 'imported';
  renderCandles($('#tradingChart'), chartBars, $('#tradingChart')?._hoverPos);
  $('#chartNote').textContent = `${chartBars.length} imported closed candles · ${lastChartSource}`;
  $('#marketRegime').textContent = candlesRes.regime || 'Insufficient history';
  $('#chartFresh').textContent = candlesRes.fresh ? 'Recent imported bars' : 'Historical / stale';

  const events = marketRes.events || [];
  for (const x of events) {
    const p = x.payload || {};
    const price = parseFloat(p.best_bid || p.close || p.mark_price || p.price);
    if (price > 0 && x.symbol) {
      latestPrices[x.symbol.toUpperCase()] = price;
    }
  }
  updateAllTicketNotionals();
  const ticker = events.find(e => e.event_type === 'book_ticker');
  const depth = events.find(e => e.event_type === 'depth20');
  const mark = events.find(e => e.event_type === 'mark_price');

  if (ticker && ticker.payload) {
    const bestBid = parseFloat(ticker.payload.best_bid) || 0;
    const bestAsk = parseFloat(ticker.payload.best_ask) || 0;
    $('#lastPrice').textContent = money(ticker.payload.best_bid);
    $('#quoteAge').textContent = `Best bid · ${Math.round(ticker.age_ms / 1000)}s old · ${ticker.stale ? 'STALE' : 'recent'}`;
    const mid = (bestBid + bestAsk) / 2;
    if (mid > 0 && bestAsk >= bestBid) {
      const spreadBps = ((bestAsk - bestBid) / mid) * 10000;
      $('#microSpread').textContent = `${spreadBps.toFixed(2)} bps`;
    } else {
      $('#microSpread').textContent = '— bps';
    }
  } else {
    $('#lastPrice').textContent = '—';
    $('#quoteAge').textContent = 'No public best-bid observation';
    $('#microSpread').textContent = '— bps';
  }

  if (m === 'futures' && mark && mark.payload && mark.payload.funding_rate !== undefined && mark.payload.funding_rate !== '') {
    const fr = parseFloat(mark.payload.funding_rate);
    if (Number.isFinite(fr)) {
      const sign = fr > 0 ? '+' : '';
      $('#fundingBasis').textContent = `${sign}${(fr * 100).toFixed(4)}% / 8h`;
      $('#fundingBasis').className = fr > 0 ? 'positive' : (fr < 0 ? 'negative' : '');
    } else {
      $('#fundingBasis').textContent = 'Futures Demo';
      $('#fundingBasis').className = '';
    }
  } else {
    $('#fundingBasis').textContent = 'Spot (N/A)';
    $('#fundingBasis').className = 'muted';
  }

  if (depth && depth.payload && (depth.payload.bids || depth.payload.asks)) {
    const currBids = depth.payload.bids || [];
    const currAsks = depth.payload.asks || [];
    const levels = Math.min(20, Math.max(currBids.length, currAsks.length));
    let sumB = 0, sumA = 0, wb = 0, wa = 0;

    for (let i = 0; i < currBids.length && i < 20; i++) {
      const q = parseFloat(currBids[i][1]) || 0;
      sumB += q;
      wb += (1.0 / Math.sqrt(i + 1.0)) * q;
    }
    for (let i = 0; i < currAsks.length && i < 20; i++) {
      const q = parseFloat(currAsks[i][1]) || 0;
      sumA += q;
      wa += (1.0 / Math.sqrt(i + 1.0)) * q;
    }

    const totalVol = sumB + sumA;
    const bidPct = totalVol > 0 ? (sumB / totalVol) * 100 : 50;
    const askPct = totalVol > 0 ? (sumA / totalVol) * 100 : 50;

    $('#bidDepthPct').textContent = `${bidPct.toFixed(1)}%`;
    $('#askDepthPct').textContent = `${askPct.toFixed(1)}%`;
    $('#bidDepthQty').textContent = sumB.toFixed(3);
    $('#askDepthQty').textContent = sumA.toFixed(3);
    $('#bidDepthBar').style.width = `${bidPct}%`;
    $('#askDepthBar').style.width = `${askPct}%`;

    const totalW = wb + wa;
    const skew = totalW > 0 ? (wb - wa) / totalW : 0;
    $('#depthSkewText').textContent = `Decay-weighted skew: ${(skew > 0 ? '+' : '') + skew.toFixed(3)} (L${levels})`;

    let ofi = skew;
    if (prevDepth && prevDepth.symbol === s && prevDepth.market === m) {
      const pB = prevDepth.bids;
      const pA = prevDepth.asks;
      let ofiSum = 0;
      let denom = 0;
      const maxL = Math.min(20, pB.length, currBids.length, pA.length, currAsks.length);
      for (let i = 0; i < maxL; i++) {
        const w = 1.0 / Math.sqrt(i + 1.0);
        const pbPrev = parseFloat(pB[i][0]) || 0, qbPrev = parseFloat(pB[i][1]) || 0;
        const pbCurr = parseFloat(currBids[i][0]) || 0, qbCurr = parseFloat(currBids[i][1]) || 0;
        let deltaB = 0;
        if (pbCurr > pbPrev) deltaB = qbCurr;
        else if (pbCurr === pbPrev) deltaB = qbCurr - qbPrev;
        else deltaB = -qbPrev;

        const paPrev = parseFloat(pA[i][0]) || 0, qaPrev = parseFloat(pA[i][1]) || 0;
        const paCurr = parseFloat(currAsks[i][0]) || 0, qaCurr = parseFloat(currAsks[i][1]) || 0;
        let deltaA = 0;
        if (paCurr < paPrev) deltaA = qaCurr;
        else if (paCurr === paPrev) deltaA = qaCurr - qaPrev;
        else deltaA = -qaPrev;

        ofiSum += w * (deltaB - deltaA);
        denom += w * (Math.max(qbCurr, qbPrev) + Math.max(qaCurr, qaPrev));
      }
      if (denom > 0) {
        ofi = Math.max(-1.0, Math.min(1.0, ofiSum / denom));
      }
    }
    prevDepth = { symbol: s, market: m, bids: currBids, asks: currAsks };

    $('#ofiValue').textContent = (ofi > 0 ? '+' : '') + ofi.toFixed(3);
    const pill = $('#ofiPill');
    const meter = $('#ofiMeter');
    if (ofi > 0.15) {
      pill.className = 'chip safe';
      pill.textContent = 'BUY PRESSURE';
      meter.style.background = '#42d4a4';
      meter.style.left = '50%';
      meter.style.width = `${Math.min(50, Math.abs(ofi) * 50)}%`;
    } else if (ofi < -0.15) {
      pill.className = 'chip danger';
      pill.textContent = 'SELL PRESSURE';
      meter.style.background = '#f4788d';
      const w = Math.min(50, Math.abs(ofi) * 50);
      meter.style.left = `${50 - w}%`;
      meter.style.width = `${w}%`;
    } else {
      pill.className = 'chip';
      pill.textContent = 'BALANCED';
      meter.style.background = 'var(--brand)';
      meter.style.left = '50%';
      meter.style.width = '2px';
    }
  } else {
    $('#bidDepthPct').textContent = '50.0%';
    $('#askDepthPct').textContent = '50.0%';
    $('#bidDepthQty').textContent = '—';
    $('#askDepthQty').textContent = '—';
    $('#bidDepthBar').style.width = '50%';
    $('#askDepthBar').style.width = '50%';
    $('#ofiValue').textContent = '0.000';
    $('#ofiPill').className = 'chip';
    $('#ofiPill').textContent = 'STANDBY';
    $('#ofiMeter').style.width = '0%';
    $('#depthSkewText').textContent = 'Decay-weighted skew: awaiting depth feed';
  }
}
for(const x of ['chartMarket','chartSymbol','chartInterval'])$('#'+x).addEventListener('change',()=>loadChart().catch(e=>announce(e.message)));
async function loadPortfolio(){const d=await api('/v1/paper/accounts');const sel=$('#paperSelect');const keep=sel.value;sel.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='Choose paper account';sel.append(empty);for(const a of d.accounts){const o=document.createElement('option');o.value=a.id;o.textContent=a.name+' · '+smallId(a.id);sel.append(o);}sel.value=d.accounts.some(a=>a.id===keep)?keep:(d.accounts[0]?.id||'');if(!sel.value){$('#paperInfo').textContent='Create a virtual account to begin';table('#paperOrders',[],()=>{});return;}const record=await api('/v1/paper/accounts/'+sel.value);const a=record.account;$('#paperInfo').textContent=`Cash: ${money(a.cash)} virtual USDT\nEquity: ${money(record.equity)}\nGross exposure: ${money(record.gross_exposure)}\nPaused: ${a.paused}\nSpot: ${JSON.stringify(a.spot)}\nFutures: ${JSON.stringify(a.futures)}`;table('#paperOrders',(a.orders||[]).slice(-25).reverse(),(tr,o)=>{td(tr,o.market);td(tr,o.symbol);td(tr,o.side);td(tr,o.type);td(tr,o.status);tdForecast(tr,o.ai_attribution);tdAttribution(tr,o.ai_attribution);td(tr,o.filled);});}
$('#paperSelect').addEventListener('change',()=>loadPortfolio().catch(e=>announce(e.message)));
$('#paperCreate').addEventListener('submit',async e=>{e.preventDefault();try{const d=fieldData(e.target);const r=await post('/v1/paper/accounts',{name:d.name,initial_usdt:d.cash});await loadPortfolio();$('#paperSelect').value=r.account.account_id;await loadPortfolio();announce('Created a virtual account. No money transferred.','#paperMessage');}catch(err){announce(err.message,'#paperMessage');}});
$('#paperOrder').addEventListener('submit',async e=>{e.preventDefault();try{const id=$('#paperSelect').value;if(!id)throw new Error('Choose virtual account');const d=fieldData(e.target);const body={market:d.market,symbol:d.symbol,side:d.side,type:d.type,quantity:d.quantity,leverage:Number(d.leverage),reduce_only:d.reduce_only==='true',client_id:crypto.randomUUID()};if(d.limit_price)body.limit_price=d.limit_price;if(d.trigger_price)body.trigger_price=d.trigger_price;const result=await post(`/v1/paper/accounts/${id}/orders`,body);announce('Paper order: '+result.order.status,'#paperMessage');await loadPortfolio();}catch(err){announce(err.message,'#paperMessage');}});
for(const action of ['pause','resume'])$('#paper'+action[0].toUpperCase()+action.slice(1)).addEventListener('click',async()=>{try{const id=$('#paperSelect').value;if(!id)throw new Error('Select account');await post(`/v1/paper/accounts/${id}/${action}`);await loadPortfolio();}catch(e){announce(e.message,'#paperMessage');}});
async function loadSandbox(){const code=$('#sandboxAccount').value;const [accounts,orders,cap]=await Promise.all([api('/v1/testnet/accounts'),api('/v1/testnet/accounts/'+code+'/orders'),api('/v1/testnet/capabilities')]);const a=accounts.accounts.find(x=>x.code===code);$('#sandboxInfo').textContent=`Account: ${code}\nStatus: ${a?.state||'UNINITIALIZED'}\nArmed: ${a?.operator_enabled||false}\nLast reconciliation: ${stamp(a?.last_reconciled_at)}\nSandbox execution: ${cap.enabled?'ENABLED':'DISABLED'}\nMaximum notional: ${cap.max_order_usdt} USDT`;table('#sandboxOrders',orders.orders,(tr,x)=>{[x.client_order_id,x.payload?.symbol,x.payload?.type,x.status].forEach(y=>td(tr,y));const el=td(tr,'');if(['NEW','PARTIALLY_FILLED','ACKNOWLEDGED'].includes(x.status))putButton(el,'Request cancel',async()=>{try{await post(`/v1/testnet/accounts/${code}/orders/${x.id}/cancel`);await loadSandbox();}catch(e){announce(e.message,'#sandboxMessage');}});});}
$('#sandboxAccount').addEventListener('change',()=>loadSandbox().catch(e=>announce(e.message)));
for(const [id,action] of [['sandboxArm','arm'],['sandboxHalt','halt']])$('#'+id).addEventListener('click',async()=>{if(action==='arm'&&!confirm('Arm this TEST account, enabling worker submission of separately approved orders?'))return;try{await post(`/v1/testnet/accounts/${$('#sandboxAccount').value}/${action}`);await loadSandbox();}catch(e){announce(e.message,'#sandboxMessage');}});
$('#sandboxOrder').addEventListener('submit',async e=>{e.preventDefault();try{const d=fieldData(e.target);const body={account:$('#sandboxAccount').value,client_order_id:'at-'+crypto.randomUUID().replace(/-/g,'').slice(0,22),symbol:d.symbol,side:d.side,type:d.type,quantity:d.quantity,reduce_only:d.reduce_only==='true'};if(d.price)body.price=d.price;if(d.trigger_price)body.trigger_price=d.trigger_price;const r=await post('/v1/governance/sandbox-orders',{order:body});announce('Approval request '+smallId(r.request.id)+' created. A second administrator must review it.','#sandboxMessage');e.target.reset();}catch(e){announce(e.message,'#sandboxMessage');}});
async function loadApprovals(){const requests=(await api('/v1/governance/requests')).requests;table('#approvalRows',requests,(tr,x)=>{td(tr,smallId(x.id));td(tr,x.kind);td(tr,x.requester_id);td(tr,x.status);td(tr,stamp(x.expires_at));const cell=td(tr,'');if(x.status==='PENDING'){for(const d of ['APPROVED','REJECTED'])putButton(cell,d==='APPROVED'?'Approve':'Reject',async()=>{const otp=prompt('Enter your own 6-digit MFA to '+d.toLowerCase()+' this request (must be a different admin):');if(!otp)return;try{await post(`/v1/governance/requests/${x.id}/review`,{decision:d,totp:otp,reason:''});await loadApprovals();}catch(err){announce(err.message,'#approvalMessage');}},d==='REJECTED'?'danger':'secondary');}if(x.status==='APPROVED'){if(x.kind==='SANDBOX_ORDER')putButton(cell,'Enqueue on TEST exchange',async()=>{if(!confirm('This will enqueue a REAL ORDER on Binance TEST environment, with test funds only. Continue?'))return;try{const r=await post(`/v1/testnet/orders?approval_id=${x.id}`,x.payload);announce('Sandbox intent queued: '+smallId(r.order.id),'#approvalMessage');await loadApprovals();}catch(err){announce(err.message,'#approvalMessage');}});else putButton(cell,'Activate SHADOW model',async()=>{try{await post(`/v1/governance/requests/${x.id}/activate-shadow`);await loadApprovals();}catch(err){announce(err.message,'#approvalMessage');}});}});}
$('#approvalRefresh').addEventListener('click',()=>loadApprovals().catch(e=>announce(e.message)));
$('#researchJob').addEventListener('submit',async e=>{e.preventDefault();try{const x=fieldData(e.target);const r=await post('/v1/research/jobs',{market:x.market,symbol:x.symbol,interval:x.interval,bars:Number(x.bars),horizon:Number(x.horizon),friction_bps:Number(x.friction_bps)});announce('Training queued: '+smallId(r.id),'#researchMessage');await loadResearch();}catch(err){announce(err.message,'#researchMessage');}});
$('#createCampaign').addEventListener('click',async()=>{try{const x=fieldData($('#researchJob'));const r=await post('/v1/research/campaigns',{market:x.market,symbol:x.symbol,interval:x.interval,bars:Number(x.bars),horizon:Number(x.horizon),friction_bps:Number(x.friction_bps),min_new_bars:250});announce('Campaign '+smallId(r.id)+' created, DISABLED by default. Enable explicitly below.','#researchMessage');await loadResearch();}catch(e){announce(e.message,'#researchMessage');}});
async function loadResearch(){
  const [jobs,models,campaigns,canaryData]=await Promise.all([
    api('/v1/research/jobs'),
    api('/v1/research/models'),
    api('/v1/research/campaigns'),
    api('/v1/research/models/active').catch(()=>({active_model:null}))
  ]);

  const active = canaryData?.active_model;
  if(active){
    $('#canaryModelId').textContent = smallId(active.id);
    $('#canaryModelId').title = active.id;
    $('#canaryInstrument').textContent = `${(active.market||'spot').toUpperCase()} · ${active.symbol} · ${active.interval}`;
    $('#canaryBrier').textContent = active.rolling_brier !== null && active.rolling_brier !== undefined ? Number(active.rolling_brier).toFixed(4) : '—';
    $('#canaryWinRate').textContent = active.realized_win_rate !== null && active.realized_win_rate !== undefined ? `${(Number(active.realized_win_rate)*100).toFixed(1)}%` : '—';
    $('#canarySamples').textContent = `Resolved trades: ${active.shadow_samples_count || 0}`;

    const driftChip = $('#canaryDriftChip');
    if(active.status === 'DECAYING'){
      driftChip.className = 'chip danger';
      driftChip.textContent = 'DRIFT DECAYING';
      $('#canaryDriftDetails').textContent = active.drift_incident?.category || 'Concept drift triggered. Model retiring to cash.';
    }else{
      driftChip.className = 'chip safe';
      driftChip.textContent = active.win_rate_health === 'DEGRADING' ? 'WATCH (WARN)' : 'HEALTHY';
      $('#canaryDriftDetails').textContent = 'KS Test (p>0.01) · Page-Hinkley Active';
    }

    const btn = $('#btnDemoteCanary');
    btn.style.display = 'inline-block';
    btn.dataset.modelId = active.id;
  }else{
    $('#canaryModelId').textContent = '—';
    $('#canaryInstrument').textContent = 'No active model';
    $('#canaryBrier').textContent = '—';
    $('#canaryWinRate').textContent = '—';
    $('#canarySamples').textContent = 'Resolved trades: 0';
    $('#canaryDriftChip').className = 'chip safe';
    $('#canaryDriftChip').textContent = 'STANDBY';
    $('#canaryDriftDetails').textContent = 'Awaiting shadow benchmark promotion';
    $('#btnDemoteCanary').style.display = 'none';
  }

  if(!$('#btnDemoteCanary').dataset.bound){
    $('#btnDemoteCanary').dataset.bound = 'true';
    $('#btnDemoteCanary').addEventListener('click', async () => {
      const id = $('#btnDemoteCanary').dataset.modelId;
      if(!id) return;
      if(!confirm(`EMERGENCY OVERRIDE: Force-demote model ${smallId(id)} to RETIRED?\n\nThis will instantly remove the model from active routing and revert execution to flat cash.`)) return;
      try{
        await post(`/v1/research/models/${id}/demote`);
        announce(`Model ${smallId(id)} successfully demoted to RETIRED.`, '#researchMessage');
        await loadResearch();
      }catch(err){
        announce(err.message, '#researchMessage');
      }
    });
  }

  table('#campaignRows',campaigns.campaigns,(tr,x)=>{[smallId(x.id),x.market+':'+x.symbol,x.min_new_bars,x.enabled?'ENABLED':'DISABLED',smallId(x.last_job_id)].forEach(y=>td(tr,y));const el=td(tr,'');putButton(el,x.enabled?'Disable':'Enable',async()=>{try{await post(`/v1/research/campaigns/${x.id}/enabled`,{enabled:!x.enabled});await loadResearch();}catch(e){announce(e.message,'#researchMessage');}},x.enabled?'danger':'secondary');});table('#jobRows',jobs.jobs,(tr,x)=>[smallId(x.id),x.market,x.symbol,x.horizon,x.bars,x.state,x.error_code||smallId(x.model_id)].forEach(y=>td(tr,y)));table('#modelRows',models.models,(tr,x)=>{[smallId(x.id),x.market+':'+x.symbol,x.status,Number(x.metrics.brier).toFixed(4),Number(x.metrics.baseline_brier).toFixed(4),(100*Number(x.metrics.holdout.net_return_fraction)).toFixed(2)+'%',x.quality_pass?'PASS':'FAIL'].forEach(y=>td(tr,y));const c=td(tr,'');if(x.status==='CANDIDATE'&&x.quality_pass)putButton(c,'Request shadow',async()=>{try{const r=await post(`/v1/research/models/${x.id}/request-shadow`);announce('Model promotion approval requested: '+smallId(r.request.id),'#researchMessage');}catch(e){announce(e.message,'#researchMessage');}});if(x.status==='SHADOW'){putButton(c,'Forecast',async()=>{try{const d=await api(`/v1/research/models/${x.id}/forecast`);announce(`${d.symbol}: ${d.signal}, probability ${(100*d.p_positive_after_cost).toFixed(1)}%; regime ${d.regime}; stale=${d.stale}. Research only.`, '#researchMessage');}catch(e){announce(e.message,'#researchMessage');}});putButton(c,'Retire shadow',async()=>{if(!confirm('Retire this shadow model? It will stop new research observations.'))return;try{await post(`/v1/research/models/${x.id}/retire-shadow`);await loadResearch();}catch(e){announce(e.message,'#researchMessage');}},'danger');putButton(c,'Observations',async()=>{try{const d=await api(`/v1/research/models/${x.id}/shadow`);announce(d.observations.length+' shadow observations loaded, '+d.observations.filter(z=>z.resolved_net_return!==null).length+' resolved.','#researchMessage');}catch(e){announce(e.message,'#researchMessage');}});}});}
async function loadOperations(){const [streams,gaps,readiness,incidents]=await Promise.all([api('/v1/market/streams'),api('/v1/market/data-quality'),api('/v1/operations/readiness'),api('/v1/operations/incidents')]);$('#streamInfo').textContent=JSON.stringify(streams.streams,null,2);$('#gapInfo').textContent=JSON.stringify(gaps,null,2);const latest=readiness.assessments[0];$('#readinessState').textContent=latest?`${latest.verdict} · ${stamp(latest.evaluated_at)}\nHash: ${latest.sha256}\nReasons: ${(latest.report?.reasons||[]).join(', ')||'Human review required; no live authorization'}`:'No recorded readiness evidence';$('#operationsIncidents').textContent=JSON.stringify(incidents.incidents,null,2);}
$('#evaluateReadiness').addEventListener('click',async()=>{try{await post('/v1/operations/evaluate');await loadOperations();announce('Readiness evidence recorded. Human review still mandatory.');}catch(e){announce(e.message);}});

async function loadLive(){
  const code=$('#liveAccount').value;
  try {
    const [ac,req,orders,report]=await Promise.all([api('/v1/live/accounts'),api('/v1/live/approvals'),api('/v1/live/accounts/'+code+'/orders'),api('/v1/live/operations/report')]);
    const a=ac.accounts.find(x=>x.code===code);
    $('#liveAccounts').textContent=`${code}\nState: ${a?.state||'UNKNOWN'}\nArmed: ${a?.armed||false}\nAutomation: ${a?.automation_enabled||false}\nReconciled: ${stamp(a?.last_reconciled_at)}\nLast error: ${a?.last_error||'None'}`;
    $('#liveBanner').textContent=a?.armed?'LIVE ACCOUNT ARMED':'LIVE LOCKED / DISARMED';
    $('#liveReport').textContent=JSON.stringify(report,null,2);
    table('#liveApprovalRows',req.requests,(tr,x)=>{
      [smallId(x.id),x.kind,x.requester,x.status,stamp(x.expires_at)].forEach(y=>td(tr,y));
      const cell=td(tr,'');
      if(x.status==='PENDING')for(const decision of ['APPROVED','REJECTED'])putButton(cell,decision==='APPROVED'?'Review: Approve':'Reject',async()=>{
        const totp=prompt('SECOND ADMIN: enter your fresh 6-digit authenticator code');if(!totp)return;
        try{await post(`/v1/live/approvals/${x.id}/review`,{decision,totp,reason:''});await loadLive();}
        catch(err){announce(err.message,'#liveMessage');}
      },decision==='REJECTED'?'danger':'secondary');
      if(x.status==='APPROVED')putButton(cell,'Original requester: Execute',async()=>{
        if(!confirm('THIS MAY AUTHORIZE REAL-MONEY ORDER EXECUTION. Independently verify exact amount, market, risk, and Binance balance. Proceed?'))return;
        const totp=prompt('ORIGINAL REQUESTER: enter a fresh authenticator code');if(!totp)return;
        const p={kind:x.kind};
        if(x.kind.endsWith('_ORDER')) p.order={account:x.payload.account,client_order_id:x.payload.client_order_id,...x.payload.order};
        if(x.kind==='SPOT_AUTOMATION')p.automation=x.payload.config;
        try{await post(`/v1/live/approvals/${x.id}/execute`,{proposal:p,totp});await loadLive();announce('Authorized request consumed; monitor order confirmation and risk status.','#liveMessage');}
        catch(err){announce(err.message,'#liveMessage');}
      },'danger');
    });
    table('#liveOrderRows',orders.orders,(tr,o)=>{
      const attr = o.payload?.ai_attribution;
      td(tr, o.client_order_id);
      td(tr, o.payload?.symbol);
      td(tr, o.payload?.side);
      td(tr, o.source);
      td(tr, o.status);
      tdForecast(tr, attr);
      tdAttribution(tr, attr);
      const cell=td(tr,'');
      if(['NEW','ACKNOWLEDGED','PARTIALLY_FILLED','QUEUED'].includes(o.status))putButton(cell,'Request cancel',async()=>{
        if(!confirm('Cancellation may race with exchange fills. It is NOT proof a position is closed.'))return;
        try{await post(`/v1/live/accounts/${code}/orders/${o.id}/cancel`);await loadLive();}
        catch(err){announce(err.message,'#liveMessage');}
      },'secondary');
    });
  } catch(e){$('#liveAccounts').textContent='Live operator panel locked: '+e.message;$('#liveBanner').textContent='LIVE LOCKED BY DEPLOYMENT';$('#liveReport').textContent='Unavailable';table('#liveApprovalRows',[],()=>{});table('#liveOrderRows',[],()=>{});}
}
$('#liveAccount').addEventListener('change',()=>loadLive().catch(e=>announce(e.message,'#liveMessage')));
$('#liveRefresh').addEventListener('click',()=>loadLive().catch(e=>announce(e.message,'#liveMessage')));
$('#liveHalt').addEventListener('click',async()=>{
  if(!confirm('Disable NEW LIVE entries? Open positions and orders WILL NOT be closed.'))return;
  try{await post('/v1/live/accounts/'+$('#liveAccount').value+'/halt');await loadLive();announce('New entries disarmed; review all remaining live exposure at Binance.','#liveMessage');}
  catch(e){announce(e.message,'#liveMessage');}
});
$('#liveProposal').addEventListener('submit',async e=>{
  e.preventDefault();const d=fieldData(e.target);const p={kind:d.kind};
  if(d.kind.endsWith('_ORDER')){
    const account=d.kind==='SPOT_ORDER'?'spot-live':'futures-live';
    const order={account,client_order_id:'at-'+crypto.randomUUID().replaceAll('-','').slice(0,24),symbol:d.symbol,side:d.side,type:d.type,quantity:d.quantity,reduce_only:d.reduce_only==='true'};
    if(d.price)order.price=d.price;if(d.protective_stop)order.protective_stop=d.protective_stop;if(d.take_profit)order.take_profit=d.take_profit;
    p.order=order;
  }else if(d.kind==='SPOT_AUTOMATION')p.automation={symbol:d.symbol,quantity:d.quantity,fast_window:Number(d.fast_window),slow_window:Number(d.slow_window)};
  if(!confirm('This will initiate approval for a REAL MONEY capability. Review values carefully.'))return;
  try{const r=await post('/v1/live/approvals',p);announce('Approval requested: '+smallId(r.id)+'. A different administrator must review.','#liveMessage');await loadLive();}
  catch(err){announce(err.message,'#liveMessage');}
});

function updateFeedHeartbeat(streams) {
  const topbar = $('#topbarFeeds');
  if (!topbar) return;
  const list = streams?.streams || [];
  const now = Date.now();
  const allHealthy = list.length > 0 && list.every(s => s.connected && s.last_event_ts && (now - s.last_event_ts < 30000));
  if (allHealthy) {
    topbar.className = 'chip safe';
    topbar.textContent = '● FEEDS: CONNECTED';
  } else {
    topbar.className = 'chip danger';
    topbar.textContent = '⚠ FEEDS: DEGRADED';
  }
}

function updateTicketNotional(formId, estId, chipId, hintId) {
  const form = $('#' + formId);
  if (!form) return;
  const d = fieldData(form);
  const qty = parseFloat(d.quantity) || 0;
  const sym = (d.symbol || 'BTCUSDT').toUpperCase();
  let price = 0;
  if (d.type === 'LIMIT' && (d.limit_price || d.price)) {
    price = parseFloat(d.limit_price || d.price) || 0;
  }
  if (price <= 0) {
    price = latestPrices[sym] || (sym.startsWith('ETH') ? 3500 : 65000);
  }
  const notional = qty * price;
  const estEl = $('#' + estId);
  const chipEl = $('#' + chipId);
  const hintEl = $('#' + hintId);
  if (!estEl || !chipEl || !hintEl) return;

  if (qty <= 0) {
    estEl.textContent = '— USDT';
    chipEl.className = 'chip';
    chipEl.textContent = 'AWAITING QTY';
    hintEl.textContent = 'Min: 5 USDT · Hard Cap: 100 USDT';
    return;
  }

  estEl.textContent = `~${notional.toFixed(2)} USDT`;
  if (notional < 5.0) {
    chipEl.className = 'chip danger';
    chipEl.textContent = 'UNDER MIN ($5)';
    hintEl.textContent = `Binance rejects orders < 5.00 USDT (Est. price: ${money(price)})`;
  } else if (notional > 100.0) {
    chipEl.className = 'chip danger';
    chipEl.textContent = 'EXCEEDS CANARY CAP ($100)';
    hintEl.textContent = `Hard safety cap is 100 USDT (Est. price: ${money(price)})`;
  } else {
    chipEl.className = 'chip safe';
    chipEl.textContent = 'WITHIN LIMITS';
    hintEl.textContent = `Estimated @ ${money(price)} USDT/unit`;
  }
}

function updateAllTicketNotionals() {
  updateTicketNotional('paperOrder', 'paperEstNotional', 'paperRiskChip', 'paperRiskHint');
  updateTicketNotional('sandboxOrder', 'sandboxEstNotional', 'sandboxRiskChip', 'sandboxRiskHint');
  updateTicketNotional('liveProposal', 'liveEstNotional', 'liveRiskChip', 'liveRiskHint');
}

function applyQuickSize(formId, targetNotional) {
  const form = $('#' + formId);
  if (!form) return;
  const d = fieldData(form);
  const sym = (d.symbol || 'BTCUSDT').toUpperCase();
  let price = 0;
  if (d.type === 'LIMIT' && (d.limit_price || d.price)) {
    price = parseFloat(d.limit_price || d.price) || 0;
  }
  if (price <= 0) {
    price = latestPrices[sym] || (sym.startsWith('ETH') ? 3500 : 65000);
  }
  if (price > 0) {
    const rawQty = targetNotional / price;
    const decimals = sym.startsWith('BTC') ? 4 : 3;
    const factor = Math.pow(10, decimals);
    const quantized = Math.max(1 / factor, Math.floor(rawQty * factor) / factor);
    const qtyInput = form.querySelector('input[name="quantity"]');
    if (qtyInput) {
      qtyInput.value = quantized.toFixed(decimals);
      qtyInput.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.quick-size-btn');
  if (!btn) return;
  const formId = btn.dataset.target;
  const notional = parseFloat(btn.dataset.notional) || 15;
  if (formId) applyQuickSize(formId, notional);
});

['paperOrder', 'sandboxOrder', 'liveProposal'].forEach(id => {
  const f = $('#' + id);
  if (f) {
    f.addEventListener('input', updateAllTicketNotionals);
    f.addEventListener('change', updateAllTicketNotionals);
  }
});

const liveKind = $('#liveKind');
if (liveKind) {
  const syncLiveExtras = () => {
    const extras = $('#liveOrderExtras');
    if (extras) extras.style.display = liveKind.value.endsWith('_ORDER') ? 'block' : 'none';
  };
  liveKind.addEventListener('change', syncLiveExtras);
  syncLiveExtras();
}

$('#topbarHalt')?.addEventListener('click', async () => {
  if (!confirm('GLOBAL EMERGENCY HALT:\n\nImmediately halt new entries across SPOT and FUTURES live accounts and cancel queued opening orders?\n\nExisting open positions on Binance will remain active.')) return;
  try {
    const results = await Promise.allSettled([
      post('/v1/live/accounts/spot-live/halt'),
      post('/v1/live/accounts/futures-live/halt')
    ]);
    const ok = results.filter(r => r.status === 'fulfilled').length;
    announce(`Global emergency halt executed (${ok}/2 accounts confirmed). New entries disabled across live accounts.`, '#globalMessage', 'danger');
    if (page === 'live') await loadLive();
  } catch (err) {
    announce('Emergency halt failed: ' + err.message, '#globalMessage', 'danger');
  }
});

updateAllTicketNotionals();

async function loadPage() {
  if (!bearer) return;
  const actions = {
    overview: loadOverview,
    trading: loadChart,
    portfolio: loadPortfolio,
    sandbox: loadSandbox,
    approvals: loadApprovals,
    research: loadResearch,
    live: loadLive,
    operations: loadOperations
  };
  api('/v1/market/streams').then(updateFeedHeartbeat).catch(() => {});
  await actions[page]();
}
