"""Create a non-production fixture for the XiaoYou usability convergence recipe.

The fixture is deliberately synthetic.  It contains no copied Workspace,
directory, recipient, or conversation data and is never used by a deployed
service.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _write(root: Path, name: str, value: object) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def create(workspace: Path) -> None:
    data = workspace / "data"
    owner_id = "owner-test"
    active_teacher_id = "teacher-active"
    historical_teacher_id = "teacher-historical"
    _write(
        data,
        "wecom_whitelist.json",
        {
            "super_users": [owner_id],
            "allowed_users": [active_teacher_id],
            "user_roles": {owner_id: "boss", active_teacher_id: "teacher"},
        },
    )
    _write(
        data,
        "staff.json",
        {
            owner_id: {"business_name": "机构负责人", "role": "boss", "status": "active"},
            active_teacher_id: {"business_name": "示例老师", "role": "teacher", "status": "active"},
            historical_teacher_id: {"business_name": "历史测试老师", "role": "teacher", "status": "left"},
        },
    )
    _write(
        data,
        "teacher_wecom_map.json",
        {"历史测试老师": historical_teacher_id, "示例老师": active_teacher_id},
    )
    _write(
        data,
        "students.json",
        {
            f"学生{i:02d}": {
                "student_id": f"student-{i:02d}",
                "teacher": active_teacher_id,
                "status": "active",
                "grade": "一年级",
                "class_name": "测试班",
            }
            for i in range(1, 37)
        },
    )
    for name, value in {
        "tasks.json": [],
        "records.json": [],
        "summer_enrollments.json": [],
        "student_service_relations.json": [],
        "work_runtime_items.json": [],
        "durable_reply_outbox.json": [],
    }.items():
        _write(data, name, value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    create(args.workspace.resolve())
    print(args.workspace.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
