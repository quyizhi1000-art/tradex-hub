# Tradex 标准数据契约

> 状态：第三阶段；当前实现 `market_overview.v1`、`quote_snapshot.v1`、
> `ohlcv_bar.v1`、`market_breadth.v1`、`sector_quote.v1`、
> `etf_quote.v1`、`leader_quote.v1`、`stock_sector_profile.v1`、`board_leader.v1` 和 `board_leader.v2`。
> 当前也已实现 `limit_event.v1`。

## 目标

标准数据契约是供应商响应与网页、MCP、分析算法之间的反腐层。更换扶摇或
其他付费数据源时，只新增或替换 Provider Mapper；下游继续消费同一个版本化
契约。契约保证接口和语义稳定，不保证新供应商能够提供其本身缺失的能力。

数据流固定为：

```text
Provider Client -> Provider Mapper -> Canonical Contract -> Quality Gate
                -> Data Gateway -> Feature Snapshot / Core MCP / Data Lake
```

## 通用元数据

| 字段 | 类型 | 语义 |
|---|---|---|
| `contract` | string | 契约名，如 `market_overview.v1` |
| `schema_version` | integer | 结构版本；破坏性变更必须升版 |
| `provider` | string | 实际提供数据的供应商标识 |
| `provider_request_id` | string/null | 供应商请求追踪 ID，不得包含密钥 |
| `provider_as_of` | timezone datetime/null | 供应商数据时间，不是本机抓取时间 |
| `fetched_at` | timezone datetime | 网关完成抓取的时间 |
| `quality` | enum | `accepted` / `degraded` / `rejected` |
| `quality_flags` | string[] | 缺失、陈旧或降级原因 |

所有时间必须带时区；中国市场统一使用 `Asia/Shanghai` 对应的 UTC 偏移。
`0`、`null` 和字段缺失含义不同，Mapper 不得用 `0` 填补缺失数据。

## `market_overview.v1`

### `IndexQuoteV1`

| 字段 | 类型 | 单位/规则 |
|---|---|---|
| `instrument_id` | string | 标准代码，如 `000001.SH` |
| `name` | string | 指数名称 |
| `available` | bool | 当前指数快照是否可用 |
| `value` | number/null | 指数点位，必须有限且非负 |
| `change` | number/null | 点位涨跌额 |
| `change_pct` | number/null | 百分点；`5.23` 表示 `5.23%` |
| `previous_close` | number/null | 昨收点位 |
| `open` / `high` / `low` | number/null | 当日点位；`high >= low` |
| `amount_cny` | number/null | 成交额，人民币元 |

### `ParticipationIndexV1`

用于风险偏好模型的全量指数输入，统一包含 `instrument_id`、`name`、
`change_pct` 和可选 `provider_as_of`。不能转换成标准市场代码的记录不会进入
标准契约。

### `MarketTurnoverV1`

沪深两市按相同分钟累计成交额比较。内部字段显式使用 `_cny` 后缀；旧页面
兼容视图仍输出 `today_amount`、`difference` 等历史字段。

不可用时必须返回 `available=false` 和明确 `reason`；不得退化成成交量，也不得
使用上一交易日数据伪装为当日数据。

### `intraday_minute_series.v1` 与 `index_intraday_series.v1`

同日盘后精确追补使用完整的 1 分钟曲线，而不是收盘价回填历史分钟。Tushare
批量股票分钟请求的正式上限为 40 只；批量响应中缺失或停留在其他交易日的证券
不会拖垮同批有效证券，而是通过标准单证券路由依次尝试 Tushare、Eastmoney
和现有末级源。追补时单证券结果还必须证明目标交易日，否则继续切源或明确失败。

`index_intraday_series.v1` 保存指数每分钟的交易日、分钟、OHLC 和人民币成交额。
Eastmoney 历史主机无结果或连接失败时可以切换到延迟主机，但只有延迟主机返回的
同日精确分钟可用于同日盘后追补；它不能证明跨日历史分钟权限。

盘后聚合重建还要求上一交易日同分钟存在已验收的 `market_watch.v1` 成交额基线，
并按目标分钟回放已持久化的轮动快照。任一必要证券、四个角色指数、沪深成交额、
基线或轮动证据缺失时，整分钟失败关闭，不插值、不使用当前值或最终值替代。

## 质量门禁

`market_overview.v1` 至少要求上证指数或深证成指之一可用，否则拒绝快照。
以下情况允许返回降级快照，并通过 `quality_flags` 说明：

- 沪深指数只返回一个；
- 市场参与指数缺失；
- 同期成交额暂不可用；
- 供应商未返回有效数据时间。

网页兼容视图会继续使用上一版字段，同时额外返回 `contract`、
`schema_version`、`quality` 和 `quality_flags`。

## `quote_snapshot.v1`

个股快照用 `instrument_id` 表示标准证券代码，如 `600519.SH`。价格、涨跌额、
昨收、开盘、最高和最低均以人民币元计；`change_pct` 和 `turnover_pct` 使用
百分点。`volume_shares` 固定为股，`amount_cny` 和市值字段固定为人民币元。

