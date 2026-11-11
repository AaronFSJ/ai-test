# 数据模型与权限约定

T01 迁移文件：`src/aitest/migrations/001_init.sql`。T02 的调度、Runner 与状态迁移表见 `003_scheduler.sql`（T08 评测集为 `002_datasets.sql`，T09 评分器为 `004_scorers.sql`，T10 采样为 `005_sampling.sql`） 与 `docs/scheduler.md`。迁移记录表为 `schema_migrations`，不属于业务实体。

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

# T08 评测集与样本版本

迁移文件：`src/aitest/migrations/002_datasets.sql`。

## T08 实体

| 表 | 作用 | 关键字段 | 外键 / 约束 |
|---|---|---|---|
| `dataset` | 评测集逻辑对象 | `id`, `project_id`, `name`, `description` | `project_id -> project`；`(project_id, name)` 唯一；`(id, project_id)` 唯一 |
| `dataset_version` | 不可变版本快照 | `id`, `dataset_id`, `project_id`, `version`, `sample_count`, `content_digest`, `source`, `import_json`, `created_by` | `(dataset_id, project_id) -> dataset`；`(dataset_id, version)` 唯一；版本号连续；创建者属于同项目 |
| `sample` | 可编辑工作集 | `id`, `dataset_id`, `project_id`, `sample_key`, `input_json`, `expected_json`, `context_json`, `context_ref`, `priority`, `tags_json` | `(dataset_id, project_id) -> dataset`；`(dataset_id, sample_key)` 唯一；`priority in (P0,P1)`；`tags_json` 必须是 JSON 数组 |
| `dataset_version_sample` | 版本内的样本快照 | `id`, `dataset_version_id`, `sample_key`, 同样的样本字段 | `dataset_version_id -> dataset_version`；`(dataset_version_id, sample_key)` 唯一 |

## 版本语义

- 每次样本新增、修改或删除都会在同一个事务内发布新的 `dataset_version`，并对当前全部样本写入 `dataset_version_sample` 快照。
- `content_digest` 是对按 `sample_key` 排序后的样本内容做规范化 JSON 编码再取 SHA-256；不包含时间戳或数据库 id，因此同一内容重复导入得到相同 digest。
- `dataset_version` 与 `dataset_version_sample` 都有禁止 UPDATE 的触发器；`dataset_version` 禁止 DELETE。历史 Run 通过 `spec_version.dataset_version`（`dsv_...` id）读取快照，不读可变的 `sample` 表。
- `spec_version.dataset_version` 既兼容 T01 的自由文本标签，也接受 `dsv_...` id；传入 id 时服务端校验该版本存在且属于当前项目，否则返回 404。
- `Run` 详情在 B 通道下附加 `dataset_version_detail` 与 `dataset_samples`；`GET /runs/{rid}/dataset-samples` 单独返回同一快照。
- 样本 HTTP 路径使用内部 `sample.id`；`sample_key`（API/导入导出中的 `sample_id`）是数据集内稳定逻辑键，更新时不可修改。

## 导入与导出

- JSONL：每行一个对象；允许字段 `sample_id`、`input`、`expected`、`context`、`context_ref`、`priority`、`tags`。`input` 必填且不能为 JSON null；`priority` 默认 `P1`。
- CSV：表头必须包含 `input`；支持的列为 `sample_id,input,expected,context,context_ref,priority,tags`。`input/expected/context` 优先按 JSON 解析，失败时按普通字符串；`tags` 支持 JSON 数组或 `,`/`;`/`|` 分隔。
- 逐行校验错误包含 `line` 与 `field`（能定位时）。`mode=abort`（默认）在任何非法行存在时不写入；`mode=skip` 写入合法行并返回非法行错误。
- 导出按 `sample_key` 排序；`tag` 可重复，默认任一所选标签命中，`all_tags=true` 时要求全部命中；`priority` 可重复或逗号分隔。导出响应头包含 `X-Sample-Digest`、`X-Sample-Count`、`X-Dataset-Version-Id`，可用于核对重导入结果。
- 导出文件重新导入后，`dataset_version.content_digest` 与导出头一致；JSONL 重新导出字节一致。

## 权限

- `member` 可创建评测集、增删改样本、导入；`readonly` 这些写操作返回 403。
- 跨项目访问数据集、样本、导入与导出均返回 404，不泄露存在性。

