#!/usr/bin/env python3
"""Static consistency checks for XiaoYou Project Knowledge V2.

This validator checks internal knowledge consistency only. It does not replace
live GitHub-main or production freshness verification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

SHA40 = re.compile(r"^[0-9a-f]{40}$")
INLINE_SHA40 = re.compile(r"\b[0-9a-f]{40}\b")

REQUIRED = [
    "knowledge/00_START_HERE.md",
    "knowledge/PROJECT_INDEX.json",
    "knowledge/05_CURRENT_STATE.md",
    "knowledge/ACTIVE_WORK.md",
    "knowledge/EVIDENCE_INDEX.json",
    "knowledge/FRESHNESS_PROTOCOL.md",
    "knowledge/04_CAPABILITY_MAP.md",
    "knowledge/DOMAIN_MODEL.md",
    "knowledge/REJECTED_APPROACHES.md",
    "knowledge/14_UPDATE_PROTOCOL.md",
    "knowledge/schema/project_index.schema.json",
    "knowledge/capabilities/01_identity_session.md",
    "knowledge/capabilities/02_query.md",
]

FORBIDDEN_SUFFIXES = {".env", ".pem", ".key", ".p12", ".pfx"}
FORBIDDEN_FILENAMES = {".env", "id_rsa", "id_ed25519"}


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def main() -> int:
    script = Path(__file__).resolve()
    root = script.parents[2]
    errors: list[str] = []

    # Pure current-tree rule.
    top = sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
    unexpected = [name for name in top if name not in {"README.md", "knowledge"}]
    if unexpected:
        fail(errors, f"non_knowledge_top_level_entries:{unexpected}")

    for rel in REQUIRED:
        if not (root / rel).is_file():
            fail(errors, f"missing_required_file:{rel}")

    index_path = root / "knowledge/PROJECT_INDEX.json"
    evidence_path = root / "knowledge/EVIDENCE_INDEX.json"
    if not index_path.is_file() or not evidence_path.is_file():
        for error in errors:
            print(error)
        return 1

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(errors, f"project_index_invalid_json:{type(exc).__name__}")
        index = {}

    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(errors, f"evidence_index_invalid_json:{type(exc).__name__}")
        evidence = {}

    if index.get("schema_version") != "xiaoyou_project_knowledge_v2":
        fail(errors, "wrong_schema_version")

    current = index.get("current") or {}
    for field in ("main_sha", "production_sha"):
        value = str(current.get(field) or "")
        if not SHA40.fullmatch(value):
            fail(errors, f"invalid_{field}:{value}")

    stage = current.get("formal_stage") or {}
    roadmap = index.get("roadmap") or []
    matches = [row for row in roadmap if row.get("id") == stage.get("id")]
    if len(matches) != 1:
        fail(errors, "current_stage_not_unique_in_roadmap")
    elif matches[0].get("name") != stage.get("name"):
        fail(errors, "current_stage_name_mismatch")

    active_work = (root / "knowledge/ACTIVE_WORK.md").read_text(encoding="utf-8")
    active_id = str(current.get("active_work_item_id") or "")
    if not active_id or active_id not in active_work:
        fail(errors, "active_work_item_mismatch")

    evidence_ids = {
        str(row.get("id"))
        for row in (evidence.get("evidence") or [])
        if row.get("id")
    }
    for capability in index.get("sealed_capabilities") or []:
        evidence_id = str(capability.get("evidence_id") or "")
        if not evidence_id or evidence_id not in evidence_ids:
            fail(errors, f"sealed_capability_missing_evidence:{capability.get('id')}")

    # START/Handoff must not freeze current SHAs.
    for rel in (
        "knowledge/00_START_HERE.md",
        "knowledge/handoff/NEXT_WINDOW_BOOTSTRAP.md",
    ):
        path = root / rel
        if path.is_file() and INLINE_SHA40.search(path.read_text(encoding="utf-8")):
            fail(errors, f"dynamic_sha_hardcoded_in_static_entry:{rel}")

    current_state = root / "knowledge/05_CURRENT_STATE.md"
    if current_state.is_file():
        content = current_state.read_text(encoding="utf-8")
        if "PROJECT_INDEX.json" not in content or "动态事实唯一权威" not in content:
            fail(errors, "current_state_missing_canonical_authority_notice")

    # Obvious sensitive-file guard.
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in FORBIDDEN_FILENAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            fail(errors, f"forbidden_sensitive_filename:{path.relative_to(root)}")

    # Optional live-main freshness check supplied by the executor.
    live_main = str(os.getenv("XIAOYOU_LIVE_MAIN_SHA") or "").strip().lower()
    if live_main:
        if not SHA40.fullmatch(live_main):
            fail(errors, "invalid_XIAOYOU_LIVE_MAIN_SHA")
        elif live_main != str(current.get("main_sha") or "").lower():
            fail(errors, f"stale_main_sha:index={current.get('main_sha')}:live={live_main}")

    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({
        "ok": True,
        "schema_version": index.get("schema_version"),
        "knowledge_revision": index.get("knowledge_revision"),
        "active_work_item_id": active_id,
        "sealed_evidence_count": len(index.get("sealed_capabilities") or []),
        "evidence_count": len(evidence_ids),
        "live_main_checked": bool(live_main),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