当前 Provider Mapper 的单位换算如下：

| Provider | 原始成交量 | 标准成交量 | 原始成交额 | 标准成交额 |
|---|---:|---:|---:|---:|
| `biying` 实时券商接口 | `pv` 股（`v` 为手） | 原值 | `cje` 元 | 原值 |
| `ths_fuyao` | 股 | 原值 | 元 | 原值 |
| `eltdx` | 手 | `× 100` | 元 | 原值 |
| `akshare` 东方财富 | 手 | `× 100` | 元 | 原值 |
| `akshare` 新浪回退 | 股 | 原值 | 元 | 原值 |
| `tencent_http` | 手 | `× 100` | 万元 | `× 10,000` |

Mapper 必须精确匹配请求代码。即使供应商只返回一行，代码不一致也会拒绝，
避免将错误标的行情传给页面。核心价格可用但盘中价、量额或供应商时间缺失时，
快照标记为 `degraded`。

MCP 兼容视图仍是单元素 JSON 数组并使用原中文字段；其中 `成交量` 的稳定
语义从本版本起为“股”，`成交额` 为“元”。缓存键包含契约版本，旧单位缓存
不会与新数据混用。

## `ohlcv_bar.v1`

`OHLCVSeriesV1` 固定声明证券、周期和复权口径；每根 `OHLCVBarV1` 包含交易日、
OHLC、成交量和成交额。当前支持日、周、月，以及不复权、前复权和后复权：

- K 线按交易日升序排列，重复交易日直接拒绝；
- `high` 必须不低于 OHLC 其他值，`low` 必须不高于其他值；
- 成交量统一为股、成交额统一为人民币元；
- 网关再次执行开始/结束日期过滤，避免不同供应商的边界行为泄漏到页面；
- Provider 声明的周期或复权口径与请求不一致时拒绝结果。

eltdx 当前历史接口不支持复权。收到前/后复权请求时，它会明确返回“不支持”，
由 SmartRouter 继续选择扶摇或 AKShare；不得再把不复权数据标记成复权数据。
MCP 兼容视图仍返回原来的中文 K 线 JSON 数组，最多 500 行。

必盈历史 K 线的 `v` 实测和成交额口径均表明单位为“手”，Mapper 固定乘以
100 后写入 `volume_shares`；`a` 直接以人民币元写入 `amount_cny`。必盈的
`d/w/m` 和 `n/f/b` 必须分别映射为契约的 daily/weekly/monthly 与
none/forward/backward，不能把供应商缩写直接作为契约值。

## `market_breadth.v1`

市场宽度表达供应商 A 股 universe 的互斥计数：`up_count`、`down_count`、`flat_count`、
`unclassified_count`、`limit_up_count`、`limit_down_count` 和四类参与股票之和
`total_count`。

`scope` 固定为 `provider_a_share_universe`，而不是宣称各供应商都覆盖完全相同的
证券集合。真实影子比较中 Fuyao 与东财分布接口的总数存在差异；无法证明覆盖
范围等价的回退源必须标记 `universe_definition_unverified`，下游不得把两个总数
直接用于跨源趋势比较。

- 计数必须是非负整数，缺失不得替换为 `0`；
- `total_count` 必须严格等于上涨、下跌、平盘、未分类之和；
- 涨停数量不得大于上涨数量，跌停数量不得大于下跌数量；
- Provider 必须恰好返回一个全市场记录，多行或空结果均拒绝。

Fuyao 会保留全市场快照和涨跌停池的一致交易日水位。个别股票缺少涨跌幅时，
必须进入 `unclassified_count` 并标记 `participation_unclassified`，不得算作平盘，
也不得让一条缺失记录拖垮整个快照。东财 push2ex 当前没有
可靠的供应商时间，仍可返回计数，但契约标记 `provider_timestamp_missing` 和
`degraded`。页面兼容视图继续使用原字段，并新增明确的 `未分类`。

## `sector_quote.v1`

行业与概念共享一个契约，但由 `sector_type` 严格分区。当前没有跨供应商统一的
板块代码，因此标准身份使用 `sector_key = sector_type + ':' + name`，供应商代码
仅保存在 `provider_sector_code`；不能把东财 `BK` 代码当成所有供应商的主键。

每条板块行情可包含点位、涨跌幅、成交额、主力净流入及占比、成分涨跌家数、
领涨股票和供应商时间。其中涨跌幅是必需字段；金额统一为人民币元，比例统一为
百分点，领涨股票代码转换为标准 A 股 `instrument_id`。

板块按涨跌幅降序返回，重复 `sector_key` 直接拒绝。以下缺失允许页面继续显示，
但会通过 `quality_flags` 标记为 `degraded`：

- 成交额不完整；
- 主力净流入不完整；
- 成分上涨/下跌家数不完整；
- 供应商时间缺失或只有部分板块有时间；
- 新 Provider 的金额单位尚未通过准入验证。

