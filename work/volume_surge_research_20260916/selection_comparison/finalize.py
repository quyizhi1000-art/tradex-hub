"""Publish the bounded point-in-time comparison from existing research evidence."""
import hashlib
import json
import os
import sys
from pathlib import Path

OUT = Path(__file__).parent
BASE = OUT.parent

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

rows = read(OUT / 'user12-daily.json')
summary = read(OUT / 'comparison-summary.json')
minutes = read(OUT / 'user12-minute.json')
frozen = BASE / 'picks_0917/frozen-top10.json'
assert hashlib.sha256(frozen.read_bytes()).hexdigest() == summary['assistant_frozen_file_sha256']
assert len(rows) == 12 and len({r['instrument_id'] for r in rows}) == 12
assert all(m['points'] == 241 and m['date'] == '2026-09-16' for m in minutes)

interpretations = {
    '长裕集团': ('当日放量、小流通盘、试探前高', '上涨6.61%，量能2.78倍，换手24.28%，流通市值20.79亿元。最高59.28元接近此前14日高点59.30元，收57.88元。', '这只更接近我的动量思路，并非缩量回踩。高换手显示交易拥挤和分歧，也可能形成充分换手；仅凭此不能断定出货。我的评分对高换手惩罚很重，未证明该惩罚有效。'),
    '夏厦精密': ('小流通盘、异常放量后的突破尝试', '上涨3.69%，量能4.90倍，流通市值8.36亿元。盘中57.23元超过此前高点56.78元，收盘53.95元，较日内最高回落5.73%；最后一小时回升1.79%。', '有异动，但收盘没有确认突破。下午修复和全天较大的上影线同时存在，不能把巨量直接解释为吸筹或洗盘。'),
    '三祥新材': ('旧放量信号后的缩量趋势延续', '9月9日建立底线39.32元；9月16日收44.85元、上涨3.27%，量能0.74倍，较放量日收盘仍高6.96%。', '这不是贴近底线的低位埋伏，而是价格已经抬高后的缩量延续。旧信号并未自动失去意义，我的信号新鲜度偏好会低估这种结构；但当日没有新的放量触发。'),
    '西昌电力': ('回踩放量底线后的反弹', '底线10.68元，9月16日最低10.71元、收11.15元；上涨3.05%，量能1.26倍，仍比原放量日收盘低2.11%。', '支撑附近出现反弹是正面证据；盘中最高11.46元后回落，当前仍属修复，并未突破。能否继续走强要看后续承接，不能仅因守底就推断次日加速。'),
    '鸿远电子': ('多次量能确认后的趋势延续', '9月11日建立底线46.92元，9月14日再次出现合格放量信号。9月16日上涨2.59%、量能1.34倍，收51.59元超过此前14日高点51.37元。', '这是你12只中唯一收盘超过此前14日高点的股票，延续证据相对清楚。与我选天奥电子的区别主要是启动阶段和当日动量强度，不是完全不同的方向。'),
    '百达精工': ('下跌后的支撑测试和反弹', '底线12.55元，当日最低12.56元，收13.05元；上涨2.35%、量能0.99倍，但近5日下跌5.91%，收盘低于原放量日收盘4.04%。', '最高13.50元后回落3.33%，收盘低于当日成交均价约0.60%。支撑尚有效，但日线弱势和分时回吐没有消失；不能把反弹等同于反转。'),
    '赛伍技术': ('缩量整理后的温和修复', '底线11.12元，收11.51元，上涨1.86%、量能0.84倍；收盘处于当日振幅约89%的位置。', '完整分钟均价序列中，约92%的分钟价格位于均价附近或以上，收盘较最高仅回落0.35%。修复质量比只看涨幅更好，但缺少次日爆发的直接证据。'),
    '八方股份': ('放量后的短期整理', '9月14日建立底线26.62元，收28.28元，上涨1.29%、量能0.88倍，较放量日收盘低1.67%。换手仅0.90%。', '上午回落后有所恢复，形态还在整理阶段。较低换手可以对应抛压减少，也可以对应买盘不足，无法单独支持即将涨停。'),
    '深南电路': ('大市值股票的趋势整理', '9月8日建立底线370.01元，9月16日收392.50元，上涨0.71%、量能0.93倍，仍高于放量日收盘2.61%。流通市值约2610亿元、当日成交额29.28亿元。', '并非缺乏流动性，而是股本规模和日内波动机制与小盘股不同。趋势维持有证据；要解释次日10%涨停，仅靠这段整理还不够。我的市值偏好会主动降低这类股票排名。'),
    '华阳股份': ('支撑附近的日内收复', '底线8.87元，当日最低8.92元，收9.18元；上涨0.22%、量能0.77倍，较放量日收盘低3.87%。', '虽然收盘接近日内最高，但只是从早盘下跌中收复至接近平盘。强收盘位置不等于强趋势，更不能独立作为隔日涨停信号。'),
    '宝新能源': ('缩量等待型', '底线5.09元，收5.25元、下跌0.19%；量能仅0.48倍，收盘低于原放量日收盘2.96%。', '缩量和底线未破成立，新的主动上涨证据不足。把成交萎缩称作卖压枯竭，需要后续买盘验证；现有数据同样允许缺乏买盘这一解释。'),
    '吉大正元': ('接近失效底线的逆势反转假设', '底线17.22元，收17.43元，当日下跌4.86%，收盘处于当日振幅底部约9%的位置；距底线仅约1.22%（以底线为分母）。', '全天仅约4%的分钟价格在成交均价附近或以上，分时明显偏弱。它尚未触发规则失效，但已出现价格走弱；如果把它归为健康洗盘，就是超出现有证据。这组中我最不认同其隔日启动依据。'),
}

