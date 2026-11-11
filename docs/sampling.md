# T10 多次采样与一致性判定

迁移文件：`src/aitest/migrations/005_sampling.sql`。实现位于 `src/aitest/sampling.py`，执行入口仍是
`POST /projects/{pid}/runs/{rid}/score`。

## 边界

平台不自行调用被测模型生成输出。采样由调用方提供：

- `actual_samples: {"s1": [out1, out2, ..., outN]}`：系统被测对象已采样 N 次，平台对 N 个输出各评分一次；
- `actual_outputs: {"s1": out}`：只提供一个输出，平台把同一输出提交评分器 N 次，适用于非确定性 judge；
- 两者同时出现时优先 `actual_samples`；每个样本的数组长度必须等于 `sampling.n`。

每个重复结果写入不可变 `run_item_sample`。条目聚合结论仍写入 `run_item` / `verdict`，并在
`verdict.aggregation_json` 保存策略、计数、比例、均值、方差、规则与原因。

## 采样配置

```json
{
  "n": 5,
  "strategy": "majority",
  "majority_threshold": 0.5,
  "expected_min": 0.8,
  "variance_max": 0.01,
  "concurrency": 4,
  "temperature": 0.2,
  "seed": 42
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `n` | 1 | 每个条目的重复次数，1–50 |
| `strategy` | `all_pass` | `all_pass` / `majority` / `expected_variance`，兼容 `all`、`majority_pass`、`score_expectation_variance` 等别名 |
| `majority_threshold` | 0.5 | 多数通过所需的最小比例；所需票数为 `ceil(N * threshold)` |
| `expected_min` | 0.8 | 得分期望策略的最小均值 |
| `variance_max` | 0.01 | 得分期望策略允许的最大（总体）方差，0–0.25 |
| `pass_threshold` | 0.5 | 保留给数值分数映射；当前聚合直接使用评分器状态与 `detail.score`/`detail.similarity` |
| `concurrency` | `min(4, n)` | 进程内有界线程池大小，1–32 |
| `temperature` | 未设置 | 采样温度，写入生效配置与模型快照；仅作追溯，平台不注入模型请求 |
| `seed` | 未设置 | 采样种子，写入生效配置 |

未知字段会返回 400，避免拼写错误静默改变成本。Run 创建时的配置存于 `run.sampling_config_json`；
评分时解析默认值、实际并发与模型快照后写入 `run.sampling_effective_json`。评分请求中的
`sampling` 只覆盖显式给出的键，其余继承 Run 配置。

## 聚合规则

### all_pass

- 任一 `fail` → `fail`；
- 否则任一 `uncertain` → `uncertain`；
- 否则任一 `error` → `error`；
- 全部 `pass` → `pass`。

### majority

设 `required = max(1, ceil(N * majority_threshold))`：

- `pass_count >= required` 且 `pass_count > fail_count` → `pass`；
- `fail_count >= required` 且 `fail_count > pass_count` → `fail`；
- pass/fail 持平 → `uncertain`；
- 两侧都未达阈值 → `uncertain`（原因中给出实际计数与所需票数）。

### expected_variance

- 只使用非 `error` 且能取得数值分数的重复（`detail.score`、`detail.similarity`，否则按状态映射 1/0/0.5）；
- 所有重复都是 `error` 或没有数值分数 → `error`；
- 均值 `< expected_min` → `fail`；
- 否则方差 `> variance_max` → `uncertain`（结果不稳定）；
- 否则 `pass`。

`error` 始终计入逐次结果和 explanation，但不进入均值/方差分母，不按 0 分记账。

## 成本与吞吐

- 响应 `summary.sampling.cost` 与 Run 详情 `sampling.cost` 包含 `multiplier`（=N）、
  `sampledEvaluations`、`scorerCalls`、token 汇总与提示语；
- 未产生模型调用时 token 字段为 `null`，并明确说明“缺失不等于 0”；
- 线程池一次提交所有重复任务，`batch.maxInflight` 记录实际观察到的并发峰值，
  不会超过 `concurrency`；模型端点的 continuous batching 由 vLLM 等端点负责。

## 可复现验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_t10.py' -v
PYTHONPATH=src python3 scripts/verify_t10.py --out ../deliverables/evidence
```

证据文件：`16-sampling-n5.json`、`17-sampling-strategy-switch.json`、
`18-sampling-repeatability.json`、`19-sampling-batch-cost.json`、`t10-ui-run.html`、
`t10-summary.json`。

## 限制

- Run 评分后不可原地重评（409）；比较策略应创建不同 Run。
- 进程内线程池只适用于单实例 SQLite；多实例需外部队列或 PostgreSQL 行锁。
- `temperature`/`seed` 只作追溯，平台不控制被测模型或 judge 的随机性；真实非确定性需模型端点支持。
- 成本闸门（T16）未实现；本任务只做成本可见提示与 token 汇总。