必盈 `hibk` 当前能提供点位、涨跌幅和资金流，但不提供成交额及成分涨跌家数，
所以若启用 `industry_quotes` 主源能力，风险偏好页面会使用可用字段，同时组件
状态明确标为 `partial`。这不是源失败，也不会用 `0` 伪造缺失证据。

风险偏好页面仍消费原中文字段兼容视图；组件状态现在额外暴露 `contract`、
`schema_version`、`quality`、`quality_flags`、`partial` 和供应商请求 ID。

## `etf_quote.v1`

当前契约只表达风险偏好页面选择代表 ETF 所需的实时摘要，不与 ETF 历史 K 线、
IOPV 或基金档案混成万能模型。每条记录包含标准 `instrument_id`、名称、最新价、
涨跌幅和人民币成交额；上海、深圳 ETF 分别使用 `510300.SH`、`159915.SZ` 形式。

结果固定按 `amount_cny` 降序并限制为请求数量，重复代码、非法交易所、缺失价格、
涨跌幅或成交额会拒绝当前 Provider，并由 `route_validated` 在同一次请求内尝试
备用源。供应商时间缺失或金额单位尚未完成准入验证时允许页面显示，但状态为
`degraded`，不得用本机抓取时间冒充供应商时间。

Dashboard 继续接收 `etf_code`、`name`、`price`、`change_pct` 和 `amount` 兼容字段；
刷新缓存仍由风险偏好 ETF 组件唯一持有，TTL 为 300 秒、最大陈旧窗口为 900 秒。
更换付费源只需要新增 Provider 注册和 Mapper 准入，不修改页面选择逻辑。

## `limit_up_status.v1`

盘中涨停页先读取轻量 `limit_up_status.v1`。该契约只要求完整分页、唯一标准证券代码、
名称、交易日和交易状态；板数与首次封板时间有源数据就规范化，缺失时明确标记部分
覆盖。`reason_type`、封单质量和主营/概念归属属于分析字段，它们缺失不能让已经返回的
涨停股票从盘中页面消失。轻量状态和完整事件使用独立 Router capability，调用方不能把
轻量状态用于风险评分、午盘归类或盘后证据。

## `limit_event.v1`

每日涨停池使用 `LimitEventSeriesV1` 表达，并允许事件列表为空。空列表只有在
Provider 明确声明 `valid_empty=true`、总数为 0、交易日和结构均有效时才会被
接受；HTTP 异常、字段漂移或截断结果不能降级成“今日没有涨停”。

每条 `LimitUpEventV1` 至少包含标准证券代码、名称和非空涨停原因。价格及封单额
使用人民币元，涨幅和封板成功率使用百分点；Provider 返回的 `0.95` 封板成功率
由 Mapper 转换为 `95.0`。连板文本同时保留为兼容标签，并解析出可比较的
`board_count`；无法可靠解析时保持 `None`，通过 `board_count_partial` 标记降级，
不得猜测为首板。

Provider 的交易状态统一映射为 `pre_open`、`trading`、`closed`、
`non_trading` 或 `unknown`。Dashboard 只根据这些规范状态决定是否可参与风险偏好
投票，不再解析同花顺状态 ID。分页总数、唯一代码、原因覆盖率、连板覆盖率、
交易日和显式源有效性都在 routed attempt 内验证，失败时可以继续选择备用源。

旧页面仍接收原中文字段、`YYYYMMDD` 数据日期和组件状态；刷新所有权暂时仍在
Dashboard，TTL 为 60 秒、最大陈旧窗口为 120 秒，且缓存身份包含交易日。

## `limit_up_pool.v2`

涨停池二级面板读取按 accepted-real 原快照版本绑定的 `limit_up_pool.v2`。每条记录只
保留规范涨停状态字段，并按标准证券代码连接同一版本的
`stock_relationship_profile.v1`。展示字段包括上级业务目录、真实主营叶子、业务标签、申万三级统计行业、
关系证据状态和目录标志；申万统计行业不得覆盖主营，供应商行业、供应商涨停原因和当日
资金方向不得写入主营或业务标签。

该契约不执行个股分钟价格与板块资金轨迹共振、首封窗口相关性、一字板同批推断或任何
午盘/盘后涨停归因分析。Collector 在盘中按分钟刷新规范涨停状态并只读连接证券关系
目录；目录无记录时 `relationship_match_status=unmatched`，主营和标签保持空值并写入
`stock_relationship_unavailable`，不能用供应商原因或相似概念补齐。

池级字段分别报告 `catalog_matched_count`、`business_classified_count` 和
`unmatched_count`。分类页签只按稳定的上级 `business_domain_key` 生成；不同主营叶子或
市场细目录属于同一大类时必须合并为一个页签，尚无可靠上级大类的记录统一进入“其他业务”，
不能把财务分部名称直接提升为页签。`directory_category`、`primary_business` 和业务标签仍在
个股卡片中分别展示。目录缺失、部分股票未匹配、主营未分类或规范涨停状态降级时，
`quality=degraded` 并保留明确 `quality_flags`。

