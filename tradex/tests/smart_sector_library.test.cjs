const test = require("node:test");
const assert = require("node:assert/strict");
const {matching, safeSource, reviewLabel} = require("../src/tradex/dashboard/watch/smart-sector-library.js");
const items = [
  {name:"三花智控", instrument_id:"002050.SZ", status:"verified", primary_sector_name:"人形机器人",chain:["机电执行器"],tags:["液冷"]},
  {name:"麦格米特", instrument_id:"002851.SZ", status:"verified", primary_sector_name:"AI算力",chain:["服务器电源"],tags:["英伟达产业链"]},
  {name:"待核验样例",instrument_id:"600001.SH",status:"unresolved",primary_sector_name:null,chain:[],tags:[]},
];

test("source concepts stay separate from verified tags and primary",()=>{
  const row = {name:"原始概念样例",status:"unresolved",primary_sector_name:null,tags:[],research:{candidate_concepts:[{name:"机器人",source:"同花顺",verified:false}]}};
  assert.equal(matching([row],{tag:"source:机器人"}).length,1);
  assert.equal(matching([row],{tag:"机器人"}).length,0);
  assert.equal(matching([row],{sector:"机器人"}).length,0);
  assert.equal(matching([row],{status:"pending",query:"机器人"}).length,1);
});
test("stock, sector, chain and tag search preserve pending coverage",()=>{
  assert.equal(matching(items,{query:"002851"})[0].name,"麦格米特");
  assert.equal(matching(items,{sector:"人形机器人"})[0].name,"三花智控");
  assert.equal(matching(items,{query:"服务器电源"}).length,1);
  assert.equal(matching(items,{tag:"液冷"}).length,1);
  assert.equal(matching(items,{status:"pending"})[0].instrument_id,"600001.SH");
  assert.equal(matching(items,{sector:"AI算力",query:"三花"}).length,0);
});
test("evidence URLs cannot execute scripts or open local files",()=>{
  assert.equal(safeSource("javascript:alert(1)"),null);
  assert.equal(safeSource("file:///secret"),null);
  assert.equal(safeSource("https://example.test/report"),"https://example.test/report");
});
test("review progress distinguishes unreviewed stocks from invalidated conclusions",()=>{
  assert.equal(reviewLabel({status:"unresolved"}),"尚未审阅");
  assert.equal(reviewLabel({status:"verified"}),"已归属");
  assert.equal(reviewLabel({status:"unresolved",reviewed_on:"2026-09-19"}),"审阅待完成");
  assert.equal(reviewLabel({status:"stale"}),"待复核 · 已到期");
  assert.equal(reviewLabel({status:"disputed"}),"待复核 · 证据冲突");
});
