# 方舟用量采集与看板

本模块负责官方小时/日用量查询、请求明细导出、分钟聚合、PostgreSQL 导入，以及 Grafana **wtpm / RPM 合并看板**。所有命令均在项目根目录 `Cancanneed/` 执行，共用根目录 `.venv` 和 `.env`。

数据链路：`官方请求明细 → records.csv → PostgreSQL → 分钟视图 → Grafana`。小时/日查询用于参考和覆盖核对，其结果不会自动导入分钟视图。

## 文件说明

| 路径 | 用途 |
| --- | --- |
| [`scripts/ark_usage.py`](scripts/ark_usage.py) | 小时/日查询，支持接入点、API Key 资源 ID 和返回维度筛选 |
| [`scripts/ark_minute_usage.py`](scripts/ark_minute_usage.py) | 单次明细导出、完整性检查、分钟聚合 |
| [`scripts/import_tpm.py`](scripts/import_tpm.py) | 校验后事务导入，按账号和记录 UUID 幂等更新 |
| [`scripts/sync_tpm_range.py`](scripts/sync_tpm_range.py) | 分片同步最近 1～24 小时，支持续传和看板更新 |
| [`sql/schema.sql`](sql/schema.sql) | 导出元数据、明细表、TPM 与 RPM 视图 |
| [`scripts/build-tpm-dashboard.mjs`](scripts/build-tpm-dashboard.mjs) | 生成 wtpm / RPM 合并看板 |
| [`grafana/dashboards/ark-model-tpm.json`](grafana/dashboards/ark-model-tpm.json) | 通用看板模板，使用相对时间，不含实际批次信息 |
| [`scripts/build-rpm-dashboard.mjs`](scripts/build-rpm-dashboard.mjs) | 旧命令兼容入口，生成同一份合并看板 |
| [`requirements.txt`](requirements.txt) | 固定版本 Python 依赖 |
| [`.env.example`](.env.example) | 方舟凭据字段示例 |
| [`tests/`](tests/) | 离线测试，包含跨分片记录时间回归用例 |
| [`docs/`](docs/) | 接口调研、验证方法、同步流程与 RPM 口径 |
| `data/` | 本地请求、原始响应、CSV、JSON 汇总及任务状态；不提交 Git |

## 初始化