该结果由 Collector 生成并持久化；Web 必须携带精确
`source_snapshot_revision` 只读查询。Web 不调用行情 Provider，不拥有刷新缓存，也
不得把其他原快照版本或其他目录版本的结果拼接到当前页面。

## Leadership 三类小契约

Dashboard 的领涨股展示不再直接调用 Tencent 或 Eastmoney 抓取函数，而是拆成
三个用途明确的契约：

- `leader_quote.v1`：配置中的代表股票小批量行情；代码、名称、最新价和涨跌幅
  是稳定字段。允许供应商只返回部分代码，但必须标记
  `quote_coverage_partial`；供应商时间缺失时标记降级。
- `stock_sector_profile.v1`：涨停池股票的行业、地域和概念标签。它是归因输入，
  必须一次完整覆盖请求代码、代码不重复、数据时间属于指定交易日；任何一项不符
  都拒绝整个批次，不能让部分分类结果扭曲板块计数。
- `board_leader.v1`：兼容既有按当日涨幅排序的板块领涨成分。
- `board_leader.v2`：板块内有限数量的涨速领先成分，金额统一为人民币元、比例统一为
  百分点。名称或代码缺失会拒绝该 Provider；资金流、成交额或供应商时间缺失可
  返回降级快照，并保留明确质量标志。

板块标签中的“共振领涨股/共振领跌股”使用独立的
`sector_resonance_batch.v1` 结果，不再把某一时刻的板块净流入和个股涨速并列就
视为共振。计算方法 `sector_fund_flow_path_resonance.v2` 要求板块近 5 分钟资金
流入或流出决定共振方向，不把板块整体涨跌方向作为候选股计算的前置门槛；随后
比较板块资金累计变化轨迹和成分股价格累计变化轨迹。两条轨迹的 Pearson 相关
系数至少为 `0.60`、逐区间同向占比至少为 `60%`、个股 5 分钟绝对涨跌幅至少为
`0.10` 个百分点。资金流入使用涨速靠前候选，资金流出使用跌速靠前候选。至少
需要 3 个共同区间；允许双方共同缺失分钟点并使用同一段不超过 2 分钟的区间，
但不跨不同缺口拼接。股票分钟源比板块轨迹最多滞后 2 分钟时，使用最近一个证据
完整、由板块点自身 `delta_5m_baseline_as_of` 绑定的共同窗口；超过 2 分钟仍按
证据不足处理，不能把缺少末端分钟误记成无共振。
不满足门槛返回 `no_match`，证据不足返回 `unavailable`，两者都不生成候选股。

共振批次由采集侧回溯生成并以原始 accepted-real 的
`source_snapshot_revision` 绑定、独立 `resonance_revision` 持久化。Web 只读叠加
完全匹配原快照版本或不超过 6 分钟的同日批次，不调用行情 Provider，也不改写
原始轨迹及其摘要版本。采集器盘中每 5 分钟生成一次，收盘后再对最后一份真实
快照补算一次；为控制分钟源调用量，每个方向按页面相同的“当前累计资金金额”顺序
计算前 8 个板块，而不是轨迹配置顺序。页面同时返回批次的证据时间和原始快照版本，
不能把旧批次伪装成当前分钟。

这三类调用使用 `route_validated`：Provider 响应只有在 Mapper、契约和质量门禁
全部通过后才计为成功。字段漂移或语义错误会计入该候选源失败，并在同一次请求内
尝试下一个源，避免“HTTP 成功”阻断 fallback。

风险偏好页面的 `static_membership` 现在直接从同一批
`stock_sector_profile.v1` 的行业、地域和概念标签派生，仅作为展示旁证，不参与
当日题材投票。页面不再为每只入选龙头逐个请求 `stock_boards`，也不再维护第二套
membership executor、重试和缓存；Profile 随 leadership pool 的 60 秒刷新周期
统一更新，失败时明确显示不可用。

当前板块领涨调用仍接收东财 `BK` 代码，这是已有 rotation/risk 模型的历史身份，
不是最终的跨供应商主键。后续接入提供不同板块体系的付费源前，应先让页面传递
`sector_key`，再由 Provider Adapter 解析到各自代码；不能把 `BK` 直接映射成
另一供应商代码并宣称完全等价。

## 必盈分层路由

必盈按能力而不是按供应商整体启停。配置了有效许可证且能力出现在
`BIYING_PRIMARY_CAPABILITIES` 时，候选顺序为：

```text
必盈(priority=1) -> Fuyao 等价能力(priority=50) -> 原有源(priority>=100)
```

不存在 Fuyao 等价接口时直接使用“必盈 -> 原有源”。同一 data type 下若只有
部分 endpoint 等价，必盈 Mapper 必须抛出 `SourceCapabilityError`，由
SmartRouter 在当前请求内继续降级；不能返回字段较少但语义不同的成功结果。

