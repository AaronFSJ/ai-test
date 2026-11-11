# T16 成本归集与配额闸门

迁移文件：`src/aitest/migrations/005_costs.sql`。成本模块：`src/aitest/costs.py`。

## 数据来源与明细

每条成本明细写入 `cost_entry`：

| 来源 | 触发点 | 记录字段 |
|---|---|---|
| 模型调用 | `llm_judge` 评分器返回的 `usage` | `tokens_in`（prompt_tokens）、`tokens_out`（completion_tokens）、`tokens_total`、`model`、`est_cost` |
| Embedding | `semantic_similarity` 且 `metric=embedding` 返回的 `usage` | 同上，`entry_type=embedding` |
| Runner 时长 | Runner `POST /runner/runs/{id}/complete` | `gpu_seconds`（优先使用 Runner 上报值，否则用墙钟时长估算）、`est_cost` |
| 手工调整 | `POST /runs/{id}/costs` / CLI `record-run-cost` | 显式 token/GPU/成本，`entry_type=manual` |

同一 Run 的 `cost_ledger` 是这些明细的派生汇总。汇总接口返回的 `entryIds` 与 `detail_ref` 指向同一批明细，可直接对账。

## 价格与配额配置

配置写入 `project.quota_config`（只合并给定字段，不清除其他配置）：

```bash
curl -X POST "$BASE/projects/$PRJ/costs/quota" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "pricing": {"tokens_in_per_1m": 1.0, "tokens_out_per_1m": 2.0,
                 "gpu_second": 0.01, "currency": "USD"},
    "run_quota": {"max_tokens_total": 100000, "max_gpu_seconds": 3600, "max_est_cost": 0.05},
    "period_quota": {"window": "day", "max_est_cost": 5.0}
  }'
```

可用的限额键：`max_tokens_in`、`max_tokens_out`、`max_tokens_total`、`max_gpu_seconds`、`max_est_cost`。周期配额额外有 `window=day|month|total`。未设置即不限制；价格未配置表示成本未知，而不是免费。

## 缺失成本

- 模型端点返回成功但没有 `usage`：明细 `cost_status=missing`、`missing_reason=usage_not_reported`、token 与成本为 NULL。
- 模型调用失败：`missing_reason=model_call_error:<code>`，仍不记为 0。
- 有 usage 但没有对应价格：`partial`，已知部分照常计算，缺少的价格项写入 `missing_reason`。
- Runner 没有时长或 GPU 上报：`missing_reason=gpu_seconds_not_reported`。
- 汇总单独返回 `missing.entryCount`、`missing.reasons` 和 `note`；`cost_status=missing` 的 `est_cost` 为 NULL。

## 超限终止

评分流程在每个样本的模型调用返回后、下一样本开始前检查单次 Run 与周期配额；Runner complete 在回写最终状态前检查。超限时：

- Run `status` 变为 `cancelled`；
- `cancel.source=quota`，`cancel.reason=quota_exceeded:<scope>.<limit>: consumed <value> > limit <limit>`；
- `run_event` 增加一条 `reason=quota_exceeded` 的迁移记录；
- `cost_quota_event(action=terminated)` 保存 `limit_json`、`consumed_json` 与阶段；
- Run 详情 `cost.quota` 返回 `exceeded`、`reasons`、`run/period.consumed`、`termination`。

已终态的 Run 不会被改写状态；若在终态之后才发现超限，会写 `violation_after_terminal` 审计记录并保留历史状态。

## 限制

- 配额是后置检查：已发出的 HTTP 模型调用无法抢占；如果调用尚未返回，终止发生在它返回之后。
- GPU 秒数在没有 Runner GPU 遥测时是墙钟时长估算，`metadata.gpu_seconds_source` 会标注 `runner_wall_clock_estimate` 或 `reported`。
- 价格是项目级显式配置，不内置供应商价格表；变更价格不会追溯改写历史明细，明细保存当次 `pricing_json`。
