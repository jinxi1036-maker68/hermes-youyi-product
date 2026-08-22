from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _home_proddata() -> Path:
    return Path(__file__).resolve().parents[3] / "home-proddata"


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="msg-mainline",
        source=SessionSource(
            platform=Platform.WECOM_CALLBACK,
            user_id="teacher1",
            chat_id="wwcorp:teacher1",
            user_name="王老师",
            chat_type="dm",
        ),
    )


def test_tuoguan_core_does_not_register_pre_model_business_decision_hooks():
    import plugins.tuoguan_core as plugin

    hooks = []
    middleware = []
    tools = []
    ctx = SimpleNamespace(
        register_hook=lambda name, fn: hooks.append((name, fn)),
        register_middleware=lambda name, fn: middleware.append((name, fn)),
        register_tool=lambda **kwargs: tools.append(kwargs),
    )

    plugin.register(ctx)

    names = [name for name, _fn in hooks]
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except Exception:
        VALID_HOOKS = {"post_gateway_response"}
    if "post_gateway_response" in VALID_HOOKS:
        assert names == ["pre_llm_call", "pre_tool_call", "post_tool_call", "post_gateway_response"]
    else:
        assert names == ["pre_llm_call", "pre_tool_call", "post_tool_call", "transform_llm_output", "post_llm_call"]
    assert "pre_gateway_dispatch" not in names
    assert [name for name, _fn in middleware] == ["llm_request"]
    assert {tool["name"] for tool in tools}


def test_core_context_hard_binds_fresh_teacher_session_identity():
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity

    identity = UserIdentity(
        platform="wecom_callback",
        platform_user_id="CeShi",
        canonical_user_id="CeShi",
        person_name="李老师",
        role="teacher",
        approval_state="approved",
    )

    context = plugin._xiaoyou_core_skill_context(identity=identity)

    assert "李老师（老师，user_id=CeShi" in context
    assert "不得从全局记忆、旧会话或其他人的材料把当前人猜成金总" in context
    assert "用户问‘我是谁’时直接依据这一可信身份回答" in context


def test_teacher_direct_salutation_cannot_drift_to_owner_name():
    from plugins.tuoguan_core.runtime_foundation import _sanitize_external_reply

    repaired = _sanitize_external_reply(
        "在的，金总。我是小优，已上线。",
        actor_role="teacher",
        actor_name="李老师",
    )
    legitimate_reference = _sanitize_external_reply(
        "这是金总安排的任务，我帮你看一下。",
        actor_role="teacher",
        actor_name="李老师",
    )

    assert repaired == "在的，李老师。我是小优，已上线。"
    assert legitimate_reference == "这是金总安排的任务，我帮你看一下。"


def test_v020_output_and_post_llm_hooks_preserve_honesty_and_audit(tmp_path, monkeypatch):
    import plugins.tuoguan_core as plugin

    store = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(plugin, "_router", lambda: SimpleNamespace(store=store))
    monkeypatch.setattr(
        plugin,
        "_foundation_transform_final_response",
        lambda **kwargs: "clean:" + kwargs["response_text"],
    )
    recorded = []
    monkeypatch.setattr(
        plugin,
        "_foundation_ensure_outbound_reply_recorded",
        lambda **kwargs: recorded.append(kwargs),
    )

    transformed = plugin._on_transform_llm_output(
        platform="wecom_callback",
        session_id="session-v020",
        response_text="已经保存",
    )
    assert transformed == "clean:已经保存"

    plugin._ACTIVE_MODEL_TURNS["session-v020"] = {
        "message_id": "msg-v020",
        "conversation_id": "corp:boss1",
        "user_id": "boss1",
        "role": "boss",
        "raw_text": "以后汇报三条以内",
    }
    plugin._on_post_llm_call_v020(
        platform="wecom_callback",
        session_id="session-v020",
        assistant_response=transformed,
    )

    assert len(recorded) == 1
    assert recorded[0]["message_id"] == "msg-v020"
    assert recorded[0]["final_reply"] == transformed
    assert recorded[0]["route_decision"] == "model_first_v020"
    assert "session-v020" not in plugin._ACTIVE_MODEL_TURNS


