const {chromium}=require('C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1600,height:1000}});
 try {
  await page.goto('http://127.0.0.1:8765/',{waitUntil:'domcontentloaded'});
  await page.evaluate(()=>{
   window.closeProbe=[];window.probeLongTasks=[];
   new PerformanceObserver(list=>window.probeLongTasks.push(...list.getEntries().map(e=>({start:e.startTime,duration:e.duration})))).observe({type:'longtask',buffered:true});
   const dialog=document.getElementById('intraday-macd-j-history-dialog');
   for(const type of ['pointerdown','pointerup','click','close']) dialog.addEventListener(type,e=>{
    window.closeProbe.push({type,time:performance.now(),eventTime:e.timeStamp,open:dialog.open,target:e.target.id});
   },true);
   new MutationObserver(()=>{
    if(!dialog.open){
     window.closeProbe.push({type:'closed-mutation',time:performance.now()});
     requestAnimationFrame(()=>requestAnimationFrame(()=>window.closeProbe.push({type:'paint',time:performance.now()})));
    }
   }).observe(dialog,{attributes:true,attributeFilter:['open']});
  });
  const results=[];
  for(const delay of [0,100,0,100,0]){
   await page.locator('#intraday-macd-j-history-open').click();
   await page.locator('#intraday-macd-j-history-result tbody tr').first().waitFor();
   await page.evaluate(()=>{window.closeProbe=[];});
   await page.mouse.click(10,10,{delay});
   await page.waitForFunction(()=>window.closeProbe.some(e=>e.type==='paint'));
   results.push(await page.evaluate(()=>({events:window.closeProbe,longTasks:window.probeLongTasks.filter(e=>e.start>=window.closeProbe[0].time-100)})));
  }
  const output={results};
  const file=process.argv[2]||'dialog-close-before.json';
  fs.writeFileSync('work/macd_j_20260917/'+file,JSON.stringify(output,null,2));
  console.log(JSON.stringify(results.map(({events,longTasks})=>{
   const t=type=>events.find(e=>e.type===type)?.time;
   return {pressToRelease:t('pointerup')-t('pointerdown'),clickToClosed:t('closed-mutation')-t('click'),clickToPaint:t('paint')-t('click'),pressToPaint:t('paint')-t('pointerdown'),longTasks};
  })));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
