const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const hasNumber = (n) => n != null && n !== "" && Number.isFinite(Number(n));
const inr = (n, decimals = 0) => hasNumber(n) ? new Intl.NumberFormat("en-IN", {style:"currency",currency:"INR",minimumFractionDigits:decimals,maximumFractionDigits:decimals}).format(Number(n)) : "—";
const num = (n, decimals = 0) => hasNumber(n) ? new Intl.NumberFormat("en-IN", {maximumFractionDigits:decimals,minimumFractionDigits:decimals}).format(Number(n)) : "—";
const clock = (v, seconds = false) => v ? new Intl.DateTimeFormat("en-IN", {timeZone:"Asia/Kolkata",hour:"2-digit",minute:"2-digit",...(seconds?{second:"2-digit"}:{}),hour12:false}).format(new Date(v)) : "—";
const signedMoney = (n) => `${Number(n) >= 0 ? "+" : "−"}${inr(Math.abs(Number(n)))}`;
let state = null;
let chartSeries = "spot";
let toastTimer;
let fetching = false;
let lastShownError = "";
let connected = false;
let controlling = false;

function toast(message, error = false) {
  const el = $("toast"); el.textContent = message; el.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.className = "toast", 3600);
}

function setText(id, value) { const el = $(id); if (el) el.textContent = value; }
function setClass(id, cls) { const el = $(id); if (el) el.className = cls; }

function showFeedAlert(title, detail) {
  $("feed-alert").hidden = !title;
  setText("feed-alert-title", title);
  setText("feed-alert-detail", detail);
}

