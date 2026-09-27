#!/usr/bin/env python3
"""Knowledge Harness V3 consistency, context-health and public-safety checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from render_current import render_current_state, render_current_work

SHA40 = re.compile(r"^[0-9a-f]{40}$")
INLINE_SHA40 = re.compile(r"\b[0-9a-f]{40}\b")

REQUIRED = [
    "knowledge/00_START_HERE.md",
    "knowledge/KNOWLEDGE_HARNESS.json",
    "knowledge/PROJECT_INDEX.json",
    "knowledge/CURRENT_WORK.json",
    "knowledge/05_CURRENT_STATE.md",
    "knowledge/CURRENT_WORK.md",
    "knowledge/ACTIVE_WORK.md",
    "knowledge/EVIDENCE_INDEX.json",
    "knowledge/FRESHNESS_PROTOCOL.md",
    "knowledge/WORKING_METHOD.json",
    "knowledge/CURRENT_WORKING_METHOD.md",
    "knowledge/CONCURRENCY_AND_RECOVERY.md",
    "knowledge/README_PUBLIC_SAFETY.md",
    "knowledge/04_CAPABILITY_MAP.md",
    "knowledge/DOMAIN_MODEL.md",
    "knowledge/REJECTED_APPROACHES.md",
    "knowledge/14_UPDATE_PROTOCOL.md",
    "knowledge/schema/project_index.schema.json",
    "knowledge/capabilities/01_identity_session.md",
    "knowledge/capabilities/02_query.md",
    "knowledge/decisions/ADR-010-knowledge-harness-v3.md",
]

FORBIDDEN_SUFFIXES = {".env", ".pem", ".key", ".p12", ".pfx"}
FORBIDDEN_FILENAMES = {".env", "id_rsa", "id_ed25519"}
SECRET_PATTERNS = [
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("credential_assignment", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}['\"]?")),
]


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def load_json(path: Path, label: str, errors: list[str]) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(errors, f"{label}_invalid_json:{type(exc).__name__}")
        return {}


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    errors: list[str] = []

    top = sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
    unexpected = [name for name in top if name not in {"README.md", "knowledge"}]
    if unexpected:
        fail(errors, f"non_knowledge_top_level_entries:{unexpected}")

    for rel in REQUIRED:
        if not (root / rel).is_file():
            fail(errors, f"missing_required_file:{rel}")

    index = load_json(root / "knowledge/PROJECT_INDEX.json", "project_index", errors)
    work = load_json(root / "knowledge/CURRENT_WORK.json", "current_work", errors)
    harness = load_json(root / "knowledge/KNOWLEDGE_HARNESS.json", "knowledge_harness", errors)
    evidence = load_json(root / "knowledge/EVIDENCE_INDEX.json", "evidence_index", errors)
    method = load_json(root / "knowledge/WORKING_METHOD.json", "working_method", errors)

    if index.get("schema_version") != "xiaoyou_project_knowledge_v2":
        fail(errors, "wrong_schema_version")
    if int(index.get("knowledge_revision") or 0) < 6:
        fail(errors, "knowledge_revision_before_harness_v3")
    if harness.get("schema_version") != "xiaoyou_knowledge_harness_v3":
        fail(errors, "wrong_harness_schema")
    if work.get("schema_version") != "xiaoyou_current_work_v1":
        fail(errors, "wrong_current_work_schema")

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

    active_id = str(current.get("active_work_item_id") or "")
    if active_id != str(work.get("work_item_id") or ""):
        fail(errors, f"current_work_item_mismatch:index={active_id}:work={work.get('work_item_id')}")

    work_stage = work.get("formal_stage_context") or {}
    if work_stage.get("id") != stage.get("id") or work_stage.get("name") != stage.get("name"):
        fail(errors, "current_work_stage_context_mismatch")

    for forbidden in ("history", "timeline", "debug_log", "completed_steps"):
        if forbidden in work:
            fail(errors, f"current_work_forbidden_history_field:{forbidden}")

    profiles = harness.get("profiles") or {}
    profile_name = str(work.get("context_profile") or "")
    if profile_name not in profiles:
        fail(errors, f"unknown_current_work_profile:{profile_name}")

    machine_order = list((harness.get("bootstrap") or {}).get("machine_read_order") or [])
    if machine_order != ["knowledge/PROJECT_INDEX.json", "knowledge/CURRENT_WORK.json"]:
        fail(errors, f"bootstrap_machine_order_invalid:{machine_order}")
    if int((harness.get("bootstrap") or {}).get("max_machine_files_before_profile") or 0) > 2:
        fail(errors, "bootstrap_preprofile_file_count_too_high")

    def verify_path(rel: str, label: str) -> None:
        if not (root / rel).is_file():
            fail(errors, f"{label}_missing:{rel}")

    for name, profile in profiles.items():
        required = list((profile or {}).get("required") or [])
        max_files = int((profile or {}).get("max_context_files") or 0)
        if max_files <= 0:
            fail(errors, f"profile_missing_budget:{name}")
        for rel in required:
            verify_path(str(rel), f"profile_{name}_required")
            if name != "audit" and (str(rel).startswith("knowledge/archive/") or str(rel) == "knowledge/12_HISTORY_AND_MILESTONES.md"):
                fail(errors, f"profile_{name}_loads_history_by_default:{rel}")
        if name == "orientation" and required:
            fail(errors, "orientation_profile_must_not_add_bootstrap_files")

    additive = list(work.get("must_read") or [])
    on_demand = list(work.get("on_demand") or [])
    for rel in additive:
        verify_path(str(rel), "current_work_must_read")
    for rel in on_demand:
        verify_path(str(rel), "current_work_on_demand")

    if profile_name in profiles:
        count = len(list((profiles[profile_name] or {}).get("required") or [])) + len(additive)
        if count > int((profiles[profile_name] or {}).get("max_context_files") or 0):
            fail(errors, f"current_context_budget_exceeded:{count}")

    authority = index.get("authority") or {}
    expected_authority = {
        "dynamic_current_state": "knowledge/PROJECT_INDEX.json",
        "current_work": "knowledge/CURRENT_WORK.json",
        "human_current_state": "knowledge/05_CURRENT_STATE.md",
        "human_current_work": "knowledge/CURRENT_WORK.md",
        "active_work_compat": "knowledge/ACTIVE_WORK.md",
        "knowledge_harness": "knowledge/KNOWLEDGE_HARNESS.json",
    }
    for key, value in expected_authority.items():
        if authority.get(key) != value:
            fail(errors, f"authority_path_mismatch:{key}:{authority.get(key)}")

    if (root / "knowledge/05_CURRENT_STATE.md").read_text(encoding="utf-8") != render_current_state(index, work):
        fail(errors, "current_state_projection_drift")
    if (root / "knowledge/CURRENT_WORK.md").read_text(encoding="utf-8") != render_current_work(work):
        fail(errors, "current_work_projection_drift")

    active_path = root / "knowledge/ACTIVE_WORK.md"
    if active_path.is_file():
        body = active_path.read_text(encoding="utf-8")
        if "DEPRECATED_COMPAT_POINTER" not in body:
            fail(errors, "active_work_not_deprecated_pointer")
        if len(body.encode("utf-8")) > 1024:
            fail(errors, "active_work_compat_too_large")

    for rel, budget in (harness.get("budgets_bytes") or {}).items():
        path = root / str(rel)
        if not path.is_file():
            fail(errors, f"budgeted_file_missing:{rel}")
        elif path.stat().st_size > int(budget):
            fail(errors, f"context_budget_exceeded:{rel}:{path.stat().st_size}>{budget}")

    rows = evidence.get("evidence") or []
    evidence_ids = [str(row.get("id")) for row in rows if row.get("id")]
    if len(evidence_ids) != len(set(evidence_ids)):
        fail(errors, "duplicate_evidence_ids")
    evidence_set = set(evidence_ids)
    for capability in index.get("sealed_capabilities") or []:
        evidence_id = str(capability.get("evidence_id") or "")
        if not evidence_id or evidence_id not in evidence_set:
            fail(errors, f"sealed_capability_missing_evidence:{capability.get('id')}")

    if method.get("schema_version") != "xiaoyou_working_method_v1":
        fail(errors, "wrong_working_method_schema")
    method_id = str(method.get("current_method_id") or "")
    if str(current.get("working_method_id") or "") != method_id:
        fail(errors, "working_method_id_mismatch")
    history = [str(x) for x in (method.get("history") or [])]
    if not history:
        fail(errors, "working_method_history_empty")
    for rel in history:
        verify_path(rel, "working_method_history")
    if history and method_id not in Path(history[-1]).name:
        fail(errors, "current_working_method_not_latest_history_entry")

    governance = index.get("governance") or {}
    if governance.get("force_push_allowed") is not False:
        fail(errors, "knowledge_force_push_not_forbidden")
    if governance.get("knowledge_write_mode") != "fast_forward_only_optimistic_concurrency":
        fail(errors, "knowledge_concurrency_mode_invalid")
    if governance.get("projection_policy") != "generated_from_machine_truth_not_hand_edited":
        fail(errors, "projection_policy_invalid")
    if governance.get("current_work_history_policy") != "archive_on_close_never_append_history":
        fail(errors, "current_work_history_policy_invalid")

    for rel in ("knowledge/00_START_HERE.md", "knowledge/handoff/NEXT_WINDOW_BOOTSTRAP.md"):
        path = root / rel
        if path.is_file() and INLINE_SHA40.search(path.read_text(encoding="utf-8")):
            fail(errors, f"dynamic_sha_hardcoded_in_static_entry:{rel}")

    validator_rel = Path("knowledge/tools/validate_knowledge.py")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if path.name in FORBIDDEN_FILENAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            fail(errors, f"forbidden_sensitive_filename:{rel}")
        if rel == validator_rel or path.suffix.lower() not in {".md", ".json", ".yaml", ".yml", ".txt", ".py"}:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(body):
                fail(errors, f"possible_secret_content:{label}:{rel}")

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
        "harness_id": harness.get("harness_id"),
        "knowledge_revision": index.get("knowledge_revision"),
        "formal_stage": stage,
        "active_work_item_id": active_id,
        "context_profile": profile_name,
        "bootstrap_machine_files": machine_order,
        "sealed_evidence_count": len(index.get("sealed_capabilities") or []),
        "live_main_checked": bool(live_main),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
