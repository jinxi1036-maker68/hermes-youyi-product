from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path


def _identity(user_id: str, role: str):
    from plugins.tuoguan_core.models import UserIdentity

    return UserIdentity(
        platform="wecom_callback",
        platform_user_id=user_id,
        canonical_user_id=user_id,
        person_name=user_id,
        role=role,
        approval_state="approved",
    )


def test_atomic_task_updates_do_not_overwrite_each_other(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    (tmp_path / "tasks.json").write_text(
        json.dumps([{"id": "task-1", "evidence": []}, {"id": "task-2", "evidence": []}]),
        encoding="utf-8",
    )
    store = TuoguanStore(tmp_path)

    def append_evidence(index: int) -> None:
        def mutate(task: dict) -> None:
            task.setdefault("evidence", []).append(f"e-{index}")

        persisted = store.update_task("task-1", mutate)
        assert persisted is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append_evidence, range(20)))

    tasks = store.load_tasks()
    task = next(item for item in tasks if item["id"] == "task-1")
    assert sorted(task["evidence"]) == sorted(f"e-{index}" for index in range(20))
    assert next(item for item in tasks if item["id"] == "task-2")["evidence"] == []


def test_jsonl_append_is_locked_flushed_and_read_back(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    store = TuoguanStore(tmp_path)

    def append_row(index: int) -> None:
        assert store.append_jsonl_verified("reliability_events.jsonl", {"record_id": f"r-{index}"}) is True

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append_row, range(30)))

    rows = [json.loads(line) for line in (tmp_path / "reliability_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["record_id"] for row in rows} == {f"r-{index}" for index in range(30)}


def test_self_evolution_is_deduplicated_scoped_and_applied(tmp_path: Path):
    from plugins.tuoguan_core.self_evolution import (
        SELF_EVOLUTION_EVENTS_FILE,
        build_self_evolution_brief,
        query_self_evolution_ledger,
        record_self_evolution_application,
        submit_self_evolution_event,
        verify_self_evolution_application,
    )
    from plugins.tuoguan_core.store import TuoguanStore

    store = TuoguanStore(tmp_path)
    boss = _identity("JinWenJie", "boss")
    teacher = _identity("CeShi", "teacher")
    first = submit_self_evolution_event(
        store,
        identity=boss,
        operation_id="evo-1",
        candidate_type="self_correction",
        summary="给老板汇报时先说结论，避免重复解释。",
        applies_to_user_id="JinWenJie",
    )
    repeated = submit_self_evolution_event(
        store,
        identity=boss,
        operation_id="evo-2",
        candidate_type="self_correction",
        summary="给老板汇报时先说结论，避免重复解释。",
        applies_to_user_id="JinWenJie",
        evidence=[{"source": "second_occurrence"}],
    )
    submit_self_evolution_event(
        store,
        identity=teacher,
        operation_id="evo-3",
        candidate_type="self_correction",
        summary="给李老师回复时不要重复追问已经回答的事实。",
        applies_to_user_id="CeShi",
    )

    assert first["ok"] is True
    assert repeated["deduplicated_update"] is True
    ledger = query_self_evolution_ledger(store, identity=boss, limit=20)
    assert ledger["event_count"] == 2
    boss_brief = build_self_evolution_brief(store, identity=boss, limit=10)
    teacher_brief = build_self_evolution_brief(store, identity=teacher, limit=10)
    assert any("老板" in line for line in boss_brief["next_day_context"])
    assert all("李老师" not in line for line in boss_brief["next_day_context"])
    assert any("李老师" in line for line in teacher_brief["next_day_context"])

    applied = record_self_evolution_application(
        store,
        identity=boss,
        source_message_id="msg-1",
        final_reply="结论：今天没有异常。",
    )
    assert applied["applied_count"] == 1
    verified = verify_self_evolution_application(
        store,
        evolution_event_id=applied["applied_event_ids"][0],
        succeeded=True,
        evidence="真实回复先给结论且没有重复解释。",
    )
    assert verified["self_evolution_event"]["status"] == "verified"
    assert (tmp_path / SELF_EVOLUTION_EVENTS_FILE).exists()


def test_core_skill_context_is_current_person_only():
    from plugins.tuoguan_core import _xiaoyou_core_skill_context

    context = _xiaoyou_core_skill_context(identity=_identity("CeShi", "teacher"))
    assert "xiaoyou-core" in context
    assert "user_id=CeShi" in context
    assert "role=teacher" in context
    assert "JinWenJie" not in context


def test_global_user_memory_migrates_to_isolated_profiles(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.workstyle_profiles import query_person_workstyle_profile
    from scripts.migrate_global_user_memory_v1 import NEUTRAL_MEMORY, run

    memory = tmp_path / "memories" / "USER.md"
    memory.parent.mkdir(parents=True)
    memory.write_text(
        "金总沟通偏好直接简洁，先给结论和重点。\n§\n"
        "李老师（CeShi）做任务时不喜欢反复追问，事实完整时直接闭环。\n§\n"
        "无论是谁都不能伪造业务成功。",
        encoding="utf-8",
    )
    dry_run = run(memory_file=memory, data_dir=tmp_path, apply=False)
    assert dry_run["candidate_count"] == 2
    assert memory.read_text(encoding="utf-8").startswith("金总")

    applied = run(memory_file=memory, data_dir=tmp_path, apply=True)
    assert applied["writeback_verified"] is True
    assert applied["saved_count"] == 2
    assert memory.read_text(encoding="utf-8").strip() == NEUTRAL_MEMORY.strip()
    assert Path(applied["backup_file"]).exists()

    store = TuoguanStore(tmp_path)
    boss_profile = query_person_workstyle_profile(
        store,
        identity=_identity("JinWenJie", "boss"),
        target_user_id="JinWenJie",
        target_role="boss",
    )
    teacher_profile = query_person_workstyle_profile(
        store,
        identity=_identity("CeShi", "teacher"),
        target_user_id="CeShi",
        target_role="teacher",
    )
    assert boss_profile["preference_count"] == 1
    assert teacher_profile["preference_count"] == 1
    assert "李老师" not in boss_profile["rendered_text"]


def test_xiaoyou_skill_bundle_installs_with_writeback(tmp_path: Path):
    from scripts.install_xiaoyou_skill_bundle import run

    project_root = Path(__file__).resolve().parents[3]
    hermes_home = tmp_path / "hermes-home"
    dry_run = run(hermes_home=hermes_home, project_root=project_root, apply=False)
    assert dry_run["ok"] is True
    assert not hermes_home.exists()

    applied = run(hermes_home=hermes_home, project_root=project_root, apply=True)
    assert applied["writeback_verified"] is True
    assert (hermes_home / "skill-bundles" / "xiaoyou-core.yaml").exists()
    assert (hermes_home / "skills" / "xiaoyou-core-contract" / "SKILL.md").exists()
