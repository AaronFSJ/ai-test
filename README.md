# ai-test

AI 自动化测试平台骨架，当前集成交付范围：

- **T01** 数据模型、项目隔离、两档权限与 Spec 版本化
- **T02** 调度、Runner 管理与 Run 生命周期 API
- **T08** 评测集管理与版本化样本导入
- **T09** 评分器框架、内置评分器、组合器与版本化
- **T16** 成本归集、按项目/分支/通道汇总、单次与周期配额闸门

不包含 T03 的证据存储、T04 的报告页面与 T10 的多次采样。实现刻意只使用 Python 标准库（3.11+）与 SQLite，避免外部依赖，便于在任意开发机复现。自托管模型通过 OpenAI 兼容端点接入；离线验证使用确定性桩端点，不宣称真实模型质量。

## 已实现

- 数据模型：`project`、`member`、`spec`、`spec_version`、`run`、`run_item`、`verdict`、`attribution`、`evidence`、`review`、`cost_ledger`
- 可重复迁移：`001_init.sql` … `005_costs.sql`；每个文件只应用一次，重复执行校验 checksum
- 项目隔离：成员 token 绑定单一项目；跨项目列表、详情与 UI 均返回 404
- 两档权限：`member` 可写，`readonly` 只能读；服务端统一返回 403
- Spec 版本化：每次追加不可变 `spec_version`；Run 必须显式绑定版本，历史 Run 不随新版本漂移
- A/B 通道字段约束：A 通道必须记录外部仓库路径与 commit；B 通道必须记录评测集版本、评分器版本与模型/参数快照
- T02 Run 生命周期：创建、状态查询、取消、强制重跑；状态迁移写入 append-only `run_event`
- T02 调度：项目级并发上限、优先级队列、单 Run 硬超时、排队超时；队列深度与优先级顺序可查询
- T02 幂等：`project + commit_sha + spec_version + 参数摘要` 派生幂等键；重复提交返回同一 `run_id` 并标记 `reused`，`force=true` 生成新 Run 并保留新旧关联
- T02 Runner：注册、心跳、容量、失效摘除与恢复；Runner 通过 claim/complete 拉取并回写任务
- T08 评测集与样本：`dataset`、`dataset_version`、`sample`；CSV/JSONL 逐行校验导入、P0/P1 与标签分组、`context_ref`
- T08 版本化：每次样本写操作发布不可变版本；Run 固定绑定 `dataset_version`，旧 Run 显示旧样本
- T09 统一 Scorer 协议：输入样本、实际输出与上下文，输出 `pass/fail/uncertain/error`、confidence 与明细
- T09 七类内置评分器：精确匹配、正则、JSON Schema 子集、规则断言、语义相似度、LLM-as-Judge、人工复核
- T09 组合器：`and`、`or`、加权投票、多数票；逐评分器明细与聚合结论同时保存
- T09 版本化：配置或 rubric 变化追加不可变 `scorer_version`；profile 成员变化追加版本；历史 trace 可回溯
- T09 模型端点降级：连接失败/超时/HTTP 错误/响应非法时该条目 `error`，Run 继续处理其余条目
- T16 成本明细：每个模型调用、embedding、Runner 时长或手工调整写一条 `cost_entry`；`cost_ledger` 由明细汇总
- T16 成本汇总：按项目/分支/通道聚合 `tokens_in`、`tokens_out`、`tokens_total`、`gpu_seconds` 与 `est_cost`
- T16 配额闸门：可设单次 Run 与周期配额；超限后 Run 终止为 `cancelled`，原因 `quota_exceeded`，返回已消耗量与限额
- T16 缺失成本：usage 或价格缺失时存 NULL 并标记 `missing/partial` 与原因，绝不按 0 记账

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
export PYTHONPATH=src
DB=./aitest.db
TOKEN=<member token>
PRJ=<project id>

# A 通道 Spec + 版本
python3 -m aitest.cli create-spec --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --channel A --name checkout --type script
python3 -m aitest.cli add-spec-version --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --spec spc_xxx --repo-path github.com/acme/web --repo-commit <40-hex>

