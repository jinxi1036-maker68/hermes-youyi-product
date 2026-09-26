#!/usr/bin/env python3
"""Static consistency and high-confidence leak checks for XiaoYou Project Knowledge.

This does not replace live GitHub-main or production freshness verification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

SHA40 = re.compile(r"^[0-9a-f]{40}$")
INLINE_SHA40 = re.compile(r"\b[0-9a-f]{40}\b")

REQUIRED = [
    "knowledge/00_START_HERE.md",
    "knowledge/PROJECT_INDEX.json",
    "knowledge/05_CURRENT_STATE.md",
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
]

FORBIDDEN_SUFFIXES = {".env", ".pem", ".key", ".p12", ".pfx"}
FORBIDDEN_FILENAMES = {".env", "id_rsa", "id_ed25519"}

# Deliberately high-confidence. This is a guardrail, not a full DLP engine.
SECRET_PATTERNS = [
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}['\"]?"
        ),
    ),
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
    script = Path(__file__).resolve()
    root = script.parents[2]
    errors: list[str] = []

    top = sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
    unexpected = [name for name in top if name not in {"README.md", "knowledge"}]
    if unexpected:
        fail(errors, f"non_knowledge_top_level_entries:{unexpected}")

    for rel in REQUIRED:
        if not (root / rel).is_file():
            fail(errors, f"missing_required_file:{rel}")

    index = load_json(root / "knowledge/PROJECT_INDEX.json", "project_index", errors)
    evidence = load_json(root / "knowledge/EVIDENCE_INDEX.json", "evidence_index", errors)
    method = load_json(root / "knowledge/WORKING_METHOD.json", "working_method", errors)

    if index.get("schema_version") != "xiaoyou_project_knowledge_v2":
        fail(errors, "wrong_schema_version")
    if int(index.get("knowledge_revision") or 0) < 4:
        fail(errors, "knowledge_revision_missing_adversarial_hardening")

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
    active_path = root / "knowledge/ACTIVE_WORK.md"
    if active_path.is_file() and active_id not in active_path.read_text(encoding="utf-8"):
        fail(errors, "active_work_item_mismatch")

    rows = evidence.get("evidence") or []
    evidence_ids = [str(row.get("id")) for row in rows if row.get("id")]
    if len(evidence_ids) != len(set(evidence_ids)):
        fail(errors, "duplicate_evidence_ids")
    evidence_set = set(evidence_ids)
    for capability in index.get("sealed_capabilities") or []:
        evidence_id = str(capability.get("evidence_id") or "")
        if not evidence_id or evidence_id not in evidence_set:
            fail(errors, f"sealed_capability_missing_evidence:{capability.get('id')}")

    # Working Method lineage/governance.
    if method.get("schema_version") != "xiaoyou_working_method_v1":
        fail(errors, "wrong_working_method_schema")
    index_method_id = str(current.get("working_method_id") or "")
    method_id = str(method.get("current_method_id") or "")
    if not index_method_id or index_method_id != method_id:
        fail(errors, f"working_method_id_mismatch:index={index_method_id}:method={method_id}")

    history = [str(x) for x in (method.get("history") or [])]
    if not history:
        fail(errors, "working_method_history_empty")
    for rel in history:
        if not (root / rel).is_file():
            fail(errors, f"missing_working_method_history:{rel}")
    if history and method_id not in Path(history[-1]).name:
        fail(errors, "current_working_method_not_latest_history_entry")

    governance = method.get("method_change_governance") or {}
    if not governance.get("class_b_material"):
        fail(errors, "working_method_missing_material_change_governance")
    if governance.get("no_silent_change") is not True:
        fail(errors, "working_method_allows_silent_change")

    projection = root / "knowledge/CURRENT_WORKING_METHOD.md"
    if projection.is_file() and method_id not in projection.read_text(encoding="utf-8"):
        fail(errors, "current_working_method_projection_mismatch")

    project_governance = index.get("governance") or {}
    if project_governance.get("force_push_allowed") is not False:
        fail(errors, "knowledge_force_push_not_forbidden")
    if project_governance.get("knowledge_write_mode") != "fast_forward_only_optimistic_concurrency":
        fail(errors, "knowledge_concurrency_mode_invalid")

    # Static entry points must not freeze current SHAs.
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

    public_safety = root / "knowledge/README_PUBLIC_SAFETY.md"
    if public_safety.is_file():
        text = public_safety.read_text(encoding="utf-8")
        if "PROJECT_INDEX.json" not in text or "public-safe" not in text:
            fail(errors, "public_safety_doc_stale")

    concurrency = root / "knowledge/CONCURRENCY_AND_RECOVERY.md"
    if concurrency.is_file():
        text = concurrency.read_text(encoding="utf-8")
        if "force=false" not in text or "禁止 force push" not in text:
            fail(errors, "knowledge_concurrency_protocol_incomplete")

    # Sensitive filenames + high-confidence content signatures.
    validator_rel = Path("knowledge/tools/validate_knowledge.py")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if path.name in FORBIDDEN_FILENAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            fail(errors, f"forbidden_sensitive_filename:{rel}")
        if rel == validator_rel:
            continue
        if path.suffix.lower() not in {".md", ".json", ".yaml", ".yml", ".txt", ".py"}:
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
        "schema_version": index.get("schema_version"),
        "knowledge_revision": index.get("knowledge_revision"),
        "active_work_item_id": active_id,
        "working_method_id": method_id,
        "working_method_history_count": len(history),
        "sealed_evidence_count": len(index.get("sealed_capabilities") or []),
        "evidence_count": len(evidence_set),
        "force_push_allowed": project_governance.get("force_push_allowed"),
        "live_main_checked": bool(live_main),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
