# 火山方舟用量查询：接口能力与实现范围

范围为火山方舟 Ark 中国站，管理面地域 `cn-beijing`。本文说明接口能力和实现范围；账号标识、请求追踪信息、查询日期与实际用量只保存在本地输出目录。

## 已实现的能力

本项目实现了 IAM AK/SK（含临时 STS 凭据）小时查询、部分模型请求明细导出、分钟聚合、PostgreSQL 导入和 Grafana 展示。主看板为一份 **wtpm / RPM 合并看板**，仅含两张趋势图。

| 需求 | 路径 | 当前结论 |
| --- | --- | --- |
| 项目、接入点或指定 Key 的小时/日用量 | `GetInferenceUsage` | 支持 AK/SK 鉴权及过滤；脚本已实现 |
| 仅使用推理 Bearer API Key 查询历史账号汇总 | `GetInferenceUsage` | 不支持这种鉴权 |
| 部分模型历史分钟用量 | `CreateRecordExportTask` → CSV → 本地聚合 | 已实现，有模型与任务配额限制 |
| 最近 1～24 小时分片同步 | 每片最长 3 小时，校验后入库 | 已实现，手动运行并支持续传 |
| 全账号所有模型的完整分钟用量 | 当前明细导出路径 | 未覆盖，不能据此承诺完整统计 |
| 分钟监控直接接入 VMP | 方舟监控工作区 → Prometheus 查询 | 尚未接入，需另行配置网络和权限 |
| 仅有 API Key 时采集自己的新请求 | 调用端记录响应 `usage` | 可另行实现，不能补回未采集的历史 |

运行命令见[模块 README](../README.md)。当前无后台定时采集，Grafana 刷新仅查询本地数据。

## 鉴权与身份边界

三个容易混淆的概念：

- **推理 API Key 密钥**：放在推理请求 `Authorization: Bearer ...` 中。
- **API Key 资源 ID**：控制台中的 Key 标识，用于查询过滤，不负责鉴权。
- **IAM AK/SK**：管理面 HMAC-SHA256 签名凭据；临时凭据还需 Session Token。

`GetInferenceUsage` 使用如下管理面地址：

```text
POST https://ark.cn-beijing.volcengineapi.com/?Action=GetInferenceUsage&Version=2024-01-01
Content-Type: application/json
Region: cn-beijing
Service: ark
```

脚本使用官方 SDK `SignerV4` 签名，签名头包含 `Host`、`Content-Type`、`X-Date`、`X-Content-Sha256` 和 `Authorization`；STS 通过 `X-Security-Token` 传入。签名时间为 UTC，请求日期为北京时间，两者含义不同。

推理地址 `https://ark.cn-beijing.volces.com/api/v3` 与管理面地址不同。管理面请求不能改用 Bearer Key。鉴权错误可能以 HTTP 400 和 `ResponseMetadata.Error` 返回，不能只检查 HTTP 401/403。

权限受 IAM 身份、项目及具体动作约束。`ProjectName` 默认 `default`，默认项目不等于全账号所有项目；过滤条件不是权限隔离。官方列有 `ArkReadOnlyAccess`，但本项目没有验证可通用于所有新接口的最小自定义策略。

## GetInferenceUsage：小时与日

| 参数 | 规范 |
| --- | --- |
| `StartTime`、`EndTime` | `yyyy-MM-dd`，北京时间，结束不早于开始 |
| `QueryInterval` | 仅 `Hour` 或 `Day`，没有 `Minute` |
| `ProjectName` | 可选，默认 `default` |
| `Filters` | 多个过滤项之间为 AND |
| `Filters[].Values` | 精确值列表；空列表用于返回该维度 |
| `Filters[].ValueLike` | 相似匹配，不与 `Values` 同时传入 |
| `ShowWindowDetail` | 计费窗口详情，不会增加分钟粒度 |

接入点与 API Key 资源 ID 的过滤模板，日期和资源 ID 均需替换：

```json
{
  "StartTime": "YYYY-MM-DD",
  "EndTime": "YYYY-MM-DD",
  "QueryInterval": "Hour",
  "ProjectName": "default",
  "Filters": [
    {"Key": "ModelEndpoint", "Values": ["ep-your-endpoint-id"]},
    {"Key": "ApikeyID", "Values": ["your-api-key-resource-id"]}
  ],
  "ShowWindowDetail": false
}
```