# B 通道 Spec + 版本（dataset_version 与 scorer_version 必须是实际存在的版本）
python3 -m aitest.cli create-spec --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --channel B --name eval --type dataset
python3 -m aitest.cli add-spec-version --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --spec spc_yyy --dataset-version dsv_xxx --scorer-version bindings \
  --model-snapshot-json '{"model":"qwen","temperature":0}'

# Run 必须显式绑定版本；同参数重复提交会复用
python3 -m aitest.cli create-run --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --spec-version-id spv_xxx --commit-sha <40-hex> --priority 80 --timeout-seconds 60

# T08 评测集与样本
python3 -m aitest.cli dataset-create --db "$DB" --token "$TOKEN" --project "$PRJ" --name eval
python3 -m aitest.cli dataset-import --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --dataset dst_xxx --file samples.jsonl
python3 -m aitest.cli dataset-export --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --dataset dst_xxx --out subset.jsonl --priority P0

# T09 评分器、组合器与 Run 评分
python3 -m aitest.cli scorer-create --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --name judge --type llm_judge --rubric-file rubric.md --config-json '{"endpoint":"http://127.0.0.1:8000/v1","model":"qwen","pass_threshold":0.7}'
python3 -m aitest.cli profile-create --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --name default --combinator and --members-json '[{"scorer_id":"scr_xxx"}]'
python3 -m aitest.cli scorer-bind --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --dataset dst_xxx --profile spr_xxx
python3 -m aitest.cli score-run --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --run run_xxx --outputs-file outputs.json

# T16 成本与配额
python3 -m aitest.cli set-cost-quota --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --tokens-in-per-1m 1.0 --tokens-out-per-1m 2.0 --gpu-second 0.01 --currency USD \
  --run-max-est-cost 0.05 --period-window day --period-max-est-cost 5.0
