# PostgreSQL ↔ Cloudflare D1 同步器

把一个 PostgreSQL schema 的普通表定期镜像到 Cloudflare D1；需要恢复时，从 D1 **只下拉一次**，在一个 PostgreSQL 事务中强制替换本地表，然后退出。提供 Dockerfile、Compose 示例、单元测试和 CI。

> **重要：这不是 PostgreSQL 完整备份工具。** D1 基于 SQLite，无法忠实表达 PostgreSQL 的所有数据库对象。下拉会删除并重建所选表，恢复列、受支持的类型、NULL 属性、主键和自增序列；**不会恢复**外键、次级索引、触发器、视图、函数、权限、一般默认值或扩展。生产环境请另外保留 `pg_dump` / PITR 备份，并先在测试库演练。下拉不会自动重试；云端快照不完整时拒绝覆盖本地。

## 工作方式

- **推送 `SYNC_MODE=push`（默认）**：每隔 `SYNC_INTERVAL_SECONDS` 秒读取一次 PostgreSQL 的一致快照。每张表先写入 D1 影子表，成功后通过 D1 batch 替换正式表，涵盖新增、修改和删除。表与表之间**不是**一个全局原子事务；D1 需要容纳正在构建的影子表，接近容量上限时可能失败。失败后保留旧正式表并按 `RETRY_DELAY_SECONDS` 重试；不完整快照不可下拉。
- **下拉 `SYNC_MODE=pull`**：必须另设 `ALLOW_DESTRUCTIVE_PULL=true`。先下载完整 D1 快照到临时文件，确认快照元数据仍然有效，再在 PostgreSQL 事务中删除、重建和插入本地表。**只执行一次，不论成功失败都退出**。默认会删除本地目标 schema 中的全部普通表，仅重建 D1 快照中的表；`SYNC_TABLES` 非空时只替换列出的表。如果推送时使用了 `SYNC_TABLES`，下拉时也必须指定表清单。请停止应用写入和推送容器，再下拉。
- 单个 PostgreSQL 数据库通过 advisory lock 避免同时运行两份同步器。D1 请视为本项目管理的专用镜像；项目不会覆盖未被自己管理的同名表。

## 支持范围

| PostgreSQL | D1 | 下拉 |
| --- | --- | --- |
| `smallint` / `integer` / `bigint` / `boolean` | `INTEGER` | 原类型；整数查询按文本读取以避免 JSON 精度损失 |
| `real` / `double precision` | `REAL` | 原类型 |
| `numeric` | `TEXT` | 原精度，不按 SQLite 浮点数处理 |
| 文本、`uuid`、`json`/`jsonb`、日期和时间 | `TEXT` | 原类型 |
| `bytea` | `BLOB` | 原类型；通过十六进制 SQL 字面量写入，单行二进制总量需低于约 50 KiB |

不支持数组、枚举、域、分区表、生成列、几何/范围等专用类型；检测到不支持的列会报错而不写入。D1 平台有每表列数、单 SQL 大小、参数数和数据库总大小等限制；建议用于小到中等、写入频率适中的数据库。镜像 D1 中只有基本表结构和数据，没有 PostgreSQL 的外键或次级索引语义。**请勿同时让 Worker 或其他程序修改镜像表**，尤其是下拉期间。

## 准备

1. 在 Cloudflare 创建一个 D1 数据库，记录 **Account ID** 和 **D1 Database ID**。创建限制为目标数据库的 API Token，授予 D1 Read / D1 Write 权限。
2. 让 Docker 容器能连接 PostgreSQL，并为使用的账号授予读取源表、创建/删除目标 schema 表的权限。下拉需要 DDL 权限；如果本地存在跨 schema 外键引用，程序会拒绝覆盖。
3. 复制 `.env.example` 为 `.env`，填写连接串和凭据。`.env` 已在 `.gitignore` 中。**不要把真实凭据提交到 GitHub。**

```sh
cp .env.example .env
# 编辑 .env 中的 POSTGRES_DSN、CLOUDFLARE_API_TOKEN、CLOUDFLARE_ACCOUNT_ID、D1_DATABASE_ID
```