过滤键枚举使用 `ApikeyID`；明细接口中的字段可能写作 `APIKeyID`，不可混用。其他过滤维度包括 `EndpointName`、`ModelName`、`ModelVersion`、`ModelUnitID`、`BillingStatus`、`BatchJobID`、`BatchType` 和 `AuthToken`。本实现不采用旧的脱敏 Key `AuthToken` 过滤。

返回通过 `Result.Fields` 定义列、`Result.Data` 保存二维数据，`DataCount` 用于核对行数。脚本动态映射字段，不硬编码列顺序；保留未知字段和空值，Token 数使用整数，检查列宽、重复列、日期及计数。

常见指标为 `InputTokens`、`OutputTokens`、`TotalTokens`、`ReqCnt`，维度随过滤设置变化。小时桶按返回的 `Day`、`Hour` 组合为 UTC+8 时间。该接口未承诺完整拆分 reasoning tokens 或 cached tokens，Token 总量也不能直接当作费用。

调研页面没有明确承诺汇总接口的落库延迟、保留期、单次最大跨度、分页或截断规则、QPS 及结束日期包含性。应使用已知数据核对边界，不能将空结果直接解释为零用量。

脚本保留 `request.json`、原始 `raw.json` 和 `normalized.json`。连接/读取超时分别为 5/30 秒；网络异常、429、5xx 最多尝试 3 次，每次重新签名，单次退避最多 60 秒。鉴权错误不重试；响应同时检查 HTTP 状态与 `ResponseMetadata.Error`。传输关闭环境代理和 `.netrc` 的隐式读取，需要代理的环境应显式调整传输配置。

## 官方请求明细导出

已实现的路径为：

```text
CreateRecordExportTask → GetRecordExportTask → 下载 CSV
→ 校验完整性 → 按记录 UUID 去重 → 分钟聚合 / PostgreSQL 导入
```

`Filters` 必须包含 `model_name`，不能省略后假定导出全账号。当前 16 个支持模型保存在 [`ark_minute_usage.py`](../scripts/ark_minute_usage.py) 的 `EXPORT_MODELS`，CLI `--help` 可查看。应使用精确名称，并在服务端列表变更时更新客户端。

| 约束 | 客户端遵循的规则 |
| --- | --- |
| 单次时间窗口 | 最长 3 小时，起点不得早于 2026-05-01 UTC |
| 结束时间 | 项目要求带时区、对齐整分钟且不晚于当前时间 |
| 任务数量 | 每账号最多 3 个活跃任务，滚动 24 小时最多提交 100 次 |
| 单任务导出量 | 上限 100,000 行；核对 `ExportCount`、`TotalCount` 及 CSV 行数 |
| 轮询 | 间隔至少 5 秒，每轮最多轮询 5 分钟 |
| 下载链接 | 有效期 12 小时，不将链接中的临时凭据写入任务 JSON |
| 创建重试 | 不自动重试，避免超时后重复创建；已知任务可续传 |

数据不是逐 Token 生成时刻：`timestamp` 是用量记录时间，长请求的 Token 可能集中于一个分钟桶。`total_tokens = input_tokens + output_tokens`；`cache_hit_tokens` 已包含在输入中，不重复相加。直接使用原始 Token 字段，不使用 `charge_value_list` 的计费缩放值替代。

同一请求可能产生多条合法用量明细，因此 Token 按记录 UUID 去重，RPM 则按账号、模型、请求 ID 去重。原始分钟输出与数据库视图保持稀疏，Grafana 查询层对缺失分钟补 0。支持范围、迟到数据及导出间隙仍需单独判断。

单片校验、跨片记录归属及数据库口径见[分钟验证](ark-minute-usage-validation.md)、[批次同步](ark-tpm-24h-sync.md)和 [RPM 说明](ark-rpm-dashboard.md)。

## PostgreSQL 与 Grafana

当前已经实现：

