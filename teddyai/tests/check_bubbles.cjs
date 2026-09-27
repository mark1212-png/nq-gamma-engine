// Offline regression: print thresholds, timestamp collisions, and overlay order.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const uri=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');
(async()=>{
 const read=n=>fs.readFileSync(path.join(__dirname,'../teddychart/static',n),'utf8');
 const store=uri(read('store.js'));
 const {Chart}=await import(uri(read('chart.js').replace("'./store.js'",JSON.stringify(store))));
 const {Tape}=await import(store),tp=new Tape();
 tp.push({seq0:0,n:3,ts:new Float64Array([1000,1000,1000]),px:new Float32Array([100,100,100]),sz:new Uint32Array([24,25,50]),side:new Uint8Array([1,2,0])});
 const chart=Object.create(Chart.prototype),arcs=[];
 Object.assign(chart,{S:{bigLot:25,bubbleMinPx:0,bubbleScale:1.6},theme:{pos:'#00ff00',neg:'#ff0000',ink3:'#aaaaaa'},store:{tape:tp},view:{spacing:2},builder:{tf:{kind:'tick'}},X:i=>i*10,Y:p=>p});
 const ctx={beginPath(){},arc(...a){arcs.push(a)},fill(){},stroke(){}};
 chart.drawBubbles(ctx,{i0:0,i1:1},2);
 chart.drawBubbles(ctx,{i0:2,i1:2},3);
 assert.equal(arcs.length,2); // 24 excluded; inclusive 25 threshold
 assert.notEqual(arcs[0][0],arcs[1][0]); // identical timestamps, distinct bars
 assert.ok(arcs.every(a=>a[2]>=4));
 const order=[];
 Object.assign(chart,{S:{bubbles:true,vp:true},builder:{bars:[{},{},{}]},plot:{x:0,y:0,w:100},price:{y:0,h:100,footer:0},i0:0,i1:2,drawBar:(c,b,i)=>order.push('bar'+i),drawBubbles:(c,b,i)=>order.push('bubble'+i),paintVolumeProfile:()=>order.push('profile')});
 chart.paint_series({save(){},beginPath(){},rect(){},clip(){},restore(){}});
 assert.deepEqual(order,['bar0','bar1','profile','bubble0','bubble1']);
 console.log('PASS: inclusive print filter, equal-timestamp bar placement, minimum visibility, bubbles after candles/profile.');
})().catch(e=>{console.error(e);process.exitCode=1});
