"""Correct one verified mis-scoped institution-discussion preference.

The original owner feedback remains in the append-only preference ledger. The
repair only marks its scope mismatch and records the equivalent low-risk
preference under ``institution_work`` so routine conversation is unaffected.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from plugins.tuoguan_core.models import UserIdentity  # noqa: E402
from plugins.tuoguan_core.store import TuoguanStore  # noqa: E402
from plugins.tuoguan_core.workstyle_profiles import (  # noqa: E402
    WORKSTYLE_EVENTS_FILE,
    _read_events,
    query_person_workstyle_profile,
    submit_person_workstyle_preference,
)
from plugins.tuoguan_core.write_guard import authorized_system_write  # noqa: E402


SOURCE_PREFERENCE_ID = "workstyle_pref_institution_pacing_2026-08-24T2130050800"
REPAIRED_RULE = "制度讨论一次只推进一个章节和一个关键问题。"


def repair_institution_workstyle_scope(store: TuoguanStore, *, apply: bool = False) -> dict[str, Any]:
    events = _read_events(store)
    source = next(
        (
            row for row in events
            if str(row.get("record_type") or "") == "person_workstyle_preference"
            and str(row.get("preference_id") or "") == SOURCE_PREFERENCE_ID
        ),
        None,
    )
    mismatch_exists = any(
        str(row.get("record_type") or "") == "person_workstyle_semantic_mismatch"
        and str(row.get("preference_id") or "") == SOURCE_PREFERENCE_ID
        and str(row.get("status") or "") == "superseded"
        for row in events
    )
    result: dict[str, Any] = {
        "ok": bool(source),
        "dry_run": not apply,
        "source_preference_found": bool(source),
        "source_preference_id": SOURCE_PREFERENCE_ID,
        "mismatch_exists": mismatch_exists,
        "target_scope": "institution_work",
        "messages_sent": False,
        "business_rules_changed": False,
        "writeback_verified": False,
    }
    if not source or not apply:
        result["preview_verified"] = bool(source)
        return result

    identity = UserIdentity(
        platform="system",
        platform_user_id=str(source.get("target_user_id") or ""),
        canonical_user_id=str(source.get("target_user_id") or ""),
        person_name=str(source.get("target_name") or "老板"),
        role=str(source.get("target_role") or "boss"),
        approval_state="approved",
    )
    with authorized_system_write(
        store.data_dir,
        job_name="repair_institution_workstyle_scope_v1",
        allowed_files={WORKSTYLE_EVENTS_FILE},
    ):
        if not mismatch_exists:
            store.append_jsonl_verified(WORKSTYLE_EVENTS_FILE, {
                "record_type": "person_workstyle_semantic_mismatch",
                "preference_id": SOURCE_PREFERENCE_ID,
                "status": "superseded",
                "reason": "institution_discussion_pacing_was_mis_scoped_as_all_communication",
                "replacement_scope": "institution_work",
                "source_preference_id": SOURCE_PREFERENCE_ID,
            })
        saved = submit_person_workstyle_preference(
            store,
            identity=identity,
            preference_type="other_low_risk",
            scope="institution_work",
            preference_text=REPAIRED_RULE,
            normalized_rule=REPAIRED_RULE,
            # The original source remains linked through the semantic-mismatch
            # event. Replaying policy wording would correctly trip the
            # low-risk guard, so the replacement records only its safe rule.
            source_text=REPAIRED_RULE,
            dimension_key="interaction_pacing",
            confidence=1.0,
            source_turn_id=str(((source.get("source") or {}).get("source_turn_id") or "")),
            operation_id="repair_institution_workstyle_scope_v1",
        )
    direct = query_person_workstyle_profile(store, identity=identity, scope="direct_reply")
    institution = query_person_workstyle_profile(store, identity=identity, scope="institution_work")
    result.update({
        "ok": bool(saved.get("ok")),
        "dry_run": False,
        "replacement_preference_id": str((saved.get("preference") or {}).get("preference_id") or ""),
        "direct_reply_has_pacing": "interaction_pacing" in set(direct.get("applied_dimensions") or []),
        "institution_work_has_pacing": "interaction_pacing" in set(institution.get("applied_dimensions") or []),
        "writeback_verified": bool(saved.get("writeback_verified"))
        and "interaction_pacing" not in set(direct.get("applied_dimensions") or [])
        and "interaction_pacing" in set(institution.get("applied_dimensions") or []),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair a verified institution-work preference scope.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = repair_institution_workstyle_scope(TuoguanStore(Path(args.data_dir)), apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
