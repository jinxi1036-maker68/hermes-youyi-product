from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from plugins.tuoguan_core.runtime_governance import module_inventory, state_ownership
from plugins.tuoguan_core.write_guard import PROTECTED_BUSINESS_FILES


def audit() -> dict[str, object]:
    plugin_root = RUNTIME / "plugins" / "tuoguan_core"
    modules = module_inventory(plugin_root)
    states = state_ownership(PROTECTED_BUSINESS_FILES)
    errors: list[str] = []
    if modules["unclassified"]:
        errors.append("unclassified_modules")
    if modules["missing_from_disk"]:
        errors.append("manifest_modules_missing_from_disk")
    if modules["duplicates"]:
        errors.append("duplicate_module_classification")
    if states["unowned"]:
        errors.append("unowned_protected_state")
    return {
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "module_inventory": modules,
        "state_ownership": states,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Xiaoyou module and state ownership.")
    parser.parse_args()
    report = audit()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

