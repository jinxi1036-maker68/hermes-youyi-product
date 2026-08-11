"""Move person-specific Hermes USER.md notes into isolated workstyle profiles."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile


NEUTRAL_MEMORY = """当前对话人的身份、角色和权限只来自企业微信可信身份与权限工具。
USER.md 不保存任何具体个人的画像、沟通偏好或任务习惯；这些内容只保存在按企业微信身份隔离的工作方式档案中。
每轮只能读取当前人的角色、有效偏好、当前任务和必要机构事实，不得把老板、店长或其他老师的个人档案注入当前会话。
员工手册是身份、业务常识和判断框架，不是关键词 Router。真实数据、写入和外发必须经过可信工具、权限、审计、幂等和写后反查。
"""

PERSON_MARKERS = {
    "JinWenJie": ("金总", "boss", ("金总", "老板")),
    "CeShi": ("李老师", "teacher", ("李老师", "CeShi", "测试老师")),
}

HIGH_RISK_SENTENCE_TERMS = ("删除", "工资", "薪资", "权限", "绩效", "联系家长", "安全事件", "改制度")
WORKSTYLE_TERMS = (
    "简洁", "简短", "直接", "结论", "重点", "不要", "不喜欢", "希望", "偏好", "追问", "提醒",
    "任务", "闭环", "完整", "列表", "确认", "沟通", "回答", "下一步", "高效", "流程",
)


def split_memory(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n?\s*§\s*\n?", str(text or "")) if part.strip()]


def migration_candidates(text: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for paragraph in split_memory(text):
        target = next(
            ((user_id, name, role) for user_id, (name, role, markers) in PERSON_MARKERS.items() if any(marker in paragraph for marker in markers)),
            None,
        )
        if target is None:
            continue
        sentences = [part.strip() for part in re.split(r"(?<=[。！？])", paragraph) if part.strip()]
        safe_sentences = [
            sentence for sentence in sentences
            if any(term in sentence for term in WORKSTYLE_TERMS)
            and not any(term in sentence for term in HIGH_RISK_SENTENCE_TERMS)
        ]
        rule = "".join(safe_sentences).strip()
        if not rule:
            continue
        user_id, name, role = target
        task_related = any(term in rule for term in ("任务", "闭环", "完成了"))
        report_related = any(term in rule for term in ("汇报", "简洁", "重点", "结论"))
        candidates.append({
            "target_user_id": user_id,
            "target_name": name,
            "target_role": role,
            "scope": "task_followup" if task_related else "all_communication",
            "preference_type": "followup_style" if task_related else ("report_length" if report_related else "other_low_risk"),
            "preference_text": rule,
            "source_text": f"从全局 USER.md 迁移：{rule}",
        })
    return candidates


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def run(*, memory_file: Path, data_dir: Path, apply: bool) -> dict:
    source = memory_file.read_text(encoding="utf-8-sig")
    candidates = migration_candidates(source)
    report = {
        "ok": True,
        "mode": "apply" if apply else "dry_run",
        "memory_file": str(memory_file),
        "candidate_count": len(candidates),
        "targets": sorted({item["target_user_id"] for item in candidates}),
        "saved_count": 0,
        "backup_file": "",
    }
    if not apply:
        report["candidates"] = candidates
        return report

    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.workstyle_profiles import WORKSTYLE_EVENTS_FILE, submit_person_workstyle_preference
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = TuoguanStore(data_dir)
    actor = UserIdentity(
        platform="system",
        platform_user_id="memory_migration_v1",
        canonical_user_id="memory_migration_v1",
        person_name="memory migration",
        role="boss",
        approval_state="approved",
    )
    saved: list[dict] = []
    with authorized_system_write(store.data_dir, job_name="global_user_memory_migration_v1", allowed_files={WORKSTYLE_EVENTS_FILE}):
        for index, candidate in enumerate(candidates):
            result = submit_person_workstyle_preference(
                store,
                identity=actor,
                preference_type=candidate["preference_type"],
                scope=candidate["scope"],
                preference_text=candidate["preference_text"],
                normalized_rule=candidate["preference_text"],
                target_user_id=candidate["target_user_id"],
                target_name=candidate["target_name"],
                target_role=candidate["target_role"],
                source_text=candidate["source_text"],
                source_turn_id="global_USER.md",
                operation_id=f"global-user-memory-migration-v1:{index}",
            )
            if result.get("ok") is not True or result.get("writeback_verified") is not True:
                raise RuntimeError(f"workstyle migration failed at candidate {index}: {result.get('error') or 'writeback_failed'}")
            saved.append(result)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    backup = memory_file.with_name(f"{memory_file.name}.pre-person-isolation-{stamp}.bak")
    backup.write_text(source, encoding="utf-8")
    _atomic_write_text(memory_file, NEUTRAL_MEMORY)
    if memory_file.read_text(encoding="utf-8-sig").strip() != NEUTRAL_MEMORY.strip():
        raise RuntimeError("neutral USER.md writeback verification failed")
    report.update({"saved_count": len(saved), "backup_file": str(backup), "writeback_verified": True})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-file", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = run(memory_file=Path(args.memory_file), data_dir=Path(args.data_dir), apply=bool(args.apply))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
