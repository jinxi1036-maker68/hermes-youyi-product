#!/usr/bin/env python3
"""Render deterministic human projections from Knowledge Harness machine truth."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def render_current_work(work: dict) -> str:
    stage = work.get("formal_stage_context") or {}
    lines = [
        "# CURRENT WORK｜自动生成投影",
        "",
        "> **GENERATED — DO NOT EDIT.** 机器权威：`knowledge/CURRENT_WORK.json`。",
        "",
        f"- work_item_id: `{work.get('work_item_id','')}`",
        f"- kind: `{work.get('kind','')}`",
        f"- status: `{work.get('status','')}`",
        f"- context_profile: `{work.get('context_profile','')}`",
        f"- formal_stage_context: Stage {stage.get('id','')} {stage.get('name','')} / {stage.get('execution_state','')}",
        "",
        "## Objective",
        "",
        str(work.get("objective") or ""),
        "",
        "## Current blocker",
        "",
        str(work.get("blocker") or ""),
        "",
        "## Next action",
        "",
        str(work.get("next_action") or ""),
        "",
        "## Acceptance criteria",
        "",
    ]
    lines.extend(f"- {item}" for item in (work.get("acceptance_criteria") or []))
    lines.extend(["", "## Stop rules", ""])
    lines.extend(f"- {item}" for item in (work.get("stop_rules") or []))
    lines.extend(["", "## Context routing", "", f"Profile: `{work.get('context_profile','')}`", "", "Must read:"])
    lines.extend(f"- `{item}`" for item in (work.get("must_read") or []))
    lines.extend(["", "On demand:"])
    lines.extend(f"- `{item}`" for item in (work.get("on_demand") or []))
    lines.append("")
    return "\n".join(lines)


def render_current_state(index: dict, work: dict) -> str:
    current = index.get("current") or {}
    stage = current.get("formal_stage") or {}
    harness = index.get("harness") or {}
    lines = [
        "# 当前状态｜自动生成投影",
        "",
        "> **GENERATED — DO NOT EDIT.** 动态事实唯一权威：`knowledge/PROJECT_INDEX.json`；当前工作权威：`knowledge/CURRENT_WORK.json`。",
        "",
        "## Formal Stage",
        "",
        f"- Stage: {stage.get('id','')} — {stage.get('name','')}",
        f"- Status: `{stage.get('status','')}`",
        f"- Sealed: `{str(bool(stage.get('sealed'))).lower()}`",
        "",
        "## Current Work",
        "",
        f"- ID: `{work.get('work_item_id','')}`",
        f"- Kind: `{work.get('kind','')}`",
        f"- Status: `{work.get('status','')}`",
        f"- Objective: {work.get('objective','')}",
        f"- Blocker: {work.get('blocker','')}",
        f"- Next: {work.get('next_action','')}",
        "",
        "## Runtime facts",
        "",
        f"- main: `{current.get('main_sha','')}`",
        f"- production: `{current.get('production_sha','')}`",
        "",
        "## Sealed capabilities",
        "",
    ]
    for capability in index.get("sealed_capabilities") or []:
        lines.append(
            f"- Stage {capability.get('id','')} {capability.get('name','')}: "
            f"{str(capability.get('status','')).upper()} — evidence `{capability.get('evidence_id','')}`"
        )
    lines.extend([
        "",
        "## Context routing",
        "",
        f"- Harness: `{harness.get('version','')}`",
        f"- Profile: `{work.get('context_profile','')}`",
        f"- Work execution state: `{(work.get('formal_stage_context') or {}).get('execution_state','')}`",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    index = _load(root / "knowledge/PROJECT_INDEX.json")
    work = _load(root / "knowledge/CURRENT_WORK.json")
    expected = {
        root / "knowledge/05_CURRENT_STATE.md": render_current_state(index, work),
        root / "knowledge/CURRENT_WORK.md": render_current_work(work),
    }

    if args.write:
        for path, content in expected.items():
            path.write_text(content, encoding="utf-8")
        return 0

    mismatches = [
        str(path.relative_to(root))
        for path, content in expected.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    ]
    if mismatches:
        print(json.dumps({"ok": False, "projection_drift": mismatches}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "projections": [str(path.relative_to(root)) for path in expected]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