def test_pre_llm_call_establishes_write_context_with_workstyle_material_only(tmp_path, monkeypatch):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state, write_authorization_for
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    store = TuoguanStore(tmp_path)
    manual_context = tmp_path / "manual_context"
    manual_context.mkdir()
    (manual_context / "hermes_model_context_injection_allowlist_v1.json").write_text(
        json.dumps(
            {
                "runtime_foundation": {"enabled": True},
                "allowed_capability_cards": [],
            }
        ),
        encoding="utf-8",
    )
    fake_router = SimpleNamespace(
        store=store,
        identities=SimpleNamespace(
            resolve=lambda *args, **kwargs: UserIdentity(
                platform="wecom",
                platform_user_id="teacher1",
                canonical_user_id="teacher1",
                person_name="王老师",
                role="teacher",
                approval_state="approved",
            )
        ),
    )
    monkeypatch.setattr(plugin, "_router", lambda: fake_router)

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="wecom_callback:teacher1",
        session_id="session-1",
        turn_id="turn-1",
        user_message="小赵今天作业完成得慢。",
    )

    assert result is not None
    assert "小优服务方式档案" in result["context"]
    assert "当前人员的服务方式偏好" not in result["context"]
    assert "当前轮写入规则" not in result["context"]
    assert write_authorization_for("teacher1", "record_student") is not None


def test_pre_llm_call_injects_narrow_rule_for_explicit_write(tmp_path, monkeypatch):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state, write_authorization_for
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    store = TuoguanStore(tmp_path)
    manual_context = tmp_path / "manual_context"
    manual_context.mkdir()
    (manual_context / "hermes_model_context_injection_allowlist_v1.json").write_text(
        json.dumps(
            {
                "runtime_foundation": {"enabled": True},
                "allowed_capability_cards": [],
            }
        ),
        encoding="utf-8",
    )
    fake_router = SimpleNamespace(
        store=store,
        identities=SimpleNamespace(
            resolve=lambda *args, **kwargs: UserIdentity(
                platform="wecom",
                platform_user_id="boss1",
                canonical_user_id="boss1",
                person_name="金总",
                role="boss",
                approval_state="approved",
            )
        ),
    )
    monkeypatch.setattr(plugin, "_router", lambda: fake_router)

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="wecom_callback:boss1",
        session_id="session-write",
        turn_id="turn-write",
        user_message="给刘宇凯加1分",
    )

    assert result is not None
    assert "模型仍负责理解用户、判断是否追问、是否写入或是否先说明边界" in result["context"]
    assert "如果要声明记录、修改、加扣分、创建、完成、确认、提交、上报、保存偏好、记住工作方式已经真实发生" in result["context"]
    assert "不要根据历史里的" in result["context"]
    assert write_authorization_for("boss1", "change_summer_points") is not None


def test_pre_llm_call_authorizes_model_selected_task_cancel(tmp_path, monkeypatch):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state, write_authorization_for
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    store = TuoguanStore(tmp_path)
    manual_context = tmp_path / "manual_context"
    manual_context.mkdir()
    (manual_context / "hermes_model_context_injection_allowlist_v1.json").write_text(
        json.dumps(
            {
                "runtime_foundation": {"enabled": True},
                "allowed_capability_cards": [],
            }
        ),
        encoding="utf-8",
    )
    fake_router = SimpleNamespace(
        store=store,
        identities=SimpleNamespace(
            resolve=lambda *args, **kwargs: UserIdentity(
                platform="wecom",
                platform_user_id="boss1",
                canonical_user_id="boss1",
                person_name="金总",
                role="boss",
                approval_state="approved",
            )
        ),
    )
    monkeypatch.setattr(plugin, "_router", lambda: fake_router)

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="wecom_callback:boss1",
        session_id="session-cancel",
        turn_id="turn-cancel",
        user_message="这个任务直接关闭删除",
    )

    assert result is not None
    assert write_authorization_for("boss1", "cancel_task") is not None
    assert write_authorization_for("boss1", "submit_fact_gap_candidate") is not None
    assert write_authorization_for("boss1", "submit_relationship_touch_candidate") is not None


