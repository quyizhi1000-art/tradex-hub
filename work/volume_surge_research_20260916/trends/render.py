"""Export auditable candlestick/minute charts without synthetic price points."""
import os,sys,json,csv,math
from pathlib import Path
sys.path.append(os.path.join(os.environ['TEMP'],'tradex-research-plotdeps'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

OUT=Path(__file__).parent
def load(name):return json.loads((OUT/name).read_text(encoding='utf-8'))
daily=load('daily-plot-data.json');minutes=load('minute-plot-data.json')
plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei'],'axes.unicode_minus':False,
 'font.size':9,'figure.facecolor':'#f5f7fa','axes.facecolor':'white','axes.spines.top':False,'axes.spines.right':False})
red='#db4747';green='#189378';blue='#436bd1';amber='#dc9d22'

def daily_chart(ax,data):
    bs=data['bars'];xs=list(range(len(bs)))
    for x,b in zip(xs,bs):
        color=red if b['close']>=b['open'] else green
        ax.vlines(x,b['low'],b['high'],color=color,linewidth=1)
        ax.add_patch(Rectangle((x-.3,min(b['open'],b['close'])),.6,max(abs(b['close']-b['open']),.012),facecolor=color,edgecolor=color))
    for key,label,color in [('event','放量信号',amber),('selection','本段首次入选',blue),('limit','首次涨停',red)]:
        if key=='selection' and data['event']==data['selection']:
            continue
        if key=='event' and data['event']==data['selection']:
            label='放量 / 本段首次入选'
        pos=next((i for i,b in enumerate(bs) if b['trade_date']==data[key]),None)
        if pos is not None:
            ax.axvline(pos,color=color,ls=':',alpha=.6)
            ax.text(pos,1.01,label,transform=ax.get_xaxis_transform(),color=color,ha='center',fontsize=7)
    ax.set_xticks(xs[::max(1,len(bs)//6)],[bs[i]['trade_date'][5:] for i in xs[::max(1,len(bs)//6)]],rotation=25)
    ax.set_ylabel('元');ax.grid(alpha=.16)
    vol=ax.inset_axes([0,-.5,1,.25])
    vol.bar(xs,[b['volume_shares']/1e6 for b in bs],color=[red if b['close']>=b['open'] else green for b in bs],width=.6)
    vol.set_ylabel('百万股',fontsize=7);vol.set_xticks([]);vol.tick_params(labelsize=7);vol.grid(axis='y',alpha=.12)
    ax.set_xlim(-.7,len(bs)-.3);vol.set_xlim(ax.get_xlim())

def minute_chart(ax,data):
    ps=data['points'];prev=data['previous_close'];x=np.arange(len(ps))
    ax.plot(x,[(p['price']/prev-1)*100 for p in ps],color=blue,lw=1.35,label='分钟收盘价')
    avg=[(p['cumulative_average_price']/prev-1)*100 if p['cumulative_average_price'] is not None else np.nan for p in ps]
    ax.plot(x,avg,color=amber,lw=.95,label='成交均价（缺失处断开）')
    ax.axhline(0,color='#999',lw=.6)
    ticks=[i for i,p in enumerate(ps) if p['minute'] in ['09:30:00','10:30:00','11:30:00','14:00:00','15:00:00']]
    ax.set_xticks(ticks,[ps[i]['minute'][:5] for i in ticks]);ax.set_ylabel('较昨收 %');ax.grid(alpha=.16)
    ax.set_xlim(0,len(ps)-1)
    ax.text(.02,.93,f"首封 {data['event']['first_sealed_at'][:8]} · 开板 {data['event']['open_count']} 次",transform=ax.transAxes,fontsize=8)
    vol=ax.inset_axes([0,-.5,1,.25]);vol.bar(x,[p['volume_shares']/1e6 for p in ps],width=1,color=blue,alpha=.6)
    vol.set_ylabel('百万股',fontsize=7);vol.set_xticks([]);vol.tick_params(labelsize=7);vol.set_xlim(ax.get_xlim())

examples=['002617.SZ','002913.SZ','600616.SH','002442.SZ']
fig,axes=plt.subplots(4,2,figsize=(14,16))
fig.subplots_adjust(top=.9,bottom=.12,hspace=.9,wspace=.2)
fig.suptitle('7日向上放量｜4个不同的涨停路径',fontsize=19,fontweight='bold')
for n,instrument in enumerate(examples):
    d=daily[instrument];m=minutes[instrument]
    daily_chart(axes[n,0],d);minute_chart(axes[n,1],m)
    axes[n,0].set_title(f"{d['name']} {instrument} · 日线",loc='left',pad=31,fontweight='bold')
    axes[n,1].set_title(f"{d['name']} · 9/16 分时",loc='left',pad=15,fontweight='bold')
fig.text(.06,.013,'数据截止2026-09-16；红涨绿跌；日线信号来自原策略档案；分时241个真实点，午休压缩。\n黄色均价线只绘制有成交额证据的区段；案例展示不是买点验证。',fontsize=9,color='#555')
fig.savefig(OUT/'代表性走势.png',dpi=150);plt.close(fig)

with PdfPages(OUT/'全部46只日线与17只分时.pdf') as pdf:
    for kind,dataset,draw in [('日线',daily,daily_chart),('9月16日分时',minutes,minute_chart)]:
        ids=sorted(dataset)
        for start in range(0,len(ids),6):
            fig,axes=plt.subplots(3,2,figsize=(14,12))
            fig.subplots_adjust(top=.86,bottom=.12,hspace=.9,wspace=.22)
            fig.suptitle(f'7日向上放量 · {kind} · {start+1}–{min(start+6,len(ids))} / {len(ids)}',fontsize=17)
            for ax,instrument in zip(axes.ravel(),ids[start:start+6]):
                data=dataset[instrument];draw(ax,data)
                ax.set_title(f"{data['name']} {instrument}",loc='left',pad=30 if kind=='日线' else 15,fontweight='bold')
            for ax in axes.ravel()[len(ids[start:start+6]):]:ax.set_visible(False)
            fig.text(.06,.012,'截至2026-09-16；只描述已涨停样本；分钟均价缺失不补值；历史分时缺失股票不画替代曲线。',fontsize=8)
            pdf.savefig(fig);plt.close(fig)
print('Charts written: representative PNG and complete PDF chartbook')

