from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

PY_COMPILE_TARGETS = [
    "runtime/plugins/tuoguan_core/__init__.py",
    "runtime/plugins/tuoguan_core/tool_service.py",
    "runtime/plugins/tuoguan_core/tools.py",
    "runtime/plugins/tuoguan_core/runtime_foundation.py",
    "runtime/plugins/tuoguan_core/active_work_context.py",
    "runtime/plugins/tuoguan_core/digital_employee_state.py",
    "runtime/plugins/tuoguan_core/daily_reporter.py",
    "runtime/plugins/tuoguan_core/self_evolution.py",
    "runtime/plugins/tuoguan_core/workstyle_profiles.py",
    "runtime/plugins/tuoguan_core/social_market_research.py",
    "scripts/tenant_initializer.py",
    "scripts/tenant_acceptance_check.py",
    "scripts/xiaoyou_regression_gate.py",
    "scripts/xiaoyou_production_load_check.py",
]

PYTEST_TARGETS = [
    "runtime/tests/plugins/test_youyi_task_context_admin_close_v1.py",
    "runtime/tests/plugins/test_youyi_employee_reliability_stage1_v1.py",
    "runtime/tests/plugins/test_youyi_xiaoyou_health_v1.py",
    "runtime/tests/plugins/test_youyi_model_mainline_hardening.py",
    "runtime/tests/plugins/test_youyi_owner_attention_reply_anchor_v1.py",
    "runtime/tests/plugins/test_youyi_relationship_touch_v1.py",
    "runtime/tests/plugins/test_youyi_workstyle_profile_v1.py",
    "runtime/tests/plugins/test_youyi_daily_reporter_v1.py",
    "runtime/tests/plugins/test_youyi_self_evolution_v1.py",
    "runtime/tests/plugins/test_youyi_social_market_research_v1.py",
]

RISKY_TRACKED_PATTERNS = (
    "runtime.secrets.env",
    "notification_outbox.json",
    "students.json",
    "records.json",
    "tasks.json",
    "wecom_whitelist.json",
    "teacher_wecom_map.json",
    "secret",
    "token",
    "credential",
)


def _run(args: list[str], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=env or os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return {
        "cmd": args,
        "returncode": completed.returncode,
        "output_tail": completed.stdout[-4000:],
    }


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    runtime = str(ROOT / "runtime")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = runtime if not existing else runtime + os.pathsep + existing
    return env


def _py_compile() -> dict[str, Any]:
    existing = [str(ROOT / path) for path in PY_COMPILE_TARGETS if (ROOT / path).exists()]
    return _run([sys.executable, "-m", "py_compile", *existing], env=_python_env())


def _pytest_available() -> bool:
    result = _run([sys.executable, "-c", "import pytest"], env=_python_env())
    return result["returncode"] == 0


def _pytest(targets: list[str]) -> dict[str, Any]:
    existing = [path for path in targets if (ROOT / path).exists()]
    return _run([sys.executable, "-m", "pytest", *existing], env=_python_env())


def _git_diff_check() -> dict[str, Any]:
    return _run(["git", "diff", "--check"])


def _sensitive_scan() -> dict[str, Any]:
    listed = _run(["git", "ls-files"])
    if listed["returncode"] != 0:
        return listed
    hits: list[str] = []
    for line in str(listed.get("output_tail") or "").splitlines():
        lowered = line.lower()
        if any(pattern in lowered for pattern in RISKY_TRACKED_PATTERNS):
            if lowered.startswith("runtime/tests/") or lowered.startswith("work/commercialization/"):
                continue
            hits.append(line)
    return {
        "cmd": ["git", "ls-files", "<risk-patterns>"],
        "returncode": 1 if hits else 0,
        "hits": hits[:80],
        "output_tail": "\n".join(hits[:80]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the fixed Xiaoyou regression gate.")
    parser.add_argument("--allow-missing-pytest", action="store_true", help="Report missing pytest as blocked instead of failing the gate.")
    parser.add_argument("--skip-pytest", action="store_true", help="Skip pytest intentionally.")
    args = parser.parse_args()

    results: dict[str, Any] = {
        "root": str(ROOT),
        "python": sys.executable,
        "checks": {},
        "status": "pass",
    }
    for name, func in (
        ("py_compile", _py_compile),
        ("git_diff_check", _git_diff_check),
        ("sensitive_scan", _sensitive_scan),
    ):
        result = func()
        results["checks"][name] = result
        if result["returncode"] != 0:
            results["status"] = "fail"

    if args.skip_pytest:
        results["checks"]["pytest"] = {"returncode": 0, "skipped": True, "reason": "requested"}
    elif _pytest_available():
        result = _pytest(PYTEST_TARGETS)
        results["checks"]["pytest"] = result
        if result["returncode"] != 0:
            results["status"] = "fail"
    else:
        results["checks"]["pytest"] = {
            "returncode": 2,
            "blocked": True,
            "reason": "pytest_missing",
            "output_tail": "pytest is not installed in this Python environment.",
        }
        if not args.allow_missing_pytest:
            results["status"] = "fail"
        elif results["status"] == "pass":
            results["status"] = "blocked_pytest_missing"

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results["status"] in {"pass", "blocked_pytest_missing"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
