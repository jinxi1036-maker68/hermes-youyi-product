from __future__ import annotations

import argparse
import glob
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


DEFAULT_BASE = Path("/opt/hermes-youyi-current")
DEFAULT_CONFIG_PATHS = [
    Path("/opt/hermes-youyi-current/config/runtime.gateway.020.env"),
    Path("/opt/hermes-youyi/config/config.yaml"),
    Path("/opt/hermes-youyi/data/config.yaml"),
    Path("/opt/hermes-youyi-current/home-proddata/config.yaml"),
]
KEY_FILES = [
    "__init__.py",
    "tool_service.py",
    "tools.py",
    "runtime_foundation.py",
    "active_work_context.py",
    "digital_employee_state.py",
    "daily_reporter.py",
]
WECOM_KEY_FILES = [
    "callback_adapter.py",
    "inbound_receipts.py",
    "wecom_crypto.py",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "sha256": ""}
    return {"exists": True, "path": str(path), "sha256": _sha256(path)}


def _package_dirs(base: Path) -> list[Path]:
    patterns = [
        base / ".venv" / "lib" / "python*" / "site-packages" / "plugins" / "tuoguan_core",
        base / ".venv" / "lib64" / "python*" / "site-packages" / "plugins" / "tuoguan_core",
    ]
    output: list[Path] = []
    for pattern in patterns:
        for value in glob.glob(str(pattern)):
            path = Path(value)
            if path.exists() and path not in output:
                output.append(path)
    home_plugin = base / "home-proddata" / "plugins" / "tuoguan_core"
    if home_plugin not in output:
        output.append(home_plugin)
    return output


def _runtime_dir(base: Path) -> Path:
    return base / "runtime" / "plugins" / "tuoguan_core"


def _wecom_runtime_dir(base: Path) -> Path:
    return base / "runtime" / "plugins" / "platforms" / "wecom"


def _wecom_package_dirs(base: Path) -> list[Path]:
    patterns = [
        base / ".venv" / "lib" / "python*" / "site-packages" / "plugins" / "platforms" / "wecom",
        base / ".venv" / "lib64" / "python*" / "site-packages" / "plugins" / "platforms" / "wecom",
    ]
    output: list[Path] = []
    for pattern in patterns:
        for value in glob.glob(str(pattern)):
            path = Path(value)
            if path.exists() and path not in output:
                output.append(path)
    core_plugin = base / "hermes-agent" / "plugins" / "platforms" / "wecom"
    if core_plugin not in output:
        output.append(core_plugin)
    return output


def _venv_python(base: Path) -> Path:
    return base / ".venv" / "bin" / "python"


