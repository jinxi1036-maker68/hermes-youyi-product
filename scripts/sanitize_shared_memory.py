"""Replace person-specific shared Hermes memory with a neutral contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path


NEUTRAL_MEMORY = """# Shared Memory Boundary

- The current person's identity, role, name, permissions, preferences, and tasks come only from the trusted Enterprise WeChat session and tenant business tools.
- Shared memory must not contain any named boss, manager, teacher, student, personal preference, private task habit, provider credential, or model endpoint.
- Person-specific work styles belong in the identity-scoped workstyle ledger. Institution facts belong in audited tenant ledgers.
- Employee manuals and Skills provide stable methods and judgment frameworks; they never override current identity, permissions, verified facts, or writeback results.
- No tool result means no claim that data was checked. No verified writeback means no claim that data was saved. No delivery receipt means no claim that a message was sent.
"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize(memory_file: Path, archive_dir: Path, *, apply: bool) -> dict[str, object]:
    original = memory_file.read_bytes() if memory_file.exists() else b""
    replacement = NEUTRAL_MEMORY.encode("utf-8")
    result: dict[str, object] = {
        "ok": True,
        "apply": apply,
        "memory_file": str(memory_file),
        "original_sha256": _sha256(original),
        "replacement_sha256": _sha256(replacement),
        "changed": original != replacement,
        "backup_file": "",
        "writeback_verified": False,
    }
    if not apply or original == replacement:
        result["writeback_verified"] = original == replacement
        return result

    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    backup = archive_dir / f"MEMORY.before-neutralization.{stamp}.md"
    backup.write_bytes(original)
    os.chmod(archive_dir, 0o700)
    os.chmod(backup, 0o600)

    temporary = memory_file.with_name(f".{memory_file.name}.{os.getpid()}.tmp")
    temporary.write_bytes(replacement)
    os.replace(temporary, memory_file)
    verified = memory_file.read_bytes() == replacement
    result.update({
        "backup_file": str(backup),
        "writeback_verified": verified,
        "ok": verified,
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-file", required=True, type=Path)
    parser.add_argument("--archive-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = sanitize(args.memory_file, args.archive_dir, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
