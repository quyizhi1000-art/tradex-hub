from pathlib import Path
p=Path('tradex/tests/test_intraday_macd_j.py');s=p.read_text(encoding='utf-8');s=s.replace('def test_partial_live_and_history_readback_preserves_immutable_scan(tmp_path, requested, evaluated, status):','def test_partial_live_and_history_readback_preserves_immutable_scan(tmp_path, monkeypatch, requested, evaluated, status):\n    monkeypatch.setattr("tradex.stock_selection.intraday_macd_j.with_previous_close_difference", lambda value, **kw: value)');s=s.replace('original = {"trade_date": str(NOW.date()), "status": "monitoring", "message": "scan",','original = {"trade_date": str(NOW.date()), "screen_version":"macd-j-upturn-main-board.v4", "status": "monitoring", "message": "scan",');p.write_text(s,encoding='utf-8')
p=Path('docs/intraday-macd-j.md');p.write_text('''# 双拐雷达与选股中心：当前规则 v4

2026-09-18 确认使用 `macd-j-upturn-main-board.v4`，仅沪深主板非 ST，排除退市、停牌和缺失证据；MACD(12,26,9)、KDJ(9,3,3)。v1/v2/v3 历史存档与原始扫描不覆盖，历史记录明确显示版本。

## 正式入选：五项同时满足

1. 当日新金叉：昨日 DIF≤DEA，今日 DIF>DEA。不限制零轴位置，水上水下均可。
2. J 当天或前一交易日拐头，且今天 J>昨天 J。拐头指前一段下降或走平、随后上升；不允许提前两天，不要求 K>D。低 J 仅作为标签。
3. 当前价格/最近60个交易日内的最高价−1 < -20%。含筛选日，取日内最高价，不是最高收盘价；全部使用同一前复权窗口。恰好 -20% 不入选。
4. 当前价格≥当日 MA5；MA5 含筛选日价格，不要求昨天位于 MA5 下方。
5. 当日量比≤1.5，恰好1.5可入选，量比缺失不放行。

## 独立待金叉预警

今日 DIF≤DEA，DIF 比昨天上升，且 DEA−DIF 连续两个交易日缩小（前天差距>昨天差距>今天差距≥0）。J 和价格、量比条件与正式入选相同。不要求明日必然金叉，不将预警计入正式入选或金叉通知。

## 数据与运行

盘后通过 Analysis Worker 使用正式六日指标和当日量比；同一 gateway 为初筛股票取得恰好60个交易日的前复权行情。盘中由 Collector 独占扫描，使用前59日行情加当日高价/现价，MACD/KDJ沿用已有全历史指标递推。盘中量比优先使用当前报价的量比，否则用经单位核验的累计成交股数/已交易分钟，与前5日平均每分钟成交量比较；午休不计时。新历史获取后重新取得实时行情，仍执行逐股时间校验。

`macd_rules.v4_signal`、`price_filter_evidence` 统一正式信号/预警及价格量比判断；不另建采集器或调度器。行情窗口 gateway 独占日期和股票维度的历史缓存，日历、窗口、复权、价格对账不通过时明确未核验。历史为空时可重试，不能用前15日高点或60日前收盘价替代60日最高价。

正式入选显示金叉日期、J拐头日期、60日高点及日期、回撤百分比、MA5、量比。预警在独立区域显示并列出连续三日的两线差距。每轮正式新增仍相对上一交易日同版本正式存档取差集；缺少基准时不声称已核验新增，原始扫描保留。收盘雷达与选股中心读取同一正式存档；当日不回退旧规则作为当前确认。

## 用户例子核对

- 通富微电给定价格58.31、高点84.7时，回撤为-31.1570%，通过回撤条件。但保存的9月17日指标显示J于9月15日拐头，提前两日，按已确认窗口不入选。
- 彤程新材9月8日：DIF从9月7日的-1.786降到-1.791，虽DEA−DIF连续缩小，仍不满足预警的DIF上升条件。图形理想不等于全部数值条件已满足。

旧存档保留其原规则，不把新版本筛选结果冒充当时已知的信号；补算记录实际生成时间。
''',encoding='utf-8')