# T09 评分器框架与组合器

迁移文件：`src/aitest/migrations/004_scorers.sql`。

## T09 实体

| 表 | 作用 | 关键字段 | 外键 / 约束 |
|---|---|---|---|
| `scorer` | 项目内的评分器逻辑名 | `id`, `project_id`, `name`, `description` | `project_id -> project`；`(project_id, name)` 唯一；`(id, project_id)` 唯一 |
| `scorer_version` | 不可变配置/rubric 版本 | `id`, `scorer_id`, `project_id`, `version`, `type`, `config_json`, `rubric_text`, `rubric_digest`, `frozen_digest`, `created_by` | `(scorer_id, project_id) -> scorer`；`(scorer_id, version)` 唯一；版本号连续；`type` 枚举；`llm_judge` 必须有 rubric |
| `scorer_profile` | 评分器组合逻辑名 | `id`, `project_id`, `name`, `description` | `project_id -> project`；`(project_id, name)` 唯一 |
| `scorer_profile_version` | 不可变组合器版本 | `id`, `profile_id`, `project_id`, `version`, `combinator`, `config_json`, `members_json`, `frozen_digest`, `created_by` | `(profile_id, project_id) -> scorer_profile`；`(profile_id, version)` 唯一；`members_json` 至少 1 项 |
| `scorer_binding` | 数据集默认/单样本绑定 | `id`, `project_id`, `dataset_id`, `sample_key`（NULL=默认）, `profile_id`, `profile_version_id`, `created_at`, `updated_at` | `(dataset_id, sample_key)` 唯一；`sample_key` 为 NULL 时按 dataset 唯一；绑定到同项目 dataset/profile/sample |

`scorer_version` 与 `scorer_profile_version` 均有禁止 UPDATE/DELETE 的触发器；版本号由触发器强制连续。`frozen_digest` 是对规范化后的 type + config + rubric 做 SHA-256；`rubric_digest` 是 rubric 文本的 SHA-256。配置、rubric 或 profile 成员变化都会追加新版本，旧版本仍可按 id 读取。

## 统一 Scorer 协议

输入：样本（`sample_id/input/expected/context/context_ref/priority/tags`）、实际输出（任意 JSON）与执行上下文（数据集 context、Run 快照、请求 context）。输出：

```json
{"state": "pass|fail|uncertain|error", "confidence": 0.0, "score": 0.0, "detail": {}}
```

`score` 由 state 派生（pass=1.0、fail=0.0、uncertain=0.5、error=null），仅用于组合器。`error` 表示评分执行本身失败，不进入质量分母。

## 内置评分器

| type | 关键配置 | 语义 |
|---|---|---|
| `exact_match` | `case_sensitive`, `trim`, `normalize_whitespace`, `expected_path`, `actual_path`, `numeric_tolerance` | 字符串规范化后比较；数字可用容差；其他 JSON 按规范化 JSON 比较 |
| `regex` | `pattern`, `flags`(ims), `target`(actual/expected), `mode`(search/fullmatch), `path`, `negate` | 正则匹配；无效正则在配置阶段报错 |
| `json_schema` | `schema`, `parse_string`, `actual_path` | 严格 JSON Schema 子集；未知 keyword 在配置阶段报错；实际输出非法 JSON 记 fail |
| `rule_assertion` | `assertions[]`(path/op/value/source/message), `require_all`, `on_missing`, `on_eval_error` | 支持 exists/eq/ne/gt/gte/lt/lte/contains/matches/in/type_is/length_* 等；路径缺失默认 uncertain |
| `semantic_similarity` | `threshold`, `metric`(lexical_cosine/jaccard/embedding), `expected_path`, `actual_path`, `embedding` | 词法 cosine/Jaccard 可离线运行；embedding 调用 OpenAI 兼容 `/v1/embeddings` |
| `llm_judge` | `endpoint`, `model`, `pass_threshold`, `temperature`, `max_tokens`, `timeout_s`, `api_key_env`, `system_prompt`, `response_format`, `max_input_chars` + 必填 rubric | 调用 OpenAI 兼容 `/v1/chat/completions`，要求返回 `score`/`verdict`/`reason` JSON |
| `human_review` | `queue`, `allow_decision`, `decision_path`, `default_state`（只能 uncertain） | 无人工结论时返回 uncertain；可读取 `confirmed_defect/false_positive/sample_error` 映射 |

