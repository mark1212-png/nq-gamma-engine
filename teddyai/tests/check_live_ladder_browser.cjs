// Offline UI/integration check. All requests are intercepted; boot() is disabled
// only in the in-memory fixture. Never opens /cc against the production server.
// Requires Playwright + Chromium. node tests/check_live_ladder_browser.cjs
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.TEDDY_PLAYWRIGHT || 'playwright');
const ROOT=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const context=await browser.newContext({viewport:{width:1500,height:1100}});
  const page=await context.newPage(),errors=[],requests=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>{
   const u=new URL(route.request().url());requests.push({path:u.pathname,method:route.request().method()});
   if(u.pathname==='/cc')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(ROOT,'command_center.html'),'utf8').replace(/\nboot\(\);\s*\n<\/script>/,'\n/* boot disabled for offline UI test */\n</script>')});
   if(u.pathname==='/static/live_ladder_explain.js')return route.fulfill({contentType:'application/javascript',body:fs.readFileSync(path.join(ROOT,'static/live_ladder_explain.js'),'utf8')});
   return route.fulfill({contentType:'application/json',body:'{}'});
  });
  await page.goto('http://gamma-fixture.test/cc');
  const chain=JSON.parse(fs.readFileSync(path.join(ROOT,'chain_live.json'),'utf8'));
  const asof=Date.parse(chain.asof),clock=asof+5*60000;
  await page.evaluate(({chain,clock})=>{
   R=JSON.parse(JSON.stringify(DEMO));R.session.date=chain.report_date;R.session.settle=chain.settle;R.session.asof=chain.asof;
   R.reference.price=chain.spot;R.reference.settle=chain.spot;R.session.em=350;
   LIVE.chain=chain;LIVE.cache={};hist=[];
   const candidates=Object.entries(liveGamma(book,chain.spot,Date.parse(chain.asof)))
     .filter(([k,v])=>Math.abs(v)>=10&&Math.abs(+k-chain.spot)<=350).sort((a,b)=>Math.abs(b[1])-Math.abs(a[1]));
   const level=+candidates[0][0];
   TAPE.connected=true;TAPE.last=level+8;TAPE.lastMs=clock;TAPE.dayOpen=clock-600000;
   TAPE.ts=[clock-240000,clock-180000,clock-120000,clock-60000,clock];
   TAPE.px=[chain.spot,level-5,level+5,level+6,level+8];
   TK={mode:'replay',state:'live',clock_ms:clock,contract:'REPLAY fixture'};
   document.getElementById('boot').remove();setView('terminal');
  },{chain,clock});
  await page.waitForTimeout(2200);
  const panel=page.locator('#v-terminal [data-live-ladder-explanation]');
  const txt=await panel.innerText();assert.match(txt,/REPLAY/);assert.match(txt,/fixed comparison window/);assert.doesNotMatch(txt,/PAUSED/);assert.match(txt,/Reclaim confirmed/);
  await panel.getByText('Comparison basis and significance',{exact:true}).click();
  await page.waitForTimeout(2200);
  assert.equal(await panel.locator('details').last().getAttribute('open'),'');
  const out=path.join(ROOT,'notes','live-ladder-review');fs.mkdirSync(out,{recursive:true});
  await panel.locator('..').screenshot({path:path.join(out,'live-ladder-desktop.png')});
  // Same rendered panel also exists in the detached Ladder view.
  await page.evaluate(()=>setView('ladder'));
  assert.match(await page.locator('#v-ladder [data-live-ladder-explanation]').innerText(),/REPLAY/);
  await page.evaluate(()=>{TK.mode='real';TK.contract=LIVE.chain.ref_symbol==='NQU6'?'NQZ6':'NQU6';});
  await page.waitForTimeout(2200);assert.match(await panel.innerText(),/differs from ladder reference/);
  await page.evaluate(()=>{TK.mode='replay';TAPE.connected=false;setView('terminal');});
  await page.waitForTimeout(2200);assert.match(await panel.innerText(),/PAUSED/);
  await panel.locator('..').screenshot({path:path.join(out,'live-ladder-paused.png')});
  await page.evaluate(()=>{TAPE.connected=true;});await page.waitForTimeout(2200);
  await page.setViewportSize({width:560,height:1000});
  await panel.locator('..').screenshot({path:path.join(out,'live-ladder-narrow.png')});
  assert.equal(await panel.evaluate(el=>el.scrollWidth>el.clientWidth+1),false,'panel overflow');
  assert.equal(requests.some(r=>r.method!=='GET'),false,'no mutation requests');
  assert.deepEqual(errors,[],'browser script errors');
  console.log('PASS: actual HTML + saved chain, live repricing, two views, details preserved, contract mismatch, disconnect/resume, narrow layout. No network or POSTs. Screenshots: '+out);
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