def _run(args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return {"cmd": args, "returncode": completed.returncode, "output_tail": completed.stdout[-4000:]}


def _import_probe(base: Path) -> dict[str, Any]:
    python = _venv_python(base)
    if not python.exists():
        return {"available": False, "error": "venv_python_missing", "path": str(python)}
    code = """
import importlib, json
mods = [
  "plugins.tuoguan_core",
  "plugins.tuoguan_core.tool_service",
  "plugins.tuoguan_core.runtime_foundation",
  "plugins.tuoguan_core.digital_employee_state",
  "plugins.platforms.wecom.callback_adapter",
  "plugins.platforms.wecom.inbound_receipts",
  "plugins.platforms.wecom.wecom_crypto",
]
out = {}
for name in mods:
    mod = importlib.import_module(name)
    out[name] = getattr(mod, "__file__", "")
print(json.dumps(out, ensure_ascii=False))
"""
    result = _run([str(python), "-c", code])
    result["available"] = result["returncode"] == 0
    return result


def _version_probe(base: Path) -> dict[str, Any]:
    hermes = base / ".venv" / "bin" / "hermes"
    if not hermes.exists():
        return {"available": False, "error": "hermes_cli_missing", "path": str(hermes)}
    result = _run([str(hermes), "--version"])
    result["available"] = result["returncode"] == 0
    return result


def _redact_config_line(line: str) -> str:
    lowered = line.lower()
    if any(term in lowered for term in ("api_key", "apikey", "secret", "token", "password")):
        if "=" in line:
            return line.split("=", 1)[0].strip() + "=<redacted>"
        if ":" in line:
            return line.split(":", 1)[0].strip() + ": <redacted>"
        return "<redacted>"
    return line.strip()


def _model_config_scan(config_paths: list[Path]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    all_model_mentions: list[dict[str, Any]] = []
    all_provider_mentions: list[dict[str, Any]] = []
    agnes_25_seen = False
    legacy_agnes_mentions: list[dict[str, Any]] = []
    for config_path in config_paths:
        file_result: dict[str, Any] = {
            "exists": config_path.exists(),
            "path": str(config_path),
            "current_model_mentions": [],
            "provider_mentions": [],
        }
        if not config_path.exists():
            files.append(file_result)
            continue
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines, 1):
            lowered = line.lower()
            if any(term in lowered for term in ("model", "base_url", "provider", "agnes")):
                item = {"path": str(config_path), "line": index, "text": _redact_config_line(line)}
                if "model" in lowered or "base_url" in lowered or "agnes" in lowered:
                    file_result["current_model_mentions"].append(item)
                    all_model_mentions.append(item)
                if "provider" in lowered:
                    file_result["provider_mentions"].append(item)
                    all_provider_mentions.append(item)
                if "agnes-2.5-flash" in lowered:
                    agnes_25_seen = True
                if "agnes-2.0-flash" in lowered:
                    legacy_agnes_mentions.append(item)
        file_result["current_model_mentions"] = file_result["current_model_mentions"][-30:]
        file_result["provider_mentions"] = file_result["provider_mentions"][-30:]
        files.append(file_result)
    return {
        "exists": any(item.get("exists") for item in files),
        "files": files,
        "current_model_mentions": all_model_mentions[-60:],
        "provider_mentions": all_provider_mentions[-60:],
        "agnes_25_seen": agnes_25_seen,
        "legacy_agnes_20_mentions": legacy_agnes_mentions[-30:],
    }


def _hash_matrix(base: Path) -> dict[str, Any]:
    runtime = _runtime_dir(base)
    package_dirs = _package_dirs(base)
    matrix: dict[str, Any] = {"runtime_dir": str(runtime), "package_dirs": [str(path) for path in package_dirs], "files": {}}
    for name in KEY_FILES:
        entries = {"runtime": _hash_file(runtime / name)}
        for package_dir in package_dirs:
            key = "site_packages:" + str(package_dir)
            entries[key] = _hash_file(package_dir / name)
        missing = [key for key, value in entries.items() if not value.get("exists")]
        existing_hashes = {
            value["sha256"]
            for value in entries.values()
            if isinstance(value, dict) and value.get("exists")
        }
        matrix["files"][name] = {
            "entries": entries,
            "hash_consistent": not missing and len(existing_hashes) == 1,
            "missing_entries": missing,
            "hash_count": len(existing_hashes),
        }
    return matrix


def _wecom_hash_matrix(base: Path) -> dict[str, Any]:
    runtime = _wecom_runtime_dir(base)
    package_dirs = _wecom_package_dirs(base)
    matrix: dict[str, Any] = {"runtime_dir": str(runtime), "package_dirs": [str(path) for path in package_dirs], "files": {}}
    for name in WECOM_KEY_FILES:
        entries = {"runtime": _hash_file(runtime / name)}
        for package_dir in package_dirs:
            entries["site_packages:" + str(package_dir)] = _hash_file(package_dir / name)
        missing = [key for key, value in entries.items() if not value.get("exists")]
        existing_hashes = {value["sha256"] for value in entries.values() if value.get("exists")}
        matrix["files"][name] = {
            "entries": entries,
            "hash_consistent": not missing and len(existing_hashes) == 1,
            "missing_entries": missing,
            "hash_count": len(existing_hashes),
        }
    return matrix


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Xiaoyou production load-path check.")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--config", type=Path, action="append", help="Config file to scan. Can be provided more than once.")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    config_paths = args.config if args.config else DEFAULT_CONFIG_PATHS

    report = {
        "ok": True,
        "read_only": True,
        "base": str(args.base),
        "hash_matrix": _hash_matrix(args.base),
        "wecom_hash_matrix": _wecom_hash_matrix(args.base),
        "import_probe": _import_probe(args.base),
        "version_probe": _version_probe(args.base),
        "model_config": _model_config_scan(config_paths),
    }
    hash_failures = [
        name
        for name, row in report["hash_matrix"]["files"].items()
        if not row.get("hash_consistent")
    ]
    report["hash_mismatch_files"] = hash_failures
    wecom_hash_failures = [
        name
        for name, row in report["wecom_hash_matrix"]["files"].items()
        if not row.get("hash_consistent")
    ]
    report["wecom_hash_mismatch_files"] = wecom_hash_failures
    if hash_failures or wecom_hash_failures or not report["import_probe"].get("available") or not report["model_config"].get("agnes_25_seen"):
        report["ok"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
