# Cancanneed · 方舟用量监控

在本地 Mac 上运行 Grafana + PostgreSQL，导出火山方舟（Ark）的官方请求用量明细，按分钟查看各模型的 **wtpm 和 RPM**。

主看板只有两张上下排列的趋势图：上方为 wtpm，下方为 RPM，共用账号、模型和时间筛选。曲线使用 1px 紫色细线、无数据点、整数显示，缺失分钟补 0。

- **wtpm**：万 Token/分钟，`(输入 Token + 输出 Token) / 10000`。
- **RPM**：每分钟去重请求数，按账号、模型和请求 ID 去重后，以最早用量记录时间分桶。
- 数据来自官方明细导出，仅覆盖接口支持的模型。看板补 0 是显示规则，不代表已证明该分钟没有用量。
- 当前采用手动批量同步。看板每 30 秒刷新只查询本地数据库，不会自动调用方舟接口。

## 环境要求

| 组件 | 要求 |
| --- | --- |
| 操作系统 | macOS Apple Silicon（ARM64）；安装脚本不适用于 Windows、Linux 或 Intel Mac |
| Grafana OSS | 13.2.4，脚本下载官方包并校验 SHA256 |
| PostgreSQL | 18.6，使用 Postgres.app 2.9.6 的原生二进制包并校验 SHA256 |
| Python | 3.12，统一使用项目根目录 `.venv` |
| uv | 创建 Python 环境、安装固定版本依赖 |
| Node.js | 建议 20+；生成看板及完整批次同步时需要，无 npm 依赖 |
| 方舟凭据 | 有相关查询/导出权限的 IAM AK/SK；临时凭据另需 Session Token |

Grafana、PostgreSQL 程序和数据库均保存在项目目录内，无需 Docker。首次安装需要访问 Grafana、GitHub 和 Python 包源。

## 快速开始

```bash
git clone https://github.com/Flysheep1223/Cancanneed.git
cd Cancanneed

# 按此顺序初始化，让脚本生成 .env 与随机密码。
./scripts/grafana.sh install
./scripts/postgres.sh setup
./scripts/grafana.sh start

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r data_ark/requirements.txt
```

打开 <http://localhost:3000>，默认免登录。此时尚未导入方舟数据；首次导入会创建方舟数据表和视图。

安装脚本自动生成根目录 `.env`，PostgreSQL 初始化会追加数据库配置。`.env.example` 仅作字段参考，无需复制覆盖生成的文件。将 [`data_ark/.env.example`](data_ark/.env.example) 中的 AK/SK 和可选 Session Token 配置追加到根目录 `.env`，保留已有数据库配置。Python 脚本使用 `load_dotenv(override=True)`，以该文件覆盖同名环境变量。

推理用的 Bearer API Key 不能替代管理面 AK/SK；`--api-key-id` 是小时/日查询的资源 ID 筛选条件。

先做一次不联网的参数检查：

```bash
.venv/bin/python data_ark/scripts/sync_tpm_range.py --hours 24 --dry-run
```

查询覆盖目标日期的小时参考数据，再同步最近 24 小时。将下面两个 `YYYY-MM-DD` 分别替换为目标窗口覆盖的北京时间起止日期：

```bash
.venv/bin/python data_ark/scripts/ark_usage.py \
  --start "YYYY-MM-DD" --end "YYYY-MM-DD" --interval Hour \
  --dimension ModelName --dimension ModelVersion --dimension ModelEndpoint

# 将路径替换为上一步输出的目录。
.venv/bin/python data_ark/scripts/sync_tpm_range.py \
  --hours 24 --hourly-reference "data_ark/data/小时查询目录/normalized.json"
```

