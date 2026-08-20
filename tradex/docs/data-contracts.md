# Tradex 标准数据契约

> 状态：第三阶段；当前实现 `market_overview.v1`、`quote_snapshot.v1`、
> `ohlcv_bar.v1`、`market_breadth.v1`、`sector_quote.v1`、
> `etf_quote.v1`、`leader_quote.v1`、`stock_sector_profile.v1` 和 `board_leader.v1`。
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

## Leadership 三类小契约

Dashboard 的领涨股展示不再直接调用 Tencent 或 Eastmoney 抓取函数，而是拆成
三个用途明确的契约：

- `leader_quote.v1`：配置中的代表股票小批量行情；代码、名称、最新价和涨跌幅
  是稳定字段。允许供应商只返回部分代码，但必须标记
  `quote_coverage_partial`；供应商时间缺失时标记降级。
- `stock_sector_profile.v1`：涨停池股票的行业、地域和概念标签。它是归因输入，
  必须一次完整覆盖请求代码、代码不重复、数据时间属于指定交易日；任何一项不符
  都拒绝整个批次，不能让部分分类结果扭曲板块计数。
- `board_leader.v1`：板块内有限数量的领涨成分，金额统一为人民币元、比例统一为
  百分点。名称或代码缺失会拒绝该 Provider；资金流、成交额或供应商时间缺失可
  返回降级快照，并保留明确质量标志。

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
300 次，并发预算满时立即交给路由回退。
移除单个 `BIYING_PRIMARY_CAPABILITIES` 项即可只回滚该能力；
`BIYING_ENABLED=false` 可整体停用。

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