python3 -m aitest.cli show-run-cost --db "$DB" --token "$TOKEN" --project "$PRJ" --run run_xxx
python3 -m aitest.cli cost-summary --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --group-by project,branch,channel
python3 -m aitest.cli list-cost-entries --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --run run_xxx
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查与 schema digest |
| GET | `/whoami` | 当前成员、项目与角色 |
| GET | `/projects` | 只返回 token 所属项目 |
| GET | `/projects/{pid}` | 项目详情（含计数） |
| GET | `/projects/{pid}/members` | 项目成员（不返回 token/hash） |
| GET/POST | `/projects/{pid}/specs` | Spec 列表 / 创建（member） |
| GET | `/projects/{pid}/specs/{sid}` | Spec 详情与全部版本 |
| POST | `/projects/{pid}/specs/{sid}/versions` | 追加 Spec 版本（member） |
| GET | `/projects/{pid}/runs` | Run 列表，支持 `status`、`limit` |
| POST | `/projects/{pid}/runs` | 创建 Run；同幂等键 200 + `reused=true`，新建 201；`force=true` 重跑 |
| GET | `/projects/{pid}/runs/{rid}` | Run 详情：`spec_version`、调度、取消、`events`、`cost` |
| POST | `/projects/{pid}/runs/{rid}/cancel` | 取消排队中或运行中的 Run |
| POST | `/projects/{pid}/runs/{rid}/rerun` | 强制重跑，记录 `rerun_of_run_id` |
| GET | `/projects/{pid}/runs/{rid}/events` | append-only 状态迁移记录 |
| GET/POST | `/projects/{pid}/runs/{rid}/items` | Run 条目 |
| GET/POST | `/projects/{pid}/runs/{rid}/costs` | Run 成本明细 / 手工追加一条成本明细 |
| GET | `/projects/{pid}/runs/{rid}/cost` | Run 成本汇总、明细、缺失说明与配额状态（别名） |
| GET | `/projects/{pid}/runs/{rid}/dataset-samples` | Run 绑定的评测集版本与样本快照 |
| POST | `/projects/{pid}/runs/{rid}/score` | 用绑定评分器执行一次 B 通道评分 |
| GET | `/projects/{pid}/runs/{rid}/scorer-trace` | 每个条目的评分器 trace 与历史版本 |
| GET | `/projects/{pid}/costs/summary` | 按 `group_by=project,branch,channel` 汇总成本 |
| GET | `/projects/{pid}/costs/entries` | 成本明细，可按 `run_id/branch/channel/from/to` 过滤 |
| GET/POST | `/projects/{pid}/costs/quota` | 读取 / 设置价格、单次 Run 与周期配额（写需 member） |
| GET | `/projects/{pid}/scheduler` | 并发上限、运行中/排队数、队列深度与优先级顺序 |
| POST | `/projects/{pid}/scheduler/tick` | 执行一次维护：排队超时、硬超时、失效 Runner 摘除 |
| POST | `/projects/{pid}/scheduler/config` | 修改并发上限、失效阈值与默认超时（member） |
| GET/POST | `/projects/{pid}/runners` | Runner 列表 / 注册（token 只返回一次） |
| GET | `/projects/{pid}/runners/{rid}` | Runner 详情与最近事件 |
| GET | `/projects/{pid}/runners/{rid}/events` | Runner 注册/心跳/失效/恢复事件 |
| POST | `/projects/{pid}/runners/{rid}/revoke` | 撤销 Runner（member） |
| POST | `/runner/heartbeat` | Runner 心跳 |
| GET | `/runner/me` | 当前 Runner 与可用性 |
| POST | `/runner/claim` | 拉取一个排队 Run；无任务返回 `data: null` 与原因 |
| POST | `/runner/runs/{rid}/heartbeat` | 续租运行中的 Run |
| POST | `/runner/runs/{rid}/complete` | 回写 `succeeded/failed/timeout`、`gpu_seconds` 与结果摘要 |
| GET/POST | `/projects/{pid}/datasets` | 评测集列表 / 创建（member） |
| GET | `/projects/{pid}/datasets/{did}` | 评测集详情、版本列表与当前样本 |
| GET/POST | `/projects/{pid}/datasets/{did}/samples` | 样本列表 / 新增（member） |
| GET/PATCH/DELETE | `/projects/{pid}/datasets/{did}/samples/{sid}` | 样本详情 / 修改 / 删除；每次写操作发布新版本 |
| POST | `/projects/{pid}/datasets/{did}/import` | JSONL/CSV 导入；`mode=abort/skip` |
| GET | `/projects/{pid}/datasets/{did}/versions/{n}/export` | 按标签/优先级导出子集 |
| GET/POST | `/projects/{pid}/scorers` | 评分器列表 / 创建（member） |
| GET | `/projects/{pid}/scorers/{sid}` | 评分器详情与全部版本 |
| POST | `/projects/{pid}/scorers/{sid}/versions` | 追加配置或 rubric 版本 |
| POST | `/projects/{pid}/scorers/preview` | 离线评估一个评分器版本或内联配置 |
| GET/POST | `/projects/{pid}/scorer-profiles` | 组合器 profile 列表 / 创建 |
| POST | `/projects/{pid}/scorer-profiles/{prid}/versions` | 追加 profile 版本 |
| GET/POST | `/projects/{pid}/datasets/{did}/scorer-bindings` | 数据集默认或单样本评分器绑定 |
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

python3 -m aitest.cli set-scheduler --db "$DB" --token "$TOKEN" --project "$PRJ" \
  --max-concurrency 1 --runner-stale-seconds 30
python3 -m aitest.cli register-runner --db "$DB" --token "$TOKEN" --project "$PRJ" --name runner-a
RT=<runner token>
python3 -m aitest.cli claim-run --db "$DB" --runner-token "$RT"
python3 -m aitest.cli heartbeat-runner --db "$DB" --runner-token "$RT"
python3 -m aitest.cli complete-run --db "$DB" --runner-token "$RT" --run run_xxx \
  --status succeeded --summary ok
python3 -m aitest.cli scheduler-status --db "$DB" --token "$TOKEN" --project "$PRJ"
```

调度是拉取式且由请求驱动：每次 Runner claim、scheduler 查询或 `scheduler/tick` 都会执行一次有界维护，不需要后台进程。详见 `docs/scheduler.md`。

## 成本归集与配额闸门

配置示例：

```bash
curl -X POST "$BASE/projects/$PRJ/costs/quota" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "pricing": {"tokens_in_per_1m": 1.0, "tokens_out_per_1m": 2.0, "gpu_second": 0.01, "currency": "USD"},
    "run_quota": {"max_est_cost": 0.05, "max_tokens_total": 100000},
    "period_quota": {"window": "day", "max_est_cost": 5.0}
  }'
