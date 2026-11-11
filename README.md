# ai-test

AI 自动化测试平台骨架：T01 数据模型、项目隔离、两档权限与 Spec 版本化；T02 调度、Runner 管理与 Run 生命周期 API。

本仓库实现 T01 与 T02 范围，不包含 T03 的证据存储和 T04 的报告页面。实现刻意只使用 Python 标准库（3.11+）与 SQLite，避免引入外部依赖，便于在任何一台开发机上复现。

## 已实现

- 数据模型：`project`、`member`、`spec`、`spec_version`、`run`、`run_item`、`verdict`、`attribution`、`evidence`、`review`、`cost_ledger`
- 可重复迁移：`001_init.sql` 只应用一次；重复执行校验 checksum，空库与已迁移库均不重复建表
- 项目隔离：成员 token 绑定单一项目；跨项目列表、详情与 UI 均返回 404，不泄露项目是否存在
- 两档权限：`member` 可写，`readonly` 只能读；服务端统一返回 403
- Spec 版本化：每次追加不可变 `spec_version`；Run 必须显式绑定版本，历史 Run 不随新版本漂移
- A/B 通道字段约束：A 通道必须记录外部仓库路径与 commit；B 通道必须记录评测集版本、评分器版本与模型/参数快照
- 可调用 API：JSON over HTTP；最小 HTML 管理视图按 token 只渲染所属项目
- CLI 管理入口：迁移、建项目、建成员、建 Spec/版本、建 Run、查看 Run
- T02 Run 生命周期：创建、状态查询、取消、强制重跑；状态迁移写入 append-only `run_event`
- T02 调度：项目级并发上限、优先级队列、单 Run 硬超时、排队超时；队列深度与优先级顺序可查询
- T02 幂等：`project + commit_sha + spec_version + 参数摘要` 派生幂等键；重复提交返回同一 `run_id` 并标记 `reused`，`force=true` 生成新 Run 并保留新旧关联
- T02 Runner：注册、心跳、容量、失效摘除与恢复；Runner 通过 claim/complete 拉取并回写任务

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
| GET | `/projects/{pid}/runs` | Run 列表，支持 `status`、`limit`，含优先级与队列位置 |
| POST | `/projects/{pid}/runs` | 创建 Run；同幂等键返回 200 + `reused=true`，新建返回 201；`force=true` 生成新 Run |
| GET | `/projects/{pid}/runs/{rid}` | Run 详情，含 `spec_version`、调度字段、取消信息与 `events` |
| POST | `/projects/{pid}/runs/{rid}/cancel` | 取消排队中或运行中的 Run，记录取消来源/人/原因 |
| POST | `/projects/{pid}/runs/{rid}/rerun` | 强制重跑，新 Run 记录 `rerun_of_run_id` |
| GET | `/projects/{pid}/runs/{rid}/events` | append-only 状态迁移记录 |
| GET/POST | `/projects/{pid}/runs/{rid}/items` | Run 条目 |
| GET | `/projects/{pid}/scheduler` | 并发上限、运行中/排队数、队列深度与优先级顺序、Runner 可用性 |
| POST | `/projects/{pid}/scheduler/tick` | 执行一次维护：排队超时、硬超时、失效 Runner 摘除 |
| POST | `/projects/{pid}/scheduler/config` | 修改并发上限、失效阈值与默认超时，member 可用 |
| GET | `/projects/{pid}/runners` | Runner 列表（心跳年龄、可用性、容量） |
| POST | `/projects/{pid}/runners` | 注册 Runner，token 只返回一次，member 可用 |
| GET | `/projects/{pid}/runners/{rid}` | Runner 详情与最近事件 |
| GET | `/projects/{pid}/runners/{rid}/events` | Runner 注册/心跳/失效/恢复事件 |
| POST | `/projects/{pid}/runners/{rid}/revoke` | 撤销 Runner，member 可用 |
| POST | `/runner/heartbeat` | Runner 心跳（runner token） |
| GET | `/runner/me` | 当前 Runner 与可用性（runner token） |
| POST | `/runner/claim` | Runner 拉取一个排队 Run；无任务返回 `data: null` 与原因（runner token） |
| POST | `/runner/runs/{rid}/heartbeat` | 续租运行中的 Run（runner token） |
| POST | `/runner/runs/{rid}/complete` | 回写 `succeeded/failed/timeout` 与结果摘要（runner token） |
| GET | `/ui?token=...` | 最小 HTML 管理视图；只渲染所属项目 |