当前已接入主源能力包括：个股实时/历史 K 线、市场指数概览、指数日成交额、
A 股目录、全量/复权 K 线、公司基本信息、三大财报与财务指标、F10 财报、
分红、流通股东、行业/概念目录及成分、概念归属、板块资金流、涨跌停池、
以及部分机构持仓。以下子能力仍明确走原有源：公司主营收入构成、财务分部、
历史估值时序、分析师评级、行业历史 K 线、指定历史日龙虎榜、1 分钟 K 线和
单只基金持仓明细。

默认主源列表刻意不包含 `valuation_snapshot`、`ths_index_catalog`、
`ths_index_constituents`、`industry_quotes`、`dragon_tiger_market_day` 和
`lockup_expiry`：必盈当前接口分别缺少完整 PS/PCF 估值字段、使用非同花顺
指数代码、只提供日级板块资金流、缺少席位买卖净额，以及缺少解禁占比。
这些近似 Mapper 保留用于后续独立契约或影子比较，但生产路由继续使用 Fuyao
或原有源，避免“成功返回”阻断精确数据的 fallback。

许可证只允许由 `biying_client` 从 `BIYING_LICENCE` 或
`BIYING_LICENCE_FILE` 读取，并在客户端内部拼入 URL 最后一个路径段。异常、
日志、测试输出和 provider metadata 均不得包含完整 URL 或许可证。客户端不
自动重试，只接受 HTTPS 根地址并拒绝自动重定向；默认普通接口预算为每分钟
300 次，并由本机所有 Tradex 进程共享，并发预算满时立即交给路由回退。
移除单个 `BIYING_PRIMARY_CAPABILITIES` 项即可只回滚该能力；
`BIYING_ENABLED=false` 可整体停用。

## `stock_selection_strategy_result.v1`

选股中心使用独立策略目录和不可变策略结果，不再把新增策略解释为综合候选池的版本升级。
每项结果固定记录 `strategy_id`、`strategy_version`、`result_contract`、`result_id`、交易日、
生成时间、输入快照契约、完整 `source_snapshot_revision`、质量状态和严格结果 payload。
归档唯一键为 `(trade_date, strategy_id, strategy_version)`；增加新策略不会改变同日已有策略的
版本、结果身份或 payload。策略语义变化只升级该策略版本，页面布局或文案变化不升级策略。

当前静态目录包含：

- `balanced-multifactor-a-share.v1`，结果契约 `balanced_stock_selection_result.v1`；
- `next-session-limit-up-tendency-main-board.v2`，结果契约 `stock_limit_up_tendency_screen.v1`；
- `long-upper-shadow-main-board.v3`，结果契约 `stock_pattern_screen.v1`。

三项策略由同一个 Analysis Worker 对同一份 `daily_stock_factor_snapshot.v1` 执行，共享采集、
交易日历、调度、质量门和单次快照，不建立第二套 Provider 调用、缓存或归档责任人。旧
`daily_stock_selection.v1` / `daily-stock-selection-balanced.v6` 总包被冻结为兼容档案；新增策略
不得再写入该总包。页面通过 `GET /api/stock-selection/strategies` 读取目录，通过
`GET /api/stock-selection/results` 读取指定交易日的独立结果；旧日期没有独立结果时只读回退
旧总包，不重算历史 Provider 数据。

`stock_selection_strategy_outcome.v1` 按策略声明评估口径。综合候选池继续记录下一交易日
开盘至收盘、扣除 0.15% 双边成本后的组合收益、基准收益与超额收益。次日涨停机会策略分别
记录下一有效交易日的盘中触板数/率和收盘封板数/率，覆盖不足 70% 时标记
`unverifiable` 并不输出比率。长上影形态没有已经确认的收益预测目标，目录明确标记
`not_defined`，不得套用综合候选池收益作为策略证明。

## `stock_pattern_screen.v1`

旧总包 `daily-stock-selection-balanced.v6` 曾同时携带长上影与涨停机会结果；该总包现仅作
兼容读取。长上影策略使用自己的 `long-upper-shadow-main-board.v3` 身份和独立策略归档。
当前规则版本为 `long-upper-shadow-main-board.v3`。数据输入仍保留信号日及此前 14 个
已完成交易日的全市场 OHLC、昨收与成交额，但本规则只消费最后 10 个交易日；因此
不增加 Provider 请求，也不会把第 11 至 15 个交易日的形态计入当前规则。普通历史读取
只消费不可变归档，不触发 Provider 请求。旧版 `long-upper-shadow-main-board.v1` 和
`long-upper-shadow-main-board.v2` 档案保持不可变，但不视为当前 10 日规则结果。

“长上影疑似试盘形态”固定判定为：上影长度不低于收盘价 3%、不低于实体 2 倍，
并占当日最高最低振幅至少 50%。股票需在 10 个交易日内至少命中 2 次、规范化市场字段为
`主板`、名称不含 ST 或退市标识，并具有完整的最近 10 日有效 K 线。停牌、缺失或
不一致的最近 10 日日线不会被填补；该股票以 `incomplete_candlestick_window` 排除。

信号日及此前 9 个交易日内不得出现收盘封涨停。非 ST 主板涨停价使用规范化昨收价
乘以 `110%`，按 A 股 `0.01` 元价格档位四舍五入；收盘价等于该价格时以
`recent_limit_up` 排除。盘中触及涨停但收盘未封板不属于这条排除条件。

