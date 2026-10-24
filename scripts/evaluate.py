"""Offline recorded-trip replay. Measures rule detection, never model success rate."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models import TravelRequest, TripPlan
from src.services.quality import audit_plan


def replay_cases() -> list[dict]:
    base = json.loads(
        (ROOT / "examples/jieyang-recorded.json").read_text(encoding="utf-8")
    )
    cases = json.loads((ROOT / "evals/quality-cases.json").read_text(encoding="utf-8"))
    results = []
    for case in cases:
        value = copy.deepcopy(base)
        for path, replacement in case.get("edits", []):
            parts = path.split(".")
            parent = value
            for part in parts[:-1]:
                parent = parent[int(part)] if isinstance(parent, list) else parent[part]
            parent[int(parts[-1]) if isinstance(parent, list) else parts[-1]] = (
                replacement
            )
        report = audit_plan(
            TravelRequest.model_validate(value["request"]),
            TripPlan.model_validate(value["plan"]),
        )
        found = {issue.code for issue in report.issues}
        passed = (
            set(case.get("expect", [])) <= found
            and not set(case.get("absent", [])) & found
            and report.errors == case.get("errors", report.errors)
            and report.status == case.get("status", report.status)
        )
        results.append(
            {
                "id": case["id"],
                "description": case["description"],
                "passed": passed,
                "status": report.status,
                "errors": report.errors,
                "warnings": report.warnings,
                "detected": sorted(found),
                "expected": case.get("expect", []),
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = replay_cases()
    output = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "One recorded Jieyang trip plus deterministic fault injections; offline rule regression, not LLM accuracy or production availability.",
        "total": len(results),
        "passed": sum(item["passed"] for item in results),
        "cases": results,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"Offline quality replay: {output['passed']}/{output['total']} cases passed")
    for result in results:
        if not result["passed"]:
            print(json.dumps(result, ensure_ascii=False))
    return int(output["passed"] != output["total"])


if __name__ == "__main__":
    raise SystemExit(main())