错误格式：

```json
{"error": {"code": "forbidden", "message": "role 'readonly' cannot perform write operations"}}
```

## 调度与 Runner 快速验证

```bash
export PYTHONPATH=src
DB=./aitest.db
TOKEN=<member token>
PRJ=<project id>

# 并发上限、失效阈值与默认超时
python3 -m aitest.cli set-scheduler --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --max-concurrency 1 --runner-stale-seconds 30

# 注册 Runner（token 只返回一次）
python3 -m aitest.cli register-runner --db "$DB" --token "$TOKEN" --project "$PRJ" --name runner-a
RT=<runner token>

# 创建带优先级的 Run（相同参数重复提交会复用）
python3 -m aitest.cli create-run --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --spec-version-id spv_xxx --commit-sha <40-hex> --priority 80 --timeout-seconds 60
python3 -m aitest.cli cancel-run --db "$DB" --token "$TOKEN" --project "$PRJ" --run run_xxx --reason "new commit"
python3 -m aitest.cli rerun-run --db "$DB" --token "$TOKEN" --project "$PRJ" --run run_xxx

# Runner 拉取、续租、回写
python3 -m aitest.cli claim-run --db "$DB" --runner-token "$RT"
python3 -m aitest.cli heartbeat-runner --db "$DB" --runner-token "$RT"
python3 -m aitest.cli complete-run --db "$DB" --runner-token "$RT" --run run_xxx --status succeeded --summary ok
python3 -m aitest.cli scheduler-status --db "$DB" --token "$TOKEN" --project "$PRJ"
python3 -m aitest.cli run-events --db "$DB" --token "$TOKEN" --project "$PRJ" --run run_xxx
```

调度是拉取式且由请求驱动：每次 Runner claim、scheduler 查询或 `scheduler/tick` 都会执行一次有界的维护（排队超时、硬超时、Runner 失效摘除），不需要后台进程。详见 `docs/scheduler.md`。

## 测试与证据

```bash
export PYTHONPATH=src
python3 -m unittest discover -s tests -v
python3 scripts/verify_t01.py --out ../deliverables/evidence
python3 scripts/verify_t02.py --out ../deliverables/evidence
```

`tests/test_t01.py` 覆盖迁移重复执行、核心表/外键、项目隔离（API + UI）、两档权限、Spec 双版本与 Run 绑定、A/B 通道字段差异。`tests/test_t02.py` 覆盖幂等复用与强制重跑、排队取消、硬超时/排队超时、并发上限与优先级、Runner 失效摘除与恢复、只读与跨项目拒绝。`scripts/verify_t01.py` 与 `scripts/verify_t02.py` 在真实 HTTP 服务上跑可复现流程，并把可核对的 JSON 证据写入 `../deliverables/evidence`。

## 设计与限制

- SQLite 作为 T01 的关系型元数据存储；生产环境可替换为 PostgreSQL，但迁移与业务规则需保持同样的约束。
- 调度为项目级：并发上限、Runner 池和队列按项目隔离，避免 T01 的两档权限被跨项目 Runner 绕过；平台级共享 Runner 池需要额外的管理角色。
- 调度由 API 请求与 Runner claim 驱动，没有后台守护进程；生产环境应由 CI 轮询或定时器调用 `scheduler/tick`。
- Runner 通过拉取式 claim 工作；无效 Runner 会被摘除，新的 Run 不会被调度给它。已撤销 Runner 的在途 Run 依赖硬超时/租约到期收敛。
- 当前 UI 是最小只读视图，管理操作通过 CLI 或 API 完成；T04 再实现完整报告视图。
- token 为开发期 bearer token，适合内网骨架验证；生产需接入短期凭证/身份提供方并加入轮换与审计。
- 项目删除会触发 `spec_version` 不可删触发器而失败；T01 不提供项目删除操作，避免误删历史结论。