lines = [
    '# 2026年9月16日：用户12只与既有10只候选的选股逻辑比较',
    '',
    '核心判断：用户名单整体更偏向“已有放量记录，整理或回落后仍守住底线，等待再次启动”；我此前的名单更偏向“当日动量已增强，期待隔日延续”。这是从名单反推的形态偏好，不是对用户真实决策过程的确认。两组均来自同一v2候选池，不能据此判断哪组有更高涨停率。',
    '',
    '## 数据与比较边界',
    '',
    '- 截止2026年9月16日收盘，目标是讨论9月17日的启动证据；不含9月17日行情，不使用未来结果解释当前选择。',
    '- 9月16日v2池166只；用户12只均在池内，与我冻结的10只无重合。原10只名单及文件哈希保持不变。',
    '- 用户已确认鸿源电子指鸿远电子（603267.SH），百大精工指百达精工（603331.SH）。',
    '- 日线来自既有Tushare标准化快照，provider_as_of为2026-09-16 18:00 +08:00；分钟来自同一标准化网关，provider_as_of为15:00。12只均有241个分钟点，收盘、高低价和成交股数与日线核对通过。',
    '- 全池档案因55只股票的历史窗口不完整标记degraded，本次12只日线窗口完整。分钟均价完整的仅百达精工、赛伍技术、宝新能源、吉大正元4只；其他8只均价部分或全部缺失，不据缺失均价推断全天均价承接，不插值。',
    '- 未系统审阅当晚公告或题材催化，因此不根据公司名称倒推题材驱动、机构吸筹或主力意图。',
    '',
    '## 两组的可验证差别',
    '',
    '| 指标 | 用户12只 | 我的10只 |',
    '|---|---:|---:|',
]
u, a = summary['user'], summary['assistant']
for label, key in [('9月16日出现新合格放量信号','new_event_today'),('当日成交量小于此前5日均量','volume_below_5d_average'),('收盘超过此前14日最高价','close_above_prior14_high'),('已经历底线建立后的后续交易日','already_has_subsequent_days'),('收盘低于底线建立日的收盘价','close_below_anchor_close')]:
    lines.append(f"| {label} | {u[key]}/{u['n']} | {a[key]}/{a['n']} |")
for label, key, fmt in [('当日涨幅中位数','change_pct','{:.2f}%'),('成交量/此前5日均量中位数','volume_multiple','{:.2f}倍'),('近5日涨幅中位数','five_day_return_pct','{:.2f}%'),('最近放量信号距今交易日中位数','last_event_age','{:.1f}天'),('收盘相对底线距离中位数','anchor_margin_pct','{:.2f}%'),('流通市值中位数','float_cap_100m','{:.2f}亿元')]:
    lines.append(f"| {label} | {fmt.format(u['medians'][key])} | {fmt.format(a['medians'][key])} |")
lines += [
    '',
    '量能倍数为全天成交股数/此前5个交易日成交股数平均值，并非行情软件的日内量比。相对底线距离=(收盘/底线−1)，不是从当前价跌至底线的收益率。以上连续变量均为中位数。',
    '',
    '用户12只中的10只经过后续交易日，较充分体现新版“放量以后守住底线”的筛选条件；我的10只中6只是当天刚建立底线，尚未受到后续整理检验。这不证明整理必优于启动，但说明我的排序覆盖在v2过滤器之上，额外偏向了当日动量。',
    '',
    '## 对12只的独立解释和反证',
    '',
]
for r in rows:
    label, evidence, critique = interpretations[r['name']]
    lines += [f"### {r['name']}（{r['instrument_id']}）：{label}", '', evidence, '', critique, '']
