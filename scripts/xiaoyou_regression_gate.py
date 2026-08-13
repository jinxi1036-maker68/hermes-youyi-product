from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

PY_COMPILE_TARGETS = [
    "runtime/utils.py",
    "runtime/plugins/tuoguan_core/__init__.py",
    "runtime/plugins/tuoguan_core/tool_service.py",
    "runtime/plugins/tuoguan_core/tools.py",
    "runtime/plugins/tuoguan_core/runtime_foundation.py",
    "runtime/plugins/tuoguan_core/active_work_context.py",
    "runtime/plugins/tuoguan_core/autonomous_employee_loop.py",
    "runtime/plugins/tuoguan_core/digital_employee_state.py",
    "runtime/plugins/tuoguan_core/proactive_work.py",
    "runtime/plugins/tuoguan_core/repair_task_companion_state.py",
    "runtime/plugins/tuoguan_core/daily_reporter.py",
    "runtime/plugins/tuoguan_core/notification_outbox_runner.py",
    "runtime/plugins/tuoguan_core/self_evolution.py",
    "runtime/plugins/tuoguan_core/workstyle_profiles.py",
    "runtime/plugins/tuoguan_core/social_market_research.py",
    "scripts/tenant_initializer.py",
    "scripts/tenant_acceptance_check.py",
    "scripts/xiaoyou_regression_gate.py",
    "scripts/xiaoyou_production_load_check.py",
    "scripts/xiaoyou_deploy_guard.py",
    "scripts/repair_semantically_retired_work_items.py",
    "scripts/repair_stale_xiaoyou_state.py",
    "scripts/repair_proactive_employee_state_v1.py",
    "scripts/repair_task_companion_state.py",
    "scripts/sanitize_shared_memory.py",
    "scripts/tune_xiaoyou_runtime_config.py",
    "runtime/plugins/platforms/wecom/callback_adapter.py",
    "runtime/plugins/platforms/wecom/inbound_receipts.py",
    "runtime/plugins/platforms/wecom/wecom_crypto.py",
]

PYTEST_TARGETS = [
    "runtime/tests/plugins",
    "runtime/tests/test_tenant_initializer_v0.py",
    "runtime/tests/test_tenant_acceptance_check_v0.py",
    "runtime/tests/test_sanitize_shared_memory_v1.py",
    "runtime/tests/test_tune_xiaoyou_runtime_config_v1.py",
    "runtime/tests/test_repair_proactive_employee_state_v1.py",
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
    list_command = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    completed = subprocess.run(
        list_command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        return {"cmd": list_command, "returncode": completed.returncode, "output_tail": completed.stdout[-4000:]}
    hits: list[str] = []
    secret_hits: list[str] = []
    secret_patterns = (
        re.compile(r"\b(?:sk|wk)-[A-Za-z0-9_-]{20,}\b"),
        re.compile(r"(?i)\b(?:api[_ -]?key|corp[_ -]?secret|access[_ -]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+-]{24,}"),
    )
    for line in completed.stdout.splitlines():
        lowered = line.lower()
        if any(pattern in lowered for pattern in RISKY_TRACKED_PATTERNS):
            if not (lowered.startswith("runtime/tests/") or lowered.startswith("work/commercialization/")):
                hits.append(line)
        path = ROOT / line
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if any(pattern.search(content) for pattern in secret_patterns):
            secret_hits.append(line)
    all_hits = [*hits, *[f"content:{path}" for path in secret_hits]]
    return {
        "cmd": [*list_command, "<risk-patterns>"],
        "returncode": 1 if all_hits else 0,
        "hits": all_hits[:80],
        "output_tail": "\n".join(all_hits[:80]),
    }


def _demo_tenant_acceptance() -> dict[str, Any]:
    profile = ROOT / "work" / "commercialization" / "demo_tenant_profile.json"
    if not profile.exists():
        return {"cmd": ["demo_tenant_acceptance"], "returncode": 2, "output_tail": "demo tenant profile missing"}
    with tempfile.TemporaryDirectory(prefix="xiaoyou-gate-") as temp_root:
        initialized = _run([
            sys.executable,
            str(ROOT / "scripts" / "tenant_initializer.py"),
            "--tenant-profile",
            str(profile),
            "--platform-root",
            temp_root,
            "--force",
        ])
        if initialized["returncode"] != 0:
            return initialized
        accepted = _run([
            sys.executable,
            str(ROOT / "scripts" / "tenant_acceptance_check.py"),
            "--tenant-root",
            str(Path(temp_root) / "tenants" / "demo_tuoguan"),
        ])
        accepted["initializer_output_tail"] = initialized["output_tail"]
        return accepted


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
        ("demo_tenant_acceptance", _demo_tenant_acceptance),
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