function renderSession() {
  const s=state.session||{}, feed=state.feed||{}, health=state.health||{}, stamp=s.time||state.server_time;
  const time=new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Kolkata",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(stamp));
  const weekday=new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Kolkata",weekday:"short"}).format(new Date(stamp));
  const closed=s.calendar?!s.calendar.open:["Sat","Sun"].includes(weekday)||time<"09:15"||time>="15:30";
  const phase=closed?"closed":time<"09:32"?"range":time<"14:45"?"entries":time<"15:15"?"manage":"exit";
  document.querySelectorAll("[data-phase]").forEach(el=>el.classList.toggle("current",el.dataset.phase===phase));
  setText("session-date",`${new Date(stamp).toLocaleDateString("en-IN",{timeZone:"Asia/Kolkata",weekday:"long",day:"numeric",month:"long"})} · ${state.mode==="demo"?"Simulated time":"IST"}`);
  setText("feed-status",({synthetic:"Synthetic demo",fresh:"Recent snapshot",stale:"Snapshot is stale",missing:"Awaiting first snapshot",error:"Feed error"})[feed.status]||"Status unavailable");
  setText("feed-age",state.market?`${clock(state.market.timestamp,true)} IST${hasNumber(feed.snapshot_age_seconds)?` · ${Math.max(0,Math.floor(feed.snapshot_age_seconds))}s since snapshot`:""}`:"No market snapshot received");
  let title=closed?"Outside regular trading hours":phase==="range"?"Building the opening range":phase==="entries"?"Watching for a confirmed breakout":"New entries are closed";
  let detail=state.decision?.reason||"Waiting for the next market snapshot.";
  if(state.strategy==="noise"&&(phase==="range"||phase==="entries"))title="Checking the noise band every half hour";
  else if(state.strategy!=="orb"&&phase==="entries")title="Watching trend and momentum";
  if(s.cooldown_until&&!state.position){title="Cooling down after the last exit";detail=`Next entry can be considered after ${clock(s.cooldown_until)} IST, if all checks pass.`;}
  if(s.trades_taken>=state.risk.max_trades_per_day&&!state.position){title="Daily trade limit reached";detail="No more entries this session.";}
  if(state.position){title="Managing an open position";detail=state.strategy==="noise"?"Monitoring half-hour band checks, the rupee loss threshold and 15:15 exit time.":"Monitoring the stop, target, rupee loss threshold and 15:15 exit time.";}
  if(state.status==="PAUSED"){title="New entries are paused";detail=state.position?"The engine continues monitoring your open position.":"Select Start engine to resume automatic entry checks.";}
  if(state.status==="HALTED"){title=state.day_locked?"Daily risk lock is active":"Engine needs attention";detail=state.position?"An exit may still be pending. Check the position and feed before restarting.":"Check the error and activity log before resuming.";}
  if(feed.status==="missing"&&!closed&&state.status==="RUNNING"&&!state.position){title="Waiting for the first market snapshot";detail="The server is running. Entry checks need usable prices, Greeks and completed candles.";}
  if(state.error){title="Market data needs attention";detail=state.error.includes("AB9019")?"Angel returned AB9019: no Greeks data available. Delta is required to select a contract; new entries are blocked.":state.error;
    showFeedAlert("Feed request failed",`${detail} ${health.next_retry_at&&state.status==="RUNNING"?`Automatic retry after ${clock(health.next_retry_at,true)} IST.`:"Check the cause before resuming a halted engine."} Refresh status only reloads this page’s data.`);
  }else if(health.candle_error){title="Candle data needs attention";detail=health.candle_error;
    showFeedAlert("Candle request failed",`${detail}. ${health.saved_candles||0} saved candles are retained; new entries need successful data checks.`);
  }else if(feed.status==="stale")showFeedAlert("Showing an old market snapshot",closed?"The regular session is closed. Displayed prices and levels are historical.":"Prices and assessments below are old. Check the feed before relying on them.");
  else showFeedAlert("","");
  if(feed.status==="stale"&&!closed&&!state.error){title="Waiting for a recent market snapshot";detail="The last assessment is historical; current entry readiness is unknown.";}
  setText("health-candles",health.candles?`${health.saved_candles||0} saved · ${health.candles.replaceAll("_"," ")}`:"Not checked");
  setText("health-greeks",health.greeks==="not_required_while_holding"?"Not needed for exit":health.greeks||"Not checked");
  setText("health-latency",hasNumber(health.collection_ms)?`${num(health.collection_ms)} ms`:"—");
  setText("health-retry",health.next_retry_at?`Retry ${clock(health.next_retry_at,true)} IST`:state.status==="HALTED"?"Manual attention required":"No retry pending");
  const costs=state.cost_model;
  setText("execution-assumptions",costs?`${costs.name==="legacy"?"Legacy estimated charges":"Itemized NSE options charges"} · ${inr(costs.brokerage_per_order)} brokerage/order · ${num(costs.slippage_bps,1)} bps assumed slippage/side. Execution delay and partial fills are not simulated.`:"Execution assumptions unavailable on this server version.");
  setText("session-title",title);setText("session-detail",detail);
  setText("capital-detail",`Starting capital ${inr(state.capital)}`);
  setText("strategy-summary",({orb:"Opening-range breakout",noise:"Noise-area intraday momentum"}[state.strategy]||"EMA 8/21 and momentum")+" · One position at a time");
}

function renderSetup() {
  const r=state.range||{}, s=state.session||{};
  $("trade-setup").hidden=state.strategy!=="orb";
  const usable=[r.range_high,r.range_low,r.buffer].every(hasNumber);
  setText("range-progress",`${r.range_bars||0} / 15 opening candles`);
  setText("range-low",num(r.range_low,2));setText("range-high",num(r.range_high,2));setText("range-buffer",num(r.buffer,2));
  setText("call-trigger",usable?num(r.range_high+r.buffer,2):"—");
  setText("put-trigger",usable?num(r.range_low-r.buffer,2):"—");
  setText("call-stop",usable?num(r.range_high-r.buffer,2):"—");
  setText("put-stop",usable?num(r.range_low+r.buffer,2):"—");
  setText("range-validity",usable?`Levels from ${r.candle_time?clock(r.candle_time)+" IST":"last assessment"}; ATR ${num(r.atr,2)}. Rechecked each poll.`:"Levels appear once the opening range and recent candles are complete.");
  const caution=state.feed?.status==="stale"||state.feed?.status==="error"||!state.market;
  $("trade-setup").classList.toggle("historical",caution);
  if(caution&&usable)setText("range-validity","Last computed levels; market data is unavailable or stale. These are not current entry instructions.");
  setText("setup-reason",state.error||state.health?.candle_error||state.decision?.reason||"Waiting for a market snapshot.");
  setText("entry-budget",inr(s.entry_risk_budget));
  setText("session-trades",`${s.trades_taken??"—"} / ${state.risk.max_trades_per_day}`);
}

