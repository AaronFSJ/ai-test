# T02 调度、Runner 与 Run 生命周期

迁移文件：`src/aitest/migrations/002_scheduler.sql`。T01 的 `001_init.sql` 保持不变。

## 设计边界

- **项目级调度**：并发上限、队列、Runner 池和超时配置都按项目隔离。T01 只有 `member` / `readonly` 两档权限；引入全局 Runner 池会让任一项目成员影响其他项目，因此本实现把 Runner 绑定到项目。平台级共享 Runner 池需要额外的管理角色，留待后续。
- **拉取式、请求驱动**：Runner 调用 `POST /runner/claim` 领取任务。没有后台守护进程；每次 claim、scheduler 查询或 `scheduler/tick` 都会执行一次有界维护。生产环境可由 CI 轮询或定时器定期调用 tick。
- **状态迁移可审计**：所有 Run 状态变化写入 append-only `run_event`，字段为 `from_status`、`to_status`、`source`、`actor`、`reason`、`detail_json`、`seq`。报告、门禁与成本模块按 `run_id` 读取。

## 状态机

```
(none) --create--> queued
queued --claim--> running
queued --cancel--> cancelled
queued --queue_timeout--> timeout
running --complete(succeeded|failed)--> succeeded|failed
running --cancel--> cancelled
running --hard_timeout--> timeout
running --runner_lease_expired--> timeout
```

终态 `succeeded / failed / cancelled / timeout` 不再迁移。重复取消返回当前记录并标记 `already_cancelled`；对终态 Run 执行取消或回写返回 409。

## 幂等键

```
params_digest = sha256(canonical_json({
  env, sampling_config, model_snapshot, baseline_run_id
}))
idempotency_key = sha256(canonical_json({
  project_id, commit_sha, spec_version_id, params_digest
}))
```

- 相同键的重复提交返回已有 Run，HTTP 200，`reused=true`，`reuse.reason=idempotency_key_match`；不会新增执行记录。
- `force=true` 创建新 Run：旧 Run 写入 `superseded_by_run_id`，新 Run 写入 `rerun_of_run_id`。若旧 Run 仍在排队，会被置为 `cancelled`（`cancel_source=system`，原因 `superseded_by_force_rerun`），确保不会执行重复工作。
- `POST /runs/{rid}/rerun` 总是强制重跑，并显式把新 Run 的 `rerun_of_run_id` 指向被重跑的 Run。
- 优先级、超时和 `queue_timeout` 属于调度控制，不进入幂等键；需要以新优先级重跑时使用 `force=true` 或 rerun 接口。

唯一性由部分唯一索引保证：

```sql
CREATE UNIQUE INDEX idx_run_active_idempotency
ON run(project_id, idempotency_key)
WHERE idempotency_key IS NOT NULL AND superseded_by_run_id IS NULL;
```

## 队列、并发与优先级

- 可调度状态只有 `queued`。排序为 `priority DESC, created_at ASC, rowid ASC`。
- 项目级 `max_concurrency` 限制同时 `running` 的 Run 数；Runner 的 `capacity` 限制单个 Runner 同时持有的 Run 数。
- `GET /projects/{pid}/scheduler` 返回：
  - `max_concurrency`、`running_count`、`queued_count`、`queue_depth`、`available_capacity`
  - `priority_order`（含 `position`）
  - `running`（含 `deadline_at`、`lease_expires_at`、`runner_id`）
  - `runners.available / unavailable / items`
- 超过并发上限的提交保持 `queued`，不会静默丢弃；队列深度可随时读取。

## 硬超时与排队超时

- `timeout_seconds`（默认 300，最小 1）在 claim 时生成 `deadline_at`。维护时 `deadline_at <= now` 的 Run 迁移到 `timeout`，事件 `reason=hard_timeout`。
- `queue_timeout_seconds`（默认 600，最小 1）在创建时生成 `queue_deadline_at`。长期排队的 Run 迁移到 `timeout`，事件 `reason=queue_timeout`。
- Runner 租约 `lease_expires_at` 在 claim 时生成，Runner 可通过 `POST /runner/runs/{rid}/heartbeat` 续租。租约到期且未到硬超时的 Run 迁移到 `timeout`，事件 `reason=runner_lease_expired`，避免 Runner 崩溃导致长期挂起。
- 所有超时都是明确终态，带 `finished_at` 与事件记录，不会保持 `running`。

## Runner 注册、心跳与摘除

`runner` 表保存 `status online|offline|unavailable|draining`、`last_heartbeat_at`、`capacity`、`labels_json`。token 只返回一次，数据库只存 SHA-256。

- 注册即写入一次心跳，Runner 立即可用。
- `POST /runner/heartbeat` 更新 `last_heartbeat_at`；`online` / `draining` 可切换。
- 维护时 `heartbeat_age > runner_stale_seconds`（默认 45，最小 1）的 `online` Runner 被置为 `unavailable`，并写入 `runner_event(kind=went_unavailable)`。
- 不可用 Runner 不会被 scheduler 选中；新的 Run 只分配给可用 Runner。Runner 再次心跳或 claim 时恢复 `online`，写入 `recovered` 事件。
- `revoke` 将 Runner 置为 `offline` 并记录 `revoked`；在途 Run 不被强杀，由硬超时/租约到期收敛。

## 数据表增量

| 表 | 作用 | 关键字段 |
|---|---|---|
| `runner` | 项目级 Runner | `project_id`, `name`, `token_hash`, `capacity`, `status`, `last_heartbeat_at` |
| `run_event` | Run 状态迁移账本 | `run_id`, `seq`, `from_status`, `to_status`, `source`, `actor`, `reason`, `detail_json` |
| `runner_event` | Runner 生命周期事件 | `runner_id`, `kind`, `detail_json`, `created_at` |
| `run` 新列 | 调度与生命周期 | `priority`, `idempotency_key`, `params_digest`, `timeout_seconds`, `queue_timeout_seconds`, `queue_deadline_at`, `deadline_at`, `lease_expires_at`, `assigned_runner_id`, `rerun_of_run_id`, `superseded_by_run_id`, `cancel_source/reason/by/at`, `result_json`, `updated_at` |
| `project` 新列 | 调度配置 | `scheduler_config_json` |

## 可复现验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_t02.py' -v
PYTHONPATH=src python3 scripts/verify_t02.py --out ../deliverables/evidence
```

`tests/test_t02.py` 与 `scripts/verify_t02.py` 覆盖：

- 相同幂等键两次提交只产生一个 Run，第二次返回同一 `run_id` 且 `reused=true`
- `force=true` 产生新 Run，旧 Run 标记 `superseded_by_run_id` 并可区分
- 取消排队 Run 后不会被 claim；取消来源、人、原因与事件可查
- 并发上限下排队深度可观测，优先级顺序按预期；硬超时/排队超时返回明确 `timeout`
- Runner 停止心跳后被标记不可用，新 Run 调度到存活 Runner

## 限制

- SQLite 单机实现；多进程/多主机部署需要 PostgreSQL 行锁或专用队列。
- 调度维护由请求触发，不是精确的定时器；无人调用 API 时超时不会立即发生。调用 `scheduler/tick` 可强制收敛。
- Runner 执行器本身不在本任务范围；Runner 只需 claim、续租、complete。
- 取消运行中的 Run 是平台侧状态终止；Runner 侧应自行停止执行并回写，回写会被 409 拒绝。