def test_write_authorization_accepts_live_session_when_entered_model_flag_is_not_visible(tmp_path, monkeypatch):
    from plugins.tuoguan_core.runtime_foundation import (
        begin_inbound,
        clear_runtime_state,
        write_authorization_for,
    )
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    store = TuoguanStore(tmp_path)
    manual_context = tmp_path / "manual_context"
    manual_context.mkdir()
    (manual_context / "hermes_model_context_injection_allowlist_v1.json").write_text(
        json.dumps(
            {
                "runtime_foundation": {"enabled": True},
                "allowed_capability_cards": [],
            }
        ),
        encoding="utf-8",
    )
    begin_inbound(
        store=store,
        message_id="msg-cancel-session",
        conversation_id="wwcorp:boss1",
        user_id="boss1",
        role="boss",
        raw_text="把这个测试任务直接关闭，不用再提醒",
    )

    def fake_get_session_env(key: str, default: str = "") -> str:
        return {
            "HERMES_SESSION_USER_ID": "wecom_callback:boss1",
            "HERMES_SESSION_ID": "session-boss-live",
            "HERMES_SESSION_KEY": "session-boss-live",
        }.get(key, default)

    import sys

    fake_session_context = SimpleNamespace(get_session_env=fake_get_session_env)
    monkeypatch.setitem(sys.modules, "gateway.session_context", fake_session_context)

    assert write_authorization_for("boss1", "cancel_task") is not None
    assert write_authorization_for("teacher1", "cancel_task") is None


def test_pre_llm_call_injects_workstyle_feedback_contract(tmp_path, monkeypatch):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state, write_authorization_for
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.workstyle_profiles import submit_person_workstyle_preference

    clear_runtime_state()
    store = TuoguanStore(tmp_path)
    identity = UserIdentity(
        platform="wecom",
        platform_user_id="boss1",
        canonical_user_id="boss1",
        person_name="金总",
        role="boss",
        approval_state="approved",
    )
    saved = submit_person_workstyle_preference(
        store,
        identity=identity,
        preference_type="report_length",
        scope="daily_report",
        preference_text="晚报只说重点。",
        normalized_rule="晚报只说重点。",
        operation_id="pre-llm-workstyle-seed",
    )
    assert saved["ok"] is True

    manual_context = tmp_path / "manual_context"
    manual_context.mkdir()
    (manual_context / "hermes_model_context_injection_allowlist_v1.json").write_text(
        json.dumps(
            {
                "runtime_foundation": {"enabled": True},
                "allowed_capability_cards": [],
            }
        ),
        encoding="utf-8",
    )
    fake_router = SimpleNamespace(
        store=store,
        identities=SimpleNamespace(resolve=lambda *args, **kwargs: identity),
    )
    monkeypatch.setattr(plugin, "_router", lambda: fake_router)

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="wecom_callback:boss1",
        session_id="session-workstyle",
        turn_id="turn-workstyle",
        user_message="以后晚报只说重点，别发一大堆。",
    )

    assert result is not None
    assert "小优服务方式档案" in result["context"]
    assert "长短" in result["context"]
    assert "晚报只说重点。" in result["context"]
    assert "tuoguan_submit_person_workstyle_preference" in result["context"]
    assert "未看到工具 ok=true 且 writeback_verified=true 前，不得说" in result["context"]
    assert write_authorization_for("boss1", "submit_person_workstyle_preference") is not None


