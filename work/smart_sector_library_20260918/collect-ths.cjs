// Read public THS pages one stock at a time through a normal browser.
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require('C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');
const {execFileSync}=require('node:child_process');
const root=__dirname,folder=path.join(root,'ths_sequential');
const stocks=JSON.parse(fs.readFileSync(path.join(root,'stocks.json'),'utf8'));
function write(file,data){const temp=file+'.tmp';fs.writeFileSync(temp,JSON.stringify(data,null,2));fs.renameSync(temp,file);}
function prior(id){for(const suffix of ['.browser.json','.json','.search.json']){const p=path.join(folder,id+suffix);if(fs.existsSync(p)){const r=JSON.parse(fs.readFileSync(p,'utf8'));if(r.status==='read')return r;}}return null;}
function publish(){execFileSync(path.resolve('.venv/Scripts/python.exe'),['-m','tradex.smart_sector_library.research','--root',root],{windowsHide:true,stdio:'ignore',timeout:30000});}
(async()=>{
 const browser=await chromium.launch({headless:true});
 const progress={contract:'ths_sequential_collection.v1',total:stocks.length,processed:0,read:0,unavailable:0,status:'running'};
 let failed=0;
 try{
  const page=await browser.newPage();
  for(const stock of stocks){
   const existing=prior(stock.id);
   if(existing){progress.processed++;progress.read++;continue;}
   const code=stock.id.slice(0,6),url=`https://basic.10jqka.com.cn/${code}/concept.html`;
   let row={instrument_id:stock.id,name:stock.name,source_url:url,acquisition:'browser',retrieved_at:new Date().toISOString(),status:'unavailable'};
   try{
    const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:15000});
    const title=await page.title();
    if(response.status()!==200 || !title.includes(`(${code})`) || !title.includes('概念'))throw Error(`page_identity_or_status:${response.status()}:${title}`);
    const concepts=await page.locator('table.gnContent').evaluateAll(tables=>tables.flatMap(table=>{
      const rows=Array.from(table.querySelectorAll('tr')),out=[];
      rows.forEach((tr,i)=>{
       const cells=Array.from(tr.querySelectorAll('td'));
       if(cells.length<3 || !/^\d{1,3}$/.test(cells[0].textContent.trim()))return;
       const name=cells[1].textContent.trim();
       const next=rows[i+1],details=next && next.querySelectorAll('td').length===1 ? next.textContent.trim():cells.at(-1).textContent.trim();
       if(name)out.push({name,explanation:details});
      });return out;
    }));
    const body=await page.locator('body').evaluate(body=>{const copy=body.cloneNode(true);copy.querySelectorAll('script,style,noscript').forEach(n=>n.remove());return copy.textContent.replace(/[ \t]+/g,' ').replace(/\n\s*\n/g,'\n').trim();});
    if(!body.includes('概念解析'))throw Error('concept_table_missing');
    row={...row,status:'read',title,concepts,body,retrieved_at:new Date().toISOString()};failed=0;progress.read++;
   }catch(e){row.error=String(e.message);failed++;progress.unavailable++;}
   write(path.join(folder,stock.id+'.browser.json'),row);
   progress.processed++;progress.last=stock.id;progress.updated_at=new Date().toISOString();
   write(path.join(root,'ths-progress.json'),progress);
   if(progress.processed%50===0){publish();console.log(JSON.stringify(progress));}
   if(row.error?.startsWith('page_identity_or_status:403:')){progress.status='paused_access_denied';break;}
   if(failed>=3){progress.status='paused_after_three_unavailable';break;}
   if(fs.existsSync(path.join(root,'STOP_THS'))){progress.status='paused_by_operator';break;}
   await page.waitForTimeout(1000);
  }
  if(progress.processed===progress.total)progress.status='acquisition_complete_review_pending';
 }finally{await browser.close();progress.updated_at=new Date().toISOString();write(path.join(root,'ths-progress.json'),progress);publish();console.log(JSON.stringify(progress));}
})().catch(e=>{console.error(e);process.exitCode=1});
