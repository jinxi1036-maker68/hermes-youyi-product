"""Install the versioned Xiaoyou Skill Bundle into a Hermes home."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def run(*, hermes_home: Path, project_root: Path, apply: bool) -> dict:
    assets = project_root / "runtime" / "plugins" / "tuoguan_core" / "skill_assets"
    pairs = [
        (assets / "skills" / "xiaoyou-core-contract" / "SKILL.md", hermes_home / "skills" / "xiaoyou-core-contract" / "SKILL.md"),
        (assets / "skill-bundles" / "xiaoyou-core.yaml", hermes_home / "skill-bundles" / "xiaoyou-core.yaml"),
    ]
    missing = [str(source) for source, _target in pairs if not source.exists()]
    report = {
        "ok": not missing,
        "mode": "apply" if apply else "dry_run",
        "hermes_home": str(hermes_home),
        "missing_sources": missing,
        "files": [{"source": str(source), "target": str(target), "sha256": _sha256(source) if source.exists() else ""} for source, target in pairs],
        "writeback_verified": False,
    }
    if missing or not apply:
        return report
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    backups: list[str] = []
    for source, target in pairs:
        if target.exists():
            backup = target.with_name(f"{target.name}.pre-xiaoyou-core-{stamp}.bak")
            shutil.copy2(target, backup)
            backups.append(str(backup))
        _atomic_copy(source, target)
        if _sha256(source) != _sha256(target):
            raise RuntimeError(f"skill asset writeback verification failed: {target}")
    report.update({"writeback_verified": True, "backups": backups})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = run(hermes_home=Path(args.hermes_home), project_root=Path(args.project_root), apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
