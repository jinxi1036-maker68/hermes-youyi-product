from __future__ import annotations

import json
import os
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
CAPABILITY_ROOT = HERE.parents[1]
RUNTIME_ROOT = os.environ.get("XIAOYOU_TEST_HERMES_RUNTIME_ROOT")
WORKSPACE_ROOT = os.environ.get("XIAOYOU_TEST_WORKSPACE")
if not RUNTIME_ROOT or not WORKSPACE_ROOT:
    _skip_message = "set XIAOYOU_TEST_HERMES_RUNTIME_ROOT and XIAOYOU_TEST_WORKSPACE to a disposable fixture"
    if __name__ == "__main__":
        print(f"SKIP production usability recipe: {_skip_message}")
        raise SystemExit(0)
    import pytest
    pytest.skip(_skip_message, allow_module_level=True)

APP = Path(RUNTIME_ROOT).resolve()
WORKSPACE = Path(WORKSPACE_ROOT).resolve()
sys.path.insert(0, str(APP))
sys.path.insert(0, str(CAPABILITY_ROOT / "plugins"))
os.environ.setdefault("HERMES_HOME", str(APP / "home"))
os.environ.setdefault("XIAOYOU_INSTITUTION_WORKSPACE", str(WORKSPACE))
os.environ.setdefault("HERMES_TUOGUAN_DATA_DIR", str(WORKSPACE / "data"))
os.environ.setdefault("HERMES_TENANT_ID", "example_institution")

OWNER_USER_ID = os.environ.get("XIAOYOU_TEST_OWNER_ID", "owner-test")
OWNER_DISPLAY_NAME = os.environ.get("XIAOYOU_TEST_OWNER_NAME", "机构负责人")
HISTORICAL_TEACHER_ID = os.environ.get("XIAOYOU_TEST_HISTORICAL_TEACHER_ID", "teacher-historical")
HISTORICAL_TEACHER_ALIAS = os.environ.get("XIAOYOU_TEST_HISTORICAL_TEACHER_ALIAS", "历史测试老师")
UNCLAIMED_TEACHER_ALIAS = os.environ.get("XIAOYOU_TEST_UNCLAIMED_TEACHER_ALIAS", "unclaimed.teacher")
ACTIVE_TEACHER_NAME = os.environ.get("XIAOYOU_TEST_ACTIVE_TEACHER_NAME", "示例老师")

from tuoguan_core.identity import IdentityService
from tuoguan_core.proactive_work import _staff_is_active, _staff_role
from tuoguan_core.runtime_foundation import (
    _redact_internal_observability_fields,
    compact_tool_result_for_model,
)
from tuoguan_core.staff_directory import query_staff_directory
from tuoguan_core.store import TuoguanStore
from tuoguan_core.tool_service import TuoguanToolService

import plugins.platforms as _platforms_package
_platforms_package.__path__.append(str(CAPABILITY_ROOT / "plugins" / "platforms"))
import plugins.platforms.wecom as _wecom_package
_wecom_package.__path__.insert(0, str(CAPABILITY_ROOT / "plugins" / "platforms" / "wecom"))
from plugins.platforms.wecom.callback_adapter import _is_hermes_session_reset_notice


def check(value: bool, label: str) -> None:
    if not value:
        raise AssertionError(label)
    print("PASS", label)


store = TuoguanStore(WORKSPACE / "data")
identity = IdentityService(store).resolve("wecom_callback", OWNER_USER_ID, user_name=OWNER_DISPLAY_NAME)
check(identity.person_name == OWNER_DISPLAY_NAME, "fresh session restores business display name")
check(identity.role == "boss" and identity.approval_state == "approved", "fresh session restores trusted boss role")
check(_staff_role(store, OWNER_USER_ID) == "boss", "proactive policy canonicalizes legacy super_admin to boss")
check(_staff_is_active(store, OWNER_USER_ID, "boss"), "active boss passes proactive role and employment recheck")