首次安装服务请按[根目录快速开始](../README.md#快速开始)执行。Python 环境使用：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r data_ark/requirements.txt
```

将本目录 [`.env.example`](.env.example) 中的配置追加到根目录 `.env`，保留现有 Grafana 和 PostgreSQL 配置：

```dotenv
VOLCENGINE_ACCESS_KEY=
VOLCENGINE_SECRET_KEY=
VOLCENGINE_SESSION_TOKEN=
```

小时/日查询和明细导出均需 IAM **AK/SK**。临时 STS 凭据必须同时填写 Session Token，普通 AK/SK 可留空。脚本以绝对路径查找根目录 `.env`，通过 `load_dotenv(override=True)` 覆盖同名环境变量。

推理用 Bearer API Key 不能为这些管理面接口鉴权。`--api-key-id` 传入的是用于筛选的 **Key 资源 ID**。

## 小时 / 日查询

日期按北京时间解释。将两个 `YYYY-MM-DD` 分别替换为目标起止日期；先加 `--dry-run` 可仅检查参数：

```bash
.venv/bin/python data_ark/scripts/ark_usage.py \
  --start "YYYY-MM-DD" --end "YYYY-MM-DD" --interval Hour \
  --project default \
  --dimension ModelName --dimension ModelVersion --dimension ModelEndpoint \
  --dry-run
```

去掉 `--dry-run` 才会查询。粒度可选 `Hour` 或 `Day`；`--endpoint ep-your-endpoint-id` 筛选接入点，`--api-key-id your-resource-id` 筛选 Key 资源 ID。`--dimension`、`--endpoint`、`--api-key-id` 均可重复。

输出默认在 `data_ark/data/<采集时间-随机后缀>/`，含请求参数 `request.json`、原始响应 `raw.json`、格式化 `normalized.json`。`--output-dir` 可指定目录。Token 按整数解析，未知字段和空值保留。

## 同步最近 24 小时

先去掉上例的 `--dry-run`，取得覆盖目标窗口日期的小时参考文件，再执行：

```bash
.venv/bin/python data_ark/scripts/sync_tpm_range.py \
  --hours 24 --hourly-reference "data_ark/data/小时查询目录/normalized.json"
```

小时参考数据需包含一个可确认的账号，用于检查返回账号、记录空窗口及标注不支持导出的模型。它不是分钟明细完整性的证明。

默认结束时间为当前最近一个完整分钟，可用 `--end` 传入带时区且对齐整分钟的 ISO 8601 时间。`--hours` 支持 1～24；24 小时拆成 8 个连续的 3 小时窗口，顺序创建导出任务。该命令需要 PostgreSQL 运行和 Node.js 可用。

```bash
# 只预览分片，不联网、不导入。
.venv/bin/python data_ark/scripts/sync_tpm_range.py --hours 24 --dry-run

# 中断后继续，路径替换为原批次目录。
.venv/bin/python data_ark/scripts/sync_tpm_range.py \
  --resume "data_ark/data/批次目录"
```

批次目录为 `data_ark/data/sync-<小时数>h-<结束时间>-<随机后缀>/`。`manifest.json` 保存状态；`window-01/` 等保存各任务原始文件；全部成功后生成批次级 `minute-model.json`、`minute-model.csv`、`summary.json`，并更新看板默认范围。

已完成的分片可跳过，已有任务继续轮询/下载。创建请求结果未知时不会自动重复提交。若单片超过 100,000 行被截断，会停止且不导入该片；需另行缩小时间范围处理，当前不会自动细分。更多细节见 [24 小时同步](docs/ark-tpm-24h-sync.md)。

## 单次明细导出与导入

单次可查询最近 1～180 个完整分钟：

```bash
.venv/bin/python data_ark/scripts/ark_minute_usage.py --minutes 60 --dry-run
.venv/bin/python data_ark/scripts/ark_minute_usage.py --minutes 60
```

默认请求脚本内服务端已确认的 16 个模型；`--model` 可指定其中的模型且可重复，`--help` 列出当前可选值。`--end` 必须带时区并对齐整分钟；`--hourly-reference` 可附带小时参考数据用于覆盖说明。

输出包含 `records.csv`、`minute-model.json`、`minute-model.csv`、`summary.json` 和任务元数据。下载链接中的临时凭据不会写入任务 JSON。聚合文件采用稀疏输出，无记录分钟不创建虚构明细。

```bash
# 对已有任务继续轮询/下载，不创建新任务。
.venv/bin/python data_ark/scripts/ark_minute_usage.py \
  --resume "data_ark/data/任务目录"

# 离线重新解析已下载的 CSV。
.venv/bin/python data_ark/scripts/ark_minute_usage.py \
  --offline "data_ark/data/任务目录"

# 校验后导入；加 --dry-run 可只校验、不连接数据库。
.venv/bin/python data_ark/scripts/import_tpm.py "data_ark/data/任务目录"

# 用该任务的窗口更新看板默认范围。
node data_ark/scripts/build-tpm-dashboard.mjs \
  "data_ark/data/任务目录/minute-model.json"
```

导入先检查任务成功、行数和截断状态，再在一个事务中创建/更新表、视图及记录，并授予 Grafana 只读权限。Token 使用 `bigint`，重复或重叠导出按 `(account_id, record_uuid)` 更新，较早导出的记录不会覆盖较新记录。

空窗口没有可从明细中识别的账号，手动导入时需通过 `--account-id` 提供已核实的账号；批次同步会从小时参考中传入。

## 合并看板与统计口径

打开 [方舟用量 · wtpm / RPM](http://localhost:3000/d/ark-model-tpm)，位于 **方舟用量** 文件夹。此看板只包含：

| 位置 | 指标 | 口径 |
| --- | --- | --- |
| 上方 | 各模型总 wtpm | 每分钟输入与输出 Token 相加，再除以 10,000 |
| 下方 | 各模型 RPM | 按账号、模型和请求 ID 去重，归入最早用量记录所在分钟 |

两图共用账号、模型和时间筛选，采用 `#B877D9` 紫色、1px 线宽、无数据点、整数显示。账号选项从本地数据库读取，仓库看板不绑定个人账号。模型的不同版本和接入点合并展示；模型筛选支持多选。

自然分钟来自明细 `timestamp`，并非请求到达时间或逐 Token 生成时间。缓存 Token 已包含在输入中，不再次累加。数据库分钟视图保留稀疏数据；看板 SQL 在所选时间与已导出总体范围的交集中生成分钟序列，将无记录处显示为 0。未导出模型、导出间隙、近期迟到数据均不能据此认定为零用量。

看板使用 [`ark.yaml`](../config/provisioning/dashboards/ark.yaml) 加载，复用只读数据源 `Local PostgreSQL`。JSON 变化约 10 秒后加载；重新打开看板可采用新默认范围，URL 中显式的时间参数仍会优先。每 30 秒刷新只查询 PostgreSQL，不会抓取新的云端明细。

仓库中的 JSON 是默认最近 24 小时的通用模板。启动脚本在本地看板不存在时将其复制到 `.runtime/ark-dashboards/`，Grafana 只读取这个忽略目录。生成器传入任务或批次文件时，也只更新该目录，因此实际查询时间不会改动 Git 跟踪文件。升级该目录配置后需重启一次 Grafana。

需要更新公开模板的样式或 SQL 时，不传入数据文件：

```bash
node data_ark/scripts/build-tpm-dashboard.mjs
```

旧 `build-rpm-dashboard.mjs` 命令已转为兼容入口，不会生成独立 RPM 看板。详细请求数口径见 [RPM 说明](docs/ark-rpm-dashboard.md)。

## 限制与验证

明细接口只支持固定列表中的部分模型，当前未接入 VMP。每次导出的支持列表、返回模型和小时参考差异保存在本地 `summary.json`，不发布账号的实际覆盖情况。

单次导出最长 3 小时、最多 100,000 行；每账号最多 3 个活跃任务、滚动 24 小时最多 100 次提交。轮询间隔至少 5 秒，单轮最多轮询 5 分钟，下载链接有效期 12 小时。当前没有后台定时采集，不能将明细快照称为全账号实时完整统计。

```bash
.venv/bin/python -m unittest discover -s data_ark/tests -p 'test_*.py' -v
```

测试使用虚构记录和模拟网络响应，无需云端凭据，不使用真实账号导出。完整依据见[接口可行性说明](docs/ark-token-usage-feasibility.md)及[分钟用量验证](docs/ark-minute-usage-validation.md)。