lines += [
    '## 对我的旧选择的批评',
    '',
    '原排名将新鲜信号、当日量能、强收盘和接近前高作为加分项，属于未经涨停结果校准的启发式偏好。不能用这个评分低来证明用户的股票差，那只是用我的偏好重复解释我的选择。',
    '',
    '第一，最近放量信号越旧分数越低，可能错失经历数日健康整理的股票。第二，优先当日放量，容易把已经消耗较多短期动能的股票排在前面。第三，换手偏好对长裕集团24.28%的换手惩罚较重，却没有历史证据证明它比中等换手更不容易隔日涨停。第四，小市值偏好会降低深南电路这类大市值趋势股的排名，不能据此宣称其趋势更弱。',
    '',
    '具体到相近行业：鸿远电子与天奥电子分别表现为温和延续和当日更强放量；赛伍技术与亚玛顿分别表现为缩量修复和放量上行。存在很多“阶段偏好”的差别，不是双方对所有产业方向都判断相反。',
    '',
    '## 对用户名单的批评',
    '',
    '这12只混合了首次放量、趋势延续、缩量修复和明显走弱四类状态。共同的“底线没破”只能说明策略没有否决它们，不能证明它们距离启动同样近。',
    '',
    '我较认可鸿远电子的突破延续证据、三祥新材的趋势保持证据，以及赛伍技术的分时修复质量。但“认可结构”并不是9月17日涨停预测。百达精工、华阳股份、宝新能源更缺少买盘重新增强的证据；吉大正元则已有相当直接的日线和分时弱势信号。夏厦精密的异常成交需要结合突破失败和后续修复一起看，不能仅凭4.90倍放量定性。',
    '',
    '用户并不是一贯偏小盘：夏厦精密流通市值约8.36亿元，深南电路约2610亿元，跨度很大；两组流通市值中位数其实接近。也不能将所有股票统一解释为低位埋伏，三祥新材已经明显高于原放量日收盘。',
    '',
    '## 当前滚动窗口的特殊影响',
    '',
    '百达精工、赛伍技术、深南电路目前唯一合格放量事件在9月8日。9月17日收盘后的最近7个交易日从9月9日起计算；若无新的合格放量事件，这3只将不再满足当前滚动窗口要求。旧信号过期不等于价格破位，也不是用户已经设定了卖出条件。',
    '',
    '## 对优化的含义',
    '',
    '应把“底线仍有效”和“现在出现启动证据”分开。前者保留观察资格；后者可研究重新放量上行、有效站回整理区上沿、强收盘、分时回落后的恢复程度。这些只是待检验特征，不宜现在凭这12只反推一组最优阈值。',
    '',
    '对9月17日涨停目标，需要比较两组的次日触板和收盘涨停比例；对用户原本“后续有多少、多久才涨停”的目标，还应统计1、3、5、10个交易日累计首次涨停比例、首次命中的交易日序号以及未命中比例。不能只在后来涨停的股票中计算等待时间而隐去没涨停的股票，也不能只研究赢家总结规律。',
    '',
    '当观察期不完整时，标记尚未观察满，不提前算作失败。需要等待更早入选样本或未来行情完成验证。两组只有10只和12只、且来自同一交易日，即使某一组次日胜出也不能证明长期优势。本次保留原名单和当前推断，不事后替换。',
    '',
    '## 图与证据',
    '',
    '- 图册：用户12只日线与分时.pdf（3页，每页4只）；对应PNG文件为走势对照-1.png至走势对照-3.png。',
    '- comparison-summary.json：统计及冻结名单哈希。',
    '- user12-daily.json / user12-minute.json：逐股日线特征与分钟检查。',
    '- 用户12只证据明细.csv：可筛选明细。',
    '- verification.json：范围、数据质量和文件哈希。',
]
(OUT / '两组选股逻辑对照.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')

sys.path.append(os.path.join(os.environ['TEMP'], 'tradex-research-plotdeps'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei'], 'axes.unicode_minus':False,
                     'font.size':9, 'axes.spines.top':False, 'axes.spines.right':False})
