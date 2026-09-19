const { chromium } = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const out = __dirname;

(async () => {
  const browser = await chromium.launch({headless:true});
  const results = {};
  try {
    for (const mode of ['before','after']) {
      const context = await browser.newContext({viewport:{width:1600,height:1000}});
      const page = await context.newPage();
      const errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      if(mode==='before') await page.route('**/watch/app.js',route=>route.fulfill({
        path:path.join(out,'before/tradex__src__tradex__dashboard__watch__app.js'),contentType:'application/javascript'}));
      await page.addInitScript(()=>{
        const original=window.setInterval;
        window.setInterval=(callback,...args)=>{
          if(callback.name==='fetchIntradayMacdJ') window.__radarPoll=callback;
          return original(callback,...args);
        };
      });
      await page.goto('http://127.0.0.1:8765/',{waitUntil:'domcontentloaded'});
      await page.locator('#intraday-macd-j-history-open').click();
      const table=page.locator('#intraday-macd-j-history-result table');
      await table.locator('tbody tr').nth(1).waitFor();
      // Use the actual persisted manual scan, real page and registered polling callback.
      await page.locator('#intraday-macd-j-history-result details').first().evaluate(el=>{el.open=true;});
      const before=await page.evaluate(()=>{
        const target=document.getElementById('intraday-macd-j-history-result');
        window.__table=target.querySelector('table');
        window.__details=target.querySelector('details');
        const scroll=target.querySelector('.stock-selection-table-scroll');
        scroll.scrollTop=200;scroll.scrollLeft=90;
        window.__scroll=scroll;
        const dialog=document.querySelector('#intraday-macd-j-history-dialog .stock-selection-dialog__shell');
        dialog.scrollTop=400;
        window.__removed=0;
        window.__observer=new MutationObserver(records=>{for(const r of records) window.__removed+=r.removedNodes.length;});
        window.__observer.observe(target,{childList:true,subtree:true});
        return {rows:window.__table.tBodies[0].rows.length,top:scroll.scrollTop,left:scroll.scrollLeft,dialogTop:dialog.scrollTop,
          selected:document.querySelector('#intraday-macd-j-history-tabs [aria-selected="true"]').textContent};
      });
      const response=page.waitForResponse(r=>r.url().includes('/intraday-macd-j/history'));
      await page.evaluate(()=>window.__radarPoll());
      await response;
      await page.waitForTimeout(350);
      const after=await page.evaluate(()=>{
        const target=document.getElementById('intraday-macd-j-history-result');
        const scroll=target.querySelector('.stock-selection-table-scroll');
        return {sameTable:window.__table===target.querySelector('table'),sameDetails:window.__details===target.querySelector('details'),
          detailsOpen:target.querySelector('details').open,top:scroll.scrollTop,left:scroll.scrollLeft,
          dialogTop:document.querySelector('#intraday-macd-j-history-dialog .stock-selection-dialog__shell').scrollTop,
          removedNodes:window.__removed,selected:document.querySelector('#intraday-macd-j-history-tabs [aria-selected="true"]').textContent,
          rows:target.querySelector('tbody').rows.length};
      });
      results[mode]={before,after,errors};
      if(mode==='after'){
        assert.equal(after.sameTable,true);assert.equal(after.sameDetails,true);assert.equal(after.detailsOpen,true);
        assert.equal(after.removedNodes,0);assert.equal(after.top,before.top);assert.equal(after.left,before.left);
        assert.ok(before.dialogTop>0,'exercise actual nonzero dialog scroll');
        assert.equal(after.dialogTop,before.dialogTop);
        assert.equal(after.selected,before.selected);assert.equal(after.rows,72);assert.deepEqual(errors,[]);
        // Select a real industry and verify the same control and filter survive polling.
        const tags=page.locator('#intraday-macd-j-history-result .stock-selection-industry-tag');
        await tags.nth(1).click();
        const filter=await page.locator('#intraday-macd-j-history-result [aria-pressed="true"]').textContent();
        const next=page.waitForResponse(r=>r.url().includes('/intraday-macd-j/history'));
        await page.evaluate(()=>window.__radarPoll());await next;await page.waitForTimeout(200);
        assert.equal(await page.locator('#intraday-macd-j-history-result [aria-pressed="true"]').textContent(),filter);
        results.after.industryPreserved=filter;
        await page.locator('#intraday-macd-j-history-dialog').screenshot({path:path.join(out,'stable-scan-window.png')});
      } else assert.equal(after.sameTable,false,'baseline must reproduce table replacement');
      await context.close();
    }
    fs.writeFileSync(path.join(out,'window-verification.json'),JSON.stringify(results,null,2));
    console.log(JSON.stringify(results,null,2));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
