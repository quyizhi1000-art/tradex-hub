const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
 const base='http://127.0.0.1:8765',out=__dirname;
 const source=await (await fetch(base+'/watch/app.js')).text();
 assert.equal(source,fs.readFileSync('tradex/src/tradex/dashboard/watch/app.js','utf8'));
 const c={objectValue:v=>v&&typeof v==='object'?v:{}};vm.createContext(c);
 for(const name of ['finiteNumber','macdStateLabel'])vm.runInContext(source.match(new RegExp(`  function ${name}\\([\\s\\S]*?(?=\\n  function )`))[0],c);
 const receipt=[];
 for(const day of ['2026-09-17','2026-09-18']){
  const center=await(await fetch(base+`/api/stock-selection/results?trade_date=${day}`)).json();
  const history=await(await fetch(base+`/api/stock-selection/intraday-macd-j/history?trade_date=${day}`)).json();
  const result=center.results.find(r=>r.strategy_id==='macd-j-upturn-main-board'&&r.strategy_version==='v5');
  const close=history.scans.find(s=>s.scan_kind==='close_confirmation');
  assert.equal(result.result_id,close.source_result_id);
  for(const [a,b] of [['candidates','scan_candidates'],['pending_candidates','pending_candidates']]){
   assert.equal(result.payload[a].length,close[b].length);
   for(const candidate of result.payload[a]){
    const record=close[b].find(r=>r.instrument_id===candidate.instrument_id);
    assert.deepEqual(record.evidence,candidate.evidence);
    const states=record.evidence.map((p,i)=>({date:p.trade_date,state:c.macdStateLabel(p,record.evidence[i-1],record.macd_cross_date)}));
    assert.equal(states.at(-1).state,a==='candidates'?'金叉点':record.dif===record.dea?'两线重合（未确认交叉）':'死叉上行');
    if(day==='2026-09-17'&&record.dif===-.167&&record.dea===-.170)receipt.push({example:record.name,instrument_id:record.instrument_id,states});
   }
  }
  receipt.push({day,confirmed:result.payload.matched_count,pending:result.payload.pending_count,identical_evidence:true});
 }
 fs.writeFileSync(out+'/live-verification.json',JSON.stringify(receipt,null,2));
 console.log(JSON.stringify(receipt));
})().catch(e=>{console.error(e);process.exitCode=1;});