snapshot = read(BASE / 'snapshot-2026-09-16.json')
histories = {h['instrument_id']:h['bars'] for h in snapshot['candlestick_histories']}
red, green, blue, gold = '#cf4646', '#158776', '#3165a7', '#bb8528'
with PdfPages(OUT / '用户12只日线与分时.pdf') as pdf:
    for page in range(3):
        fig, axes = plt.subplots(4, 2, figsize=(14, 15))
        fig.subplots_adjust(top=.935, bottom=.11, hspace=1.02, wspace=.20)
        fig.suptitle(f'9月16日收盘证据 · 用户12只候选（{page+1}/3）', fontsize=17, y=.978)
        for row_idx, r in enumerate(rows[page*4:page*4+4]):
            bars = sorted(histories[r['instrument_id']], key=lambda b:b['trade_date'])
            bars = [b for b in bars if b['trade_date'] <= '2026-09-16'][-15:]
            ax, minute_ax = axes[row_idx]
            xs = list(range(len(bars)))
            for x,b in zip(xs,bars):
                color = red if b['close'] >= b['open'] else green
                ax.vlines(x,b['low'],b['high'],color=color,lw=1)
                if b['open'] == b['close']:
                    ax.hlines(b['close'],x-.27,x+.27,color=color,lw=1)
                else:
                    ax.add_patch(Rectangle((x-.27,min(b['open'],b['close'])),.54,abs(b['close']-b['open']),facecolor=color))
            ax.axhline(r['anchor_low'],color=gold,ls='--',lw=1,label=f"底线 {r['anchor_low']:.2f}")
            ix = next(i for i,b in enumerate(bars) if b['trade_date']==r['anchor_date'])
            ax.axvline(ix,color=gold,ls=':',alpha=.65)
            ax.set_title(f"{r['name']} {r['instrument_id']} · {interpretations[r['name']][0]}",loc='left',fontsize=10)
            ticks = sorted(set(xs[::3] + [xs[-1]]))
            ax.set_xticks(ticks, [bars[i]['trade_date'][5:] for i in ticks])
            ax.set_ylabel('元'); ax.grid(alpha=.15); ax.legend(loc='upper left',fontsize=7)
            ax.set_xlim(-.7,len(bars)-.3)
            vol = ax.inset_axes([0,-.43,1,.23])
            vol.bar(xs,[b['volume_shares']/1e6 for b in bars],color=[red if b['close']>=b['open'] else green for b in bars],width=.6)
            vol.set_xlim(ax.get_xlim());vol.set_xticks([]);vol.tick_params(labelsize=7);vol.set_ylabel('百万股',fontsize=7)
            series = read(OUT/f"minutes-{r['instrument_id']}.json")
            ps = series['points']; prev = bars[-1]['previous_close']
            minute_ax.plot(range(len(ps)),[(p['price']/prev-1)*100 for p in ps],color=blue,lw=1,label='分钟价格')
            avg = [(p['cumulative_average_price']/prev-1)*100 if p['cumulative_average_price'] is not None else np.nan for p in ps]
            minute_ax.plot(range(len(ps)),avg,color=gold,lw=.9,label='成交均价（缺失处断开）')
            minute_ax.axhline(0,color='#999',lw=.7,ls='--');minute_ax.grid(alpha=.15)
            minute_ax.set_xticks([0,60,120,180,240],['09:30','10:30','11:30/13:00','14:00','15:00'])
            minute_ax.set_ylabel('较前收盘 %')
            minute_ax.set_title(f"9/16 {r['change_pct']:+.2f}% · 日量/前5日均量 {r['volume_multiple']:.2f}倍",loc='left',fontsize=10)
            minute_ax.legend(loc='best',fontsize=7)
            minute_ax.set_xlim(0,240)
            mv = minute_ax.inset_axes([0,-.43,1,.23])
            mv.bar(range(len(ps)),[p['volume_shares']/1e4 for p in ps],color=blue,width=1)
            mv.set_xlim(0,240);mv.set_xticks([]);mv.tick_params(labelsize=7);mv.set_ylabel('万股/分钟',fontsize=7)
        fig.text(.05,.012,'来源：Tradex 标准化快照及分钟网关 · 仅使用截至 2026-09-16 的数据 · 金色虚线为有效轮次底线 · 日线红涨绿跌 · 均价缺失不插值',fontsize=8,color='#555')
        fig.savefig(OUT/f'走势对照-{page+1}.png',dpi=140)
        pdf.savefig(fig)
        plt.close(fig)

verification = {
    'cutoff':'2026-09-16 close',
    'names_user_confirmed':{'鸿源电子':'鸿远电子 603267.SH','百大精工':'百达精工 603331.SH'},
    'candidate_count':12,
    'minute_count_each':241,
    'minute_full_average_count':sum(m['vwap_available']==241 for m in minutes),
    'minute_daily_checks':'close/high/low absolute error < 0.011 CNY; volume relative error < 0.001',
    'assistant_original_list_unchanged':True,
    'assistant_frozen_sha256':hashlib.sha256(frozen.read_bytes()).hexdigest(),
    'inference_scope':'price-volume structure; no calibrated limit-up probabilities; no future observations',
    'files_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.suffix in ('.md','.csv','.pdf','.png')},
}
(OUT/'verification.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(verification,ensure_ascii=False,indent=2))