def test_pre_llm_call_asks_one_question_when_short_reply_has_no_active_context(tmp_path, monkeypatch):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    store = TuoguanStore(tmp_path)
    manual_context = tmp_path / "manual_context"
    manual_context.mkdir()
    (manual_context / "hermes_model_context_injection_allowlist_v1.json").write_text(
        json.dumps(
            {
                "runtime_foundation": {"enabled": True},
                "allowed_capability_cards": [],
            }
        ),
        encoding="utf-8",
    )
    fake_router = SimpleNamespace(
        store=store,
        identities=SimpleNamespace(
            resolve=lambda *args, **kwargs: UserIdentity(
                platform="wecom",
                platform_user_id="boss1",
                canonical_user_id="boss1",
                person_name="金总",
                role="boss",
                approval_state="approved",
            )
        ),
    )
    monkeypatch.setattr(plugin, "_router", lambda: fake_router)

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        session_id="session-retry",
        turn_id="turn-retry",
        user_message="你再试一下",
    )

    assert result is not None
    assert "当前没有可验证的活动线程" in result["context"]
    assert "只追问一个最关键的区分问题" in result["context"]


def test_retired_pre_gateway_dispatch_is_noop_even_if_called(tmp_path, monkeypatch):
    import plugins.tuoguan_core as plugin

    data_dir = tmp_path / "tuoguan-data"
    data_dir.mkdir()
    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(data_dir))
    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(adapters={Platform.WECOM_CALLBACK: adapter})

    result = plugin._on_pre_gateway_dispatch(
        event=_event("在线吗"),
        gateway=gateway,
        session_store=SimpleNamespace(),
    )

    assert result is None
    adapter.send.assert_not_called()