function renderHistory() {
  const planning=state.planning||{}, context=planning.context, reviewer=state.reviewer||{}, plan=reviewer.last_plan;
  const day=state.session?.date||state.server_time?.slice(0,10);
  const current=context?.session_date===day;
  const latest=context?.latest_session;
  setText("history-status",planning.status||"Waiting for history");
  setText("history-source",context?`${context.source} · Reference session ${latest.date} · ${context.sessions.length} observed sessions${current?"":" · Previous context"}`:
    state.mode==="demo"?"Synthetic demo has no historical market plan.":"History is collected while flat in Angel paper mode, with the reviewer enabled.");
  setText("prior-high",num(latest?.high,2));setText("prior-low",num(latest?.low,2));setText("prior-close",num(latest?.close,2));
  setText("prior-coverage",latest?`${latest.candle_count} / 375 minutes`:"—");
  setText("history-warnings",context?`${current?"":"This context is from an earlier planning date. "}${(context.warnings||[]).join(" ")}`:"No prior-session levels are available yet.");
  setText("plan-status",reviewer.plan_status||"Not prepared");
  setText("plan-text",plan?.text||"No AI plan yet. Historical levels remain useful context even if the model is unavailable.");
  setText("plan-meta",plan?`${plan.session_date===day&&plan.provider===reviewer.provider?"For this session":"Older or different-provider plan"} · Prepared ${new Date(plan.ts).toLocaleString("en-IN",{timeZone:"Asia/Kolkata"})} IST · ${plan.model} · Reference ${plan.reference_date} · Advisory only; no early entries.`:
    "Advisory only. The plan cannot place orders or enable earlier entries.");
}

function sparkline(values, width = 115, height = 48) {
  if (!values.length) return "";
  const lo = Math.min(...values), hi = Math.max(...values), range = Math.max(hi-lo, 1);
  const points = values.map((v,i) => `${(i/Math.max(values.length-1,1)*width).toFixed(1)},${(height-5-(v-lo)/range*(height-13)).toFixed(1)}`).join(" ");
  return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true"><polyline fill="none" stroke="#c5e66b" stroke-width="2" points="${points}"/></svg>`;
}

