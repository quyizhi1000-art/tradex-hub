const { chromium } = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1600,height:1000}});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  try {
    await page.goto('http://127.0.0.1:8765/', {waitUntil:'domcontentloaded'});
    await page.waitForFunction(() => document.getElementById('intraday-macd-j-status').textContent.includes('收盘确认'));
    const status = await page.locator('#intraday-macd-j-status').innerText();
    await page.locator('#intraday-macd-j-history-open').click();
    const finalTab = page.getByRole('tab', {name:/收盘确认 · 56 只/});
    await finalTab.click();
    const result = page.locator('#intraday-macd-j-history-result');
    const confirmed = await result.locator('tbody tr').count();
    if (confirmed !== 56) throw Error(`confirmed count ${confirmed}`);
    if (!(await result.locator('thead').innerText()).includes('收盘价')) throw Error('missing close label');
    const ids = await result.locator('tbody td:first-child small').allTextContents();
    await page.screenshot({path:'work/macd_j_20260917/close-confirmed-live.png'});
    await page.getByRole('tab', {name:/收盘预估 · 43 只/}).click();
    if (await result.locator('tbody tr').count() !== 43) throw Error('old estimate lost');
    if (!(await result.innerText()).includes('属于预估')) throw Error('old estimate not labelled');
    await page.locator('#intraday-macd-j-history-close').click();
    await page.locator('#stock-selection-open-button').click();
    await page.waitForFunction(() => !document.getElementById('stock-selection-status').textContent.includes('正在读取'));
    console.log('center status:', await page.locator('#stock-selection-status').innerText());
    await page.locator('#stock-selection-date-select').selectOption('2026-09-17');
    await page.getByRole('tab', {name:'MACD 金叉 + J 线拐头',exact:true}).click();
    await page.waitForFunction(() => document.getElementById('stock-macd-j-table-body').querySelectorAll('tr').length === 56);
    const center = await page.locator('#stock-macd-j-table-body tr').count();
    const centerText = await page.locator('#stock-macd-j-table-body').innerText();
    if (!ids.every(id => centerText.includes(id))) throw Error('displayed IDs differ');
    if (errors.length) throw Error(errors.join('\n'));
    const receipt = {status,confirmed,center,sameDisplayedIds:true,oldEstimatePreserved:43,pageErrors:errors};
    fs.writeFileSync('work/macd_j_20260917/close-parity-ui.json',JSON.stringify(receipt,null,2));
    console.log(JSON.stringify(receipt));
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1);});
