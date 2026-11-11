# 数据模型与权限约定

T01 迁移文件：`src/aitest/migrations/001_init.sql`。T02 的调度、Runner 与状态迁移表见 `002_scheduler.sql` 与 `docs/scheduler.md`。迁移记录表为 `schema_migrations`，不属于业务实体。

## 核心实体

| 表 | 作用 | 关键字段 | 外键 / 约束 |
|---|---|---|---|
| `project` | 项目空间 | `id`, `name`, `default_branch`, `quota_config` | `name` 唯一；`quota_config` 必须是合法 JSON |
| `member` | 项目成员与 token | `id`, `project_id`, `name`, `role`, `token_hash`, `token_prefix` | `project_id -> project(id) ON DELETE CASCADE`；`(project_id, name)` 唯一；`token_hash` 唯一；`role in (member, readonly)` |
| `spec` | 被测资产逻辑对象 | `id`, `project_id`, `channel`, `type`, `name` | `project_id -> project`；`(project_id, name)` 唯一；A 只允许 `script`，B 只允许 `dataset` |
| `spec_version` | 不可变版本快照 | `id`, `spec_id`, `project_id`, `channel`, `version`, 通道字段, `created_by` | `(spec_id, channel) -> spec(id, channel)`；`(spec_id, project_id) -> spec`；`(created_by, project_id) -> member`；`(spec_id, version)` 唯一；版本号连续 |
| `run` | 一次执行实例 | `id`, `project_id`, `spec_version_id`, `channel`, `commit_sha`, `branch`, `status`, `baseline_run_id` | 复合外键绑定 `spec_version` 的 id/project/channel；不能跨项目或跨通道绑定；`baseline_run_id` 同项目 |
| `run_item` | Run 内的条目 | `id`, `run_id`, `case_ref`, `state`, `confidence`, `duration_ms` | `run_id -> run ON DELETE CASCADE`；`(run_id, case_ref)` 唯一；四态枚举 |
| `verdict` | 条目判定明细 | `id`, `run_item_id`, `state`, `confidence`, `scorer_trace_json` | `run_item_id -> run_item ON DELETE CASCADE` 且唯一；四态枚举 |
| `attribution` | 失败归因 | `id`, `run_item_id`, `label`, `sub_label`, `evidence_refs_json` | `run_item_id -> run_item`；归因标签为封闭枚举 |
| `evidence` | 证据元数据 | `id`, `run_item_id`, `kind`, `uri`, `bytes` | `run_item_id -> run_item`；`kind` 为 `log/dom/screenshot/request/model_io` |
| `review` | 人工复核 | `id`, `run_item_id`, `reviewer`, `conclusion`, `note` | `run_item_id -> run_item`；结论为 `confirmed_defect/false_positive/sample_error` |
| `cost_ledger` | 成本归集 | `id`, `run_id`, `channel`, `tokens_in`, `tokens_out`, `gpu_seconds`, `est_cost` | `(run_id, channel) -> run(id, channel)`；每个 Run 一行 |

## Run 与 Spec 版本的绑定

`run.spec_version_id` 非空，并且通过以下复合外键同时约束项目与通道：

```sql
FOREIGN KEY (spec_version_id, project_id) REFERENCES spec_version(id, project_id),
FOREIGN KEY (spec_version_id, channel) REFERENCES spec_version(id, channel)
```

因此：

- 不能创建没有 `spec_version` 的 Run（API 也要求显式传 `spec_version_id` 或 `spec_id` + `version`）。
- 不能把 Run 绑定到其他项目的版本。
- 不能把 A 通道 Run 绑定到 B 通道版本（反之亦然）。
- `get_run` 返回嵌套 `spec_version` 快照；`list_runs` 至少返回 `spec_version` 版本号与 `spec_version_id`。

`spec_version` 上有触发器：

- `spec_version_no_update`：禁止原地修改，必须追加新版本。
- `spec_version_no_delete`：禁止删除历史版本。
- `spec_version_contiguous`：版本号必须为 `MAX(version)+1`。

## A / B 通道字段差异

| 字段 | A 通道（`script`） | B 通道（`dataset`） |
|---|---|---|
| `repo_path` | 必填，外部仓库路径 | 必须为 NULL |
| `repo_commit` | 必填，外部 commit | 必须为 NULL |
| `dataset_version` | 必须为 NULL | 必填，评测集版本 |
| `scorer_version` | 必须为 NULL | 必填，评分器配置版本 |
| `model_snapshot_json` | 必须为 NULL | 必填，模型/参数快照 JSON |
| `params_json` | 可选 | 可选 |

该差异同时由数据库 `CHECK` 与 `service.add_spec_version` 校验。A 通道传 B 字段、B 通道传 A 字段都会返回 400，不会静默丢弃。

## 权限矩阵

| 操作 | `member` | `readonly` | 未认证 |
|---|---|---|---|
| 查看自己项目的 Spec / Run / 成员 | 允许 | 允许 | 401 |
| 查看其他项目 | 404 | 404 | 401 |
| 创建 Spec / 追加版本 / 创建 Run / 添加条目 | 允许 | 403 | 401 |
| 创建项目、创建成员 | 本机 CLI 管理操作 | 本机 CLI 管理操作 | 不通过 HTTP 暴露 |

成员 token 只在创建时返回一次，数据库保存 `sha256(token)` 与用于展示的前缀。API 通过 `Authorization: Bearer <token>` 认证。

## 迁移可重复性

`migrations.migrate(db_path)` 的行为：

1. 创建 `schema_migrations`（若不存在）。
2. 按文件名顺序读取 `NNN_*.sql`。
3. 已应用且 checksum 一致：跳过。
4. 已应用但 checksum 改变：报错，不静默覆盖。
5. 未应用：在 `BEGIN IMMEDIATE` 中执行 SQL 并写入迁移记录。

在空库上执行两次，第二次 `appliedNow` 为空且 `schemaDigest` 不变；同一 SQL 在两个独立空库上得到相同 digest。证据见 `deliverables/evidence/01-migrations.json` 与 `schema.sql`。
