# ai-test

T01 交付：AI 自动化测试平台骨架、数据模型、项目隔离、两档权限与 Spec 版本化。

本仓库只实现 T01 范围，不包含 T02 的调度/Runner 生命周期、T03 的证据存储和 T04 的报告页面。实现刻意只使用 Python 标准库（3.11+）与 SQLite，避免引入外部依赖，便于在任何一台开发机上复现。

## 已实现

- 数据模型：`project`、`member`、`spec`、`spec_version`、`run`、`run_item`、`verdict`、`attribution`、`evidence`、`review`、`cost_ledger`
- 可重复迁移：`001_init.sql` 只应用一次；重复执行校验 checksum，空库与已迁移库均不重复建表
- 项目隔离：成员 token 绑定单一项目；跨项目列表、详情与 UI 均返回 404，不泄露项目是否存在
- 两档权限：`member` 可写，`readonly` 只能读；服务端统一返回 403
- Spec 版本化：每次追加不可变 `spec_version`；Run 必须显式绑定版本，历史 Run 不随新版本漂移
- A/B 通道字段约束：A 通道必须记录外部仓库路径与 commit；B 通道必须记录评测集版本、评分器版本与模型/参数快照
- 可调用 API：JSON over HTTP；最小 HTML 管理视图按 token 只渲染所属项目
- CLI 管理入口：迁移、建项目、建成员、建 Spec/版本、建 Run、查看 Run

## 快速开始

```bash
export PYTHONPATH=src
DB=./aitest.db

python3 -m aitest.cli migrate --db "$DB"
python3 -m aitest.cli create-project --db "$DB" --name alpha
# 把输出中的 project id 填到下面（示例：prj_xxx）
python3 -m aitest.cli add-member --db "$DB" --project prj_xxx --name alice --role member
python3 -m aitest.cli serve --db "$DB" --port 8080
# 浏览器打开：http://127.0.0.1:8080/ui?token=<add-member 返回的 token>
```

`add-member` 输出的 token 只返回一次，数据库只保存 SHA-256 哈希。不要把 token 写入仓库。

## 常用 CLI

```bash
python3 -m aitest.cli status --db "$DB"
python3 -m aitest.cli list-projects --db "$DB"
python3 -m aitest.cli add-member --db "$DB" --project prj_xxx --name bob --role readonly

# A 通道 Spec + 版本
python3 -m aitest.cli create-spec --db "$DB" --token "$TOKEN" --project prj_xxx --channel A --name checkout
python3 -m aitest.cli add-spec-version --db "$DB" --token "$TOKEN" --project prj_xxx \
  --spec spc_xxx --repo-path github.com/acme/web --repo-commit <40-hex>

# B 通道 Spec + 版本
python3 -m aitest.cli create-spec --db "$DB" --token "$TOKEN" --project prj_xxx --channel B --name eval
python3 -m aitest.cli add-spec-version --db "$DB" --token "$TOKEN" --project prj_xxx \
  --spec spc_yyy --dataset-version eval-2026.11.1 --scorer-version scorer-v1 \
  --model-snapshot-json '{"model":"qwen2.5-7b","temperature":0}'

# Run 必须显式绑定版本
python3 -m aitest.cli create-run --db "$DB" --token "$TOKEN" --project prj_xxx \
  --spec-version-id spv_xxx --commit-sha <40-hex>
python3 -m aitest.cli show-run --db "$DB" --token "$TOKEN" --project prj_xxx --run run_xxx
```

## HTTP API

所有 `/projects/...` 接口需要 `Authorization: Bearer <token>`；token 由 `add-member` 生成。路径中的项目必须与 token 所属项目一致，否则返回 404。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查与 schema digest |
| GET | `/whoami` | 当前成员、项目与角色 |
| GET | `/projects` | 只返回 token 所属项目 |
| GET | `/projects/{pid}` | 项目详情（含计数） |
| GET | `/projects/{pid}/members` | 项目成员（不返回 token/hash） |
| GET | `/projects/{pid}/specs` | Spec 列表（含全部不可变版本） |
| POST | `/projects/{pid}/specs` | 创建 Spec，member 可用 |
| GET | `/projects/{pid}/specs/{sid}` | Spec 详情与版本 |
| POST | `/projects/{pid}/specs/{sid}/versions` | 追加 Spec 版本，member 可用 |
| GET | `/projects/{pid}/runs` | Run 列表，含 `spec_version` 版本号 |
| POST | `/projects/{pid}/runs` | 创建 Run，必须显式指定 `spec_version_id` 或 `spec_id` + `version` |
| GET | `/projects/{pid}/runs/{rid}` | Run 详情，含嵌套 `spec_version` 快照 |
| GET/POST | `/projects/{pid}/runs/{rid}/items` | Run 条目 |
| GET | `/ui?token=...` | 最小 HTML 管理视图；只渲染所属项目 |

错误格式：

```json
{"error": {"code": "forbidden", "message": "role 'readonly' cannot perform write operations"}}
```

## 测试与证据

```bash
export PYTHONPATH=src
python3 -m unittest discover -s tests -v
python3 scripts/verify_t01.py --out ../deliverables/evidence
```

`tests/test_t01.py` 覆盖迁移重复执行、核心表/外键、项目隔离（API + UI）、两档权限、Spec 双版本与 Run 绑定、A/B 通道字段差异。`scripts/verify_t01.py` 在真实 HTTP 服务上跑两条独立空库的同一流程，并把可核对的 JSON 证据写入 `../deliverables/evidence`。

## 设计与限制

- SQLite 作为 T01 的关系型元数据存储；生产环境可替换为 PostgreSQL，但迁移与业务规则需保持同样的约束。
- 当前没有 T02 的调度、Runner 心跳、取消/超时；Run 初始状态为 `queued`，生命周期由后续任务补齐。
- 当前 UI 是最小只读视图，管理操作通过 CLI 或 API 完成；T04 再实现完整报告视图。
- token 为开发期 bearer token，适合内网骨架验证；生产需接入短期凭证/身份提供方并加入轮换与审计。
- 项目删除会触发 `spec_version` 不可删触发器而失败；T01 不提供项目删除操作，避免误删历史结论。
