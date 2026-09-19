const { chromium } = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1600, height: 1000}});
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto('http://127.0.0.1:8765/', {waitUntil: 'domcontentloaded'});
    await page.locator('#stock-selection-open-button').click();
    await page.getByRole('tab', {name: 'MACD 金叉 + J 线拐头', exact: true}).click();
    const day = process.argv[2] || '2026-09-16';
    await page.locator('#stock-selection-date-select').selectOption(day);
    await page.waitForFunction(day => !document.getElementById('stock-macd-j-content').hidden
      && document.getElementById('stock-macd-j-table-body').textContent.includes(day), day);
    const summary = await page.locator('#stock-macd-j-summary').innerText();
    const counts = {};
    for (const group of ['all', 'same_day', 'prior_3_sessions']) {
      await page.locator('#stock-macd-j-group').selectOption(group);
      counts[group] = await page.locator('#stock-macd-j-table-body tr').count();
    }
    if (counts.all !== counts.same_day + counts.prior_3_sessions) throw Error('group counts mismatch');
    await page.locator('#stock-macd-j-group').selectOption('all');
    await page.locator('#stock-macd-j-table-body details').first().locator('summary').click();
    if (!await page.locator('#stock-macd-j-table-body details').first().getAttribute('open') &&
        !await page.locator('#stock-macd-j-table-body details').first().evaluate(el => el.open)) throw Error('details not expanded');
    await page.locator('#stock-selection-panel-macd-j').screenshot({path: `work/macd_j_20260917/ui-${day}.png`});
    const historic = await page.locator('#stock-selection-date-select option').evaluateAll(nodes => nodes.map(n => n.value).find(v => v && v < '2026-09-16'));
    if (historic) {
      await page.locator('#stock-selection-date-select').selectOption(historic);
      await page.locator('#stock-macd-j-empty').waitFor({state: 'visible'});
      if (await page.locator('#stock-macd-j-table-body tr').count()) throw Error('old rows were not cleared');
    }
    if (errors.length) throw Error(errors.join('\n'));
    const receipt = {day, summary, counts, olderMissingArchiveClearsRows: !!historic, pageErrors: errors};
    fs.writeFileSync(`work/macd_j_20260917/ui-${day}.json`, JSON.stringify(receipt, null, 2));
    console.log(JSON.stringify(receipt));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
