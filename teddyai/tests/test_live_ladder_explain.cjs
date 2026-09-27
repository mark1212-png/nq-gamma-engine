// Run with: node --test tests/test_live_ladder_explain.cjs
// No network, browser, Python, credentials, subprocess, or production file writes.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const L = require('../static/live_ladder_explain.js');
test('expanded read explains significance, model limits and missing evidence',()=>{
  const r=L.createTracker().update(snapshot()),text=L.detailedRead(r).join(' ');
  assert.match(text,/40 meets the 16-contract/);
  assert.match(text,/does not separately attribute/);
  assert.match(text,/no flow confirmation is assumed/);
  assert.match(text,/Evidence still needed/);
});
test('expanded read describes opposing flow without claiming absorption',()=>{
  const r=L.createTracker().update(snapshot({flow:{windows:{'60':{delta:30,displacement:-2,count:15}}}}));
  assert.match(L.detailedRead(r).join(' '),/price still fell/);
  assert.match(L.detailedRead(r).join(' '),/cannot identify absorption or a reversal/);
});
test('expanded read keeps missing flips and tiny denominators unavailable',()=>{
  const r=L.createTracker().update(snapshot({base:{g:{1000:0},flip:null},current:{g:{1000:3},flip:null,last:1001}}));
  const text=L.detailedRead(r).join(' ');
  assert.match(text,/too small for a useful percentage/);
  assert.match(text,/no comparable crossing/);
  assert.doesNotMatch(text,/NaN|Infinity/);
});
function snapshot(overrides={}) {
  return {id:'a',now:600000,lastMs:600000,connected:true,feedState:'live',mode:'real',
    baseAt:0,center:1000,radius:100,asof:'fixture',settle:'fixture',vintage:'fixture',label:'NQ',
    base:{g:{1000:100,1100:-20,1200:500},flip:1020},
    current:{g:{1000:130,1100:-10,1200:600},flip:1030,last:1050},...overrides};
}
test('fixed window excludes distant strikes even after price moves',()=>{
  const a=L.compare(snapshot()); assert.equal(a.facts[0].before,80);assert.equal(a.facts[0].now,120);
  assert.equal(a.facts[0].delta,40);assert.equal(a.facts[0].percent,50);
});
test('abs and relative materiality must both pass',()=>{
  assert.equal(L.measure(100,115,20).material,false);
  assert.equal(L.measure(100,120,20).material,true);
  assert.equal(L.compare(snapshot({base:{g:{1000:1},flip:1020},current:{g:{1000:9},flip:1020,last:1000}})).facts[0].material,false);
});
test('small denominators do not manufacture a percent',()=>assert.equal(L.measure(0,20,10).percent,null));
test('negative baseline percentages use absolute denominator',()=>assert.equal(L.measure(-100,-150,20).percent,-50));
test('flip threshold scales with expected move',()=>assert.equal(L.compare(snapshot({radius:400})).facts[1].threshold,8));
test('disappearing flip remains unavailable, never coerced to zero',()=>{
  const r=L.compare(snapshot({current:{g:{1000:130},flip:null,last:1050}}));
  assert.equal(r.facts[1].delta,null);assert.equal(r.facts[1].material,false);
});
test('missing strike in valid map is zero',()=>{
  const r=L.compare(snapshot({current:{g:{1100:-20},flip:1020,last:1050}}));
  assert.equal(r.facts.find(x=>x.id==='node:1000').delta,-100);
});
test('nonfinite input is rejected',()=>assert.throws(()=>L.compare(snapshot({current:{g:{1000:NaN},flip:10,last:1000}})),/Invalid/));
test('tiny sign crossing is not called a regime change',()=>{
  const r=L.compare(snapshot({base:{g:{1000:.2},flip:1020},current:{g:{1000:-.2},flip:1020,last:1000}}));
  assert.match(L.explanation(r),/near zero/);assert.doesNotMatch(L.explanation(r),/turned negative/);
});
test('substantial sign crossing produces a quantified explanation',()=>{
  const r=L.compare(snapshot({base:{g:{1000:100},flip:1020},current:{g:{1000:-80},flip:1020,last:1000}}));
  assert.match(L.explanation(r),/turned negative/);assert.match(L.explanation(r),/−?\-180/);
});
test('log waits 30 seconds and then deduplicates',()=>{
  const t=L.createTracker();assert.equal(t.update(snapshot()).events.length,0);
  assert.equal(t.update(snapshot({now:629000,lastMs:629000})).events.length,0);
  const r=t.update(snapshot({now:630000,lastMs:630000}));assert.ok(r.events.length>0);
  assert.equal(t.update(snapshot({now:690000,lastMs:690000})).events.length,r.events.length);
});
test('brief excursion resets the persistence clock',()=>{
  const t=L.createTracker();t.update(snapshot());
  t.update(snapshot({now:620000,lastMs:620000,current:{g:{1000:100,1100:-20},flip:1020,last:1000}}));
  assert.equal(t.update(snapshot({now:630000,lastMs:630000})).events.length,0);
});
test('disconnect suppresses explanations and restarts persistence',()=>{
  const t=L.createTracker();t.update(snapshot());assert.equal(t.update(snapshot({now:620000,connected:false})).paused,true);
  assert.equal(t.update(snapshot({now:640000,lastMs:640000})).events.length,0);
});
test('stale, future, off, and catch-up feeds pause',()=>{
  for(const s of [{lastMs:400000},{lastMs:610000},{mode:'off'},{feedState:'replaying'},{baseAt:700000}])assert.ok(L.reason(snapshot(s)));
});
test('replay uses supplied market clock, never host wall clock',()=>assert.equal(L.reason(snapshot({mode:'replay'})),null));
test('new snapshot, book selection, and rewind reset the event log',()=>{
  for (const override of [{id:'new'},{now:500000,lastMs:500000}]) {
    const t=L.createTracker();t.update(snapshot());assert.ok(t.update(snapshot({now:630000,lastMs:630000})).events.length);
    assert.equal(t.update(snapshot(override)).events.length,0);
  }
});
const bar=(minute,c,h=c,l=c)=>({t:minute*60000,o:c,h,l,c});
const levels=[{price:100,label:'snapshot strike'}];
test('reclaim requires two completed closes after prior below',()=>{
  const bars=[bar(1,97),bar(2,103),bar(3,104)];
  assert.equal(L.priceConfirmations(bars,levels,240000,0,100).signals[0].kind,'Reclaim confirmed');
  assert.equal(L.priceConfirmations(bars,levels,239999,0,100).signals.length,0);
});
test('failed reclaim requires a probe above and two closes below',()=>{
  const bars=[bar(1,97),bar(2,98,103,96),bar(3,97)];
  const c=L.priceConfirmations(bars,levels,240000,0,100).signals[0];
  assert.equal(c.kind,'Failed reclaim from below');assert.deepEqual(c.closes,[98,97]);
  assert.equal(L.priceConfirmations([bar(1,97),bar(2,98),bar(3,97)],levels,240000,0,100).signals.length,0);
});
test('missing minute cannot confirm a reclaim',()=>assert.equal(L.priceConfirmations([bar(0,97),bar(2,103),bar(3,104)],levels,240000,0,100).signals.length,0));
test('no confirmation before the snapshot or from old completed bars',()=>{
  const bars=[bar(1,97),bar(2,103),bar(3,104)];
  assert.equal(L.priceConfirmations(bars,levels,240000,90000,100).signals.length,0);
  assert.equal(L.priceConfirmations(bars,levels,360000,0,100).ready,false);
});
test('repeated closes on an established side are not repeated reclaims',()=>assert.equal(L.priceConfirmations([bar(1,103),bar(2,104),bar(3,105)],levels,240000,0,100).signals.length,0));
test('incremental bars exclude first partial minute and ignore out-of-order ticks',()=>{
  const b=L.createMinuteBars();let ts=[65000,121000,122000,123000],px=[1,100,102,99];
  let r=b.consume(ts,px,'a');assert.equal(r.length,1);assert.equal(r[0].h,102);assert.equal(r[0].c,99);
  ts.push(124000);px.push(103);r=b.consume(ts,px,'a');assert.equal(r[0].c,103);
  assert.deepEqual(b.consume([65000],[1],'b'),[]);
});
test('HTML escapes source labels and marks sim explicitly',()=>{
  const r=L.createTracker().update(snapshot({mode:'sim',label:'<img src=x>'}));
  const html=L.render(r);assert.match(html,/SIMULATION/);assert.doesNotMatch(html,/<img/);assert.match(html,/not statistical significance/);
});
test('gap invalidates pre-gap confirmation evidence',()=>{
  const t=L.createTracker(),bars=[bar(1,97),bar(2,103),bar(3,104)];
  const s=snapshot({now:240000,lastMs:240000,bars,levels,radius:100});
  assert.equal(t.update(s).confirmations.signals.length,1);
  t.update({...s,now:241000,connected:false});
  assert.equal(t.update({...s,now:242000,lastMs:242000}).confirmations.signals.length,0);
});
test('render retains quarter-point prices and avoids signed zero',()=>{
  const s=snapshot({center:1000.25,current:{g:{1000:100},flip:1020.00001,last:1000.5}});
  const html=L.render(L.createTracker().update(s));assert.match(html,/1,000.25 → 1,000.5/);assert.doesNotMatch(html,/Δ \+0 /);
});
test('two-closes above and retest held are distinct evidence',()=>{
  assert.equal(L.priceConfirmations([bar(1,100),bar(2,103),bar(3,104)],levels,240000,0,100).signals[0].kind,'Two closes above');
  assert.equal(L.priceConfirmations([bar(1,103),bar(2,104,104,97),bar(3,105)],levels,240000,0,100).signals[0].kind,'Retest held from above');
});
