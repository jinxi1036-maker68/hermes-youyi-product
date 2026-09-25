#!/usr/bin/env python3
"""Fail closed when a XiaoYou candidate shadows or breaks Hermes Core constants."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import grp
import subprocess
from typing import Any, Callable

try:
    from .xiaoyou_runtime_topology import validate_runtime_topology
except ImportError:
    from xiaoyou_runtime_topology import validate_runtime_topology


DEFAULT_SERVICE_USER = "hermes-youyi"
DEFAULT_SERVICE_GROUP = "hermes-youyi"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _identity(user_name: str, group_name: str) -> tuple[int, int]:
    return pwd.getpwnam(user_name).pw_uid, grp.getgrnam(group_name).gr_gid


def _preexec(user_name: str, uid: int, gid: int) -> Callable[[], None]:
    def drop_privileges() -> None:
        os.initgroups(user_name, gid)
        os.setgid(gid)
        os.setuid(uid)
    return drop_privileges


def _probe_code() -> str:
    return r"""
import ast
import importlib
import importlib.util
import json
from pathlib import Path

constants = importlib.import_module("hermes_constants")
constants_path = Path(constants.__file__).resolve()

cli_spec = importlib.util.find_spec("hermes_cli")
cli_locations = [Path(p).resolve() for p in (cli_spec.submodule_search_locations or [])] if cli_spec else []

hard_required = set()
guarded_optional = set()
parse_errors = []

def catches_import_failure(node):
    if not isinstance(node, ast.Try):
        return False
    for handler in node.handlers:
        if handler.type is None:
            return True
        names = []
        if isinstance(handler.type, ast.Name):
            names = [handler.type.id]
        elif isinstance(handler.type, ast.Tuple):
            names = [item.id for item in handler.type.elts if isinstance(item, ast.Name)]
        if any(name in {"Exception", "BaseException", "ImportError"} for name in names):
            return True
    return False

class ContractVisitor(ast.NodeVisitor):
    def __init__(self):
        self.guard_stack = []

    def visit_Try(self, node):
        guarded = catches_import_failure(node)
        self.guard_stack.append(guarded or any(self.guard_stack))
        for item in node.body:
            self.visit(item)
        self.guard_stack.pop()

        inherited = any(self.guard_stack)
        self.guard_stack.append(inherited)
        for item in node.handlers:
            self.visit(item)
        for item in node.orelse:
            self.visit(item)
        for item in node.finalbody:
            self.visit(item)
        self.guard_stack.pop()

    def visit_ImportFrom(self, node):
        if node.module != "hermes_constants":
            return
        target = guarded_optional if any(self.guard_stack) else hard_required
        for alias in node.names:
            if alias.name != "*":
                target.add(alias.name)

for root in cli_locations:
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            parse_errors.append(f"{path}:{type(exc).__name__}")
            continue
        ContractVisitor().visit(tree)

# An unguarded use is authoritative even if the same symbol also appears in
# a guarded compatibility fallback elsewhere.
guarded_optional.difference_update(hard_required)

