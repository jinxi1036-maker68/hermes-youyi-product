from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, name: str, rows: list[dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / name).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _seed_store(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {"super_users": ["boss1"], "allowed_users": ["teacher1"], "user_roles": {"boss1": "boss", "teacher1": "teacher"}},
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "李老师": "teacher1"})
    _write_json(tmp_path, "staff.json", {"teacher1": {"name": "李老师", "role": "teacher"}})
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "academic_term_state.json", {"state": "summer_transition", "label": "暑期过渡期"})
    _append_jsonl(
        tmp_path,
        "hermes_work_items.jsonl",
        [
            {
                "work_item_id": "work-goal-1",
                "tenant_id": "youyi_tuoguan",
                "record_type": "work_item",
                "status": "waiting",
                "focus_key": "goal:sept_renewal",
                "title": "目标推进：九月份续费率更稳",
                "focus_summary": "等待老板确认服务类型和主责老师。",
                "current_waiting": {"wait_for": "老板确认服务类型和主责老师"},
                "next_attention_at": "2026-07-28T10:00:00+08:00",
                "created_at": "2026-07-28T08:00:00+08:00",
                "updated_at": "2026-07-28T08:00:00+08:00",
                "source": {"actor_user_id": "boss1", "actor_role": "boss"},
                "auto_effects": {"forces_next_action": False, "changes_router": False},
            }
        ],
    )
    return TuoguanStore(tmp_path)


def _decision(_materials: dict) -> dict:
    return {
        "employee_summary": "目标卡在责任确认，不是已经完成。",
        "institution_understanding": "优益托管当前缺少服务类型和部分主责老师事实。",
        "goal_progress_view": "应先补事实，再准备第一批沟通名单。",
        "observations": [{"event_type": "goal_blocked_by_missing_facts", "event_text": "续费目标缺服务关系事实。"}],
        "work_item_updates": [
            {
                "focus_key": "goal:sept_renewal",
                "title": "目标推进：九月份续费率更稳",
                "focus_summary": "目标推进卡在责任确认。",
                "status": "waiting",
                "update_text": "等待老板确认服务类型、主责老师和当前优先级。",
                "blocked_by": [{"type": "missing_fact", "text": "服务类型和主责老师未确认"}],
                "ask_candidates": [{"ask_role": "boss", "question": "请确认服务类型、主责老师和当前优先级。"}],
                "owner_escalation_reason": "缺少老板确认后无法进入第一批推进。",
                "value_progress_note": "补齐后可减少错派并推进续费沟通。",
                "current_waiting": {"wait_type": "owner_confirmation", "wait_for": "服务类型、主责老师、优先级"},
                "next_contact_after": "2026-07-28T10:30:00+08:00",
                "next_attention_at": "2026-07-28T11:00:00+08:00",
            }
        ],
        "questions_to_humans": [{"ask_role": "boss", "question": "请确认服务类型、主责老师和当前优先级。"}],
        "boss_attention_candidates": [
            {
                "focus_key": "goal:sept_renewal",
                "reason": "目标推进缺老板确认事实。",
                "message": "金总，我现在推进“九月份续费率更稳”卡在责任确认：需要你确认服务类型、主责老师和当前优先级。确认后我会先准备第一批重点沟通名单，不会直接安排老师或发家长。",
                "urgency": "normal",
            }
        ],
        "self_review": {"what_i_checked": "目标和工作项", "quality_score": 88},
        "external_actions": [],
    }


