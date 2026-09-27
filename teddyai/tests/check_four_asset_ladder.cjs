// Exercise real boot(): idle/pending service must open the report without a job.
// Every request is intercepted; no real pipeline, model, or data calls.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.TEDDY_PLAYWRIGHT||'playwright');
const root=path.resolve(__dirname,'..'),read=p=>fs.readFileSync(path.join(root,p),'utf8');
const chain=JSON.parse(read('chain_live.json'));
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1500,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.EventSource=class{addEventListener(){} close(){}}});
  await page.route('**/*',r=>{
   const u=new URL(r.request().url()),p=u.pathname;
   const json=x=>r.fulfill({contentType:'application/json',body:JSON.stringify(x)});
   if(p==='/cc')return r.fulfill({contentType:'text/html',body:read('command_center.html')});
   if(p==='/static/live_ladder_explain.js')return r.fulfill({contentType:'application/javascript',body:read('static/live_ladder_explain.js')});
   if(p==='/cc/api/state')return json({running:false,steps:['pull','verify','ladder','build','comment'].map(id=>({id,state:'pending'})),ticks:{mode:'replay',clock_ms:Date.parse(chain.asof)+60000,state:'live'},feed:'off'});
   if(p==='/cc/api/chain')return json(chain);
   if(p==='/cc/api/opra')return r.fulfill({contentType:'application/json',body:read(`data/opra/${chain.settle}-compare.json`)});
   if(p==='/cc/api/opra/chain')return r.fulfill({contentType:'application/json',body:read(`data/opra/${chain.settle}-${u.searchParams.get('root')}-chain_live.json`)});
   if(p==='/cc/api/report')return json(null); // built-in report fixture, real chain fixtures
   if(p==='/cc/api/bars')return json([]);
   return json({});
  });
  await page.goto('http://four-assets.test/cc');
  await page.waitForFunction(()=>document.querySelector('#boot').classList.contains('done'),null,{timeout:8000});
  const result=await page.evaluate(()=>{
   setView('terminal');
   TAPE.last=LIVE.chain.spot;TAPE.lastMs=nowMs();TAPE.connected=true;
   TK.es={contract:'ESU6',last:XREF.spx.F_ref,last_ms:nowMs()};
   const summary=()=>xlColumns('near').map(c=>({name:c.name,spot:c.spot,net:Object.values(c.g||{}).reduce((a,b)=>a+b,0),n:Object.keys(c.g||{}).length}));
   const before=summary();TAPE.last+=20;TK.es.last+=10;const after=summary();
   const modes=[];for(const mode of ['strikes','bands'])for(const bk of ['0dte','near','full']){XL.mode=mode;book=bk;renderXL(true);modes.push(document.querySelectorAll('#xl .h').length)}
   return{before,after,modes};
  });
  assert.deepEqual(result.before.map(c=>c.name),['NQ','QQQ','SPX cx','SPY']);
  for(let i=0;i<4;i++){assert.ok(result.before[i].n>0);assert.notEqual(result.before[i].spot,result.after[i].spot);assert.notEqual(result.before[i].net,result.after[i].net)}
  assert.ok(result.modes.every(n=>n===5));assert.deepEqual(errors,[]);
  console.log('PASS: real startup with idle/pending steps; all four assets reprice; 0DTE/near/full in bands/strikes; no JS errors. All requests intercepted.');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