```

- 明细来源：评分器调用返回的 usage、Runner complete 的 `gpu_seconds`（未上报时用 Runner 墙钟时长估算并标注）、以及手工调整。
- 汇总与明细可对账：`cost_ledger` 是 `cost_entry` 的派生汇总；`/costs/summary` 的 `entryIds` 与 `detail_ref` 指向同一批明细。
- 超限终止：Run 变为 `cancelled`，`cancel.source=quota`，`cancel.reason=quota_exceeded:<scope>.<limit>: consumed ... > limit ...`；`cost.quota.termination` 保存限额与已消耗量，`run_event` 记录终止事件。
- 缺失成本：`usage` 缺失或价格未配置时，`tokens_*` / `est_cost` 为 `null`，`cost_status` 为 `missing` 或 `partial`，`missing_reason` 说明原因；汇总单独列出 `missing.entryCount` 与 `missing.reasons`。
- 已知限制：配额在模型调用返回后立即检查，无法抢占已经发出的 HTTP 请求；GPU 秒数是 Runner 墙钟估算，除非 Runner 显式上报。

## 测试与证据

```bash
export PYTHONPATH=src
python3 -m unittest discover -s tests -v
python3 scripts/verify_t01.py --out ../deliverables/evidence
python3 scripts/verify_t02.py --out ../deliverables/evidence
python3 scripts/verify_t08.py --out ../deliverables/evidence
python3 scripts/verify_t09.py --out ../deliverables/evidence
python3 scripts/verify_t16.py --out ../deliverables/evidence
```

`tests/test_t01.py` 覆盖迁移、核心表/外键、项目隔离、两档权限与 Spec 版本绑定；`test_t02.py` 覆盖幂等、取消、超时、并发与 Runner 摘除；`test_t08.py` 覆盖导入校验、版本漂移与子集导出；`test_t09.py` 覆盖七类评分器、组合器、版本回溯与端点降级；`test_t16.py` 覆盖成本对账、单次/周期配额终止与缺失成本标注。

## 设计与限制

- SQLite 作为关系型元数据存储；生产环境可替换为 PostgreSQL，但迁移与业务规则需保持同样的约束。
- 调度为项目级：并发上限、Runner 池和队列按项目隔离；平台级共享 Runner 池需要额外的管理角色。
- 调度由 API 请求与 Runner claim 驱动，没有后台守护进程；生产环境应由 CI 轮询或定时器调用 `scheduler/tick`。
- Runner 通过拉取式 claim 工作；无效 Runner 会被摘除。已撤销 Runner 的在途 Run 依赖硬超时/租约到期收敛。
- 当前 UI 是最小只读视图，管理操作通过 CLI 或 API 完成；T04 再实现完整报告视图。
- token 为开发期 bearer token，适合内网骨架验证；生产需接入短期凭证/身份提供方并加入轮换与审计。
- 项目删除会触发 `spec_version`/`dataset_version`/`scorer_version`/`scorer_profile_version` 不可删触发器而失败；不提供项目删除操作。
- T09 是单次评分；多次采样与一致性属于 T10。评分后的 Run 再次评分返回 409。
- 评分器解析优先级：请求显式 `profile_version_id`/`scorer_version_id` > Spec 版本 `scorer_version` > 单样本绑定 > 数据集默认绑定 > 按名称回退。要让数据集/样本绑定生效，Spec 的 `scorer_version` 可写 `bindings`（或 `auto`）。
- LLM-as-Judge 仅依赖 OpenAI 兼容 `POST /v1/chat/completions`，不自动重试。离线证据使用确定性桩端点验证协议与解析，不代表真实模型质量；本环境未验证真实 vLLM 质量。
- JSON Schema 评分器只实现文档列出的子集，遇到不支持的 keyword 在配置阶段显式报错。语义相似度默认使用词法 cosine，`metric=embedding` 时才调用 `/v1/embeddings`。