staff = query_staff_directory(store, query=HISTORICAL_TEACHER_ALIAS, role="teacher", limit=5)
check(staff.get("result_count") == 0, "unconfirmed historical identity does not resolve to a formal business identity")
historical = query_staff_directory(store, query=HISTORICAL_TEACHER_ALIAS, role="teacher", include_inactive=True, limit=5)
check(historical.get("result_count") == 1, "historical test mapping remains auditable")
historical_identity = historical["staff"][0]
check(historical_identity.get("user_id") == HISTORICAL_TEACHER_ID and historical_identity.get("is_active_staff") is False, "historical test identity remains inactive")
check(historical_identity.get("has_wecom_recipient_binding") is False and historical_identity.get("outbound_eligible") is False, "historical test identity is not a formal outbound recipient")
formal = query_staff_directory(store, query=UNCLAIMED_TEACHER_ALIAS, role="teacher", include_inactive=True, limit=5)
check(formal.get("result_count") == 0, "unclaimed identity is not auto-claimed into the business roster")
check(not _staff_is_active(store, HISTORICAL_TEACHER_ID, "teacher"), "historical identity cannot pass formal proactive identity recheck")
check(not _staff_is_active(store, UNCLAIMED_TEACHER_ALIAS, "teacher"), "unclaimed identity cannot pass formal proactive identity recheck before confirmation")
check("user_id=" not in historical.get("rendered_text", ""), "historical staff render hides transport userid")

service = TuoguanToolService(
    store,
    platform="wecom_callback",
    user_id=OWNER_USER_ID,
    user_name=OWNER_DISPLAY_NAME,
    chat_id=f"wecom_callback:dm:{OWNER_USER_ID}",
    session_key="production-usability-read-only",
    identity=identity,
)
unconfirmed_touch = service.submit_relationship_touch_candidate(
    target_role="teacher",
    target_name=HISTORICAL_TEACHER_ALIAS,
    touch_type="owner_progress",
    message="请反馈当前任务进度。",
    reason="验证未确认身份不会被自动绑定或创建外发。",
    operation_id="production-usability:unconfirmed-historical-binding",
    work_related=True,
    execute_if_authorized=True,
)
check(unconfirmed_touch.get("ok") is False, "unconfirmed historical identity cannot create an outbound candidate")
check(
    str(unconfirmed_touch.get("error") or "") in {"relationship_touch_target_not_found", "relationship_touch_target_identity_unconfirmed"},
    "unconfirmed historical identity fails at the trusted identity boundary",
)
full = service.query_students(teacher_name=ACTIVE_TEACHER_NAME, query_scope="visible", limit=100)
data = full.get("data") or {}
check(full.get("ok") is True and data.get("total_count") == 36, "authoritative teacher total is 36")
check(data.get("returned_count") == 36 and len(data.get("students") or []) == 36, "Tool returns all 36 requested students")
check(data.get("has_more") is False and data.get("next_offset") is None, "complete roster reports no missing page")

compact = compact_tool_result_for_model(
    tool_name="tuoguan_query_students",
    args={"teacher_name": ACTIVE_TEACHER_NAME, "query_scope": "visible", "limit": 100},
    result=json.dumps(full, ensure_ascii=False),
)
compact_data = json.loads(compact or "{}")
model_students = ((compact_data.get("data") or {}).get("students") or [])
check(len(model_students) == 36, "model-visible Tool projection preserves all 36 names")

page1 = service.query_students(teacher_name=ACTIVE_TEACHER_NAME, query_scope="visible", limit=10, offset=0)["data"]
page2 = service.query_students(teacher_name=ACTIVE_TEACHER_NAME, query_scope="visible", limit=10, offset=10)["data"]
names1 = {row.get("name") for row in page1.get("students") or []}
names2 = {row.get("name") for row in page2.get("students") or []}
check(page1.get("next_offset") == 10 and page1.get("has_more") is True, "student pagination exposes continuation")
check(len(names1) == 10 and len(names2) == 10 and not names1.intersection(names2), "student pagination returns distinct pages")

reset_notice = "✨ Session reset! Starting fresh.\n\nModel: agnes-3.0-flash\nProvider: Agnes\nContext: 10 / 128000\n✦ Tip: internal"
check(_is_hermes_session_reset_notice(reset_notice), "Hermes reset control envelope is structurally recognized")
check(not _is_hermes_session_reset_notice("用户说：Session reset! Starting fresh."), "ordinary model prose is not classified as reset control")

cleaned, changed = _redact_internal_observability_fields(
    "任务还在跟进。\n- user_id: historical_test_teacher\n- task_id: task_1\n- Provider: ExampleProvider\n下一步我会按真实进度继续。"
)
check(changed and "historical_test_teacher" not in cleaned and "task_1" not in cleaned and "ExampleProvider" not in cleaned, "machine observability fields stay out of business reply")
check("任务还在跟进" in cleaned and "真实进度" in cleaned, "business language survives structural redaction")

print("RESULT production_usability_convergence PASS")
