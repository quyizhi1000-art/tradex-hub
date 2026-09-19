# 数据采集修复验收（2026-09-17）

与任务 `01a0ad6e-424e-7a31-b058-3129441885ca` 达成一致，结合任务 `01a0ad74-2c66-74a1-a0af-0229b39cfc78` 诊断，由本任务实施。双方确认本批范围为：质量误验收、追补发布、严格分钟完整性。原诊断任务保持只读。

## 实施结果

- 正常采集在历史写入前拒绝 stale/unavailable；异常恢复必须提供真实质量证据。有效数据已经落盘、仅账本绑定失败的恢复仍保留。
- 重启对账按旧 snapshot ID、旧 revision、真实质量精确纠正错误成功指针，保留原始失败快照及所有尝试。
- 现有采集器主循环在当分钟采集之后执行本地发布检查，间隔 30 秒；基准 revision、曲线 manifest 和追补目标未改变时不重建。没有新增线程、进程或上游请求。
- 仅重建两个轨迹字段，保持整体快照时间、其他业务字段和质量不变。持久化前比较基准 revision，持久化后核对实际内容和账本指针。发布失败/重启会重试。
- 缓存已齐、此次零下载也会触发发布。只有已发布目标集合覆盖历史能力预期分钟，才能标 complete。
- 收盘完整性要求 09:31–11:30、13:01–15:00 的逐分钟集合，不再放过缺开头、单分钟缺失或只有尾点。
- 页面明确显示缓存完成与发布尚未完成的差别，展示实际发布截止时间和请求目标。

## 现场验收

新发布 revision：`16b25c769cf2c44bf860556c46b28dd21b82b40094c332ed7ca668f1e4fd2e1d`。

- 今日账本由错误的 121 条接受，纠正为 100 条有效、21 条未解决；下午尚未到期的 118 个时点保持 pending。
- 最新有效快照是 11:27；88 条曲线均覆盖 09:31–11:27 的 117 个分钟桶。当日 detail API 与五日 API 的对应点完全一致。
- 所有原有点的时间均保留；7 个旧值由当前历史源同一时间的真实值修正，逐项新旧值记录在 `runtime_verification.json`，没有不明来源值或插值。
- 旧 revision 请求返回 HTTP 409；新版本条件请求返回 304、0 字节；重复发布不生成新 revision。
- 重启保留窗口内另纠正 149 条较早日期的同类错误，共纠正 170 条。`ledger_audit.json` 记录逐日数量；原始失败快照和尝试均与备份逐项一致。
- `before_rows.sqlite3` 是变更前受影响快照/账本/尝试的精确备份；`before_invalid_accepted.json` 保留更长历史范围的审计发现，早于现有重启对账窗口的记录没有在本批另做全库迁移。
- 受管采集器已重启，模块路径为 `G:\money\tradex\src\tradex`，状态和心跳正常；实际服务 `/watch/app.js` 与工作区文件 SHA-256 一致。

## 验证与边界

实现文件：`market_watch/collection_store.py`、`market_watch/collector.py`、`market_watch/history.py`、新增 `market_watch/trajectory_publication.py`、`dashboard/collector_worker.py`、`data_gateway/sector_flow.py`、`data_gateway/sector_flow_store.py`、`dashboard/watch/app.js`（均位于 `tradex/src/tradex`）。对应测试位于 `tradex/tests`：`test_market_watch_collection_store.py`、`test_data_gateway_sector_flow.py`、新增 `test_market_watch_trajectory_publication.py`、`test_collection_recovery_ui.cjs`。

116 个不同的相关 Python 测试通过（分批执行，涵盖 ledger、history、publisher、worker、sector-flow、architecture boundaries）；4 个 UI 状态回归测试通过；`git diff --check` 通过。

现场修复发生在午休。11:28、11:29 的整体快照仍不合格，追补请求明确保持 partial；没有把历史轨迹补齐冒充整体盘面补齐。后续出现有效快照时，采集器会再次发布和验收。

本批没有修复实时板块上午只录下 12 个分钟的根因，没有放宽整体完整性门槛，没有实施并发提速。没有进行下午实际采样、当日收盘执行、完整测试套件或浏览器视觉验收；相关运行层不能宣称已验证。

本批未提交 Git 或推送；其他任务已有配置和研究产物未纳入实现修改。任务通信工具使用完毕后按项目要求关闭。
