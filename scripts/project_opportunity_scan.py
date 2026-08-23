"""Run a read-only, privacy-minimized project-opportunity evidence scan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from plugins.tuoguan_core.project_opportunities import scan_project_opportunity_evidence
from plugins.tuoguan_core.store import TuoguanStore


def _safe_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "bundle_id": bundle.get("bundle_id"),
        "project_id": bundle.get("project_id"),
        "project_name": bundle.get("project_name"),
        "dimension": bundle.get("dimension"),
        "title": bundle.get("title"),
        "maturity": bundle.get("maturity"),
        "strong_evidence": bool(bundle.get("strong_evidence")),
        "total_student_count": int(bundle.get("total_student_count") or 0),
        "covered_student_count": int(bundle.get("covered_student_count") or 0),
        "coverage_rate": float(bundle.get("coverage_rate") or 0),
        "minimum_affected_student_count": int(bundle.get("minimum_affected_student_count") or 0),
        "affected_student_count": int(bundle.get("affected_student_count") or 0),
        "teacher_count": int(bundle.get("teacher_count") or 0),
        "evidence_date_count": len(bundle.get("evidence_dates") or []),
        "latest_evidence_at": bundle.get("latest_evidence_at"),
        "evidence_type_counts": bundle.get("evidence_type_counts") or {},
        "gates": bundle.get("gates") or {},
        "missing_gates": bundle.get("missing_gates") or [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Xiaoyou project-opportunity evidence scan; never writes candidate state."
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args(argv)

    result = scan_project_opportunity_evidence(
        TuoguanStore(args.data_dir),
        project_id=args.project_id,
        limit=args.limit,
    )
    safe_result = {
        "ok": bool(result.get("ok")),
        "mode": "read_only_dry_run",
        "writes_performed": False,
        "scanned_at": result.get("scanned_at"),
        "project_count": int(result.get("project_count") or 0),
        "evaluated_bundle_count": int(result.get("evaluated_bundle_count") or 0),
        "strong_bundle_count": int(result.get("strong_bundle_count") or 0),
        "bundles": [
            _safe_bundle(item)
            for item in result.get("bundles") or []
            if isinstance(item, dict)
        ],
    }
    print(json.dumps(safe_result, ensure_ascii=False, indent=2))
    return 0 if safe_result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