function drawChart() {
  const data = chartSeries === "spot" ? (state.bars || []).map(x => ({ts:x.ts,value:Number(x.spot)})) : (state.curve || []).map(x => ({ts:x.ts,value:Number(x.value)}));
  const root = $("chart");
  if (!data.length) { root.innerHTML = `<div class="activity-empty" style="padding:85px;text-align:center">Waiting for observations</div>`; return; }
  const w=900,h=260,padL=35,padR=54,padT=18,padB=30;
  const values=data.map(x=>x.value), minimum=Math.min(...values), maximum=Math.max(...values);
  const buffer=Math.max((maximum-minimum)*.15,chartSeries==="spot"?10:50);
  const low=minimum-buffer, high=maximum+buffer;
  const x=(i)=>padL+i/Math.max(data.length-1,1)*(w-padL-padR);
  const y=(v)=>padT+(high-v)/(high-low)*(h-padT-padB);
  const path=data.map((d,i)=>`${i?"L":"M"}${x(i).toFixed(1)} ${y(d.value).toFixed(1)}`).join(" ");
  const area=`${path} L${x(data.length-1).toFixed(1)} ${h-padB} L${x(0).toFixed(1)} ${h-padB} Z`;
  const grid=[0,1,2,3].map(i=>{const gy=padT+i*(h-padT-padB)/3;const val=high-i*(high-low)/3;return `<line x1="${padL}" y1="${gy}" x2="${w-padR}" y2="${gy}" stroke="#e9eee9" stroke-dasharray="3 5"/><text x="${w-43}" y="${gy+3}" fill="#a9b6ab" font-size="10" font-family="Arial">${chartSeries==="spot"?num(val,0):num(val/1000,0)+"k"}</text>`}).join("");
  const last=data[data.length-1]; const lastX=x(data.length-1).toFixed(1),lastY=y(last.value).toFixed(1);
  root.innerHTML=`<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#88be91" stop-opacity=".24"/><stop offset="1" stop-color="#88be91" stop-opacity="0"/></linearGradient></defs>${grid}<path d="${area}" fill="url(#chart-fill)"/><path d="${path}" fill="none" stroke="#64a27b" stroke-width="2.7" stroke-linejoin="round" stroke-linecap="round"/><circle cx="${lastX}" cy="${lastY}" r="5" fill="#fff" stroke="#64a27b" stroke-width="2.5"/><line x1="${lastX}" x2="${lastX}" y1="${lastY}" y2="${h-padB}" stroke="#b8d5bd" stroke-dasharray="3 4"/><text x="${padL}" y="${h-7}" fill="#a4b3a6" font-size="10" font-family="Arial">${clock(data[0].ts)}</text><text x="${w-padR-38}" y="${h-7}" fill="#a4b3a6" font-size="10" font-family="Arial">${clock(last.ts)}</text></svg>`;
  setText("chart-range",`${data.length} observations · ${clock(data[0].ts)}–${clock(last.ts)}`);
  setText("chart-legend",chartSeries==="spot"?"NIFTY 50":"Estimated equity");
  setText("chart-y-label",chartSeries==="spot"?"INDEX / INR":"EQUITY / INR");
  root.setAttribute("aria-label",chartSeries==="spot"?"NIFTY 50 price history":"Estimated equity history");
  const sparkValues=(state.curve||[]).map(x=>Number(x.value));
  $("mini-spark").innerHTML=sparkline(sparkValues.length>1?sparkValues:[state.capital,state.equity]);
}

function renderPosition() {
  const p=state.position;
  setText("position-count",p?"1 OPEN":"0 OPEN");
  setText("exposure",p?inr(Number(p.entry_price)*Number(p.qty)):inr(0));
  setText("exposure-detail",p?`${p.qty} units · ${p.option_type}`:"No active position");
  if (!p) { $("position-content").innerHTML=`<div class="empty-state"><span>◇</span><b>No active position</b><p>No money is committed. A position appears only after a fill.</p></div>`; return; }
  const pnl=Number(state.unrealized);
  $("position-content").innerHTML=`<div class="open-position"><div class="position-emblem">${p.option_type==="CE"?"↗":"↘"}</div><div class="position-info"><b>${esc(p.symbol)}</b><span>${esc(p.qty)} units · Entry ${inr(p.entry_price,2)} · Expiry ${esc(p.expiry)}</span></div><div class="position-mark"><b style="color:${pnl>=0?"#31856a":"#b45c51"}">${signedMoney(pnl)}</b><small>UNREALIZED EST.</small></div></div>`;
  if(p.plan?.stop_spot!=null)$("position-content").innerHTML+=`<dl class="position-plan"><div><dt>NIFTY stop</dt><dd>${num(p.plan.stop_spot,2)}</dd></div><div><dt>NIFTY target</dt><dd>${num(p.plan.target_spot,2)}</dd></div><div><dt>Rupee exit threshold</dt><dd>${inr(p.plan.risk_budget)}</dd></div><div><dt>Time exit</dt><dd>15:15 IST</dd></div></dl><p class="position-note">Exit at the first triggered condition, using an available quote. Software exits can exceed the planned loss.</p>`;
}

