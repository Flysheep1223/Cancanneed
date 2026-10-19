# 最近 24 小时分钟用量同步

[`sync_tpm_range.py`](../scripts/sync_tpm_range.py) 将最近 1～24 小时拆成每片最长 3 小时的连续窗口，依次导出、校验并导入 PostgreSQL。全部成功后生成一份 **wtpm / RPM 合并看板**，两图使用相同的默认时间范围。

这是手动批量同步流程，没有后台定时任务；Grafana 每 30 秒刷新不会触发云端导出。

## 执行

前提：根目录 `.venv` 已安装依赖，`.env` 已配置 AK/SK（临时凭据另需 Session Token），PostgreSQL 正在运行，Node.js 可用。首次部署见[项目 README](../../README.md#快速开始)。

先查询覆盖目标窗口日期的小时参考数据。将两个 `YYYY-MM-DD` 分别替换为北京时间起止日期：

```bash
.venv/bin/python data_ark/scripts/ark_usage.py \
  --start "YYYY-MM-DD" --end "YYYY-MM-DD" --interval Hour \
  --dimension ModelName --dimension ModelVersion --dimension ModelEndpoint

.venv/bin/python data_ark/scripts/sync_tpm_range.py \
  --hours 24 --hourly-reference "data_ark/data/小时查询目录/normalized.json"
```

参考文件需包含单一可确认账号，用于核对导出归属、记录空窗口及标注未支持模型，不会将小时总量推算成分钟数据。

默认结束时间取当前最近一个完整分钟。需要固定窗口时，可通过 `--end` 传入 ISO 8601 时间；必须带时区、对齐整分钟且不晚于当前时间。范围为左闭右开，起点不早于 2026-05-01 UTC。

```bash
# 预览分片，不发请求、不导入。
.venv/bin/python data_ark/scripts/sync_tpm_range.py --hours 24 --dry-run

# 中断后使用原批次目录续传。
.venv/bin/python data_ark/scripts/sync_tpm_range.py \
  --resume "data_ark/data/批次目录"
```

24 小时通常创建 8 个任务，顺序执行；每账号滚动 24 小时最多提交 100 次、最多 3 个活跃任务。一次完整运行消耗 8 次提交额度，已有任务的续传不重新提交。

## 批次文件与恢复

输出目录为 `data_ark/data/sync-<小时数>h-<结束时间>-<随机后缀>/`：

| 文件 / 目录 | 用途 |
| --- | --- |
| `manifest.json` | 批次范围、账号、模型、小时参考路径和各分片状态 |
| `window-01/` 等 | 各分片请求、任务元数据、原始 CSV 及局部聚合 |
| `merged-records.csv` | 合并的原始明细，供全局范围重聚合 |
| `minute-model.json`、`minute-model.csv` | 整个批次的稀疏分钟数据 |
| `summary.json` | 模型汇总、覆盖信息和验证计数 |

已按批次范围导入的窗口会跳过；存在 `created.json` 的窗口继续使用原任务。若任务创建超时且结果未知，脚本会停止，不能直接自动重复提交。参考文件路径记录在 manifest 中，续传时应保持该文件可用。

若 `ExportCount != TotalCount`，该窗口标为截断且不导入。当前脚本不会自动把截断分片再细分；应另行缩小范围，通过单次导出/导入流程处理，或改用更短的批次。先前成功导入的数据保留。

若数据已导入但看板生成失败，确认 Node.js 可用后重跑 `--resume`。导入按 `(account_id, record_uuid)` 幂等更新，重叠窗口不会重复累计。

## 跨分片时间边界

记录时间可能早于来源分片起点、但仍在整个批次内。同步流程按**整个批次的范围**决定是否纳入，随后按记录实际时间分分钟；数据库继续保留任务的真实查询窗口。

批次汇总重新读取全部原始 CSV，在全局范围内去重聚合，不直接相加各片的局部汇总。这样可以恢复来源分片外的有效记录，同时去掉跨片重复 UUID。回归测试见 [`test_sync_tpm_range.py`](../tests/test_sync_tpm_range.py)。

8 个连续查询窗口只证明这些范围已被查询，不证明所有模型、所有迟到记录都已覆盖。原始分钟文件保持稀疏；看板对缺失分钟补 0，不能将补零解读为最终零用量。

## 看板更新

全部完成后，脚本调用 `build-tpm-dashboard.mjs`，更新 `.runtime/ark-dashboards/ark-model-tpm.json` 中两图共用的默认时间。此文件受 Git 忽略，不会改变仓库中的通用模板。Grafana provisioning 约 10 秒加载配置，重新打开 [方舟用量 · wtpm / RPM](http://localhost:3000/d/ark-model-tpm) 即可查看。

已打开页面可能保留 URL 中的旧时间范围，应重新选择范围或打开不含时间参数的链接。wtpm 为万 Token/分钟，RPM 为去重请求/分钟，统计细节见 [RPM 说明](ark-rpm-dashboard.md)。
