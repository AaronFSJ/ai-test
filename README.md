# ai-test

T01 + T08 交付：AI 自动化测试平台骨架、数据模型、项目隔离、两档权限、Spec 版本化，以及评测集管理、CSV/JSONL 样本导入、不可变样本版本与按标签/优先级导出。

本仓库只实现 T01 与 T08 范围，不包含 T02 的调度/Runner 生命周期、T03 的证据存储和 T04 的报告页面。实现刻意只使用 Python 标准库（3.11+）与 SQLite，避免引入外部依赖，便于在任何一台开发机上复现。

## 已实现

- 数据模型：`project`、`member`、`spec`、`spec_version`、`run`、`run_item`、`verdict`、`attribution`、`evidence`、`review`、`cost_ledger`
- 可重复迁移：`001_init.sql` 只应用一次；重复执行校验 checksum，空库与已迁移库均不重复建表
- 项目隔离：成员 token 绑定单一项目；跨项目列表、详情与 UI 均返回 404，不泄露项目是否存在
- 两档权限：`member` 可写，`readonly` 只能读；服务端统一返回 403
- Spec 版本化：每次追加不可变 `spec_version`；Run 必须显式绑定版本，历史 Run 不随新版本漂移
- A/B 通道字段约束：A 通道必须记录外部仓库路径与 commit；B 通道必须记录评测集版本、评分器版本与模型/参数快照
- 评测集与样本：`dataset`、`dataset_version`、`sample`、`dataset_version_sample`；样本增删改、CSV/JSONL 逐行校验导入、标签与 P0/P1 优先级分组、`context_ref` 上下文字段引用
- 评测集版本化：每次样本变更自动发布不可变 `dataset_version`（全量快照 + 内容 digest）；Run 通过 `spec_version.dataset_version` 绑定版本，旧 Run 始终显示旧版本样本
- 子集导出：按标签与优先级筛选导出 JSONL/CSV，导出文件可重新导入且 digest 一致
- 可调用 API：JSON over HTTP；最小 HTML 管理视图按 token 只渲染所属项目，并展示评测集与最新版本
- CLI 管理入口：迁移、建项目、建成员、建 Spec/版本、建 Run、查看 Run，以及评测集/样本导入导出

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
python3 -m aitest.cli show-run-samples --db "$DB" --token "$TOKEN" --project prj_xxx --run run_xxx

# T08 评测集与样本
python3 -m aitest.cli dataset-create --db "$DB" --token "$TOKEN" --project prj_xxx --name eval
python3 -m aitest.cli dataset-import --db "$DB" --token "$TOKEN" --project prj_xxx \
  --dataset dst_xxx --file samples.jsonl --format jsonl
python3 -m aitest.cli dataset-show --db "$DB" --token "$TOKEN" --project prj_xxx --dataset dst_xxx
python3 -m aitest.cli sample-add --db "$DB" --token "$TOKEN" --project prj_xxx --dataset dst_xxx \
  --input-json '{"question":"..."}' --expected-json '"..."' --priority P0 --tag smoke
python3 -m aitest.cli dataset-export --db "$DB" --token "$TOKEN" --project prj_xxx \
  --dataset dst_xxx --tag smoke --priority P0 --out subset.jsonl
```

## HTTP API

所有 `/projects/...` 接口需要 `Authorization: Bearer <token>`；token 由 `add-member` 生成。路径中的项目必须与 token 所属项目一致，否则返回 404。样本路径中的 `{sid}` 是数据库 `id` 字段；导入/导出与响应中的 `sample_id` 是数据集内稳定的逻辑键。

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
| GET | `/projects/{pid}/runs/{rid}` | Run 详情，含嵌套 `spec_version` 快照；B 通道含绑定样本快照 |
| GET/POST | `/projects/{pid}/runs/{rid}/items` | Run 条目 |
| GET | `/projects/{pid}/runs/{rid}/dataset-samples` | Run 绑定的评测集版本与样本快照 |
| GET/POST | `/projects/{pid}/datasets` | 评测集列表 / 创建（member） |
| GET | `/projects/{pid}/datasets/{did}` | 评测集详情、版本列表与当前样本 |
| GET/POST | `/projects/{pid}/datasets/{did}/samples` | 样本列表（可按 `tag`/`priority` 过滤）/ 新增样本（member） |
| GET/PATCH/DELETE | `/projects/{pid}/datasets/{did}/samples/{sid}` | 样本详情 / 修改 / 删除（member）；每次写操作发布新版本 |
| GET | `/projects/{pid}/datasets/{did}/stats` | 按优先级与标签分组的计数 |
| GET | `/projects/{pid}/datasets/{did}/versions` | 不可变版本列表 |
| GET | `/projects/{pid}/datasets/{did}/versions/{n}` | 指定版本及全量样本快照 |
| POST | `/projects/{pid}/datasets/{did}/import` | JSONL/CSV 导入；`mode=abort` 时原子失败，`mode=skip` 跳过非法行 |
| GET | `/projects/{pid}/datasets/{did}/versions/{n}/export` | 按 `tag`、`priority`、`all_tags` 导出 JSONL/CSV |
| GET | `/projects/{pid}/datasets/{did}/export` | 从最新版本导出 |
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
python3 scripts/verify_t08.py --out ../deliverables/evidence
```

`tests/test_t01.py` 覆盖迁移重复执行、核心表/外键、项目隔离（API + UI）、两档权限、Spec 双版本与 Run 绑定、A/B 通道字段差异。`tests/test_t08.py` 覆盖 100 条 JSONL 导入、非法行定位、样本修改后的版本变化与旧 Run 快照、子集导出重导入一致性、CSV 往返、权限与项目隔离。`scripts/verify_t08.py` 在真实 HTTP 服务上生成 `07-dataset-schema.json`、`08-dataset-import.json`、`09-sample-versioning.json`、`10-subset-roundtrip.json`、`11-dataset-permissions.json` 与 `t08-summary.json`。

## 设计与限制

- SQLite 作为 T01/T08 的关系型元数据存储；生产环境可替换为 PostgreSQL，但迁移与业务规则需保持同样的约束。
- 当前没有 T02 的调度、Runner 心跳、取消/超时；Run 初始状态为 `queued`，生命周期由后续任务补齐。
- 当前 UI 是最小只读视图，评测集写入、导入与导出通过 CLI 或 API 完成；T04 再实现完整报告视图。
- 样本每次写操作都会发布新版本，即使只改一个字段；这保证历史 Run 不漂移，但版本数量会随编辑增加。`mode=abort` 导入是全有或全无，`mode=skip` 会导入合法行并返回非法行错误。
- CSV 单元格中的 `input/expected/context` 优先按 JSON 解析，解析失败时按普通字符串保存；`tags` 支持 JSON 数组或 `,`/`;`/`|` 分隔。
- 单次导入最多 10000 行、单行最多 64 KiB；当前验收在 100 条量级上验证，更大规模需补充性能测试。
- token 为开发期 bearer token，适合内网骨架验证；生产需接入短期凭证/身份提供方并加入轮换与审计。
- 项目删除会触发 `spec_version`/`dataset_version` 不可删触发器而失败；不提供项目删除操作，避免误删历史结论。