function renderOrders() {
  const rows=(state.orders||[]).slice(0,6);
  $("orders-body").innerHTML=rows.length?rows.map(o=>`<tr><td>${clock(o.ts)}</td><td title="${esc(o.symbol)}">${esc(o.symbol)}</td><td class="${o.action==="BUY"?"buy-side":"sell-side"}">${esc(o.action)}</td><td>${num(o.qty)}</td><td>${inr(o.price,2)}</td><td><span class="status-tag">${esc(o.status)}</span></td></tr>`).join(""):`<tr><td colspan="6" class="table-empty">No executions yet</td></tr>`;
}

function renderRisk() {
  const r=state.risk,loss=Math.max(0,-Number(state.day_pnl)),used=Math.min(100,loss/Number(r.max_daily_loss)*100);
  const remaining=state.session?.daily_remaining;
  setText("risk-cap",inr(r.max_daily_loss));setText("risk-used",hasNumber(remaining)?`${Math.round(used)}%`:"—");setText("risk-remaining",inr(remaining));
  $("risk-ring").style.background=`conic-gradient(${state.day_locked?"#d27d70":"#9cc47b"} ${used*3.6}deg,#edf2eb ${used*3.6}deg)`;
  $("risk-meter").style.width=`${used}%`;
  setText("risk-premium",inr(r.max_premium));setText("risk-trade",inr(r.max_loss_per_trade));
  const count=state.session?.trades_taken??"—";
  setText("risk-count",`${count} / ${r.max_trades_per_day}`);setText("risk-spread",`${num(r.max_spread_pct,1)}%`);
  const lock=$("risk-lock");lock.className=`risk-lock${state.day_locked?" locked":""}`;lock.innerHTML=state.day_locked?"<span>●</span> Daily risk lock active":"<span>●</span> Daily risk lock inactive";
  if(!state.session?.day_started){setText("risk-lock","Awaiting this session’s first snapshot");lock.className="risk-lock";$("risk-ring").style.background="#edf2eb";$("risk-meter").style.width="0%";}
}

function renderChain() {
  const rows=state.chain||[];
  const spot=Number(state.market?.spot||0);
  $("chain-body").innerHTML=rows.length?rows.map(o=>`<tr class="${Math.abs(Number(o.strike)-spot)<=25?"near-money":""}"><td>${num(o.strike)}</td><td><span class="type-chip ${o.type==="PE"?"pe":""}">${esc(o.type)}</span></td><td>${esc(o.expiry)}</td><td>${num(o.lot_size)}</td><td>${inr(o.bid,2)}</td><td>${inr(o.ask,2)}</td><td>${num(o.spread_pct,2)}%</td><td>${num(o.delta,2)}</td><td>${num(o.oi)}</td></tr>`).join(""):`<tr><td colspan="9" class="table-empty">No option quotes yet. Check market feed status above.</td></tr>`;
}

function renderActivity() {
  const rows=(state.events||[]).slice(0,12),icons={info:"◇",signal:"◎",trade:"↗",warning:"!",error:"!"};
  $("activity-list").innerHTML=rows.length?rows.map(e=>`<div class="activity-item"><span class="activity-icon ${esc(e.level)}">${icons[e.level]||"◇"}</span><div class="activity-main"><b>${esc(e.title)}</b><p>${esc(e.detail)}</p></div><span class="activity-time">${clock(e.ts)}</span></div>`).join(""):`<div class="activity-empty">Waiting for engine events</div>`;
}

