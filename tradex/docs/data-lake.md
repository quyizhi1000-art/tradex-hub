# Tradex + CNEquity 数据湖

## 定位

这套连接不是把 CNEquity 注册成另一个实时行情源。两边职责不同：

- CNEquity 保存收盘后可校验的日线、指数、市场宽度和板块历史，作为基准湖与结果标签来源。
- Tradex 继续通过现有 SmartRouter 获取实时数据，并由独立采集器保存每次决策实际使用的输入。
- 特征与攻/防/现金候选都带版本和输入引用；当前只运行 `shadow`，不会自动交易。

## 数据流

```text
SmartRouter live
  -> capture observer
  -> content-addressed JSON (完整原始证据)
  -> Parquet raw partitions (分析扫描)
  -> SQLite catalog transaction (发布边界)
  -> versioned feature
  -> shadow decision
  -> next-session outcome labels from CNEquity

CNEquity EOD
  -> read-only bridge
  -> baseline/replay/outcome labels
```

控制面只会返回 `completed` capture。进程若在文件写完、目录事务提交前退出，最多留下不可见的 orphan 文件，不会暴露半条决策。

## 90 日基础湖

本机默认：

```powershell
G:\money\scripts\cnequity_seed_90d.ps1
```

精确窗口按首尾都包含的 90 个自然日计算。2026-08-19 对应 2026-05-22 至 2026-08-19。

最小基础集包含：

- instruments、trading_calendar
- daily_bars、index_bars
- market_breadth（由 daily_bars 本地派生）
- sector_bars

`analyst_consensus`、`fund_flow`、`sector_members`、`hot_rank`、`sector_fund_flow`、`news_headlines`、`flash_news_wire`、`economic_calendar` 是快照语义，只能从启用当天起沉淀，不能伪造过去 90 日。

历史 `trading_status` 的全市场 Baostock 扫描和北京交易所 issued-code discovery 都是独立长任务，不阻塞第一版湖；交付状态必须明确这两个覆盖缺口。

## 独立采集器

安装湖依赖：

```powershell
G:\money\.venv\Scripts\python.exe -m pip install -e "G:\money\tradex[lake]"
G:\money\.venv\Scripts\python.exe -m pip install -e "G:\CNEquity"
```

第二条命令是运行时连接，不只是开发依赖：Tradex MCP 与标签任务必须在自身
Python 环境中导入 CNEquity 的公共 reader，两个互相隔离的 venv 不会自动共享包。

单次采集与状态：

```powershell
$env:TRADEX_DATA_LAKE_ROOT = 'G:\money\.tradex-run\lake'
G:\money\.venv\Scripts\python.exe -m tradex.data_lake collect-once
G:\money\.venv\Scripts\python.exe -m tradex.data_lake status
```

回放指定采集：

```powershell
G:\money\.venv\Scripts\python.exe -m tradex.data_lake replay <capture_id>
```

回放会分别核验对象、Parquet、Bundle、基础快照和 shadow 决策，并报告每个分项。
`match_scope=declared_checks_only` 是刻意的边界：当前可以从已存最终 Feature 重跑权重策略，
但轨迹/稳定化等状态化最终 Feature 尚不能只靠 raw objects 完整重算，因此结果会明确返回
`final_feature_recomputed=false`，不能把它解释成全链路字节级再现。

交易时段独立运行：

```powershell
G:\money\.venv\Scripts\python.exe -m tradex.data_lake collect-run --interval 60
```

看板普通读取不会写湖。只有显式采集器传入 observer，才会保存输入并推进原有轨迹存储。
默认采集器在联网前做一次真实 DuckDB/Parquet 往返预检；同一分钟已有失败记录时不会
反复联网，常驻采集器也会记录每次结果和连续失败次数。

## CNEquity 只读连接

设置：

```powershell
$env:CNEQUITY_DATA_ROOT = 'G:\CNEquity\data\cnequity'
```

桥接层只调用 CNEquity 公共读取 API，并用 Parquet 文件的相对路径、大小和纳秒修改时间生成 `source_generation`。CNEquity 更新或 compact 时不要并发读取；标签和回放应固定到记录下来的 generation。

在 CNEquity 日终更新完成、湖代际稳定后，为已成熟的 shadow 决策写入代理标签：

```powershell
$env:TRADEX_DATA_LAKE_ROOT = 'G:\money\.tradex-run\lake'
$env:CNEQUITY_DATA_ROOT = 'G:\CNEquity\data\cnequity'
G:\money\.venv\Scripts\python.exe -m tradex.data_lake label-pending `
  --through-date 2026-08-19 `
  --proxy 510300.SH
```

代理固定来自 `daily_bars`。若配置基准，必须同时明确数据集，避免把指数代码误查
到证券日线：ETF/证券使用 `--benchmark-dataset daily_bars`，指数代码使用
`--benchmark-dataset index_bars`。标签严格依赖 CNEquity `trading_calendar` 的下一
交易日；该日缺 bar 时保持 pending，不会向后滑日伪装成 1 日结果。

## 权重语义

三个概念必须分开：

- evidence contribution：每个信号如何影响候选。
- shadow allocation：当前策略提出的攻/防/现金研究候选。
- executed allocation：真实执行仓位；当前系统不生成这一层。

数据质量低、关键组件缺失/过期、开盘观察或方向未知时，shadow 策略弃权并记录 `(offense=0, defense=0, cash=1)`。其余权重也只是等待样本和结果标签验证的先验，不应当被描述为已确认的实盘比例。
