const {chromium}=require('C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const base='http://127.0.0.1:8765';
(async()=>{
  const get=async route=>{const r=await fetch(base+route);assert.equal(r.status,200);return r.json();};
  const watch=await get('/api/stock-selection/intraday-macd-j');
  const history=await get('/api/stock-selection/intraday-macd-j/history?trade_date=2026-09-18');
  const raw=JSON.parse(fs.readFileSync(path.join(__dirname,'forced-raw.json'),'utf8'));
  const baseline=JSON.parse(fs.readFileSync(path.join(__dirname,'baseline-archive.json'),'utf8'));
  const old=new Set(baseline.payload.candidates.map(c=>c.instrument_id));
  const expected=raw.scan_candidates.filter(c=>!old.has(c.instrument_id)).map(c=>c.instrument_id).sort();
  assert.deepEqual(watch.scan_candidates.map(c=>c.instrument_id).sort(),expected);
  assert.equal(watch.comparison.new_count,229);
  assert.equal(watch.comparison.removed_count,138);
  const current=history.scans.find(s=>s.last_scan_slot===raw.last_scan_slot);
  assert.deepEqual(current.scan_candidates,watch.scan_candidates);
  for(const [url,file] of [['/watch/app.js','app.js'],['/','index.html']]){
    const served=Buffer.from(await(await fetch(base+url)).arrayBuffer());
    assert.ok(served.equals(fs.readFileSync('tradex/src/tradex/dashboard/watch/'+file)),file+' exact served bytes');
  }
  const browser=await chromium.launch({headless:true});
  const receipt={apiDifferenceExact:true,comparison:watch.comparison,servedAssetsMatch:true};
  try{
    const page=await browser.newPage({viewport:{width:1600,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(()=>{const original=window.setInterval;window.setInterval=(cb,...args)=>{
      if(cb.name==='fetchIntradayMacdJ')window.__poll=cb;return original(cb,...args);};});
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.locator('#intraday-macd-j-history-open').click();
    await page.locator('#intraday-macd-j-history-result tbody tr').nth(1).waitFor();
    assert.equal(await page.locator('#intraday-macd-j-history-result tbody tr').count(),229);
    assert.match(await page.locator('#intraday-macd-j-history-result').innerText(),/本轮新增 229 只/);
    await page.evaluate(()=>{
      const target=document.getElementById('intraday-macd-j-history-result');
      window.__table=target.querySelector('table');window.__details=target.querySelector('details');
      window.__details.open=true;
      document.querySelector('#intraday-macd-j-history-dialog .stock-selection-dialog__shell').scrollTop=400;
    });
    const response=page.waitForResponse(r=>r.url().includes('/intraday-macd-j/history'));
    await page.evaluate(()=>window.__poll());await response;await page.waitForTimeout(200);
    receipt.window=await page.evaluate(()=>({sameTable:window.__table===document.querySelector('#intraday-macd-j-history-result table'),
      detailsOpen:window.__details.open,scrollTop:document.querySelector('#intraday-macd-j-history-dialog .stock-selection-dialog__shell').scrollTop}));
    assert.deepEqual(receipt.window,{sameTable:true,detailsOpen:true,scrollTop:400});
    await page.locator('#intraday-macd-j-history-dialog').screenshot({path:path.join(__dirname,'radar-v2.png')});
    await page.keyboard.press('Escape');
    await page.locator('#stock-selection-open-button').click();
    await page.getByRole('tab',{name:'MACD 金叉 + J 线拐头',exact:true}).click();
    await page.locator('#stock-selection-date-select').selectOption('2026-09-17');
    await page.waitForFunction(()=>document.getElementById('stock-macd-j-rule').textContent.includes('v2') &&
      document.querySelectorAll('#stock-macd-j-table-body tr').length===243);
    receipt.dailySummary=await page.locator('#stock-macd-j-summary').innerText();
    receipt.dailyRule=await page.locator('#stock-macd-j-rule').innerText();
    await page.locator('#stock-selection-panel-macd-j').screenshot({path:path.join(__dirname,'daily-v2.png')});
    await page.locator('#stock-selection-date-select').selectOption('2026-09-16');
    await page.waitForFunction(()=>document.getElementById('stock-macd-j-rule').textContent.includes('v1'));
    receipt.legacyRule=await page.locator('#stock-macd-j-rule').innerText();
    assert.deepEqual(errors,[]);receipt.pageErrors=errors;
    fs.writeFileSync(path.join(__dirname,'live-verification.json'),JSON.stringify(receipt,null,2));
    console.log(JSON.stringify(receipt,null,2));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
