# 分钟用量：验证方法与覆盖范围

本文说明如何验证小时查询、请求明细导出、分钟聚合及 [wtpm / RPM 合并看板](http://localhost:3000/d/ark-model-tpm)，不包含任何账号的实测数据或查询日期。

本文记录验证方法和已确认的接口行为。真实账号、任务 ID、请求 ID、原始明细及用量数值保留在 Git 忽略的 `data_ark/data/`，公开仓库不包含这些数据。

## 调用路径与检查点

1. `GetInferenceUsage` 支持 AK/SK 与 STS 临时凭据，可获取小时/日参考数据。
2. `CreateRecordExportTask` 必须传入 `model_name` 筛选，参数错误需检查服务端允许列表。
3. 模型名称应使用脚本支持列表中的精确值，不将文档中的别名直接当成服务端模型 ID。
4. 成功导出通过 `GetRecordExportTask` 取得任务状态及官方签发的 CSV 下载地址。
5. 原始 CSV、分钟汇总、模型汇总与 PostgreSQL 数据应独立核对，面板查询应使用 Grafana 只读账号验证。
6. 本项目的分钟数据来自明细导出；VMP 是尚未集成的另一条路径。

当前固定支持列表见 [`EXPORT_MODELS`](../scripts/ark_minute_usage.py)，共 16 个模型。服务端名单可能变更，不能把该列表当成全部 Ark 模型。

## 输出与验收字段

| 文件 | 核对内容 |
| --- | --- |
| `request.json` | 查询范围、模型列表和接口参数 |
| `created.json`、`task.json` | 任务 ID、状态、`ExportCount` 和 `TotalCount`；不保存下载链接凭据 |
| `records.csv` | 官方原始用量明细，保留以便重算 |
| `minute-model.json` | 窗口、范围、验证计数和稀疏分钟数据 |
| `minute-model.csv` | 可读分钟聚合 |
| `summary.json` | 不含逐分钟行的模型汇总、覆盖与验证信息 |

导入前检查任务成功、`ExportCount == TotalCount`，并核对 CSV 实际行数。一致说明该任务未因导出上限截断，不证明近期数据已全部落库或最终结算。

聚合以 `(account_id, uuid)` 去重：相同记录只计一次，同 UUID 内容冲突会报错。输出中的 `duplicate_records_skipped`、`outside_window_skipped` 应与原始记录和窗口相符。

## Token 与时间口径

- 时间来自 CSV `timestamp`（Unix 秒），按自然分钟分桶；同模型不同接入点、版本合并。
- `total_tokens = input_tokens + output_tokens`，缓存命中属于输入 Token 的子集，不重复相加。
- 直接使用原始 Token 字段，不把 `charge_value_list` 中的计费缩放值当作 Token。
- 同一请求可能有多条合法明细，Token 不按请求 ID 删除记录；RPM 单独去重，见 [RPM 说明](ark-rpm-dashboard.md)。
- 记录时间不等于请求开始时间或逐 Token 生成时间。视频/图像模型的官方 Token 字段也不能直接解释为文本长度或费用。

原始分钟文件和数据库视图为稀疏数据；看板在已导出总体范围与所选时间的交集中补齐分钟，将缺失处显示为 0。补零只是曲线显示规则，不能据此断言无请求、无消耗或全模型覆盖。

## 模型与窗口覆盖

`scope.requested_models` 表示请求的模型，`scope.returned_models` 表示实际有记录的模型，`models_without_exported_records` 表示请求了但没有返回记录的模型。

提供小时参考文件后，`hourly_reference.models_not_supported_by_export` 会列出小时数据中存在、但当前导出列表不支持的模型。小时窗口中的存在性也不证明某个精确分钟一定有请求。实际模型覆盖保留在本地输出中。

对于时间戳落在来源分片外、但位于整个批次范围内的记录，批次按全局范围重聚合原始 CSV，保留真实来源窗口，不直接相加各片的局部分钟汇总。详见[分片同步](ark-tpm-24h-sync.md)。

## 可重复执行的检查

在项目根目录、依赖已安装的 `.venv` 中执行：

```bash
.venv/bin/python -m unittest discover -s data_ark/tests -p 'test_*.py' -v

# 已有任务离线重算，不联网。
.venv/bin/python data_ark/scripts/ark_minute_usage.py \
  --offline "data_ark/data/任务目录"

# 校验待导入任务，不连接数据库。
.venv/bin/python data_ark/scripts/import_tpm.py \
  "data_ark/data/任务目录" --dry-run
```

离线测试全部使用虚构记录，覆盖字段映射、大整数精度、时间边界、签名正文、模拟重试、明细去重、截断检查及跨分片记录恢复。网络测试使用 mock，不代表服务端限流或权限策略已完整验证。

真实数据验收还应独立按原始 CSV 求和，并与分钟汇总及数据库对比。RPM 应按请求去重后核对，不能直接使用 CSV 行数代替。对于近期数据、缺失模型及控制台最终账单，仍需额外对账。

官方依据见[接口能力说明](ark-token-usage-feasibility.md#官方资料)。