JSON Schema 子集支持：`type`, `enum`, `const`, `properties`, `required`, `additionalProperties`, `min/maxProperties`, `items`, `min/maxItems`, `uniqueItems`, `min/maxLength`, `pattern`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `allOf`, `anyOf`, `oneOf`, `not`，以及 `$schema/$id/title/description/default/examples` 元数据。其他 keyword 直接拒绝。

## 组合器

`scorer_profile_version.combinator` 取 `and`、`or`、`weighted`、`majority`，`config.on_error` 默认 `error`（任一成员 error 则整体 error）；显式设为 `ignore` 才从其余成员裁决。

| combinator | 语义 |
|---|---|
| `and` | 有 fail 则 fail；否则有 uncertain 则 uncertain；否则 pass |
| `or` | 有 pass 则 pass；否则有 uncertain 则 uncertain；否则 fail |
| `weighted` | `sum(weight * state_score) / sum(weight)`，默认 uncertain=0.5；`score >= threshold` 为 pass，否则 fail |
| `majority` | pass/fail 计票，uncertain 弃权；默认法定票数为 `valid//2+1`，可配置 `quorum`；一方达到法定票数且严格多于另一方才裁决，否则 uncertain |

组合结果把每个成员的 `scorer_id/version_id/state/confidence/score/weight/detail/error/duration_ms` 与聚合 `aggregate`（计数、加权分、票数、阈值）一起写入 verdict。

## 解析优先级与 Run 评分

- 优先级：请求显式 `profile_version_id`/`scorer_version_id` > Spec 版本 `scorer_version` > 单样本绑定 > 数据集默认绑定 > 按评分器/profile 名称回退。
- Spec 的 `scorer_version` 可写 `scpv_...`（profile 版本）、`scv_...`（单个评分器版本）、`spr_...`/`scr_...`（取最新版本）或 `bindings`/`auto`（强制走数据集/样本绑定）。
- `POST /runs/{rid}/score` 只接受 B 通道且已绑定 `dsv_...` 的 Run；请求体 `actual_outputs` 为 `{sample_id: actual}` 或数组。每个样本写一条 `run_item` + `verdict`，`case_ref = sample_id`。
- 未提供某样本 actual、评分器配置缺失、模型端点失败或评分器内部异常时，该条目记为 `error` 并写原因；Run 继续处理其余样本，结束后状态为 `succeeded`。已存在条目的 Run 再次评分返回 409。
- Run 详情与 `GET /runs/{rid}/scorer-trace` 可读出每个条目的历史 trace；trace 包含当次解析到的 `scorer_version_id`、`profile_version_id`、`frozen_digest`、`rubric_digest`，并返回这些历史版本行与每个 scorer 的当前最新版本，用于回溯。

## 权限

- `member` 可创建评分器/版本/profile/绑定并执行评分；`readonly` 写操作返回 403。
- 跨项目访问评分器、profile、绑定、评分与 trace 均返回 404。

## T10 采样增量

迁移文件：`src/aitest/migrations/005_sampling.sql`。

| 表/列 | 作用 | 关键字段 |
|---|---|---|
| `run_item_sample` | 不可变的逐次评分结果 | `run_item_id`, `run_id`, `sample_index`, `state`, `confidence`, `score`, `duration_ms`, `actual_digest`, `scorer_trace_json`, `cost_json` |
| `run.sampling_effective_json` | 评分时实际生效的采样配置与模型快照 | `config`, `batch`, `model_snapshot`, `cost`, `captured_at` |
| `verdict.sample_count` | 条目聚合使用的重复次数 | 默认 1，`>= 1` |
| `verdict.aggregation_json` | 聚合策略、计数、比例、均值、方差、规则与原因 | `strategy`, `counts`, `mean_score`, `variance`, `state`, `reason`, `rule` |

`run.sampling_config_json`（T01/T02 已有）保存创建 Run 时的原始采样配置；两者同时保留，
避免评分时的默认值覆盖创建时的契约快照。`run_item_sample` 有 `BEFORE UPDATE` 触发器，
重复结果不可改写。