function render() {
  if(!state) return;
  const mode=state.mode.toUpperCase(), status=state.status.toLowerCase();
  const provider=state.data_provider==="angel"?"Angel One":"Upstox";
  setText("side-status",state.status);setText("side-mode",mode);setText("control-status-text",state.status);setText("footer-mode",`${mode} MODE`);
  for(const id of ["side-led","control-led"]) setClass(id,`led ${status}`);
  $("connection").className="connection online";$("connection").innerHTML="<i></i> Server connected";
  setText("clock",clock(state.server_time,true));
  const banner=$("mode-banner");banner.className=`mode-banner ${state.mode}`;
  const bannerText={demo:["Synthetic demonstration","Prices and fills are generated locally. This is a working simulation, not market data."],paper:["Paper trading on live quotes",`Market quotes come from ${provider}. Orders remain simulated and P&L includes estimated costs.`],live:["Live exchange execution","This engine can send limit IOC orders through Upstox. Reconcile all fills with your broker."]}[state.mode];
  banner.innerHTML=`<span class="mode-banner-icon">${state.mode==="live"?"◈":"◇"}</span><div><b>${bannerText[0]}</b><span>${bannerText[1]}</span></div><span class="banner-tail">${mode} MODE${state.mode==="demo"?" · <button id=\"reset-demo\" style=\"border:0;background:none;color:#67874e;font:inherit;font-weight:800;cursor:pointer\">RESET TAPE</button>":""}</span>`;
  $("reset-demo")?.addEventListener("click",()=>control("reset_demo"));
  $("start-btn").disabled=controlling||state.status==="RUNNING"||(state.mode==="live"&&state.status==="HALTED");
  $("pause-btn").disabled=controlling||state.status==="PAUSED"||state.status==="HALTED";
  $("halt-btn").disabled=controlling||state.status==="HALTED";
  setText("equity",inr(state.equity));setText("day-pnl",state.session?.day_started?signedMoney(state.day_pnl):"—");setText("realized",signedMoney(state.realized));
  const total=(Number(state.equity)/Number(state.capital)-1)*100;
  const ret=$("total-return");ret.textContent=`${total>=0?"+":""}${num(total,2)}%`;ret.className=`delta-chip${total<0?" negative":""}`;
  $("day-pnl").style.color=Number(state.day_pnl)<0?"#b75a51":"";
  if(state.market){
    setText("market-price",num(state.market.spot,2));setText("vix",state.market.vix>0?num(state.market.vix,2):"—");
    setText("market-source",state.mode==="demo"?"SYNTHETIC":provider.toUpperCase());
    const change=(Number(state.market.spot)/Number(state.market.previous_close)-1)*100;
    const ch=$("market-change");ch.textContent=`${change>=0?"↗ +":"↘ "}${num(change,2)}%`;ch.className=`market-change${change<0?" down":""}`;
  }
  const d=state.decision||{},signal=d.signal||"WAIT";
  setText("signal",signal);setClass("signal",`signal ${signal.toLowerCase()}`);setText("signal-reason",d.reason||"Waiting for the first market snapshot.");
  const noise=state.strategy==="noise", r=state.range||{};
  if(noise){
    setText("level-high-label",`Upper noise band${r.check_time?` · ${r.check_time} check`:""}`);setText("level-low-label","Lower noise band");
    setText("review-title","Noise-band review");setText("first-entry-time","09:45");setText("first-entry-label","First noise-band check");
    setText("review-overview-text","A 14-session average move from the open sets the band. Half-hour checks enter outside it; one option lot must fit the remaining risk budget.");
    setText("review-overview-note","No index target: the same-side band is rechecked every half hour. The rupee stop works between checks; option returns can differ as volatility and time value change.");
  }
  setText("trend-gap",num(noise?r.upper_band:r.range_high,2));
  setText("momentum",num(noise?r.lower_band:r.range_low,2));
  setText("confirmation",r.confirmation||(noise?`${state.history?.sessions??0}/14 prior sessions`:`${r.range_bars||0}/15 range candles`));
  const plan=state.position?.plan||d.metrics||{};
  setText("structural-stop",plan.stop_spot==null?"—":num(plan.stop_spot,2));
  setText("structural-target",plan.target_spot==null?"—":num(plan.target_spot,2));
  setText("estimated-risk",plan.estimated_stop_loss==null?"—":inr(plan.estimated_stop_loss,2));
  const reviewer=state.reviewer||{}, review=reviewer.last_review;
  setText("review-status",reviewer.status||"Disabled");
  setText("review-text",review?.text||"No AI review yet. The engine’s current signal and skip reason are shown above.");
  setText("review-meta",review?`${review.model} · Signal ${review.signal} at ${new Date(review.source_ts).toLocaleString("en-IN",{timeZone:"Asia/Kolkata"})} IST · Advisory only`:
    "Review and explain only. The agent cannot place orders or change risk limits.");
  setText("candidate",d.candidate?.symbol||"—");setText("last-check",clock(state.market?.timestamp));
  renderSession();renderSetup();renderHistory();renderPosition();renderOrders();renderRisk();renderChain();renderActivity();drawChart();
  if(state.error && state.error !== lastShownError) toast(state.error,true);
  lastShownError = state.error || "";
}

