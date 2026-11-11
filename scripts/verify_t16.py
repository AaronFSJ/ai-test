#!/usr/bin/env python3
"""Reproducible T16 evidence: cost detail, reconciliation, quota gate and
missing-cost marking.

The script starts the real local HTTP API and a deterministic OpenAI-compatible
stub endpoint. The stub proves usage parsing and accounting; it is not evidence
of real model quality or real pricing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from aitest import service  # noqa: E402
from test_t16 import CostFixture  # noqa: E402
from test_t09 import create_profile, create_run  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt(value: Any, limit: int = 6000) -> Any:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(text) <= limit:
            return value
        return {"excerpt": text[:limit] + "...", "truncated": True}
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, passed: bool, detail: Any = None) -> None:
        self.items.append({"name": name, "passed": bool(passed), "detail": excerpt(detail)})

    def get(self, name: str) -> bool:
        return any(item["name"] == name and item["passed"] for item in self.items)

    def as_dict(self) -> dict:
        return {"allPassed": all(item["passed"] for item in self.items), "checks": self.items}


def judge_setup(fx: CostFixture, name: str, rows: list[dict] | None = None) -> tuple[dict, dict]:
    dataset = fx.dataset(
        name,
        rows
        or [
            {"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"},
            {"sample_id": "s2", "input": "q2", "expected": "no", "priority": "P0"},
        ],
    )
    scorer = fx.judge_scorer(f"judge-{name}")
    profile = create_profile(fx, f"profile-{name}", "and", [{"scorer_id": scorer["id"]}])[
        "versions"
    ][0]
    fx.bind_default(dataset, profile)
    run = create_run(fx, dataset, "bindings", commit=(name[:1] or "a") * 40)
    return dataset, run


def collect_reconciliation(checks: Checks) -> dict:
    fx = CostFixture()
    try:
        pricing = fx.set_pricing(
            {"tokens_in_per_1m": 1.0, "tokens_out_per_1m": 2.0, "currency": "USD"}
        )
        _, run = judge_setup(fx, "reconcile")
        status, body = fx.score(run, {"s1": "yes", "s2": "no"})
        if status != 200:
            raise RuntimeError(body)
        run_cost = fx.run_cost(run["id"])[1]["data"]
        summary = fx.summary()[1]["data"]
        detail = fx.api(
            "GET", f"/projects/{fx.alpha_id}/costs/entries?channel=B", fx.alpha_token
        )[1]["data"]
        ledger = run_cost["ledger"]
        entries = run_cost["entries"]
        sums = {
            "tokens_in": sum(e["tokens_in"] for e in entries if e["tokens_in"] is not None),
            "tokens_out": sum(e["tokens_out"] for e in entries if e["tokens_out"] is not None),
            "tokens_total": sum(e["tokens_total"] for e in entries if e["tokens_total"] is not None),
            "est_cost": round(sum(e["est_cost"] for e in entries if e["est_cost"] is not None), 10),
        }
        checks.add(
            "reconcile.entries_match_ledger",
            len(entries) == 2
            and sums["tokens_in"] == ledger["tokens_in"] == 22
            and sums["tokens_out"] == ledger["tokens_out"] == 14
            and sums["tokens_total"] == ledger["tokens_total"] == 36
            and abs(sums["est_cost"] - ledger["est_cost"]) < 1e-12,
            {"ledger": ledger, "entrySums": sums, "entries": entries},
        )
        checks.add(
            "reconcile.summary_matches_detail",
            summary["totals"]["tokens_in"] == ledger["tokens_in"]
            and summary["totals"]["tokens_out"] == ledger["tokens_out"]
            and abs(summary["totals"]["est_cost"] - ledger["est_cost"]) < 1e-12
            and detail["summary"]["tokens_total"] == ledger["tokens_total"]
            and summary["reconciliation"]["entriesMatchGroupTotal"],
            {"summaryTotals": summary["totals"], "detailSummary": detail["summary"]},
        )
        checks.add(
            "reconcile.group_traces_to_entry_ids",
            bool(summary["groups"])
            and sorted(summary["groups"][0]["entryIds"]) == sorted(e["id"] for e in entries),
            {"group": summary["groups"][0] if summary["groups"] else None},
        )
        return {
            "generatedAt": now(),
            "pricing": pricing["pricing"],
            "run": {
                "id": run["id"],
                "status": run_cost["quota"].get("status"),
                "specVersionId": run["spec_version_id"],
            },
            "ledger": ledger,
            "entries": entries,
            "entrySums": sums,
            "summary": summary,
            "detailSummary": detail["summary"],
            "note": "Stub model returns prompt_tokens=11 and completion_tokens=7 per call.",
        }
    finally:
        fx.close()


def collect_quota(checks: Checks) -> dict:
    fx = CostFixture()
    try:
        fx.set_pricing(
            {"tokens_in_per_1m": 1.0, "tokens_out_per_1m": 2.0, "currency": "USD"},
            run_quota={"max_est_cost": 0.000001},
        )
        _, run = judge_setup(fx, "quota")
        status, body = fx.score(run, {"s1": "yes", "s2": "no"})
        if status != 200:
            raise RuntimeError(body)
        result = body["data"]
        detail = fx.api(
            "GET", f"/projects/{fx.alpha_id}/runs/{run['id']}", fx.alpha_token
        )[1]["data"]
        quota = detail["cost"]["quota"]
        checks.add(
            "quota.run_terminated",
            detail["status"] == "cancelled"
            and detail["cancel"]["source"] == "quota"
            and detail["cancel"]["reason"].startswith("quota_exceeded:run.max_est_cost"),
            {"status": detail["status"], "cancel": detail["cancel"]},
        )
        checks.add(
            "quota.reason_and_consumed_visible",
            quota["exceeded"]
            and quota["terminated"]
            and quota["run"]["consumed"]["est_cost"] > 0.000001
            and quota["termination"]["consumed"]["est_cost"] > 0.000001,
            {"quota": quota},
        )
        checks.add(
            "quota.stopped_before_remaining_items",
            result["summary"]["itemCount"] == 1
            and len(detail["items"]) == 1
            and any(event["reason"] == "quota_exceeded" for event in detail["events"]),
            {"summary": result["summary"], "itemCount": len(detail["items"]),
             "events": [event for event in detail["events"] if event["reason"] == "quota_exceeded"]},
        )
        return {
            "generatedAt": now(),
            "run": {
                "id": run["id"],
                "status": detail["status"],
                "cancel": detail["cancel"],
                "itemCount": len(detail["items"]),
                "events": detail["events"],
            },
            "quota": quota,
            "ledger": detail["cost"]["ledger"],
            "entries": detail["cost"]["entries"],
            "summary": result["summary"],
            "note": "The quota is hard and checked after each model call's usage is known. "
                    "An in-flight HTTP call cannot be preempted.",
        }
    finally:
        fx.close()


def collect_missing(checks: Checks) -> dict:
    fx = CostFixture(no_usage=True)
    try:
        fx.set_pricing({"tokens_in_per_1m": 1.0, "tokens_out_per_1m": 2.0, "currency": "USD"})
        dataset = fx.dataset(
            "missing", [{"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"}]
        )
        scorer = fx.judge_scorer("no-usage-judge")
        profile = create_profile(fx, "no-usage-profile", "and", [{"scorer_id": scorer["id"]}])[
            "versions"
        ][0]
        fx.bind_default(dataset, profile)
        run = create_run(fx, dataset, "bindings", commit="9" * 40)
        status, body = fx.score(run, {"s1": "yes"})
        if status != 200:
            raise RuntimeError(body)
        detail = fx.run_cost(run["id"])[1]["data"]
        summary = fx.summary()[1]["data"]
        entry = detail["entries"][0]
        checks.add(
            "missing.entry_is_null_not_zero",
            entry["tokens_in"] is None
            and entry["tokens_out"] is None
            and entry["est_cost"] is None
            and entry["cost_status"] == "missing"
            and entry["missing_reason"] == "usage_not_reported",
            {"entry": entry},
        )
        checks.add(
            "missing.ledger_and_summary_explain_missing",
            detail["ledger"]["est_cost"] is None
            and detail["ledger"]["tokens_total"] is None
            and detail["ledger"]["cost_status"] == "missing"
            and detail["ledger"]["missing_entries"] == 1
            and summary["missing"]["entryCount"] == 1
            and summary["missing"]["reasons"].get("usage_not_reported") == 1
            and summary["totals"]["est_cost"] is None,
            {"ledger": detail["ledger"], "missing": summary["missing"],
             "totals": summary["totals"]},
        )
        return {
            "generatedAt": now(),
            "run": {"id": run["id"], "status": body["data"]["run"]["status"]},
            "entry": entry,
            "ledger": detail["ledger"],
            "missing": detail["missing"],
            "summaryMissing": summary["missing"],
            "summaryTotals": summary["totals"],
            "note": "The stub omits the usage object entirely. No zero-cost row is invented.",
        }
    finally:
        fx.close()


def collect_aggregation(checks: Checks) -> dict:
    fx = CostFixture()
    try:
        conn = fx.connect()
        try:
            actor = service.authenticate(conn, fx.alpha_token)
            b_spec = service.create_spec(conn, actor, fx.alpha_id, {"channel": "B", "name": "b-cost"})
            b_version = service.add_spec_version(
                conn, actor, fx.alpha_id, b_spec["id"],
                {"dataset_version": "manual-v1", "scorer_version": "bindings",
                 "model_snapshot": {"model": "stub"}, "params": {"suite": "cost"}},
            )
            a_spec = service.create_spec(conn, actor, fx.alpha_id, {"channel": "A", "name": "a-cost"})
            a_version = service.add_spec_version(
                conn, actor, fx.alpha_id, a_spec["id"],
                {"repo_path": "github.com/acme/web", "repo_commit": "c" * 40, "params": {}},
            )
            runs = [
                service.create_run(conn, actor, fx.alpha_id, {
                    "spec_version_id": b_version["id"], "commit_sha": "1" * 40,
                    "branch": "feature-a"}),
                service.create_run(conn, actor, fx.alpha_id, {
                    "spec_version_id": b_version["id"], "commit_sha": "2" * 40,
                    "branch": "feature-b"}),
                service.create_run(conn, actor, fx.alpha_id, {
                    "spec_version_id": a_version["id"], "commit_sha": "3" * 40,
                    "branch": "main"}),
            ]
        finally:
            conn.close()
        for run, (tokens_in, tokens_out, est) in zip(
            runs, [(100, 20, 0.5), (200, 40, 1.0), (50, 10, 0.25)]
        ):
            status, body = fx.api(
                "POST", f"/projects/{fx.alpha_id}/runs/{run['id']}/costs", fx.alpha_token,
                {"tokens_in": tokens_in, "tokens_out": tokens_out, "est_cost": est},
            )
            if status != 201:
                raise RuntimeError(body)
        summary = fx.summary(group_by="project,branch,channel")[1]["data"]
        groups = {(g["branch"], g["channel"]): g for g in summary["groups"]}
        checks.add(
            "aggregate.groups_by_project_branch_channel",
            set(groups) == {("feature-a", "B"), ("feature-b", "B"), ("main", "A")}
            and summary["totals"]["tokens_in"] == 350
            and summary["totals"]["tokens_out"] == 70
            and abs(summary["totals"]["est_cost"] - 1.75) < 1e-12,
            {"groups": summary["groups"], "totals": summary["totals"]},
        )
        detail = fx.api(
            "GET", f"/projects/{fx.alpha_id}/costs/entries?branch=feature-a&channel=B",
            fx.alpha_token,
        )[1]["data"]
        checks.add(
            "aggregate.group_traces_to_detail",
            len(detail["entries"]) == 1
            and detail["entries"][0]["id"] == groups[("feature-a", "B")]["entryIds"][0]
            and detail["summary"]["tokens_in"] == 100,
            {"detail": detail, "group": groups[("feature-a", "B")]},
        )
        return {
            "generatedAt": now(),
            "runs": [{"id": run["id"], "branch": run["branch"], "channel": run["channel"]}
                     for run in runs],
            "summary": summary,
            "filteredDetail": detail,
        }
    finally:
        fx.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run T16 verification and write evidence")
    parser.add_argument("--out", default="../deliverables/evidence")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = Checks()
    payloads = {
        "16-cost-reconciliation.json": collect_reconciliation(checks),
        "17-quota-termination.json": collect_quota(checks),
        "18-missing-cost.json": collect_missing(checks),
        "19-cost-aggregation.json": collect_aggregation(checks),
    }
    paths = {}
    for filename, payload in payloads.items():
        path = out / filename
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
        path.write_text(text, encoding="utf-8")
        paths[filename] = {"path": str(path), "bytes": len(text.encode("utf-8")), "sha256": sha256(text)}
    acceptance = {
        "T16-a1": checks.get("reconcile.entries_match_ledger")
        and checks.get("reconcile.summary_matches_detail"),
        "T16-a2": checks.get("quota.run_terminated")
        and checks.get("quota.reason_and_consumed_visible")
        and checks.get("quota.stopped_before_remaining_items"),
        "T16-a3": checks.get("aggregate.groups_by_project_branch_channel")
        and checks.get("aggregate.group_traces_to_detail"),
        "T16-a4": checks.get("missing.entry_is_null_not_zero")
        and checks.get("missing.ledger_and_summary_explain_missing"),
    }
    summary = {
        "generatedAt": now(),
        "task": "T16",
        "allChecksPassed": checks.as_dict()["allPassed"],
        "checkCount": len(checks.items),
        "checks": checks.items,
        "acceptance": acceptance,
        "evidence": paths,
        "environment": {
            "python": sys.version.split()[0],
            "database": "temporary SQLite database",
            "http": "real local ThreadingHTTPServer",
            "model": "deterministic OpenAI-compatible stub; no real vLLM quality or billing is claimed.",
            "pricing": "explicit per-1M-token and per-GPU-second configuration supplied by the verification run.",
            "gpu": "Runner wall-clock duration is used as GPU seconds when the Runner does not report gpu_seconds.",
        },
        "limitations": [
            "Quota enforcement is post-call: an already in-flight HTTP model call cannot be preempted.",
            "The stub endpoint validates usage parsing and accounting, not real model quality.",
            "GPU seconds are an estimate from Runner duration unless the Runner reports gpu_seconds.",
        ],
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    (out / "t16-summary.json").write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"allChecksPassed": summary["allChecksPassed"], "acceptance": acceptance,
                      "out": str(out)}, ensure_ascii=False, indent=2))
    return 0 if summary["allChecksPassed"] else 2


if __name__ == "__main__":
    sys.exit(main())