Windows/macOS 的容器可通过 `host.docker.internal` 连接宿主机 PostgreSQL；Compose 文件也为 Linux 配置了 `host-gateway`。请确认 PostgreSQL 监听地址、`pg_hba.conf`、防火墙和认证配置允许来自容器的连接。若 PostgreSQL 在其他主机，把主机名直接写进连接串。

## Docker 运行

持续推送：

```sh
docker compose up -d --build
docker compose logs -f sync
```

只推送一次（测试配置或手动运行）：

```sh
docker compose run --rm -e SYNC_ONCE=true sync
```

**破坏性下拉**：先停止持续推送，并备份当前 PostgreSQL；再执行一次性容器：

```sh
docker compose stop sync
docker compose run --rm -e SYNC_MODE=pull -e ALLOW_DESTRUCTIVE_PULL=true sync
```

下拉成功后，如果想恢复持续推送，再运行 `docker compose up -d sync`。不要用 `docker compose up` 长期启动 `SYNC_MODE=pull` 并保留自动重启策略；一次性下拉请始终使用 `docker compose run --rm`。

不用 Compose：

```sh
docker build -t pg-d1-sync .
docker run -d --name pg-d1-sync --restart unless-stopped --env-file .env pg-d1-sync
docker stop pg-d1-sync
docker run --rm --env-file .env -e SYNC_MODE=pull -e ALLOW_DESTRUCTIVE_PULL=true pg-d1-sync
```

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `POSTGRES_DSN` | 必填 | PostgreSQL 连接串，也接受 `DATABASE_URL` |
| `POSTGRES_SCHEMA` | `public` | 要镜像的普通表所在 schema |
| `CLOUDFLARE_API_TOKEN` | 必填 | D1 API Token，也接受 `CF_API_TOKEN` |
| `CLOUDFLARE_ACCOUNT_ID` | 必填 | Cloudflare Account ID |
| `D1_DATABASE_ID` | 必填 | D1 数据库 UUID |
| `SYNC_MODE` | `push` | `push` / `pull`；下拉一次后必定退出 |
| `SYNC_INTERVAL_SECONDS` | `300` | 推送周期，按任务开始到下次任务开始计算 |
| `RETRY_DELAY_SECONDS` | `30` | 推送失败后的重试等待 |
| `SYNC_TABLES` | 空 | 逗号分隔表名；空表示全部支持的普通表 |
| `SYNC_ONCE` | `false` | `push` 只运行一轮后退出 |
| `ALLOW_DESTRUCTIVE_PULL` | `false` | 下拉覆盖的显式确认 |
| `D1_BATCH_SIZE` | `20` | 每次 API batch 的行数，1–50 |
| `D1_PAGE_SIZE` | `100` | 下拉每页行数，1–500 |
| `D1_REQUEST_TIMEOUT_SECONDS` | `30` | 单次 D1 API 超时 |
| `LOG_LEVEL` | `INFO` | Python 日志级别 |

推送不持久化本地游标：每轮全量读取源表、写入影子表，所以断电重启后仍能收敛；代价是 O(总行数) 的读取、写入、API 调用和临时 D1 空间。大量数据或需要 CDC、低延迟、完整 PostgreSQL 对象备份时，不建议使用这个项目作为唯一方案。

## 本地开发与测试

```sh
python -m venv .venv
# 激活虚拟环境后
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试覆盖配置、D1 REST 请求、SQL 标识符转义、类型转换、精确数值/二进制、影子表失败保护、强制覆盖前的快照校验和一次性下拉。设置 `TEST_POSTGRES_DSN` 后还会运行真实 PostgreSQL 集成测试；GitHub Actions 提供 PostgreSQL 服务并构建 Docker 镜像。真实 D1 凭据未配置到 CI，上线前仍需演练。

## 官方参考

- [Cloudflare D1 Query API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/)
- [Cloudflare D1 平台限制](https://developers.cloudflare.com/d1/platform/limits/)
- [Psycopg 3 transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)
