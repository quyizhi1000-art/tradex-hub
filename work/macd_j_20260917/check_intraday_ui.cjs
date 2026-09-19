const { chromium } = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport:{width:1600,height:1000}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  try {
    await page.goto('http://127.0.0.1:8765/',{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.getElementById('intraday-macd-j-status').textContent.includes('非交易时段'));
    if(await page.locator('#sector-move-radar').count()) throw Error('old radar is still present');
    if(await page.locator('.intraday-focus-grid > #intraday-macd-j-section').count() !== 1) throw Error('wrong panel location');
    await page.locator('#intraday-macd-j-history-open').click();
    await page.waitForFunction(()=>document.getElementById('intraday-macd-j-history-status').textContent.includes('暂无扫描记录'));
    await page.locator('#intraday-macd-j-history-close').click();
    await page.locator('.intraday-focus-grid').screenshot({path:'work/macd_j_20260917/ui-intraday-live.png'});
    const actual = await (await page.request.get('http://127.0.0.1:8765/api/stock-selection/intraday-macd-j')).json();
    // Browser-only fixtures verify tab/date interaction; no archive/alert is written.
    await page.route('**/api/stock-selection/intraday-macd-j/history**',route=>{
      const day = new URL(route.request().url()).searchParams.get('trade_date') || '2026-09-17';
      const candidate={instrument_id:day==='2026-09-17'?'600000.SH':'000001.SZ',name:'交互测试样本',
        price:10,dif:.2,dea:.1,j:30,signal_group:'同日拐头',zero_axis_zone:'零轴上方',j_turn_date:day,observed_at:day+'T10:00:00+08:00'};
      return route.fulfill({json:{contract:'intraday_macd_j_history.v1',trade_date:day,
        dates:['2026-09-17','2026-09-16'],scans:[
          {last_scan_slot:day+'T11:45:00+08:00',status:'monitoring',message:'午盘复核',scan_candidates:[]},
          {last_scan_slot:day+'T10:00:00+08:00',status:'monitoring',message:'测试扫描',scan_candidates:[candidate]},
        ]}});
    });
    await page.locator('#intraday-macd-j-history-open').click();
    await page.locator('#intraday-macd-j-history-tabs button').nth(1).click();
    await page.waitForFunction(()=>document.getElementById('intraday-macd-j-history-result').textContent.includes('600000.SH'));
    await page.locator('#intraday-macd-j-history-tabs button').nth(1).press('Home');
    await page.waitForFunction(()=>document.getElementById('intraday-macd-j-history-result').textContent.includes('本次命中 0 只'));
    await page.locator('#intraday-macd-j-history-date').selectOption('2026-09-16');
    await page.locator('#intraday-macd-j-history-tabs button').nth(1).click();
    await page.waitForFunction(()=>document.getElementById('intraday-macd-j-history-result').textContent.includes('000001.SZ'));
    if((await page.locator('#intraday-macd-j-history-result').innerText()).includes('600000.SH')) throw Error('old date rows leaked');
    if(errors.length) throw Error(errors.join('\n'));
    const receipt={liveStatus:actual.status,oldRadarRemoved:true,panelInOriginalLocation:true,
      actualHistoryEmpty:true,fixtureOnlyDateAndTimeTabChecks:true,pageErrors:errors};
    fs.writeFileSync('work/macd_j_20260917/ui-intraday-acceptance.json',JSON.stringify(receipt,null,2));
    console.log(JSON.stringify(receipt));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