结果保留每只命中股票的全部命中日期、OHLC、上影/收盘比例、上影/实体倍数和
上影/振幅比例。`quality` 按主板非 ST 股票的完整窗口覆盖标记为 `accepted`、
`degraded` 或 `unavailable`。这个契约只表达可复现的形态代理，不证明资金主体
真实试盘，也不表达买入建议、目标价或收益概率。

## `stock_limit_up_tendency_screen.v1`

当前规则 `next-session-limit-up-tendency-main-board.v2` 复用同一份
`daily_stock_factor_snapshot.v1`，不增加 Provider 请求、缓存或调度器。它只保留主板、
非 ST/退市、上市满 120 日、收盘价不低于 3 元、信号日成交额不低于 5000 万元且流通
市值不低于 20 亿元的股票。15 个已完成交易日 OHLC、昨收、成交额以及信号日换手率、
量比和流通市值必须完整；缺失时按明确原因排除，不做估算或填补。

完整评估后，信号日必须上涨且收盘位于当日最高最低振幅的 55% 以上。排序同时消费
当日涨幅、收盘位置、相对前 14 日高点的位置、同业上涨广度、成交额放大、量比、换手
热度、5 日动量、流通市值弹性和信号日前 5 日封板历史。当日涨幅与 5 日动量使用非线性
偏好，避免把接近涨停或已经大幅加速本身当成免费上行空间。

普通主板收盘涨停仍按昨收乘以 `110%` 并四舍五入到 `0.01` 元识别。当日未涨停者标为
`pre_limit_up`，已收盘涨停者标为 `limit_up_continuation`；后者不会因当日封板直接加分，
并对次日成交与持有期风险施加 `0.85` 可执行性系数。结果最多返回 20 只，同分按规范化
证券代码排序；每只保留阶段、突破位置、行业上涨广度、连续封板数、原始指标、归一化
分项、差异化机会结构和风险。旧版 `next-session-limit-up-tendency-main-board.v1` 档案保持
不可变，不被页面当成当前规则结果。

该分数只描述同一交易日证据集里的相对机会强弱，不是已校准的涨停概率。当前档案不含
首次封板时间、炸板次数、封单金额、公告新闻、题材持续性、龙虎榜或隔夜事件，因此不能
仅凭榜单形成打板结论。若下一交易日打板成交，按 T+1 最早只能再下一交易日卖出，真实
收益要到第三个交易日才可兑现。历史读取只消费不可变档案，不重新获取或重算 Provider
数据。

## `daily_stock_selection_generation.v1`

每日选股生成由独立 `tradex.analysis_worker` 中的
`DailyStockSelectionService` 拥有。`POST /api/daily-stock-selection` 只向本地
SQLite 作业账本排队或复用当日唯一任务，并立即返回状态；`GET
/api/daily-stock-selection/generation` 只读返回同一持久化状态，不触发 Provider。

任务状态为 `idle`、`queued`、`running`、`succeeded` 或 `failed`。运行阶段可进一步
标记 `acquiring`、`selecting`、`archiving` 或 `publishing`。成功状态只带结果标识，
页面随后读取 Worker 发布的 `stock_selection_strategy_archive.v1` 展示结果；旧
`daily_stock_selection_archive.v1` 仅作历史兼容，不在任务响应中重复传输完整档案。失败时只返回安全错误、失败阶段和非敏感失败类型。手动生成与
18:30 自动补生成共享同一 Worker 和不可变归档，不会由 Dashboard 请求线程执行。

## 后台分析展示边界

`limit_sentiment_daily.v1` 是 Collector 在已验证交易日 16:10 后生成的不可变日级归档。
一次归档固定使用同一 TuShare 批次：当日 `limit_list_ths` 涨停池、当日炸板池、上一交易日
涨停池和当日日线，共四次有界请求。封板率与炸板率共享“涨停数 + 炸板数”分母；连板延续率、
首板晋级率和昨日涨停开收盘反馈都只使用该批次内的标准证券代码连接结果，禁止与旧 THS
归档或其他来源拼接分母。Provider 不给数据时间戳时保留
`provider_timestamp_missing` 并降级，不以抓取时间冒充供应商时点；空分母返回 `null`，不写
0%。Analysis Worker 只读该归档并把它加入复盘 evidence，Dashboard 不刷新 Provider。

当前使用范围是个人研究，符合 `limit_list_ths` 的研究/个人用途边界；如果使用场景改变为
商业化，仍需重新核验同花顺授权。当前 TuShare 账户对 `news` 和 `anns_d` 返回权限拒绝，
因此媒体新闻不进入复盘事实栏。