missing_hard = sorted(name for name in hard_required if not hasattr(constants, name))
missing_guarded = sorted(name for name in guarded_optional if not hasattr(constants, name))
print(json.dumps({
    "constants_path": str(constants_path),
    "test_shim_detected": bool(getattr(constants, "XIAOYOU_TEST_SHIM", False)),
    "hermes_cli_found": bool(cli_spec),
    "hermes_cli_locations": [str(p) for p in cli_locations],
    "hard_required_symbols": sorted(hard_required),
    "guarded_optional_symbols": sorted(guarded_optional),
    "missing_hard_symbols": missing_hard,
    "missing_guarded_optional_symbols": missing_guarded,
    "parse_errors": parse_errors,
}))
"""


def _evaluate_probe(*, probe: dict[str, Any], release_root: Path) -> dict[str, Any]:
    root = release_root.resolve()
    constants_path = Path(str(probe.get("constants_path") or ""))
    cli_locations = [Path(str(value)) for value in probe.get("hermes_cli_locations") or []]
    hard_required = sorted(str(value) for value in probe.get("hard_required_symbols") or [])
    guarded_optional = sorted(str(value) for value in probe.get("guarded_optional_symbols") or [])
    missing_hard = sorted(str(value) for value in probe.get("missing_hard_symbols") or [])
    missing_guarded = sorted(str(value) for value in probe.get("missing_guarded_optional_symbols") or [])
    parse_errors = [str(value) for value in probe.get("parse_errors") or []]

    constants_inside = bool(constants_path) and _inside(constants_path, root)
    cli_inside = bool(cli_locations) and all(_inside(path, root) for path in cli_locations)
    shim = bool(probe.get("test_shim_detected"))
    cli_found = bool(probe.get("hermes_cli_found"))
    contract_nonempty = bool(hard_required or guarded_optional)

    errors: list[str] = []
    if not constants_inside:
        errors.append("hermes_constants_outside_candidate")
    if shim:
        errors.append("xiaoyou_test_shim_shadowed_core")
    if not cli_found:
        errors.append("hermes_cli_missing")
    if not cli_inside:
        errors.append("hermes_cli_outside_candidate")
    if not contract_nonempty:
        errors.append("hermes_constants_contract_not_discovered")
    if missing_hard:
        errors.append("hermes_constants_missing_required_symbols")
    if parse_errors:
        errors.append("hermes_cli_contract_scan_error")

    return {
        "ok": not errors,
        "error": "" if not errors else "hermes_core_compatibility_failed",
        "errors": errors,
        "release_root": str(root),
        "hermes_constants_path": str(constants_path),
        "hermes_constants_inside_candidate": constants_inside,
        "xiaoyou_test_shim_detected": shim,
        "hermes_cli_found": cli_found,
        "hermes_cli_locations": [str(path) for path in cli_locations],
        "hermes_cli_inside_candidate": cli_inside,
        "hard_required_symbols": hard_required,
        "guarded_optional_symbols": guarded_optional,
        "missing_hard_symbols": missing_hard,
        "missing_guarded_optional_symbols": missing_guarded,
        "parse_errors": parse_errors,
        "secret_content_inspected": False,
    }


def inspect_core_compatibility(
    *,
    release_root: Path,
    runtime_home: Path,
    python_executable: Path,
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    root = release_root.resolve()
    home = runtime_home.resolve()
    python_path = python_executable.resolve()

    topology = validate_runtime_topology(
        release_root=root,
        runtime_home=home,
        require_existing=True,
    )
    if not topology.get("ok"):
        return {"ok": False, "error": "runtime_topology_invalid", "topology": topology}
    if not python_path.is_file() or not _inside(python_path, root):
        return {
            "ok": False,
            "error": "candidate_python_invalid",
            "python_executable": str(python_path),
        }

    uid, gid = _identity(service_user, service_group)
    current_uid = os.geteuid()
    if current_uid not in {0, uid}:
        return {
            "ok": False,
            "error": "executor_cannot_assume_service_identity",
            "current_uid": current_uid,
            "required_uid": uid,
        }

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["HERMES_HOME"] = str(home)
    kwargs: dict[str, Any] = {
        "cwd": root,
        "env": env,
        "text": True,
        "capture_output": True,
        "check": False,
    }
    if current_uid == 0:
        kwargs["preexec_fn"] = _preexec(service_user, uid, gid)

    completed = runner([str(python_path), "-c", _probe_code()], **kwargs)
    if completed.returncode != 0:
        return {
            "ok": False,
            "error": "core_compatibility_probe_failed",
            "returncode": completed.returncode,
            "stderr_tail": str(completed.stderr or "")[-2000:],
            "secret_content_inspected": False,
        }
    try:
        probe = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "core_compatibility_probe_invalid_json",
            "secret_content_inspected": False,
        }

    result = _evaluate_probe(probe=probe, release_root=root)
    result["python_executable"] = str(python_path)
    result["runtime_home"] = str(home)
    result["service_user"] = service_user
    result["service_group"] = service_group
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that a candidate uses an intact Hermes Core constants API."
    )
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--service-group", default=DEFAULT_SERVICE_GROUP)
    args = parser.parse_args()

    try:
        result = inspect_core_compatibility(
            release_root=args.release_root,
            runtime_home=args.runtime_home,
            python_executable=args.python,
            service_user=args.service_user,
            service_group=args.service_group,
        )
    except (KeyError, OSError, PermissionError) as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "secret_content_inspected": False,
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