| 数据对象 | 用途 |
| --- | --- |
| `ark_usage_exports` | 任务、账号、查询窗口和覆盖信息 |
| `ark_usage_records` | 官方明细，主键 `(account_id, record_uuid)`，Token 为 `bigint` |
| `ark_model_tpm` | 记录时间的分钟 × 账号 × 模型 Token 聚合 |
| `ark_model_rpm` | 请求先去重再按最早记录时间分桶 |
| 看板 `ark-model-tpm` | 上方总 wtpm、下方 RPM，共用筛选器 |

导入过程先校验再事务写入，重复或重叠导出不会重复累计，旧导出不覆盖更新的记录。Grafana 使用独立只读账号。看板仅显示趋势，不包含统计卡片或明细表；每 30 秒刷新不触发云端查询。

小时/日 JSON 当前仅作为参考文件保存，没有另建小时/日数据库表。模拟性能数据位于独立的 `metric_samples`，不与官方明细相加。

## 其他路径及适用边界

**已知请求 ID 查询。** `GetInferenceRequestUsage` 接受 `QueryHour` 和同步推理响应体的 `RequestID`，同样使用 AK/SK，可返回该请求的多条用量记录。它适用于对账，不能枚举时间范围内全部请求。查询的追踪 `RequestId` 与推理响应 ID 是不同标识。

**VMP 监控。** 方舟接入点监控基于托管 Prometheus。调研文档描述方舟监控指标保留最近 15 天，可使用工作区 BasicAuth 或具备 `VMPQueryAccess` 的 AK/SK 查询。开启监控所需权限、监控保留期和费用规则不能套用到历史用量 API。

使用前需确认工作区查询地址可达，并核实实际指标、标签和采样频率。`query_range?step=60s` 只表示求值步长，不证明有精确自然分钟用量；counter、rate、gauge 应按指标定义处理。

**调用端采集。** 只有推理 API Key 时，可以在自己控制的调用链保留响应 `usage`。对支持的 Responses API 模型，可提取 `input_tokens`、`output_tokens`、`total_tokens`、`input_tokens_details.cached_tokens` 和 `output_tokens_details.reasoning_tokens`。缓存和推理 Token 分别属于输入与输出明细，不能再次加到总量中。

流式采集宜保留原始 SSE JSONL，终态生成一份可读 JSON，避免重复累计；中断、失败或缺少 usage 时标为缺失。此路线尚未实现为本项目的真实采集器，也无法恢复未采集过的账号历史。

旧 `GetUsage` 文档曾计划于 2026-06-30 下线，本项目未探测其当前可用性，未将其作为依赖。

## 官方资料

以下为本次调研使用的资料，能力与额度可能随服务更新；未知约束没有使用其他接口额度替代。

1. [GetInferenceUsage](https://docs.volcengine.com/docs/ark/get-inference-usage-api?lang=zh)：当前主接口、参数与资源 ID 规则。
2. [Base URL 及鉴权](https://docs.volcengine.com/docs/82379/1298459)：推理数据面与管理面的区别。
3. [签名机制](https://www.volcengine.com/docs/6369/67269)：HMAC-SHA256。
4. [GetInferenceRequestUsage](https://docs.volcengine.com/docs/ark/get-inference-request-usage-api?lang=zh)：已知请求 ID 的明细。
5. [IAM 权限管理](https://docs.volcengine.com/docs/ark/use-iam-to-manage-permissions?lang=zh)：身份、项目及策略。
6. [CreateRecordExportTask](https://docs.volcengine.com/docs/ark/create-record-export-task-api?lang=zh)：导出模型、参数与配额。
7. [GetRecordExportTask](https://docs.volcengine.com/docs/ark/get-record-export-task-api?lang=zh)：状态、完整性、下载与轮询。
8. [CSV 解析最佳实践](https://docs.volcengine.com/docs/82379/2673887)：用量明细字段。
9. [接入点监控](https://www.volcengine.com/docs/82379/1182403?lang=zh)：VMP 集成与保留期。
10. [VMP Python 查询](https://www.volcengine.com/docs/6731/2550797?lang=zh)：鉴权和 Prometheus API。
11. [ListWorkspaces](https://www.volcengine.com/docs/6731/1037505?lang=zh)：工作区与查询地址。
12. [Responses API 深度思考](https://docs.volcengine.com/docs/ark/responses-api-deep-thinking?lang=zh)与[上下文缓存](https://www.volcengine.com/docs/82379/1602228?lang=zh)：usage 明细口径。