async function refresh(){
  if(fetching)return; fetching=true;
  try{const res=await fetch("/api/state",{cache:"no-store",signal:AbortSignal.timeout(8000)});if(!res.ok)throw new Error(`HTTP ${res.status}`);state=await res.json();connected=true;render();}
  catch(err){connected=false;$("connection").className="connection";$("connection").innerHTML="<i></i> Disconnected";
    showFeedAlert("Dashboard connection lost","Cannot reach the server. Values below are the last received data. Check that the server is running; refresh does not start it.");
    setText("session-title","Server status unknown");setText("session-detail","The dashboard cannot confirm whether the engine or market feed is running.");
    setText("feed-status","Unknown while disconnected");
    for(const id of ["side-led","control-led"])setClass(id,"led");
    setText("side-status","Unknown");setText("control-status-text","Unknown");
    document.querySelectorAll("[data-action]").forEach(btn=>btn.disabled=true);
  }
  finally{fetching=false;}
}

async function control(action){
  if(!connected||controlling)return;
  controlling=true;document.querySelectorAll("[data-action]").forEach(btn=>btn.disabled=true);
  try{
    let token=sessionStorage.getItem("meridian_control_token")||"";
    const request=()=>fetch("/api/control",{method:"POST",headers:{"Content-Type":"application/json",...(token?{"X-Control-Token":token}:{})},body:JSON.stringify({action}),signal:AbortSignal.timeout(10000)});
    let res=await request();
    if(res.status===401){token=window.prompt("Enter the local control token configured for this desk:")||"";if(!token)return;sessionStorage.setItem("meridian_control_token",token);res=await request();}
    const body=await res.json();if(!res.ok)throw new Error(body.error||`HTTP ${res.status}`);
    toast(action==="halt"?"Kill switch engaged. Exit will be attempted on a fresh quote.":action==="reset_demo"?"Synthetic tape reset.":`Engine ${body.status.toLowerCase()}.`);await refresh();
  }catch(err){toast(err.message,true);}
  finally{controlling=false;await refresh();}
}

$("refresh-btn").addEventListener("click",refresh);
document.querySelectorAll("[data-action]").forEach(btn=>btn.addEventListener("click",()=>control(btn.dataset.action)));
document.querySelectorAll("[data-chart]").forEach(btn=>btn.addEventListener("click",()=>{chartSeries=btn.dataset.chart;document.querySelectorAll("[data-chart]").forEach(b=>b.classList.toggle("selected",b===btn));drawChart();}));
document.querySelectorAll("[data-nav]").forEach(link=>link.addEventListener("click",()=>{document.querySelectorAll("[data-nav]").forEach(l=>l.classList.toggle("active",l===link));}));
refresh();setInterval(refresh,3000);
