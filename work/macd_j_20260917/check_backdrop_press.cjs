const {chromium}=require('C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1600,height:1000}});
 try {
  await page.goto('http://127.0.0.1:8765/',{waitUntil:'domcontentloaded'});
  await page.locator('#intraday-macd-j-history-open').click();
  await page.locator('#intraday-macd-j-history-result tbody tr').first().waitFor();
  await page.evaluate(()=>{
   const button=document.createElement('button');button.id='backdrop-test-button';
   button.style='position:fixed;left:0;top:0;width:30px;height:30px;z-index:999999';
   window.backgroundClicks=0;button.onclick=()=>window.backgroundClicks++;
   document.body.append(button);
  });
  const receipts=[];
  for(const id of ['intraday-macd-j-history-dialog','stock-selection-followup-dialog']){
   if(id==='stock-selection-followup-dialog'){
    await page.evaluate(()=>{
     document.getElementById('stock-selection-dialog').showModal();
     document.getElementById('stock-selection-followup-dialog').showModal();
    });
   }
   const dialog=page.locator('#'+id);
   await dialog.locator('h2').click();
   if(!await dialog.evaluate(e=>e.open)) throw Error('content click closed '+id);
   await page.mouse.click(10,10,{button:'right'});
   if(!await dialog.evaluate(e=>e.open)) throw Error('right button closed '+id);
   await page.mouse.move(10,10);await page.mouse.down();
   if(await dialog.evaluate(e=>e.open)) throw Error('must close before release '+id);
   await page.mouse.up();
   if(await page.evaluate(()=>window.backgroundClicks)!==0) throw Error('clicked through backdrop');
   if(id==='stock-selection-followup-dialog' && !await page.locator('#stock-selection-dialog').evaluate(e=>e.open)) throw Error('parent closed too');
   await page.evaluate(id=>document.getElementById(id).showModal(),id);
   await page.keyboard.press('Escape');
   if(await dialog.evaluate(e=>e.open)) throw Error('Escape stopped working');
   receipts.push({id,closedBeforeRelease:true,contentAndRightClickPreserved:true,noClickThrough:true,escapeWorks:true});
  }
  await page.evaluate(()=>document.getElementById('stock-selection-dialog').close());
  await page.mouse.click(10,10);
  if(await page.evaluate(()=>window.backgroundClicks)!==1) throw Error('subsequent click swallowed');
  fs.writeFileSync('work/macd_j_20260917/backdrop-press-verification.json',JSON.stringify(receipts,null,2));
  console.log(JSON.stringify(receipts));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