候选标的公告使用独立的 `review_announcement_candidate_manifest.v1` 和
`review_official_announcements.v1`：Analysis 先物化 presentation，Collector 再把其中
`watch_items.stocks` 的最多 12 只股票固化为候选清单并定向查询巨潮，
时间窗口从复盘交易日开始且最长 7 天，每只股票最多读取 2 页。交易日 21:10、每日 08:00
各允许一次刷新，以覆盖收盘后和周末披露；结果写入追加式 SQLite 归档。Collector 随后只
排队同一 `review_id` 的 presentation 重物化，Analysis 只读公告归档，核心 review 事实行
保持不变。公告标题、披露日期和 PDF 原文可以进入“候选标的官方公告”，但标题不会自动
标成利好、利空或次日涨跌原因；窗口内无公告也必须明确显示。媒体内容仍只能作为待核线索。

`tradex.analysis_worker` 同时拥有 `market_watch_evaluation.v1` 的回放评估，以及
`post_market_review.v1` / `post_market_review_presentation.v4` 的生成和展示物化。
Dashboard 的普通 GET 只读取 `tradex_analysis_artifact.v1`：回放读取紧凑分钟元数据和
预计算评估；日复盘读取 presentation、档案身份、学习结果与 evidence 组件覆盖摘要，
完整原始 evidence 仍保留在不可变档案中但不发送给浏览器。

`post_market_review_presentation.v4` 的正文明确展示领涨/领跌板块、板块扩散比例与
大小盘指数差，并用上一交易日不可变档案验收强势方向是否维持。潜伏异动只在板块
净流入进入同类前 10、价格未进入前三且上涨覆盖达到 55% 时出现。`watch_items`
同时携带 `stance`、`checkpoint`、`metrics`、`action` 与 evidence-linked `stocks`；
股票观察标的优先主板，且必须服从所属板块的确认与失效条件。
GET 使用 SQLite `mode=ro` 与 `query_only` 的独立 reader，数据库不存在时返回 503，
不能创建目录、数据库、表或 WAL。显式 POST 仅通过既有 Worker 数据库的 command
writer 入队；建库建表、任务执行和 artifact 写入只归 Analysis Worker。

`POST /api/post-market-review` 返回 `post_market_review_generation.v1`，页面通过
`GET /api/post-market-review/generation` 轮询 `queued/running/succeeded/failed`，成功后
再读取展示档案。网页打开、滚动、日期切换和定时刷新都不能创建分析任务、解压完整
历史、执行评估或调用 Provider；无物化结果时必须返回后台准备中，而不是在 Web 进程
内即时回退计算。
受管启动器要求 Worker 心跳不早于进程启动且距检查时刻不超过 90 秒；进程仍在但心跳
过期时按异常处理，并在下一次受管启动时停止旧进程树后重建唯一 Worker。

## `stock_relationship_profile.v1`

统一证券关系目录把过去含义混杂的“所属板块”拆成五类字段，所有功能必须按用途取值，
不能再选一个名字覆盖其余关系：

- `business_domain_name`：稳定的上级业务目录，只用于导航和宽口径展示，不能覆盖主营叶子；
- `directory_category_name`：市场目录的主显示归属。默认取精确主营叶子；有正式披露支持且
  经过人工核验时，才可改用更合适的上级目录、明确下游产业链或重大业务主题，同时保留
  上级目录与主营叶子；人工复核必须沉淀成“受控主营键 + 直接证据门槛”的通用目录规则，
  不能只追加证券代码例外；
- `primary_business_name` / `business_tags`：公司真实主营叶子及细分业务，用于股票身份、展示、
  主营筛选和主营同类；
- `statistical_industry`：带分类版本和有效期的申万行业路径，用于横向统计、标准化和同行
  比较；
- `regulatory_industry`：监管统计分类，不能自动替代主营；
- `concept_memberships`：带来源和时点的概念成员关系，只表示被某套概念分类纳入；
- 当日题材、涨停原因和资金跟随板块保留在各自的日级证据契约中，不能回写为公司长期
  主营。

涨停池的“主显示归属”属于日级市场证据，不等于上面的长期 `directory_category_name`。
它优先采用对当前交易日生效的人工复核；否则只有当涨停题材与正式披露的产品、服务、
合同或应用关系同时命中同一受控规则时，才可自动采用当期市场主题。只有题材、概念成员
关系或媒体猜测而没有直接业务证据时，必须退回长期目录。日级归因携带交易日和依据类型，
不能跨日沿用，也不能改写 `primary_business_name`。

同一催化同时存在宽泛主题和明确产品时，主显示优先选择市场正在交易的直接产品，例如
算力网络行情中的数据中心交换机显示为“交换机”；直接产品不足以形成市场共识时再使用
稳定板块父目录，例如输电线路铁塔在电网建设行情中显示为“电力电网”。金属材料企业与
珠宝零售企业必须分流：工业触头材料、白银原料和贵金属回收归入“小金属”，黄金珠宝
零售跟随黄金行情时才显示“贵金属”。

