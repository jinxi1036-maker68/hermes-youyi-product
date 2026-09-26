#!/usr/bin/env python3
"""Validate and install a Xiaoyou release through one canonical code tree."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from typing import Any

try:
    from .xiaoyou_release_package import verify_release_directory
except ImportError:
    from xiaoyou_release_package import verify_release_directory


DEFAULT_BASE = Path("/opt/hermes-youyi-current")


def _relative_files(path: Path) -> set[str]:
    if not path.is_dir():
        return set()
    return {
        item.relative_to(path).as_posix()
        for item in path.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"
    }


def _targets(base: Path) -> list[tuple[str, Path, str]]:
    targets: list[tuple[str, Path, str]] = [
        ("tuoguan_runtime", base / "runtime/plugins/tuoguan_core", "runtime/plugins/tuoguan_core"),
        ("wecom_runtime", base / "runtime/plugins/platforms/wecom", "runtime/plugins/platforms/wecom"),
        ("scripts", base / "scripts", "scripts"),
    ]
    for root_name in ("lib", "lib64"):
        for package_dir in sorted((base / ".venv" / root_name).glob("python*/site-packages/plugins")):
            targets.append((f"tuoguan_{root_name}_{package_dir.parent.parent.name}", package_dir / "tuoguan_core", "runtime/plugins/tuoguan_core"))
            targets.append((f"wecom_{root_name}_{package_dir.parent.parent.name}", package_dir / "platforms/wecom", "runtime/plugins/platforms/wecom"))
    hermes_wecom = base / "hermes-agent/plugins/platforms/wecom"
    if hermes_wecom.exists() or hermes_wecom.parent.exists():
        targets.append(("wecom_hermes_source", hermes_wecom, "runtime/plugins/platforms/wecom"))
    unique: list[tuple[str, Path, str]] = []
    seen: set[str] = set()
    for label, target, source in targets:
        key = str(target)
        if key not in seen:
            seen.add(key)
            unique.append((label, target, source))
    return unique


def plan_stage_release(*, release_root: Path, base: Path) -> dict[str, Any]:
    """Verify a release can be staged without touching active runtime links."""

    verification = verify_release_directory(release_root)
    if not verification.get("ok"):
        return {
            "ok": False,
            "error": "release_verification_failed",
            "verification": verification,
            "applied": False,
            "active_targets_modified": False,
        }

    if not base.is_dir():
        return {
            "ok": False,
            "error": "base_missing_or_not_directory",
            "base": str(base),
            "applied": False,
            "active_targets_modified": False,
        }

    manifest = verification["manifest"]
    release_id = str(manifest.get("release_id") or "")
    source_commit = str(verification.get("source_commit") or "")
    release_parent = base / "xiaoyou-releases"
    canonical_release = release_parent / release_id
    canonical_payload = canonical_release / str(
        manifest.get("payload_root") or "payload"
    )

    if release_parent.is_symlink():
        return {
            "ok": False,
            "error": "release_parent_symlink_forbidden",
            "release_parent": str(release_parent),
            "applied": False,
            "active_targets_modified": False,
        }

    already_present = canonical_release.exists() or canonical_release.is_symlink()
    existing_verification: dict[str, Any] | None = None
    if canonical_release.is_symlink():
        return {
            "ok": False,
            "error": "canonical_release_symlink_forbidden",
            "canonical_release": str(canonical_release),
            "applied": False,
            "active_targets_modified": False,
        }
    if already_present:
        existing_verification = verify_release_directory(canonical_release)
        if (
            not existing_verification.get("ok")
            or str(existing_verification.get("release_id") or "") != release_id
            or str(existing_verification.get("source_commit") or "")
            != source_commit
        ):
            return {
                "ok": False,
                "error": "existing_canonical_release_invalid",
                "canonical_release": str(canonical_release),
                "existing_verification": existing_verification,
                "applied": False,
                "active_targets_modified": False,
            }

    return {
        "ok": True,
        "error": "",
        "release_id": release_id,
        "source_commit": source_commit,
        "base": str(base),
        "release_parent": str(release_parent),
        "canonical_release": str(canonical_release),
        "canonical_payload": str(canonical_payload),
        "already_present": bool(already_present),
        "applied": False,
        "staged": bool(already_present),
        "active_targets_modified": False,
    }


def stage_release(
    *,
    release_root: Path,
    base: Path,
    apply: bool = False,
) -> dict[str, Any]:
    """Stage verified candidate code without switching any active code links."""

    plan = plan_stage_release(release_root=release_root, base=base)
    if not plan.get("ok") or not apply:
        return plan
    if plan.get("already_present"):
        return {
            **plan,
            "ok": True,
            "staged": True,
            "applied": False,
            "already_present": True,
            "active_targets_modified": False,
        }

    release_parent = Path(str(plan["release_parent"]))
    canonical_release = Path(str(plan["canonical_release"]))
    release_parent.mkdir(mode=0o755, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    temporary = release_parent / (
        f".{plan['release_id']}.stage-{os.getpid()}-{stamp}"
    )
    if temporary.exists() or temporary.is_symlink():
        return {
            **plan,
            "ok": False,
            "error": "stage_temporary_path_collision",
            "temporary": str(temporary),
        }

    try:
        shutil.copytree(release_root, temporary)
        staged_verification = verify_release_directory(temporary)
        if (
            not staged_verification.get("ok")
            or str(staged_verification.get("release_id") or "")
            != str(plan["release_id"])
            or str(staged_verification.get("source_commit") or "")
            != str(plan["source_commit"])
        ):
            shutil.rmtree(temporary, ignore_errors=True)
            return {
                **plan,
                "ok": False,
                "error": "staged_release_verification_failed",
                "verification": staged_verification,
                "temporary": str(temporary),
            }

        if canonical_release.exists() or canonical_release.is_symlink():
            shutil.rmtree(temporary, ignore_errors=True)
            concurrent = plan_stage_release(
                release_root=release_root,
                base=base,
            )
            return {
                **concurrent,
                "concurrent_stage_detected": True,
            }

        temporary.rename(canonical_release)
    except Exception as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        return {
            **plan,
            "ok": False,
            "error": "stage_failed",
            "detail": f"{type(exc).__name__}:{exc}",
            "temporary": str(temporary),
            "active_targets_modified": False,
        }

    final_verification = verify_release_directory(canonical_release)
    final_ok = (
        final_verification.get("ok")
        and str(final_verification.get("release_id") or "")
        == str(plan["release_id"])
        and str(final_verification.get("source_commit") or "")
        == str(plan["source_commit"])
    )
    return {
        **plan,
        "ok": bool(final_ok),
        "error": "" if final_ok else "canonical_stage_verification_failed",
        "staged": bool(final_ok),
        "applied": True,
        "already_present": False,
        "canonical_verification": final_verification,
        "active_targets_modified": False,
    }


def plan_install(*, release_root: Path, base: Path) -> dict[str, Any]:
    verification = verify_release_directory(release_root)
    if not verification.get("ok"):
        return {"ok": False, "error": "release_verification_failed", "verification": verification}
    manifest = verification["manifest"]
    release_id = str(manifest.get("release_id") or "")
    canonical_root = base / "xiaoyou-releases" / release_id / "payload"
    source_payload = release_root / str(manifest.get("payload_root") or "payload")
    rows: list[dict[str, Any]] = []
    unsafe_extra_count = 0
    for label, target, source_relative in _targets(base):
        source = source_payload / source_relative
        current_files = _relative_files(target.resolve() if target.is_symlink() else target)
        release_files = _relative_files(source)
        extras = sorted(current_files - release_files)
        unsafe_extra_count += len(extras)
        rows.append({
            "label": label,
            "target": str(target),
            "source_relative": source_relative,
            "exists": target.exists() or target.is_symlink(),
            "is_symlink": target.is_symlink(),
            "unknown_existing_files": extras,
        })
    return {
        "ok": unsafe_extra_count == 0,
        "error": "unknown_existing_modules" if unsafe_extra_count else "",
        "release_id": release_id,
        "source_commit": verification.get("source_commit"),
        "base": str(base),
        "canonical_root": str(canonical_root),
        "unknown_existing_file_count": unsafe_extra_count,
        "targets": rows,
        "applied": False,
    }


def install_release(*, release_root: Path, base: Path, apply: bool = False) -> dict[str, Any]:
    plan = plan_install(release_root=release_root, base=base)
    if not plan.get("ok") or not apply:
        return plan
    release_id = str(plan["release_id"])
    source_payload = release_root / "payload"
    canonical_release = base / "xiaoyou-releases" / release_id
    canonical_payload = canonical_release / "payload"
    if canonical_release.exists():
        verification = verify_release_directory(canonical_release)
        if not verification.get("ok"):
            return {**plan, "ok": False, "error": "existing_canonical_release_invalid"}
    else:
        canonical_release.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(release_root, canonical_release)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = base / "backups" / f"pre-release-link-{release_id}-{stamp}"
    backup_root.mkdir(parents=True, exist_ok=False)
    applied_targets: list[dict[str, str]] = []
    rollback_steps: list[dict[str, str]] = []
    try:
        for label, target, source_relative in _targets(base):
            canonical_source = canonical_payload / source_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = backup_root / label
            if target.is_symlink():
                old_link = os.readlink(target)
                backup.write_text(old_link, encoding="utf-8")
                target.unlink()
                rollback_steps.append({"kind": "symlink", "target": str(target), "source": old_link})
            elif target.exists():
                shutil.move(str(target), str(backup))
                rollback_steps.append({"kind": "directory", "target": str(target), "source": str(backup)})
            else:
                rollback_steps.append({"kind": "missing", "target": str(target), "source": ""})
            os.symlink(canonical_source, target, target_is_directory=True)
            applied_targets.append({"label": label, "target": str(target), "source": str(canonical_source)})
    except Exception as exc:
        rollback_errors: list[str] = []
        for step in reversed(rollback_steps):
            target = Path(step["target"])
            try:
                if target.is_symlink():
                    target.unlink()
                elif target.exists():
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
                if step["kind"] == "directory":
                    shutil.move(step["source"], str(target))
                elif step["kind"] == "symlink":
                    os.symlink(step["source"], target, target_is_directory=True)
            except Exception as rollback_exc:
                rollback_errors.append(f"{step['target']}:{type(rollback_exc).__name__}:{rollback_exc}")
        return {
            **plan,
            "ok": False,
            "error": "install_failed_rolled_back" if not rollback_errors else "install_failed_rollback_incomplete",
            "detail": str(exc),
            "backup_root": str(backup_root),
            "applied_targets": applied_targets,
            "rollback_succeeded": not rollback_errors,
            "rollback_errors": rollback_errors,
        }
    receipt = {
        "schema_version": "xiaoyou_release_install_receipt_v1",
        "release_id": release_id,
        "source_commit": plan.get("source_commit"),
        "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "backup_root": str(backup_root),
        "targets": applied_targets,
    }
    receipt_path = canonical_release / "install_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**plan, "ok": True, "applied": True, "backup_root": str(backup_root), "install_receipt": str(receipt_path)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan/stage a Xiaoyou release or apply canonical active links."
        )
    )
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help=(
            "Stage the verified release under xiaoyou-releases without "
            "mutating active runtime/plugin/script links."
        ),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            stage_release(
                release_root=args.release_root,
                base=args.base,
                apply=args.apply,
            )
            if args.stage_only
            else install_release(
                release_root=args.release_root,
                base=args.base,
                apply=args.apply,
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        result = {"ok": False, "error": str(exc), "applied": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
