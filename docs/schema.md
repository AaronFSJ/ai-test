# T01 数据模型与权限约定

迁移文件：`src/aitest/migrations/001_init.sql`（T01 核心模型）与 `002_evidence_report.sql`（T03 证据/保留期/报告快照）。迁移记录表为 `schema_migrations`，不属于业务实体。

## 核心实体

| 表 | 作用 | 关键字段 | 外键 / 约束 |
|---|---|---|---|
| `project` | 项目空间 | `id`, `name`, `default_branch`, `quota_config` | `name` 唯一；`quota_config` 必须是合法 JSON |
| `member` | 项目成员与 token | `id`, `project_id`, `name`, `role`, `token_hash`, `token_prefix` | `project_id -> project(id) ON DELETE CASCADE`；`(project_id, name)` 唯一；`token_hash` 唯一；`role in (member, readonly)` |
| `spec` | 被测资产逻辑对象 | `id`, `project_id`, `channel`, `type`, `name` | `project_id -> project`；`(project_id, name)` 唯一；A 只允许 `script`，B 只允许 `dataset` |
| `spec_version` | 不可变版本快照 | `id`, `spec_id`, `project_id`, `channel`, `version`, 通道字段, `created_by` | `(spec_id, channel) -> spec(id, channel)`；`(spec_id, project_id) -> spec`；`(created_by, project_id) -> member`；`(spec_id, version)` 唯一；版本号连续 |
| `run` | 一次执行实例 | `id`, `project_id`, `spec_version_id`, `channel`, `commit_sha`, `branch`, `status`, `baseline_run_id` | 复合外键绑定 `spec_version` 的 id/project/channel；不能跨项目或跨通道绑定；`baseline_run_id` 同项目 |
| `run_item` | Run 内的条目 | `id`, `run_id`, `case_ref`, `priority`, `state`, `confidence`, `duration_ms` | `run_id -> run ON DELETE CASCADE`；`(run_id, case_ref)` 唯一；四态枚举；`priority in (P0, P1)` |
| `verdict` | 条目判定明细 | `id`, `run_item_id`, `state`, `confidence`, `scorer_trace_json` | `run_item_id -> run_item ON DELETE CASCADE` 且唯一；四态枚举 |
| `attribution` | 失败归因 | `id`, `run_item_id`, `label`, `sub_label`, `evidence_refs_json` | `run_item_id -> run_item`；归因标签为封闭枚举 |
| `evidence` | 证据元数据与脱敏结果 | `id`, `run_item_id`, `project_id`, `run_id`, `kind`, `filename`, `uri`, `storage_key`, `content_sha256`, `content_type`, `bytes`, `bytes_original`, `redacted`, `redaction_json`, `retention_class`, `retention_expires_at`, `deleted_at` | `run_item_id -> run_item`；`kind` 为 `log/dom/screenshot/request/model_io`；`storage_key` 唯一；内容 SHA-256 与对象一致 |
| `review` | 人工复核 | `id`, `run_item_id`, `reviewer`, `conclusion`, `note` | `run_item_id -> run_item`；结论为 `confirmed_defect/false_positive/sample_error` |
| `cost_ledger` | 成本归集 | `id`, `run_id`, `channel`, `tokens_in`, `tokens_out`, `gpu_seconds`, `est_cost` | `(run_id, channel) -> run(id, channel)`；每个 Run 一行 |
| `retention_policy` | 按项目/优先级配置保留天数 | `project_id`, `case_priority`, `retain_days`, `updated_by`, `updated_at` | 主键 `(project_id, case_priority)`；`retain_days` 0–3650；未配置时使用默认 P0=90 / P1=30 |
| `evidence_cleanup_log` | 证据清理审计 | `id`, `project_id`, `evidence_id`, `run_id`, `storage_key`, `retention_expires_at`, `deleted_at`, `reason`, `actor`, `bytes` | 每个被清理对象一行；`reason='expired'` |
| `report_snapshot` | 只读报告快照 | `id`, `project_id`, `run_id`, `spec_version_id`, `report_version`, `content_json`, `content_sha256`, `storage_key`, `created_by`, `created_at` | `(run_id, report_version)` 唯一；`content_sha256` 为 64 位十六进制；UPDATE/DELETE 由触发器拒绝 |

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