每只股票同时携带 `verification_status`、证据引用和异常标志。`verified` 需要公司官网或
正式披露材料；`corroborated` 至少需要公司资料、财务主营构成与标准行业成员关系中的
多项互相支持；只有单一结构化来源时标为 `provider_only`；冲突、陈旧或无法解析分别
标为 `disputed`、`stale`、`unresolved`，消费者必须展示待核验或降级，不能猜测补齐。
关键词规则只把已经有来源的业务文本规范化为受控标签，本身不是证据。
上级目录由受控主营键确定；当前明确支持“芯片”“医疗医药”“房地产”“消费”等宽口径。
例如香农芯创默认保留精确主营“存储”；深圳华强经过核验后主显示“芯片”，上海三毛主显示
“消费”。千金药业的正式年报披露多个中药和化药创新药项目，因此主显示归属可为“创新药”；
底层仍保留“医疗医药 → 中成药”和“创新药研发”，不能把在研项目伪写成已经实现收入的
中成药主营叶子。

主显示目录按以下优先级选择：已经审阅并写入正式证据的明确目录；受控主营叶子与披露文本
共同命中的目录关系规则；精确主营叶子。关系规则允许四类转换：上卷到市场通用父目录
（如农药到农业、纺织贸易到消费）、进入有直接产业链证据的下游目录（如含PCB应用证据的
特种高分子材料到PCB）、进入有产品/装备证据的主题目录（如含机器人装备证据的智能物流到
机器人），以及保留已经足够稳定的精确叶子（如存储、化肥）。任何转换都不能改写
`primary_business_name`；缺少所需证据时必须继续显示精确主营，例如普通智能物流不能自动
显示机器人，普通特种高分子材料也不能自动显示PCB。

`stock_relationship_catalog_status.v1` 对全量目录给出不可变 `catalog_revision`、数据日、
生成时间、覆盖计数、来源和请求标识。`InstrumentTaxonomyService` 是唯一刷新与 SQLite
写入所有者；一次刷新先完整构建 5,000+ 股票关系，再在单一事务内原子替换。Dashboard、
MCP、风险、复盘、涨停池和选股只使用只读 Reader；普通页面 GET 永不触发 Provider 或
联网检索。Analysis Worker 在目录缺失或数据日落后时后台刷新，失败保留上一个完整版本，
不会发布半成品。

网页检索用于高价值冲突和重点股票的官方证据核验，并把结果作为受控 evidence seed
进入版本库；搜索结果摘要本身不作为证据。自动全市场层使用 Tushare 网关提供的证券
主表、上市公司资料、最新主营构成和申万行业成员关系交叉印证。概念数据可以作为成员
关系补充，但不得仅凭概念名称提升为主营。深南电路当前官方证据只确认 PCB、封装基板、
FC-BGA 和电子装联；在找到直接官方材料前，`ABF` 保持 `abf_unverified`，不作为已核验
标签。

自动词表只匹配能说明产品或业务节点的限定表达。普通“存储”、不带半导体上下文的
“硅片”等宽泛词不能单独提升为主营，以免把细胞存储归到存储芯片，或把光伏硅片归到
半导体材料。“合计特别调整”“分部间抵销”等会计汇总项也不能成为主营标签。纺织、
氟化工和中成药等容易跨界的行业节点，必须有当期主营分部支撑；只有旧版公司简介命中
时不能自动升级。纺织和中成药等宽行业
归并还要求对应当期分部收入不低于总收入的 20%，且不低于任一竞争分部；低占比副业只
保留原披露标签。对于分销与产品、传统与转型业务并存的高冲突公司，以最新正式披露和最新
主营构成为准：主归属表达当前核心业务，仍有依据的次级业务保留在 `business_tags`，
历史描述或当期零收入分部没有最新披露支持时，不得继续占据主归属。
同一收入竞争门槛也约束其他自动规则：任何已识别节点占比低于 20%，或收入低于最大的
未归类竞争分部时，都不能抢占主归属。比如煤炭仅占约 1% 时必须保留为标签，不能覆盖占比
更高的尿素、甲醇等当前主营。

只读入口为 `GET /api/stock-relationships`（目录状态）和
`GET /api/stock-relationships?symbol=001309`（单股完整关系）。涨停池页面只按真实主营
保留长期目录与主营，同时按当日交叉核验的主显示归属分类；复盘观察股同时显示“主营 / 观察方向”；每日选股使用申万三级
行业做统计同类并保留供应商行业；MCP 公司资料、同行比较、行业信号和条件筛选均附带
目录版本与关系证据状态。

## 换源准入流程

1. 新增 Provider Client 和 Mapper，禁止在功能代码中出现供应商字段。
2. 通过契约、单位、时间、空值和语义不变量测试。
3. 与当前主源影子双跑，比较覆盖率、延迟、缺失率和数值偏差。
4. 灰度到一个功能快照；页面仍消费相同契约。
5. 通过配置切换主源；回滚只修改路由配置。

## 后续契约顺序

1. `valuation_snapshot.v1` 与 `financial_period.v1`。
2. 将涨停事件与行业 Profile 合成为 provider-neutral 的 Leadership Feature
   Snapshot，并把该功能的刷新缓存从 Dashboard 移至唯一 Feature Service。

每个契约单独演进，不建立包含所有金融字段的万能模型。
