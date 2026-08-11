"""Read-only Hermes v0.20 source compatibility gate for Xiaoyou."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


def _contains(root: Path, pattern: str, glob: str = "*.py") -> bool:
    for path in root.rglob(glob):
        try:
            if pattern in path.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


def run(*, candidate_root: Path, project_root: Path) -> dict:
    pyproject = candidate_root / "pyproject.toml"
    body = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
    version_match = re.search(r'^version\s*=\s*"([^"]+)"', body, re.MULTILINE)
    requires_match = re.search(r'^requires-python\s*=\s*"([^"]+)"', body, re.MULTILINE)
    cron_tools = candidate_root / "tools" / "cronjob_tools.py"
    cron_body = cron_tools.read_text(encoding="utf-8", errors="ignore") if cron_tools.exists() else ""
    checks = {
        "candidate_version_is_0_20": bool(version_match and version_match.group(1).startswith("0.20")),
        "running_python_supported": (3, 11) <= sys.version_info[:2] < (3, 14),
        "plugin_hook_registration_present": _contains(candidate_root, "register_hook"),
        "plugin_tool_registration_present": _contains(candidate_root, "register_tool"),
        "skill_bundle_support_present": _contains(candidate_root, "skill-bundles"),
        "cron_preflight_support_present": all(
            marker in cron_body
            for marker in ("def _validate_cron_base_url", "def _validate_cron_script_path")
        ),
        "session_context_api_present": (candidate_root / "gateway" / "session_context.py").exists(),
        "hermes_constants_present": (candidate_root / "hermes_constants.py").exists(),
        "xiaoyou_bundle_present": (project_root / "runtime" / "plugins" / "tuoguan_core" / "skill_assets" / "skill-bundles" / "xiaoyou-core.yaml").exists(),
        "xiaoyou_core_contract_present": (project_root / "runtime" / "plugins" / "tuoguan_core" / "skill_assets" / "skills" / "xiaoyou-core-contract" / "SKILL.md").exists(),
    }
    hard_failures = [name for name, passed in checks.items() if not passed and name not in {"session_context_api_present"}]
    warnings = []
    if not checks["session_context_api_present"]:
        warnings.append("gateway.session_context path changed or is absent; callback context integration needs an adapter test before upgrade.")
    return {
        "ok": not hard_failures,
        "mode": "read_only_source_dry_run",
        "candidate_root": str(candidate_root),
        "project_root": str(project_root),
        "candidate_version": version_match.group(1) if version_match else "unknown",
        "requires_python": requires_match.group(1) if requires_match else "unknown",
        "checks": checks,
        "hard_failures": hard_failures,
        "warnings": warnings,
        "production_actions": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    try:
        result = run(candidate_root=Path(args.candidate_root).resolve(), project_root=Path(args.project_root).resolve())
    except Exception as exc:
        result = {"ok": False, "error": type(exc).__name__, "message": str(exc), "production_actions": []}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