def test_daytime_employee_loop_queues_owner_attention_only(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.digital_employee_state import query_hermes_work_items
    from plugins.tuoguan_core.models import UserIdentity

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = run_autonomous_employee_loop(store, now=datetime(2026, 7, 28, 10, 0, tzinfo=cn_tz), decision_provider=_decision)

    assert result["ok"] is True
    assert result["work_cadence"]["owner_attention_allowed"] is True
    assert any(row["kind"] == "owner_attention_queued" and row["ok"] for row in result["writes"])
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    assert outbox[0]["notification_type"] == "autonomous_owner_attention"
    assert outbox[0]["target_user_id"] == "boss1"
    assert outbox[0]["touser"] == "boss1"
    assert outbox[0]["role"] == "boss"
    assert outbox[0]["auto_effects"]["sends_parent_messages"] is False
    assert outbox[0]["auto_effects"]["sends_teacher_messages"] is False
    assert json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8")) == []

    identity = UserIdentity(platform="system", platform_user_id="boss1", canonical_user_id="boss1", person_name="金总", role="boss", approval_state="approved")
    item = query_hermes_work_items(store, identity=identity, focus_key="goal:sept_renewal")["items"][0]
    assert item["blocked_by"][0]["type"] == "missing_fact"
    assert item["ask_candidates"][0]["ask_role"] == "boss"
    assert item["value_progress_note"]


def test_evening_employee_loop_defers_owner_attention(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = run_autonomous_employee_loop(store, now=datetime(2026, 7, 28, 20, 0, tzinfo=cn_tz), decision_provider=_decision)

    assert result["ok"] is True
    assert result["work_cadence"]["owner_attention_allowed"] is False
    assert not any(row["kind"] == "owner_attention_queued" and row["ok"] for row in result["writes"])
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []


def test_autonomous_policy_is_not_blanket_ban_on_asking(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import build_employee_loop_materials, _ACTIONS_PROMPT, _DIAGNOSIS_PROMPT
    from plugins.tuoguan_core.models import UserIdentity

    store = _seed_store(tmp_path)
    identity = UserIdentity(platform="system", platform_user_id="boss1", canonical_user_id="boss1", person_name="金总", role="boss", approval_state="approved")
    materials = build_employee_loop_materials(store, identity=identity, timestamp=datetime(2026, 7, 28, 20, 0, tzinfo=timezone(timedelta(hours=8))))

    assert materials["owner_attention_policy"]["not_a_blanket_ban"]
    assert "事实归属人" in _DIAGNOSIS_PROMPT
    assert "老板关注问题" in _ACTIONS_PROMPT


def test_autonomous_materials_use_confirmed_public_employee_name(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import build_employee_loop_materials, _DIAGNOSIS_PROMPT
    from plugins.tuoguan_core.models import UserIdentity

    store = _seed_store(tmp_path)
    (tmp_path / "operational_facts.json").write_text(json.dumps({
        "schema_version": 1,
        "tenant_id": "youyi_tuoguan",
        "facts": [
            {
                "fact_id": "fact_name_xiaoyou",
                "tenant_id": "youyi_tuoguan",
                "fact_type": "owner_rule",
                "subject": "数字员工称呼",
                "value": "对外称呼为\"小优\"",
                "scope": "institution",
                "risk_level": "low",
                "status": "active",
                "source_text": "金总说：我给你起一个名字，你以后叫小优",
                "confirmed_by": "boss1",
                "confirmed_at": "2026-08-01T21:37:53+08:00",
            }
        ],
    }, ensure_ascii=False), encoding="utf-8")
    identity = UserIdentity(platform="system", platform_user_id="boss1", canonical_user_id="boss1", person_name="金总", role="boss", approval_state="approved")
    materials = build_employee_loop_materials(store, identity=identity, timestamp=datetime(2026, 8, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))))

    assert materials["public_identity"]["public_name"] == "小优"
    assert materials["public_identity"]["internal_name"] == "Hermes"
    assert "Xiaoyou" in materials["identity"]
    assert "数字员工小优" in _DIAGNOSIS_PROMPT


def test_proactive_work_radar_uses_handbook_employee_map(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import query_proactive_work_radar
    from plugins.tuoguan_core.models import UserIdentity

    store = _seed_store(tmp_path)
    _write_json(
        tmp_path,
        "institution_operating_model.json",
        {
            "schema_version": 1,
            "institution_name": "优益托管",
            "programs": {"regular_tuoguan": {"label": "正式托管"}},
        },
    )
    identity = UserIdentity(platform="system", platform_user_id="boss1", canonical_user_id="boss1", person_name="金总", role="boss", approval_state="approved")

    radar = query_proactive_work_radar(store, identity=identity, limit=10)

    assert radar["ok"] is True
    assert radar["read_only"] is True
    assert radar["model_decides_next_action"] is True
    assert radar["actions_taken"] == []
    domain_keys = {item["domain_key"] for item in radar["domains"]}
    assert "institution_work_map" in domain_keys
    assert "organization_permissions" in domain_keys
    assert "teacher_work_habits" in domain_keys
    assert "goals_and_work_items" in domain_keys
    assert any(anchor["chapter"] == "第3章" for anchor in radar["handbook_anchors"])
    assert any("店长" in item["question"] for item in radar["question_candidates"])
    assert any(item["domain_key"] == "organization_permissions" for item in radar["priority_gaps"])
    assert "no_teacher_messages_sent" in radar["forbidden_actions_confirmed_absent"]


def test_autonomous_materials_include_proactive_work_radar(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import build_employee_loop_materials, _model_payload, _DIAGNOSIS_PROMPT
    from plugins.tuoguan_core.models import UserIdentity

    store = _seed_store(tmp_path)
    identity = UserIdentity(platform="system", platform_user_id="boss1", canonical_user_id="boss1", person_name="金总", role="boss", approval_state="approved")
    materials = build_employee_loop_materials(store, identity=identity, timestamp=datetime(2026, 8, 8, 9, 0, tzinfo=timezone(timedelta(hours=8))))
    payload = _model_payload(materials)

    assert materials["proactive_work_radar"]["report_type"] == "proactive_work_radar_v1"
    assert materials["materials_summary"]["proactive_radar_question_candidate_count"] >= 1
    assert payload["proactive_work_radar"]["model_decides_next_action"] is True
    assert "teacher_work_habits" in json.dumps(payload["proactive_work_radar"], ensure_ascii=False)
    assert "事实诊断" in _DIAGNOSIS_PROMPT


def test_proactive_work_radar_tool_is_registered():
    from plugins.tuoguan_core.tools import TOOLS

    assert "tuoguan_query_proactive_work_radar" in {name for name, _schema, _handler in TOOLS}


def test_owner_attention_is_deduplicated_per_focus_per_day(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    now = datetime(2026, 7, 28, 10, 0, tzinfo=cn_tz)
    first = run_autonomous_employee_loop(store, now=now, decision_provider=_decision)
    second = run_autonomous_employee_loop(store, now=now.replace(hour=11), decision_provider=_decision)

    assert first["ok"] is True
    assert second["ok"] is True
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    assert any(row["kind"] == "owner_attention_deduplicated" and row["ok"] for row in second["writes"])


def test_similar_owner_attention_is_deduplicated_across_focus_keys(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    first = run_autonomous_employee_loop(store, now=datetime(2026, 8, 7, 9, 0, tzinfo=cn_tz), decision_provider=_decision)

    def same_question_new_focus(materials: dict) -> dict:
        decision = _decision(materials)
        decision["boss_attention_candidates"][0]["focus_key"] = "report:sept_renewal_owner_confirmation"
        return decision

    second = run_autonomous_employee_loop(store, now=datetime(2026, 8, 7, 12, 0, tzinfo=cn_tz), decision_provider=same_question_new_focus)

    assert first["ok"] is True
    assert second["ok"] is True
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    assert any(row["kind"] == "owner_attention_deduplicated" and row["ok"] for row in second["writes"])


def test_owner_question_is_bridged_to_attention_candidate(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    def decision_without_candidate(_materials: dict) -> dict:
        return {
            "employee_summary": "续费目标需要老板确认优先抓哪类未续原因。",
            "institution_understanding": "已有续费原因分类。",
            "goal_progress_view": "缺老板优先级后才能继续整理候选。",
            "observations": [],
            "work_item_updates": [
                {
                    "focus_key": "goal:sept_renewal",
                    "title": "目标推进：九月份续费率更稳",
                    "focus_summary": "需要老板确认优先级。",
                    "status": "active",
                    "update_text": "需要老板确认先抓哪类未续原因。",
                    "next_actions": ["按老板确认的优先类别整理候选和话术。"],
                    "blocked_by": [{"type": "missing_owner_priority", "text": "未确认优先抓哪类未续原因"}],
                }
            ],
            "questions_to_humans": [
                {
                    "ask_role": "boss",
                    "reason": "缺老板确认：价格、转校、等开学三类未续原因先抓哪一类。",
                    "question": "请确认价格、转校、等开学三类里，今天先优先抓哪一类？",
                    "urgency": "normal",
                }
            ],
            "boss_attention_candidates": [],
            "institution_fact_gaps": [],
            "value_progress_entries": [],
            "self_review": {},
            "external_actions": [],
        }

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = run_autonomous_employee_loop(store, now=datetime(2026, 7, 28, 10, 0, tzinfo=cn_tz), decision_provider=decision_without_candidate)

    assert result["ok"] is True
    assert any(row["kind"] == "owner_attention_queued" and row["ok"] for row in result["writes"])
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    assert "卡点：" in outbox[0]["content"]
    assert "需要你确认：" in outbox[0]["content"]
    assert "确认后：" in outbox[0]["content"]
    assert "价格、转校、等开学" in outbox[0]["content"]

    attention_rows = [
        json.loads(line)
        for line in (tmp_path / "attention_threads.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert attention_rows[-1]["status"] == "queued"
    assert attention_rows[-1]["source_decision_summary"]
    assert "价格、转校、等开学" in json.dumps(attention_rows[-1]["needed_facts"], ensure_ascii=False)


def test_deferred_service_relation_question_is_not_bridged(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    def deferred_relation_question(_materials: dict) -> dict:
        return {
            "employee_summary": "新学期服务关系缺失。",
            "institution_understanding": "",
            "goal_progress_view": "",
            "observations": [],
            "work_item_updates": [
                {
                    "focus_key": "goal:sept_renewal",
                    "title": "目标推进：九月份续费率更稳",
                    "focus_summary": "服务关系延后确认。",
                    "status": "active",
                    "update_text": "服务关系延后确认。",
                }
            ],
            "questions_to_humans": [
                {
                    "ask_role": "boss",
                    "reason": "缺少新学期服务关系。",
                    "question": "请确认新学期名单、服务类型和主责老师。",
                    "urgency": "normal",
                }
            ],
            "boss_attention_candidates": [],
            "institution_fact_gaps": [],
            "value_progress_entries": [],
            "self_review": {},
            "external_actions": [],
        }

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=cn_tz),
        decision_provider=deferred_relation_question,
        wakeup_summary={"term_state": {"service_relation_policy": "defer_until_new_term"}},
    )

    assert result["ok"] is True
    assert not any(row["kind"] == "owner_attention_queued" and row["ok"] for row in result["writes"])
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []


def test_generic_owner_attention_candidate_is_rejected(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    def generic_candidate(_materials: dict) -> dict:
        base = _decision(_materials)
        base["boss_attention_candidates"] = [
            {
                "focus_key": "goal:sept_renewal",
                "reason": "需要沟通。",
                "message": "金总，我整理好了情况。",
                "urgency": "normal",
            }
        ]
        base["questions_to_humans"] = []
        return base

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = run_autonomous_employee_loop(store, now=datetime(2026, 7, 28, 10, 0, tzinfo=cn_tz), decision_provider=generic_candidate)

    assert result["ok"] is True
    assert not any(row["kind"] == "owner_attention_queued" and row["ok"] for row in result["writes"])
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []
def test_autonomous_loop_write_allowlist_covers_delegation_ledgers():
    from plugins.tuoguan_core import autonomous_employee_loop as loop
    from plugins.tuoguan_core.digital_employee_state import AGENT_DELEGATIONS_FILE, AGENT_DELEGATION_RESULTS_FILE

    assert AGENT_DELEGATIONS_FILE in loop._ALLOWED_FILES
    assert AGENT_DELEGATION_RESULTS_FILE in loop._ALLOWED_FILES


def test_model_decision_uses_small_phases_and_skips_daytime_review(monkeypatch):
    from plugins.tuoguan_core import autonomous_employee_loop as loop

    phases: list[str] = []

    def fake_phase(phase, _prompt, _payload, *, max_tokens):
        phases.append(phase)
        if phase == "diagnosis":
            return {
                "employee_summary": "已核验当前事实。",
                "institution_understanding": "机构事实可用。",
                "goal_progress_view": "目标继续推进。",
                "observations": [],
                "institution_fact_gaps": [],
                "questions_to_humans": [],
            }
        return {
            "work_item_updates": [],
            "boss_attention_candidates": [],
            "relationship_touch_candidates": [],
            "value_progress_entries": [],
            "agent_delegation_decisions": [],
        }

    monkeypatch.setattr(loop, "_request_model_phase", fake_phase)
    result = loop._call_model_for_decision({"work_cadence": {"mode": "daytime_goal_progress"}})

    assert phases == ["diagnosis", "actions"]
    assert result["employee_summary"] == "已核验当前事实。"
    assert result["evolution_candidates"] == []
    assert result["external_actions"] == []


def test_model_decision_runs_review_only_at_night(monkeypatch):
    from plugins.tuoguan_core import autonomous_employee_loop as loop

    phases: list[str] = []

    def fake_phase(phase, _prompt, _payload, *, max_tokens):
        phases.append(phase)
        if phase == "diagnosis":
            return {"employee_summary": "复盘", "observations": [], "institution_fact_gaps": [], "questions_to_humans": []}
        if phase == "actions":
            return {"work_item_updates": [], "boss_attention_candidates": [], "relationship_touch_candidates": [], "value_progress_entries": [], "agent_delegation_decisions": []}
        return {"evolution_candidates": [{"candidate_type": "tomorrow_focus", "summary": "明天先查事实"}], "self_review": {"tomorrow_focus": "先查事实"}}

    monkeypatch.setattr(loop, "_request_model_phase", fake_phase)
    result = loop._call_model_for_decision({"work_cadence": {"mode": "night_read_only_review"}})

    assert phases == ["diagnosis", "actions", "review"]
    assert result["evolution_candidates"][0]["candidate_type"] == "tomorrow_focus"


def test_model_phase_rejects_repeated_truncated_json(monkeypatch):
    import pytest
    from plugins.tuoguan_core import autonomous_employee_loop as loop

    monkeypatch.setattr(loop, "_load_model_configs", lambda: [{"base_url": "https://example.test", "api_key": "test", "model": "test"}])
    monkeypatch.setattr(loop, "_request_model_content", lambda *_args, **_kwargs: "{")

    with pytest.raises(RuntimeError, match="all_model_providers_failed:diagnosis:.*invalid_json"):
        loop._request_model_phase("diagnosis", "system", {"fact": "value"}, max_tokens=900)


def test_model_content_exhausts_repeated_429_without_success(monkeypatch):
    import pytest
    from plugins.tuoguan_core import autonomous_employee_loop as loop

    class RateLimitedResponse:
        status_code = 429
        headers = {"Retry-After": "0"}

        def raise_for_status(self):
            request = loop.httpx.Request("POST", "https://example.test/chat/completions")
            response = loop.httpx.Response(429, request=request)
            raise loop.httpx.HTTPStatusError("rate limited", request=request, response=response)

    monkeypatch.setattr(loop.httpx, "post", lambda *args, **kwargs: RateLimitedResponse())
    monkeypatch.setattr(loop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(loop, "_load_model_configs", lambda: [{"base_url": "https://example.test", "api_key": "test", "model": "test", "timeout": 1}])

    with pytest.raises(RuntimeError, match="all_model_providers_failed:diagnosis"):
        loop._request_model_phase("diagnosis", "system", {"fact": "value"}, max_tokens=900)


def test_model_phase_reports_timeout_as_failure(monkeypatch):
    import pytest
    from plugins.tuoguan_core import autonomous_employee_loop as loop

    monkeypatch.setattr(loop, "_load_model_configs", lambda: [{"base_url": "https://example.test", "api_key": "test", "model": "test", "timeout": 1}])
    monkeypatch.setattr(
        loop.httpx,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(loop.httpx.TimeoutException("timed out")),
    )

    with pytest.raises(RuntimeError, match="all_model_providers_failed:diagnosis"):
        loop._request_model_phase("diagnosis", "system", {"fact": "value"}, max_tokens=900)


def test_autonomous_evidence_queries_use_tenant_owner_without_identity_pollution(tmp_path):
    from plugins.tuoguan_core import autonomous_employee_loop as loop

    store = _seed_store(tmp_path)

    assert loop._query_onboarding(store).get("error") != "owner_identity_missing"
    assert loop._query_operating_evidence(store).get("error") != "owner_identity_missing"

    whitelist = store.read_json("wecom_whitelist.json", {})
    assert whitelist.get("pending_users") in (None, [])
    assert "JinWenJie" not in json.dumps(whitelist, ensure_ascii=False)


def test_autonomous_evidence_queries_fail_closed_when_tenant_owner_is_missing(tmp_path):
    from plugins.tuoguan_core import autonomous_employee_loop as loop
    from plugins.tuoguan_core.store import TuoguanStore

    store = TuoguanStore(tmp_path)

    assert loop._query_onboarding(store)["error"] == "owner_identity_missing"
    assert loop._query_operating_evidence(store)["error"] == "owner_identity_missing"
    assert not (tmp_path / "wecom_whitelist.json").exists()