## T03 证据、保留期与报告快照

### 对象分区与元数据

- 对象键由服务端生成，格式为 `projects/{project_id}/runs/{run_id}/{evidence_id}-{safe_filename}`；`evidence.uri` 暴露为 `object://<key>`，不写入本机绝对路径。
- 对象内容在写入前完成脱敏，`content_sha256`、`bytes` 与对象实际字节一致；`bytes_original` 记录脱敏前长度。
- `redaction_json` 记录规则版本、匹配计数与是否需人工复核；二进制证据（截图）原样保存并标记 `manualReviewRequired=true`。
- 检索维度：`project_id`、`run_id`、`run_item_id`、`kind`、`filename`、`deleted_at`。
- `evidence_context_guard` 触发器拒绝跨项目/跨 Run 的条目引用与跨项目作者；`report_snapshot_context_guard` 拒绝快照绑定非该 Run 的 Spec 版本。

### 脱敏规则

规则清单由 `redaction.rules_inventory()` 提供，API 为 `GET /projects/{pid}/redaction-rules`，CLI 为 `redaction-rules`。默认包含：

| 规则 | 类别 | 行为 |
|---|---|---|
| `private_key_block` | credential | PEM/OpenSSH 私钥块整体替换 |
| `jwt` / `bearer_token` / `basic_auth` | credential | JWT、Bearer/Basic 凭证替换 |
| `aws_access_key` / `github_token` | credential | 云与代码托管平台 token |
| `authorization_header` | credential | Authorization / Cookie 头值整体替换 |
| `secret_key_value` | credential | `password`、`token`、`api_key`、`session` 等键值 |
| `url_query_secret` | credential | 签名 URL / SAS / AWS 凭证参数值替换，保留参数名 |
| `url_userinfo` / `url_fragment` | credential | URL 中的用户名密码与 fragment 去除 |
| `json_sensitive_key` | credential | JSON 对象的敏感键值替换 |
| `phone_cn` / `phone_e164` | personal_data | 手机号（含 JSON 数字） |
| `email` / `id_card_cn` | personal_data | 邮箱与 18 位身份证号 |

JSON 证据（`application/json`）按结构递归脱敏后重新序列化，避免数字型个人信息破坏 JSON。文本证据按规则顺序处理。

### 保留期与清理

- 入库时按条目的 `priority` 读取当前策略，固化 `retention_expires_at`；后续修改策略不追溯已入库证据。
- 清理通过 `POST /projects/{pid}/evidence/cleanup` 或 `cleanup-evidence` 显式触发，支持 `dry_run` 与 ISO-8601 `cutoff`。
- 清理只删除对象字节，`evidence` 元数据保留为 tombstone（`deleted_at`），并逐条写入 `evidence_cleanup_log`；重复清理对已删除对象不再计数。
- 读取已清理证据返回 `410 evidence_expired`。

### 报告快照

- `report_snapshot` 由 `POST /projects/{pid}/runs/{rid}/reports` 生成；内容包含 Run、绑定的 `spec_version`、条目/判定/证据元数据、配置与保留期快照。
- 内容使用规范化 JSON（排序键、紧凑分隔符），`content_sha256` 为内容文本的 SHA-256；读取时重新校验 digest。
- 快照只引用生成时 Run 绑定的不可变 `spec_version`，不读取“当前最新”评分器/模型配置；因此追加 Spec 版本、修改保留期或清理过期证据都不会改变历史快照。
- 数据库触发器 `report_snapshot_no_update` / `report_snapshot_no_delete` 拒绝原地修改和删除。
- `retention_policy.updated_by`、`report_snapshot.created_by` 与 `evidence_cleanup_log.actor` 使用 `(member_id, project_id)` 复合外键，避免跨项目作者引用。
