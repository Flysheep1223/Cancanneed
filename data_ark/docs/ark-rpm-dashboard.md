# wtpm / RPM 合并看板与请求数口径

打开 [方舟用量 · wtpm / RPM](http://localhost:3000/d/ark-model-tpm)。上方为各模型总 wtpm，下方为 RPM，共用账号、模型和时间筛选。此看板只有两张趋势图，不含统计卡片、表格或其他面板。

两图使用 `#B877D9` 紫色、1px 实线、无数据点、整数显示及固定 1 分钟桶。`1 wtpm = 10,000 Token/分钟`，RPM 为请求/分钟。

## 去重与时间归属

官方 CSV 中的 `req_id` 导入为 `ark_usage_records.request_id`。同一请求可能有多条合法用量记录，所以不能直接把明细行数当作 RPM。

视图 `public.ark_model_rpm` 先按 `(account_id, model_name, request_id)` 去重，取该请求在已导入记录中的最早 `event_time`，再按自然分钟计数。同模型的不同版本与接入点合并。

例如，同一请求在 12:00:58 和 12:01:02 各有一条记录，RPM 只在 12:00 桶计一次；两条记录的 Token 则分别按自身记录时间计入 TPM。因而不能简单相加 `ark_model_tpm.request_count` 来替代此 RPM 视图。

这表示已导出请求的记录时间口径，不是网关接收请求的精确时间，也不是服务配置的 RPM 配额。未进入用量明细的失败、限流请求及未支持模型均不保证覆盖。后续导入更早的同请求记录时，最早时间及对应分钟计数可能随之修正。

## 补零和模型筛选

数据库分钟视图保持稀疏。看板 SQL 在所选时间与账号已导出总体范围的交集中生成分钟序列，再将所选模型的缺失值显示为 0。模型选项来自已导入的明细，账号也由数据库查询取得。

补零保证显示连续，不代表已核实该分钟没有实际用量。导出间隙、迟到记录、未导出模型仍是覆盖限制。原始 CSV 和范围说明保留在本地输出中。

## 实现与同步

| 文件 | 用途 |
| --- | --- |
| [`sql/schema.sql`](../sql/schema.sql) | 明细表、TPM 与 RPM 视图 |
| [`scripts/build-tpm-dashboard.mjs`](../scripts/build-tpm-dashboard.mjs) | 两张趋势图及共用筛选器 |
| [`grafana/dashboards/ark-model-tpm.json`](../grafana/dashboards/ark-model-tpm.json) | 不含实际数据和时间窗口的合并看板模板 |
| [`scripts/sync_tpm_range.py`](../scripts/sync_tpm_range.py) | 更新数据与看板默认范围 |

手动重新生成时，使用某次任务或批次的分钟 JSON：

```bash
node data_ark/scripts/build-tpm-dashboard.mjs \
  "data_ark/data/任务或批次目录/minute-model.json"
```

旧 `build-rpm-dashboard.mjs` 命令会转到同一个生成器，不会创建独立 RPM 看板。新记录经 `import_tpm.py` 导入后，两个视图会反映新数据；30 秒页面刷新只查询 PostgreSQL。

传入任务/批次文件时，生成器只写入 Git 忽略的 `.runtime/ark-dashboards/`，Grafana 从该目录加载。无参数运行生成器则更新仓库中的通用模板，默认范围为 `now-24h` 到 `now`。

验收时应使用原始 CSV 独立去重后核对分钟计数，并用 Grafana 只读账号验证面板 SQL、筛选和补零；同请求的多条记录跨分钟时应只计一次。真实对账产物只保存在本地。