def test_tool_descriptions_are_capabilities_not_phrase_routers():
    from plugins.tuoguan_core.tools import TOOLS

    descriptions = "\n".join(str(schema.get("description") or "") for _name, schema, _handler in TOOLS)
    forbidden_fragments = [
        "用户问",
        "当用户",
        "这句话",
        "优先用本工具",
        "必须调用本工具",
        "不得改用学生查询",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in descriptions


def test_youyi_digital_employee_handbook_is_present_and_model_led():
    skill_dir = _home_proddata() / "skills" / "youyi-digital-employee"
    skill_md = skill_dir / "SKILL.md"
    references = skill_dir / "references"

    expected_refs = {
        "constitution.md",
        "institution-model.md",
        "role-permissions.md",
        "business-operations.md",
        "goal-operator.md",
        "wakeup-playbook.md",
        "memory-learning.md",
        "tool-execution.md",
    }

    assert skill_md.exists()
    assert {path.name for path in references.glob("*.md")} == expected_refs

    body = skill_md.read_text(encoding="utf-8")
    assert "handbook for reasoning, not a keyword router" in body
    assert "The model understands and decides" in body
    assert "The system verifies identity, permissions" in body
    assert "It must not block Hermes from thinking, analyzing, asking, suggesting" in body
    assert "Capability checks are not business actions" in body
    assert "Employee Operating Loop" in body
    assert "revenue through better service" in body

    all_text = "\n".join(path.read_text(encoding="utf-8") for path in [skill_md, *references.glob("*.md")])
    assert "Do not let the system initiate business writes" in all_text
    assert "Permissions are execution boundaries, not thinking boundaries" in all_text
    assert "V1 is read-only observation and summary" in all_text
    assert "Do not teach Hermes to prove ability by doing real production writes" in all_text
    assert "New Institution Onboarding" in all_text
    assert "Institution Self-Check" in all_text
    assert "Memory Architecture" in all_text
    assert "Profile layer" in all_text
    assert "Daily Work Rhythm" in all_text
    assert "Revenue Insight" in all_text
    assert "Self-Evaluation" in all_text
    assert "Hermes Native Capabilities" in all_text
    assert "Goal Decomposition" in all_text
    assert "trial conversion" in all_text
    assert "renewal risks" in all_text
    assert "possible profit programs" in all_text


def test_handbook_v2_product_master_and_core_card_are_layered():
    home = _home_proddata()
    master = home / "docs" / "handbook" / "Hermes数字员工手册V2.md"
    core = home / "HERMES.md"
    audit = home / "docs" / "audits" / "current_architecture_audit_20260726.md"

    assert master.exists()
    assert core.exists()
    assert audit.exists()

    master_text = master.read_text(encoding="utf-8")
    assert "Hermes 不是普通聊天机器人" in master_text
    assert "最重要的阅读说明" in master_text
    assert "每月一次真实家校沟通" in master_text
    assert "每周至少形成一次真实表现记录" in master_text
    assert "最终绩效分数和工资结算由老板确认" in master_text
    assert "动态召回不能变成 Router" in master_text
    assert "不得在 Hook 中按关键词分类普通文本" in master_text
    assert "预写 model_intent、next_tool、workflow_step、expected_reply" in master_text

    core_text = core.read_text(encoding="utf-8")
    assert len(core_text) < 1600
    assert "手册提供员工身份、业务常识、岗位责任、制度边界、判断框架和经验参考" in core_text
    assert "它不是 Router、状态机、固定工具路径或模板话术" in core_text
    assert "模型主导理解、选择、追问、查询、写入、等待或停止" in core_text
    assert "系统只负责身份、权限、参数、安全、幂等、真实执行、写后反查和审计" in core_text
    assert "系统不得因业务流程偏好、模板路径或关键词判断限制模型思考" in core_text
    assert "不得预写 intent、next_tool、workflow_step 或 expected_reply" in core_text

    audit_text = audit.read_text(encoding="utf-8")
    assert "生产目录：`/opt/hermes-youyi-upgrade-0.19.0`" in audit_text
    assert "普通文本不应由旧 Router 抢在模型前执行" in audit_text


def test_priority_handbook_skills_are_present_and_not_workflow_routers():
    skills_root = _home_proddata() / "skills"
    expected = {
        "institution-onboarding": [
            "new employee",
            "Do not dump a fixed long questionnaire",
            "Do not turn onboarding into a hidden intent router",
        ],
        "student-service-relations": [
            "multiple service relationships",
            "Lunch-care",
            "Evening-care",
            "Do not guess responsibility",
        ],
        "active-information-acquisition": [
            "Active information acquisition is a core duty",
            "Formal question",
            "No reply does not mean no issue",
        ],
        "goal-management": [
            "Owner goals usually define a desired result",
            "This is a capability loop, not a mandatory fixed sequence",
            "Completion needs real evidence",
        ],
        "memory-evidence-learning": [
            "separate raw feedback, confirmed facts, stage profiles, insights, memory, and learning",
            "Profiles expire",
            "Runtime bugs and tool workarounds become tests or fix lists",
        ],
    }

    for skill_name, required_fragments in expected.items():
        skill_md = skills_root / skill_name / "SKILL.md"
        assert skill_md.exists()
        text = skill_md.read_text(encoding="utf-8")
        assert "description:" in text
        for fragment in required_fragments:
            assert fragment in text

    all_text = "\n".join((skills_root / name / "SKILL.md").read_text(encoding="utf-8") for name in expected)
    forbidden_execution_fragments = [
        "用户说",
        "关键词命中",
        "必须调用工具",
        "next_tool:",
        "workflow_step:",
        "expected_reply:",
    ]
    for fragment in forbidden_execution_fragments:
        assert fragment not in all_text


def test_youyi_handbook_does_not_become_phrase_router_or_execution_script():
    skill_dir = _home_proddata() / "skills" / "youyi-digital-employee"
    texts = "\n".join(path.read_text(encoding="utf-8") for path in skill_dir.rglob("*.md"))

    forbidden = [
        "关键词命中",
        "固定意图",
        "系统自动执行",
        "系统发起写入",
        "自动给家长发送",
        "自动修改工资",
        "自动删除",
    ]
    for fragment in forbidden:
        assert fragment not in texts

    assert texts.count("Do not") >= 10
    assert "not a keyword router" in texts
    assert "not as a cage" in texts


def test_memory_keeps_business_facts_not_runtime_debugging():
    memory = (_home_proddata() / "memories" / "MEMORY.md").read_text(encoding="utf-8")

    assert "金总" in memory
    assert "write guard" not in memory
    assert "runtime foundation" not in memory
    assert "inject_model_context" not in memory
    assert "unauthorized_write_blocked" not in memory
