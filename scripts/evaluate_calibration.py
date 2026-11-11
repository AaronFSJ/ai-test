"""Evaluate a scorer against a T12 calibration export.

The export is JSONL (or a JSON array) of rows written by
``GET /projects/{pid}/calibration-samples/export``. Each usable row contains
the original sample, actual output, and the human expected state. This script
feeds those rows back through the existing scorer preview path, which is the
same protocol a future scorer-calibration UI would use.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, scoring, service  # noqa: E402


def _load_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        payload = json.loads(stripped)
        if not isinstance(payload, list):
            raise ValueError("JSON export must be an array")
        return payload
    rows = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"line {line_no} is not an object")
        rows.append(row)
    return rows


def evaluate(
    conn,
    actor: dict,
    project_id: str,
    rows: list[dict],
    *,
    scorer_version_id: str | None,
    scorer_type: str | None,
    config: dict | None,
) -> dict:
    evaluated = 0
    correct = 0
    confusion: dict[str, dict[str, int]] = {}
    results: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        sample_id = (row.get("sample") or {}).get("sampleId")
        if not row.get("usableForCalibration"):
            skipped.append(
                {
                    "sampleId": sample_id,
                    "reason": row.get("exclusionReason") or "not usable for calibration",
                }
            )
            continue
        expected = row.get("expectedState")
        if expected not in ("pass", "fail"):
            skipped.append({"sampleId": sample_id, "reason": "missing expectedState"})
            continue
        payload: dict[str, Any] = {
            "sample": {
                "sample_id": sample_id,
                "input": row["sample"].get("input"),
                "expected": row["sample"].get("expected"),
                "context": row["sample"].get("context"),
            },
            "actual": row.get("actual"),
            "context": row["sample"].get("context"),
        }
        if scorer_version_id:
            payload["scorer_version_id"] = scorer_version_id
        else:
            payload["type"] = scorer_type
            payload["config"] = config or {}
        preview = scoring.preview_scorer(conn, actor, project_id, payload)["result"]
        state = preview["state"]
        evaluated += 1
        if state == expected:
            correct += 1
        confusion.setdefault(expected, {})
        confusion[expected][state] = confusion[expected].get(state, 0) + 1
        results.append(
            {
                "sampleId": sample_id,
                "expectedState": expected,
                "scorerState": state,
                "correct": state == expected,
                "confidence": preview.get("confidence"),
                "detail": preview.get("detail"),
            }
        )
    return {
        "schemaVersion": 1,
        "projectId": project_id,
        "scorer": {
            "versionId": scorer_version_id,
            "type": scorer_type,
        },
        "inputRows": len(rows),
        "evaluated": evaluated,
        "correct": correct,
        "accuracy": round(correct / evaluated, 6) if evaluated else None,
        "confusion": confusion,
        "skipped": skipped,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evaluate-calibration")
    parser.add_argument("--db", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--scorer-version-id", default=None)
    parser.add_argument("--type", default=None)
    parser.add_argument("--config-json", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    if not args.scorer_version_id and not args.type:
        parser.error("pass --scorer-version-id or --type")
    config = json.loads(args.config_json) if args.config_json else {}
    rows = _load_rows(Path(args.file))
    conn = db.connect(args.db)
    try:
        actor = service.authenticate(conn, args.token)
        result = evaluate(
            conn,
            actor,
            args.project,
            rows,
            scorer_version_id=args.scorer_version_id,
            scorer_type=args.type,
            config=config,
        )
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
