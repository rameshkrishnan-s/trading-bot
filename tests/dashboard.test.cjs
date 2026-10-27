// Verify the dashboard does not present unavailable data as a valid trading setup.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
for (const match of fs.readFileSync('static/index.html', 'utf8').matchAll(/id="([^"]+)"/g)) {
  nodes.set(match[1], {textContent:'',innerHTML:'',className:'',style:{},classList:{toggle(){}},addEventListener(){},setAttribute(){}});
}
const ctx=vm.createContext({document:{getElementById:id=>nodes.get(id),querySelectorAll:()=>[]},Intl,Date,Number,String,Math,AbortSignal,
  setTimeout:()=>1,clearTimeout(){},setInterval(){},fetch:()=>new Promise(()=>{})});
vm.runInContext(fs.readFileSync('static/app.js','utf8'),ctx);
const base={mode:'paper',strategy:'orb',status:'RUNNING',data_provider:'angel',capital:25000,equity:25000,day_pnl:0,
  realized:0,unrealized:0,server_time:'2026-10-15T09:35:00+05:30',session:{time:'2026-10-15T09:35:00+05:30',trades_taken:0,day_started:false,daily_remaining:null,entry_risk_budget:null},
  feed:{status:'missing'},risk:{max_daily_loss:800,max_loss_per_trade:400,max_premium:10000,max_trades_per_day:2,max_spread_pct:2},range:{},decision:{signal:'WAIT'},reviewer:{}};
function render(s){ctx.input=s;vm.runInContext('state=input;render()',ctx);}
render(base);
assert.equal(nodes.get('call-trigger').textContent,'—');
assert.equal(nodes.get('entry-budget').textContent,'—');
assert.equal(nodes.get('risk-used').textContent,'—');
assert.equal(nodes.get('day-pnl').textContent,'—');
assert.equal(nodes.get('session-title').textContent,'Waiting for the first market snapshot');
render({...base,status:'HALTED',error:'Angel request rejected (AB9019)',feed:{status:'error'}});
assert.equal(nodes.get('feed-alert').hidden,false);
assert.match(nodes.get('feed-alert-detail').textContent,/no Greeks data available/);
assert.match(nodes.get('feed-alert-detail').textContent,/new entries are blocked/);
const ready={...base,market:{timestamp:base.server_time,spot:25024,vix:14,previous_close:24900},
  feed:{status:'fresh',snapshot_age_seconds:10},range:{range_high:25020,range_low:24980,buffer:2,range_bars:15,atr:20,candle_time:base.server_time},
  session:{...base.session,day_started:true,daily_remaining:800,entry_risk_budget:400}};
render(ready);
assert.equal(nodes.get('call-trigger').textContent,'25,022.00');
assert.equal(nodes.get('put-trigger').textContent,'24,978.00');
assert.equal(nodes.get('call-stop').textContent,'25,018.00');
render({...ready,feed:{status:'stale',snapshot_age_seconds:200}});
assert.equal(nodes.get('feed-alert').hidden,false);
assert.match(nodes.get('range-validity').textContent,/not current entry instructions/);
render({...ready,status:'PAUSED'});
assert.equal(nodes.get('session-title').textContent,'New entries are paused');
render({...ready,position:{symbol:'<img src=x onerror=alert(1)>',option_type:'CE',qty:75,entry_price:100,expiry:'2026-10-20',plan:{stop_spot:25018,target_spot:25036,risk_budget:400}}});
assert.match(nodes.get('position-content').innerHTML,/NIFTY stop/);
assert.ok(!nodes.get('position-content').innerHTML.includes('<img'));
console.log('Dashboard checks passed: missing data, Greeks failure, levels, stale feed, pause, position and escaping.');

const history={session_date:'2026-10-15',source:'Angel historical',sessions:[{}],latest_session:{date:'2026-10-14',high:25020,low:24980,close:25000,candle_count:370},warnings:['Latest observed session is incomplete.']};
render({...ready,planning:{status:'History available with missing candles',context:history},reviewer:{provider:'antigravity',plan_status:'Plan available',last_plan:{session_date:'2026-10-14',provider:'antigravity',ts:'2026-10-14T08:30:00+05:30',reference_date:'2026-10-13',model:'Test model',text:'<script>Example advisory</script>'}}});
assert.equal(nodes.get('prior-coverage').textContent,'370 / 375 minutes');
assert.match(nodes.get('history-warnings').textContent,/incomplete/);
assert.match(nodes.get('plan-meta').textContent,/Older or different-provider plan/);
assert.equal(nodes.get('plan-text').textContent,'<script>Example advisory</script>');
console.log('Historical panel checks passed: coverage, stale plan labels and plain-text output.');

render({...ready,health:{candles:'error',candle_error:'Broker candles unavailable; saved candles retained, entries blocked',saved_candles:5,greeks:'available',collection_ms:1200},cost_model:{name:'angel_nse_2026_04',brokerage_per_order:20,slippage_bps:5}});
assert.equal(nodes.get('feed-alert-title').textContent,'Candle request failed');
assert.match(nodes.get('health-candles').textContent,/5 saved/);
assert.match(nodes.get('setup-reason').textContent,/saved candles retained/);
assert.match(nodes.get('execution-assumptions').textContent,/partial fills are not simulated/);
render({...ready,error:'Angel connection failed',health:{next_retry_at:'2026-10-15T09:33:00+05:30',retry_count:1}});
assert.match(nodes.get('feed-alert-detail').textContent,/Automatic retry/);
console.log('Reliability UI checks passed: retained candles, automatic retry and honest execution assumptions.');

render({...ready,strategy:'noise',history:{sessions:14},range:{upper_band:25100,lower_band:24800.4,check_time:'10:15',confirmation:'Waiting'}});
assert.equal(nodes.get('trend-gap').textContent,'25,100.00');
assert.equal(nodes.get('momentum').textContent,'24,800.40');
assert.equal(nodes.get('level-high-label').textContent,'Upper noise band · 10:15 check');
assert.match(nodes.get('strategy-summary').textContent,/Noise-area intraday momentum/);
assert.equal(nodes.get('trade-setup').hidden,true);
assert.equal(nodes.get('first-entry-label').textContent,'First noise-band check');
render({...ready,strategy:'noise',history:{sessions:9},range:{}});
assert.equal(nodes.get('confirmation').textContent,'9/14 prior sessions');
console.log('Noise strategy UI checks passed: band levels, labels, history count and hidden ORB setup.');
