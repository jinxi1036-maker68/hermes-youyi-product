from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from plugins.tuoguan_core.repair_task_companion_state import repair_duplicate_task_pair  # noqa: E402
from plugins.tuoguan_core.store import TuoguanStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge trusted evidence into the original task and supersede one proven duplicate.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--primary-task-id", required=True)
    parser.add_argument("--duplicate-task-id", required=True)
    parser.add_argument("--evidence-text", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = repair_duplicate_task_pair(
        TuoguanStore(Path(args.data_dir)),
        primary_task_id=args.primary_task_id,
        duplicate_task_id=args.duplicate_task_id,
        evidence_text=args.evidence_text,
        apply=bool(args.apply),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