同步将 24 小时拆成 8 个连续的 3 小时窗口，依次导出、校验、去重入库，并更新看板默认时间范围。打开 [方舟用量 · wtpm / RPM](http://localhost:3000/d/ark-model-tpm) 查看结果。仅保留这一份方舟看板，UID 为 `ark-model-tpm`。

单次导出上限 100,000 行；遇到截断会停止，需要缩小窗口。每账号滚动 24 小时最多提交 100 个导出任务，不适合每分钟提交一次。完整操作、续传和模型覆盖说明见 [`data_ark/README.md`](data_ark/README.md)。

## 服务管理

以下命令均在项目根目录执行：

```bash
./scripts/grafana.sh status
./scripts/grafana.sh restart
./scripts/grafana.sh stop
./scripts/grafana.sh logs

./scripts/postgres.sh status
./scripts/postgres.sh restart
./scripts/postgres.sh stop
./scripts/postgres.sh psql     # metrics_writer 写入账号
./scripts/postgres.sh reader   # grafana_reader 只读账号
./scripts/postgres.sh admin    # postgres 管理员
./scripts/postgres.sh backup   # 备份到 data/backups/
```

两个服务都在后台运行，关闭启动终端后继续运行；未配置开机自启。Mac 重启后依次执行：

```bash
./scripts/postgres.sh start
./scripts/grafana.sh start
```

停止服务不会删除数据。迁移前停止服务，备份 `data/`、`data_ark/data/`、`.runtime/ark-dashboards/`、`config/` 和 `.env`。

## 本地访问与数据库

Grafana 默认只监听 `127.0.0.1:3000`，匿名用户在 `Main Org.` 中拥有 **Admin** 权限，可管理看板和数据源。这是本机使用配置；若要开放给其他机器，应先调整认证与访问权限。

[`config/grafana.ini`](config/grafana.ini) 关闭登录表单、Basic 认证和退出入口。旧登录、注册及密码找回链接由 [`config/grafana-no-login.html`](config/grafana-no-login.html) 跳转至首页；启动时生成 `.runtime/grafana-public-13.2.4/` 静态目录，其余资源复用官方安装包。

PostgreSQL 默认只监听 `127.0.0.1:5432`，采用 SCRAM 密码认证。业务库为 `metrics`：

| 账号 | 用途 | 密码配置 |
| --- | --- | --- |
| `metrics_writer` | 写入及维护业务表 | `POSTGRES_PASSWORD` |
| `grafana_reader` | Grafana 只读查询 | `GRAFANA_POSTGRES_PASSWORD` |
| `postgres` | 数据库管理 | `POSTGRES_ADMIN_PASSWORD` |

数据源 **Local PostgreSQL**（UID `local-postgres`）由 [`postgresql.yaml`](config/provisioning/datasources/postgresql.yaml) 自动配置。Token 明细采用 `bigint`，时间采用 `timestamptz`；数据库时区为 UTC，看板展示北京时间。

`.env` 权限为 `600`。管理员和数据库密码仅在首次创建账号时读取；修改 `.env` 不会重置已有密码。数据库密码可在管理员命令行用 `\password metrics_writer` 或 `\password grafana_reader` 修改，再同步更新 `.env`。修改数据库端口或只读账号密码后需重启 Grafana。

## 可选：模拟数据演示

无需方舟凭据即可启动 [模型性能 · PostgreSQL 实时演示](http://localhost:3000/d/postgres-live-demo)，位于独立的 **Demo** 文件夹：

```bash
./scripts/demo.sh start   # 补最近 2 小时历史，随后每 5 秒追加
./scripts/demo.sh status
./scripts/demo.sh stop    # 停止追加，保留历史
./scripts/demo.sh seed    # 仅补历史，已有时间点不重复写入
./scripts/demo.sh clean   # 停止并删除带 grafana_demo_v1 标签的模拟数据
```

演示包含三个虚拟模型的 TTFT、E2E、输入/输出 Token、独立的推理 Token、生成速度及缓存指标，**不代表真实模型性能**。数据保存在 `public.metric_samples`，运行期间仅保留最近 6 小时模拟数据，方舟数据不受影响。

修改演示看板生成脚本后，可运行 `node scripts/build-demo-dashboard.mjs` 重新生成 JSON。方舟看板的生成与同步方法见 [`data_ark` 说明](data_ark/README.md)。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| [`scripts/`](scripts/) | Grafana、PostgreSQL、模拟采集器管理脚本 |
| [`config/`](config/) | 服务配置、数据库初始化 SQL 和演示看板 |
| [`config/provisioning/`](config/provisioning/) | 自动配置数据源和看板 |
| [`data_ark/`](data_ark/README.md) | 方舟查询、导出、同步、SQL、合并看板、测试与文档 |
| `.env` | 本地凭据和服务配置，不提交 |
| `.venv/` | Python 虚拟环境，不提交 |
| `.runtime/` | 下载包、程序、静态资源、包含批次时间的本地看板及 PID，不提交 |
| `data/` | Grafana、PostgreSQL 数据及备份，不提交 |
| `data_ark/data/` | 原始响应、CSV、汇总 JSON 和批次状态，不提交 |
| `logs/` | 服务与采集日志，不提交 |

仓库看板只保留 `now-24h` 到 `now` 的通用模板。Grafana 启动时将模板复制到 `.runtime/ark-dashboards/`（已有本地看板则保留），实际同步仅更新该忽略目录。真实凭据、账号、导出文件、查询时间和用量不进入源码；离线测试使用虚构记录。常见导出文件名和数据库备份也已加入 `.gitignore`。

## 验证与排查

```bash
# 离线测试，不需要云端凭据。
.venv/bin/python -m unittest discover -s data_ark/tests -p 'test_*.py' -v

# Grafana 健康检查：database 应为 ok。
curl --fail http://127.0.0.1:3000/api/health
```

看板无数据时，先确认 PostgreSQL 正在运行且已完成导入，再检查账号、模型和时间范围。已打开页面的 URL 可能保留旧时间，重新打开不带时间参数的看板链接可采用最新同步的默认范围。数据较新但曲线仍无变化时，需要执行新一轮同步；页面刷新不会抓取云端数据。

接口支持范围与鉴权结论见[可行性说明](data_ark/docs/ark-token-usage-feasibility.md)，导出验证方法见[分钟用量验证](data_ark/docs/ark-minute-usage-validation.md)，批量同步与请求数口径分别见 [24 小时同步](data_ark/docs/ark-tpm-24h-sync.md)及 [RPM 说明](data_ark/docs/ark-rpm-dashboard.md)。
