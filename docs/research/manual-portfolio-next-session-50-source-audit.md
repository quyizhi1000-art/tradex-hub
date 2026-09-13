# 持仓观察次日分析：50 篇公开资料审计

检索日期：2026-09-02。用途：改造 `manual_portfolio_outlook.v1` 与每日复核的输出逻辑。这里记录的是可迁移的方法边界，不把任何单篇论文、单一市场统计关系或供应商观点直接写成 A 股个股的确定性预测。

## 来源账本

1. [Paying Attention: Overnight Returns and the Hidden Cost of Buying at the Open](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1625495) — 高关注股票的开盘价格可能受零售买压影响，开盘缺口不能直接当作日内延续。
2. [Return Differences between Trading and Non-Trading Hours: Like Night and Day](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID1266293_code265947.pdf?abstractid=1004081&mirid=1&rulid=200546) — 隔夜与日内收益应分开，不应只用收盘到收盘掩盖路径差异。
3. [Overnight Returns and Firm-Specific Investor Sentiment](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2554010) — 隔夜收益含情绪成分，短期延续与较长期反转可以并存。
4. [Information Flows around the Globe: Predicting Opening Gaps from Overnight Foreign Stock Price Patterns](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID1510069_code356671.pdf?abstractid=1510069&mirid=1) — 次日开盘缺口受隔夜信息影响；收盘前瞻必须承认尚未取得隔夜证据。
5. [A Surprise That Keeps You Awake: Overnight Returns After Earnings Announcements](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3293638) — 公告后的隔夜与日内反应可能相反，公告/注意力应单列而非混进价格方向。
6. [Overnight Returns as a Market Timing Strategy](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID3801218_code522567.pdf?abstractid=3692068&mirid=1) — 风险调整和市场状态会改变隔夜/日内结论，不能只报表面均值。
7. [Overnight Reversal and the Asymmetric Reaction to News](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4307675) — 隔夜新闻可能造成开盘过度或不足反应；开盘后需再次确认。
8. [Profitable Mean Reversion after Large Price Drops](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2272795) — 大跌后的反转依赖日/夜区段，不能把反转当作无条件规律。
9. [Trading mechanisms and return volatility: Evidence from Thailand](https://www.sciencedirect.com/science/article/pii/0927538X95000045) — 开盘波动、隔夜反转和开盘延续可能同时存在，必须描述时段。
10. [Overnight vs. Intraday Returns: Investor Disagreement, Information Uncertainty, and Future Stock Returns](https://papers.ssrn.com/sol3/Delivery.cfm/5164956.pdf?abstractid=5164956&mirid=1&type=2) — 隔夜与日内方向不一致本身可代表分歧，输出要保留冲突而不是强行合并。
11. [Overnight Returns, Trading Constraints, and Future Stock Returns: Evidence from China](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6199399) — A 股 T+1 约束改变隔夜信号的含义，海外结论不能原样套用。
12. [International volatility risk and Chinese stock return predictability](https://www.sciencedirect.com/science/article/pii/S0261560616301085) — A 股隔夜/日内结构与美国不同，市场环境是必要条件。
13. [融券制度对A股隔夜-日内收益率反转效应的影响研究](https://newetds.lib.tsinghua.edu.cn/qh/paper/summary?dbCode=ETDQH&sysId=302483) — 制度变化会改变反转结构，模型必须暴露制度和样本边界。
14. [A股市场上的收益率隔夜特征探讨](https://newetds.lib.tsinghua.edu.cn/qh/paper/summary?dbCode=ETDQH&sysId=292192) — 日夜增强与夜日反转的稳健性不同，不能用一个“强弱”标签替代。
15. [A股市场隔夜-日内收益率反转效应研究](https://tsjj.cbpt.cnki.net/portal/journal/portal/client/paper/748143d862e9d9e62f0045cd24482197) — A 股存在市场特有的隔夜/日内反转证据，次日检查需区分开盘与收盘。
16. [Are pre-opening periods important? Evidence from Chinese market lunch breaks](https://www.sciencedirect.com/science/article/pii/S0927538X24003299) — 集合竞价有价格发现作用，但只是开盘条件，不能补画成连续趋势。
17. [Buy-side divergence of opinion and stock returns: Evidence from call auctions](https://www.sciencedirect.com/science/article/abs/pii/S1544612326004563) — 集合竞价订单分歧与后续价格有关，单一开盘价不足以表达分歧。
18. [Statistical Properties and Pre-hit Dynamics of Price Limit Hits in the Chinese Stock Markets](https://arxiv.org/abs/1503.03548) — 涨跌停后的次日延续/反转不对称，价格限制状态必须单列。
19. [Daily Equity Returns and Price Limit in China's Stock Market](https://www.scitepress.org/PublishedPapers/2015/60182/60182.pdf) — 涨跌幅限制可能延迟价格发现，触板日不能沿用普通区间逻辑。
20. [上海证券交易所交易规则（2026年修订）](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml) — 前收、竞价、连续竞价和涨跌幅限制有明确制度定义。
21. [深交所主板投资入市手册](https://investor.szse.cn/institute/bookshelf/manualseriesbook/P020230403389861343977.pdf) — 开盘/收盘集合竞价与有效申报范围是解释 A 股开盘情景的制度边界。
22. [Positive feedback trading, the T+1 rule, and asymmetric return reversals in China](https://www.sciencedirect.com/science/article/pii/S0264999326003123) — T+1 下高换手下跌与次日反转具有不对称性，量价缺失时必须禁止此类判断。
23. [Intraday momentum and stock return predictability: Evidence from China](https://www.researchgate.net/profile/Yaojie-Zhang/publication/325060757_Intraday_momentum_and_stock_return_predictability_Evidence_from_China/links/5b73ed4aa6fdcc87df7e7456/Intraday-momentum-and-stock-return-predictability-Evidence-from-China.pdf?origin=publication_detail) — 早盘、午后与尾盘包含不同信息，全天只留高低收会损失路径意义。
24. [Intraday Patterns in the Cross-Section of Stock Returns](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1509466) — 同一日内时点存在可重复结构，输出应给出具体观察时点。
25. [Momentum Strategies](https://www.nber.org/papers/w5375) — 价格动量和盈利信息反应可并存，单日涨幅不能替代信息来源。
26. [Momentum Trading, Return Chasing, and Predictable Crashes](https://www.nber.org/papers/w20660) — 动量伴随崩跌风险，强势叙述必须同时给出失效条件。
27. [Reversals and the Returns to Liquidity Provision](https://www.nber.org/papers/w30917) — 波动和换手影响反转速度与持续性；流动性缺失时不输出反转断言。
28. [Factor momentum in the Chinese stock market](https://www.sciencedirect.com/science/article/pii/S0927539823001251) — A 股动量受因子和信息不对称影响，个股信号需与环境拆分。
29. [Horse race of weekly idiosyncratic momentum strategies in China](https://arxiv.org/abs/1910.13115) — 风险度量会改变动量结果，不能把一个指标称为稳定优势。
30. [One-Month Individual Stock Return Reversals and Industry Return Momentum](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1914629) — 个股反转与行业动量可能同时存在，板块归属必须使用稳定目录。
31. [A study of cross-industry return predictability in the Chinese stock market](https://www.sciencedirect.com/science/article/pii/S1057521922002071) — A 股行业间信息扩散可形成领先关系，但需固定行业分类口径。
32. [How Predictable is the Chinese Stock Market?](https://ink.library.smu.edu.sg/lkcsb_research/3146/) — 预测性在行业、规模和所有权分组间不同，输出需要说明适用范围。
33. [Time-varying return predictability in the Chinese stock market](https://arxiv.org/abs/1611.04090) — 预测关系会随时间变化，不可把历史阈值写成永久规律。
34. [Predictability of Chinese Stock Returns: Contrarian or Momentum?](https://cicfconf.org/past/pdf/2004.pdf) — 反转与动量是假设竞争关系，分析应展示哪类证据会推翻当前读法。
35. [Oil and stock market momentum](https://www.sciencedirect.com/science/article/pii/S0140988317303286) — 行业动量受外部状态变量影响，板块标签本身不是因果解释。
36. [Industry Information Diffusion and the Lead-Lag Effect in Stock Returns](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=463005) — 行业内部大/小公司信息扩散造成领先滞后，供应商临时标签不适合作稳定归属。
37. [Investor Attention, Information Diffusion and Industry Returns](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2649549) — 行业领先性受注意力和经济状态调节，应降低无条件板块共振表述。
38. [Do Industries Predict the Stock Market Due to Slow Diffusion of Information?](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1982641) — 行业预测力随期限衰减，次日与 2–5 日结论要分开。
39. [Limit Order Imbalances and Return Predictability](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=495202) — 收盘订单不平衡可能影响次日，但没有订单证据时不能以价格替代。
40. [Trading Volume and Serial Correlation in Stock Returns](https://www.nber.org/papers/w4193) — 成交量改变收益自相关解释，成交额不完整时禁止量价结论。
41. [Do Retail Trades Move Markets?](https://academic.oup.com/rfs/article-abstract/22/1/151/1585397) — 订单流在不同期限可呈相反关系，短期和长期语言不能混用。
42. [Caught On Tape: Institutional Order Flow and Stock Returns](https://www.nber.org/papers/w11439) — 资金流与未来收益的关系可能反映流动性供给，不能把净流入写成上涨原因。
43. [How and When are High-Frequency Stock Returns Predictable?](https://www.nber.org/system/files/working_papers/w30366/w30366.pdf) — 高频可预测性依赖极及时的盘口数据，收盘档案不应冒充盘口模型。
44. [Statistical Predictions of Trading Strategies in Electronic Markets](https://academic.oup.com/jfec/article/23/2/nbae025/7826742) — 盘口、价差和订单身份影响短时预测，缺字段时要明确能力边界。
45. [A Dynamic Structural Model for Stock Return Volatility and Trading Volume](https://www.nber.org/papers/w4988) — 波动与成交持续性可来自交易行为而非新闻，不能随意编故事。
46. [The impact of trading volume, number of trades and overnight returns on forecasting the daily realized range](https://www.sciencedirect.com/science/article/abs/pii/S026499931300429X) — 样本内改善不等于样本外有效，输出必须避免“模型看起来更丰富就更准”。
47. [Predicting volatility: getting the most out of return data sampled at different frequencies](https://www.sciencedirect.com/science/article/pii/S0304407605000060) — 日内绝对收益与区间对波动预测有用，价格路径应保留而非只存最后两个点。
48. [Forecasting stock market volatility: The role of technical variables](https://www.sciencedirect.com/science/article/pii/S0264999318309398) — 价格、波动、成交组合优于单类变量，但经济状态会改变表现。
49. [Retail Attention, Institutional Attention](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/abs/retail-attention-institutional-attention/C92DB27218CFED1690F116E95D0FC933) — 宏观新闻会改变市场处理公司新闻的方式，个股与大盘证据需分层。
50. [Investor Inattention, Firm Reaction, and Friday Earnings Announcements](https://www.nber.org/papers/w11683) — 注意力不足会延迟公告反应，未采集公告时必须显式注明。

## 由资料形成的输出逻辑

1. **先列已知事实，再给解释。** 固定展示前收、09:30 参考、日内高低与发生时刻、收盘在区间的位置、上午/下午/尾盘路径、最大回撤与低点后回升。缺失就写未取得。
2. **把四个生命周期拆开。** 收盘路径、隔夜信息、09:25 集合竞价、开盘后确认各自独立；收盘前瞻不替隔夜和竞价编结论。
3. **只给一个“明日主问题”。** 例如“高位是否被接受”“回收能否站稳中位”“低位是否继续成交”，避免把同一模板复制成三套场景。
4. **按时间复核。** 09:25 只记录缺口；09:45 看个股区间与本地目录同类；10:00 看指数、全A广度、中位数和成交；14:30 后区分盘中触及与收盘接受。
5. **板块归属只认本地关系库。** 个股业务目录、统计行业和概念来自 `tradex.instrument_taxonomy` 的修订绑定投影；绝不使用供应商 `provider_industry` 或复盘临时热点名补位。
6. **量价结论受质量闸门约束。** `amount_partial`、累计均价缺失、订单流未取得时，可以描述价格路径，但不能写放量、缩量、承接资金或主力意图。
7. **每日回顾不是命中统计。** 分别记录“昨天有用的部分、漏掉的部分、下一次怎么改”；触及不等于收盘确认，失效后不沿用旧区间。
8. **保持观察边界。** 不输出胜率、目标价、收益承诺、账户仓位或买卖指令；2–5 日观察必须由连续交易日证据重新确认。
