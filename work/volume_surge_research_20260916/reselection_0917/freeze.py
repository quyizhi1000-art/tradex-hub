"""Freeze a second discretionary shortlist without modifying the first list."""
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OUT=Path(__file__).parent
BASE=OUT.parent
def load(p):return json.loads(p.read_text(encoding='utf-8'))
allrows=load(BASE/'picks_0917/all166-evidence.json')
byname={r['name']:r for r in allrows}
oldpath=BASE/'picks_0917/frozen-top10.json'
old=load(oldpath)
oldhash=hashlib.sha256(oldpath.read_bytes()).hexdigest()
assert oldhash=='87d2e4565250222047b8649c16635cd11bb20997bfc10970a7852729e41e6b26'
snapshot=load(BASE/'snapshot-2026-09-16.json')
histories={h['instrument_id']:h['bars'] for h in snapshot['candlestick_histories']}
names=['鸿远电子','振江股份','健盛集团','华微电子','亚玛顿','天奥电子','三祥新材','泰永长征','长裕集团','木林森']
reasons={
 '鸿远电子':('放量后突破延续','9/11建立底线，9/14再次出现合格放量；9/16收51.59元，超过此前14日高点51.37元。整理后重新向上，符合新版关注点。','当日量能1.34倍，突破幅度仅0.43%；仍需后续确认，分时均价缺失。','后续是否保持51.37元附近突破区域，并继续向52.13元当日高点推进；这不是自动交易条件。'),
 '振江股份':('连续放量确认','9/15建立底线，9/16再次放量3.22倍，收盘位于日内振幅约91%的位置，最后一小时上涨0.73%。','只有一个后续交易日，不能称作充分整理；距此前14日高点仍约3.05%。','后续是否继续接近23.67元当日高点，并保持成交承接。'),
 '健盛集团':('当日放量突破','9/16放量2.77倍上涨4.32%，收盘超过此前14日高点约3.16%；最后一小时继续上涨1.01%。','首次建立底线，尚未经历后续交易日检验；突破后同样可能回落。','后续是否维持突破区域，接近13.15元当日高点时是否出现持续买盘。'),
 '华微电子':('较早放量后的再转强','9/11建立底线，窗口内有两次合格信号；9/16量能1.48倍，收盘超过此前14日高点，最后一小时上涨2.59%。','距有效底线已约16.75%，位置并不低；当日增量还没有达到2倍信号标准。','尾盘转强能否延续，后续是否站稳此前高点并接近11.20元。'),
 '亚玛顿':('当日放量突破','9/16量能3.41倍，涨3.58%，收盘略超此前14日高点；午后与尾盘有恢复。','突破幅度仅约0.18%，可能是假突破；底线当天刚建立。','后续是否保持原高点上方，并突破16.96元当日高点。'),
 '天奥电子':('当日放量走强','9/16上涨6.79%，量能2.64倍，收盘略超此前14日高点，最后一小时上涨1.31%。','已经出现较大单日涨幅，短期动能可能提前释放；尚无后续整理检验。','后续是否继续保持强势，接近16.38元当日高点时是否出现明显回吐。'),
 '三祥新材':('缩量趋势保持','9/9放量后底线始终有效，9/16量能仅0.74倍仍涨3.27%，收盘比原放量日高6.96%。','流通市值约266亿元，并非小盘弹性逻辑；缺少新的放量信号且尚未突破此前14日高点。','后续是否由缩量保持转为买盘增强，并接近45.41元与45.73元两层高点。'),
 '泰永长征':('放量后缩量延续','9/15放量建立底线，9/16量小于放量日但仍为此前5日均量的1.65倍，收盘接近日内最高；最后一小时上涨0.61%。','只有一天后续确认，尚未突破此前14日高点；属于早期延续，非成熟横盘。','后续是否保持17.02元底线之上的结构，并向18.24元当日高点继续推进。'),
 '长裕集团':('高换手放量异动','9/16上涨6.61%，量能2.78倍，流通市值约20.79亿元；59.28元当日高点接近此前59.30元高点。','换手24.28%，冲高后回吐2.36%，分歧较大。解除旧评分的重罚不等于已证明高换手有优势。','后续能否消化高换手，接近59.28—59.30元时继续形成有效承接。'),
 '木林森':('放量后整理再突破','9/14建立底线，随后两个交易日量小于放量日；9/16涨4.38%、量能1.26倍，收盘超过此前14日高点约2.82%。','距底线约14.42%，不是贴近支撑的低位形态；存在突破回吐风险。','后续是否维持突破区域、接近12.45元当日高点并保持分时承接。'),
}
selected=[]
checks=[]
for rank,name in enumerate(names,1):
    r=dict(byname[name]); key=r['instrument_id']
    paths=[BASE/'picks_0917'/f'minutes-{key}.json',BASE/'selection_comparison'/f'minutes-{key}.json']
    path=next(p for p in paths if p.exists())
    series=load(path); ps=series['points']
    assert series['trading_date']=='2026-09-16' and len(ps)==241
    day=next(b for b in histories[key] if b['trade_date']=='2026-09-16')
    assert abs(ps[-1]['price']-day['close'])<.011
    assert abs(max(p['high'] for p in ps)-day['high'])<.011
    assert abs(min(p['low'] for p in ps)-day['low'])<.011
    vr=sum(p['volume_shares'] for p in ps)/day['volume_shares']
    assert abs(vr-1)<.001
    p14=next(p for p in ps if p['minute']=='14:00:00')
    kind,reason,risk,confirmation=reasons[name]
    r.update(priority=rank,pattern=kind,selection_reason=reason,counterevidence=risk,followup_observation=confirmation,
             minute_path=str(path),minute_source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
             last_hour_return_pct=(r['close']/p14['price']-1)*100,
             vwap_available_points=sum(p['cumulative_average_price'] is not None for p in ps),
             has_post_anchor_session=r['anchor_date']<'2026-09-16')
    # The old score describes the previous heuristic, not the new ordering.
    r.pop('research_score');r.pop('score_components')
    selected.append(r)
    checks.append({'instrument_id':key,'minute_points':len(ps),'volume_ratio':vr,'quality':series['metadata']['quality'],
                   'quality_flags':series['metadata']['quality_flags']})
