const { chromium } = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs = require('node:fs');
(async()=>{
  const browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1600,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto('http://127.0.0.1:8765/',{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.getElementById('intraday-macd-j-status').textContent.includes('手动收盘扫描完成'));
    const status=await page.locator('#intraday-macd-j-status').innerText();
    if(await page.locator('#intraday-macd-j-section .alert-card, #intraday-macd-j-list').count())throw Error('homepage stock previews remain');
    const toast=await page.locator('#intraday-macd-j-toast-text').innerText();
    if(!toast.includes('43 只'))throw Error('close notification missing');
    await page.locator('#intraday-macd-j-history-open').click();
    const tab=page.getByRole('tab',{name:/手动收盘 · 43 只/});
    await tab.waitFor({state:'visible'});
    await tab.click();
    const result=page.locator('#intraday-macd-j-history-result');
    const count=await result.locator('table.stock-selection-table tbody tr').count();
    if(count!==43)throw Error(`wrong candidate count ${count}`);
    if(await result.locator('.alert-card').count())throw Error('history still contains cards');
    const headers=await result.locator('thead th').allTextContents();
    if(headers.join('|')!=='股票|收盘价|信号分组|金叉位置|J 拐头与低位标签|指标证据|数据时间')throw Error(`wrong table headers ${headers}`);
    if(await result.locator('tbody a.stock-selection-quote-link').count()!==43)throw Error('stock links missing');
    const details=result.locator('tbody details').first();
    await details.locator('summary').click();
    if(!await details.evaluate(e=>e.open))throw Error('indicator evidence did not expand');
    await details.locator('summary').click();
    const text=await page.locator('#intraday-macd-j-history-result').innerText();
    if(!text.includes('2026-09-17 收盘日线'))throw Error('price basis missing');
    await page.screenshot({path:'work/macd_j_20260917/manual-close-live.png'});
    if(errors.length)throw Error(errors.join('\n'));
    const receipt={status,tab:await tab.innerText(),headers,renderedCount:count,pageErrors:errors};
    fs.writeFileSync('work/macd_j_20260917/manual-close-ui.json',JSON.stringify(receipt,null,2));
    console.log(JSON.stringify(receipt));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
