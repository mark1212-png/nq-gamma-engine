// Offline actual store/wire/renderer check; all browser traffic intercepted.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.TEDDY_PLAYWRIGHT||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1200,height:800}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>{
   const name=new URL(r.request().url()).pathname.slice(1);
   if(['store.js','chart.js','wire.js','settings.js'].includes(name))return r.fulfill({contentType:'application/javascript',body:fs.readFileSync(path.join(__dirname,'../teddychart/static',name),'utf8')});
   return r.fulfill({contentType:'text/html',body:'<style>#chart{width:1100px;height:700px}.layer{position:absolute;inset:0}</style><div id="chart"></div>'});
  });
  await page.goto('http://heatmap.test/');
  const result=await page.evaluate(async()=>{
   const {Store,BookHistory}=await import('/store.js'),{Chart,LV}=await import('/chart.js'),{DEFAULTS}=await import('/settings.js'),{decode}=await import('/wire.js');
   const store=new Store(), now=Date.UTC(2026,8,14,0,10);store.now=()=>now;
   store.addSeed({rows:Array.from({length:10},(_,i)=>[now-600000+i*60000,29000,29005,28995,29001,100])});
   const chart=new Chart(document.getElementById('chart'),store,{...DEFAULTS,tpo:false,scaleToLevels:false});
   const snapshot=(ts,p,z)=>{const b=new ArrayBuffer(176),v=new DataView(b);v.setUint8(0,2);v.setFloat64(8,ts,true);v.setFloat32(16,p,true);v.setUint32(20,z,true);return decode(b)};
   for(let i=0;i<300;i++)store.addBook(snapshot(now-300000+i*1000,29000,30));
   chart.invalidate(LV.FULL);chart.frame();
   const pixels=()=>Array.from(chart.layers.heatmap.ctx.getImageData(0,0,1100,700).data).filter((v,i)=>i%4===3&&v>0).length;
   const painted=pixels(),oldKey=chart.heat.key;
   store.addBook(snapshot(now-500,29002,300));chart.invalidate(LV.FULL);chart.frame();
   const rescaled=chart.heat.key!==oldKey;
   const book=new BookHistory();book.push(snapshot(now,29000,300));book.push(snapshot(now+50,29001,10));
   const moved=book.px[0]===29001&&book.sz[0]===10;
   book.push(snapshot(now-1000,28000,500));const ordered=book.last.ts===now+50;
   book.push(snapshot(now+70,0,0));const cleared=book.px[0]===0&&book.sz[0]===0;
   return {painted,rescaled,moved,ordered,cleared};
  });
  assert.ok(result.painted>100);assert.ok(result.rescaled&&result.moved&&result.ordered&&result.cleared);
  assert.deepEqual(errors,[]);console.log(result);
  fs.mkdirSync(path.join(__dirname,'../notes/live-ladder-review'),{recursive:true});
  await page.screenshot({path:path.join(__dirname,'../notes/live-ladder-review/heatmap-test.png')});
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