newset=set(names);oldset={r['name'] for r in old['selected']}
result={
 'created_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
 'evidence_cutoff':'2026-09-16 close','target_trade_date':'2026-09-17',
 'research_version':'reselection-v2','strategy_version':'v2',
 'selection_basis':'discretionary evidence priority; no calibrated probabilities; not an optimized strategy',
 'source_result_id':old['source_result_id'],'source_snapshot_revision':old['source_snapshot_revision'],
 'reviewed_daily_candidate_count':len(allrows),'selected_minute_verified_count':10,
 'older_anchor_count':sum(r['has_post_anchor_session'] for r in selected),
 'old_list_sha256':oldhash,'retained':sorted(oldset&newset),'added':[n for n in names if n not in oldset],
 'removed':[r['name'] for r in old['selected'] if r['name'] not in newset],
 'unresolved_additional_minute_review':['征和工业','茶花股份','万润股份'],
 'additional_minute_failure':'2026-09-17 realtime minute batch empty; no interpolation or replacement',
 'external_check_limitation':'Search snippet for 运机集团 showed a conflicting 9/16 row; opened page only contained data through 9/2. No web row was substituted into the canonical snapshot. Conflict remains unverified.',
 'selected':selected,'verification':checks,
}
(OUT/'frozen-top10-v2.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
fields=['priority','name','instrument_id','pattern','close','change_pct','volume_multiple','anchor_date','anchor_low','day_high','selection_reason','counterevidence']
with (OUT/'9月17日新版10只.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:r[k] for k in fields} for r in selected)
lines=['# 9月17日新版10只候选','',
 '基于2026年9月16日收盘档案，重新审阅全部166只v2候选的日线特征，并复核最终10只的既有分钟证据。没有使用9月17日行情。目标仍为9月17日启动/收盘涨停研究；顺序是主观证据优先级，不是校准后的涨停概率。','',
 '本次调整：取消旧评分中对信号年龄的机械惩罚、对高换手的机械重罚，不把缩量本身当成利好。先区分首次放量、放量后延续、缩量修复和弱势未破位，再结合突破位置、收盘强弱及分时恢复。没有为凑比例设置分组配额，最终得到6只有后续交易日、4只当日首次建立底线的名单。','',
 '这些是新的研究偏好，未经历史结果验证，不能称作策略已优化。只有1个后续交易日的振江和泰永，也不能描述为充分整理。','',
 '| 顺序 | 股票 | 形态 | 9/16收盘 | 涨幅 | 量能倍数 | 底线日期/价格 |','|---|---|---|---:|---:|---:|---|']
for r in selected:
    lines.append(f"| {r['priority']} | {r['name']} {r['instrument_id']} | {r['pattern']} | {r['close']:.2f} | {r['change_pct']:.2f}% | {r['volume_multiple']:.2f} | {r['anchor_date']} / {r['anchor_low']:.2f} |")
lines+=['','量能倍数=全天成交股数/此前5日平均成交股数，不是行情软件的日内量比。底线是当前有效轮次首次合格放量日最低价，不是建议止损价。','']
for r in selected:
    lines += [f"## {r['priority']}. {r['name']}",'',r['selection_reason'],'','反证：'+r['counterevidence'],'','后续观察：'+r['followup_observation'],'']
lines += ['## 相对上一版的变化','',
 '新增：'+ '、'.join(result['added'])+'。','',
 '移出：运机集团、双枪科技、侨银股份、万盛股份。运机、双枪均属当日较大涨幅启动，只有当天底线；此次更重视已有后续结构的候选，不把涨幅最大自动排在前面。侨银虽强，但距底线约17.70%、当天量能0.96倍，按此次偏好下调；华微同样离底线较远，保留依据是两次信号、1.48倍量能和尾盘恢复，这属于主观取舍。万盛最后一小时回落约1.13%，相较保留的首次放量候选，尾盘承接弱一些。','',
 '保留旧名单原文件，单独冻结新版，不事后改写。名单变化不等于被移出股票不可能涨停，也不是证明新增股票更优。','',
 '用户原名单中纳入鸿远电子、三祥新材、长裕集团。赛伍技术分时修复较好，但仅凭0.84倍量能的温和回升，隔日爆发依据仍不足；夏厦精密盘中突破后较日内最高回吐5.73%；吉大正元处于明显弱势。这些股票不因用户选过就自动进入新版。','',
 '## 数据与限制','',
 '日线来自既有Tushare标准化快照（provider_as_of：9/16 18:00 +08:00）。全池档案因55只历史窗口不完整而degraded，入选10只的所需窗口完整。分钟provider_as_of为9/16 15:00；10只均为241点，日线收盘/高低价误差小于0.011元、股数相对误差小于0.001。只有健盛集团、华微电子、木林森3只均价序列完整，其余不作全天均价判断。','',
 '全部166只审阅日线，不代表166只全部完成分时或公告核验。征和工业、茶花股份、万润股份追加分钟查询返回空数据，未将其列为分时已核实候选，可能遗漏更优候选。无补值，没有修改选股产品逻辑或原档案。','',
 '公开检索中，运机集团的历史行情摘要与本地9/16存档出现不同数值；打开对应网页却只有截至9/2的旧数据，未能核实9/16行。因此保留为未解决的外部来源差异，不混入本次计算，也不据此判定本地存档错误。参考：https://cn.investing.com/equities/sichuan-zigong-conveying-machine-historical-data 。本次未完成系统公告、催化和独立行情源交叉核验。','',
 '后续分别检验原版、新版和用户12只的次日触板与收盘涨停，以及1/3/5/10交易日首次命中和未命中占比。单日胜负不足以证明长期有效，当前未计算或承诺涨停概率。','']
(OUT/'9月17日新版10只分析.md').write_text('\n'.join(lines),encoding='utf-8')
assert hashlib.sha256(oldpath.read_bytes()).hexdigest()==oldhash
print(json.dumps({k:result[k] for k in ['created_at','added','retained','removed','older_anchor_count']},ensure_ascii=False,indent=2))
print('verified final 10 minute curves; original file unchanged')
