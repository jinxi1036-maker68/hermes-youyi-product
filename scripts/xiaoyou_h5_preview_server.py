"""Local, synthetic-data preview server for the frozen Xiaoyou H5 workbench."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from plugins.tuoguan_core.dashboard_workbench_v1 import DASHBOARD_WORKBENCH_V1_HTML


NOW = "2026-08-24T09:35:00+08:00"


def _teacher() -> dict:
    return {
        "role": "teacher", "display_name": "示例老师", "generated_at": NOW,
        "summary": {"student_count": 12, "open_task_count": 1, "today_valid_records": 2},
        "open_tasks": [{"id": "task-1", "title": "完成一名学生的家长沟通", "student_name": "学生甲", "status": "in_progress", "level": "A", "due_at": "2026-08-24T18:30:00+08:00", "source_label": "老板安排"}],
        "suggested_records": [{"student_name": "周同学", "reason": "本周还缺一条成长证据"}],
        "student_completion": [{"student_name": name, "campus_id": "一店"} for name in ("学生甲", "周同学", "学生乙")],
        "today_feedback": [{"student_name": "学生乙", "summary": "今天能够主动检查计算步骤。", "created_at": NOW, "use_cases": ["成长报告"]}],
        "materials": {"quality_records": [{"student_name": "学生乙", "summary": "主动检查计算步骤，错误率下降。", "created_at": NOW, "use_cases": ["成长报告", "家长沟通"]}], "parent_communication": [], "growth_reports": [], "student_tags": []},
    }


def _manager() -> dict:
    return {
        "role": "manager", "display_name": "示例店长", "generated_at": NOW,
        "summary": {"student_count": 36, "open_task_count": 3, "today_valid_records": 7, "high_risk_count": 1},
        "task_status": {"safety_risks": [], "open_by_status": {"in_progress": 2}, "closure_evidence": {"missing_evidence_count": 1, "recent_completed_count": 2, "missing_evidence_tasks": [{"task_id": "task-2", "task_title": "补充家长沟通结果", "student_name": "学生甲", "status": "waiting_evidence", "level": "A", "missing_fields": ["家长态度", "下一步安排"]}], "recent_events": []}},
        "hermes_assistant": {"top_suggestions": [{"title": "帮示例老师看一下未闭环任务", "detail": "家长沟通还缺结果证据。"}], "pending_confirmations": []},
        "student_roster": [{"student_name": "学生甲", "teacher_userid": "示例老师", "campus_id": "一店"}, {"student_name": "学生乙", "teacher_userid": "示例老师", "campus_id": "一店"}],
        "risk": {"priority_students": [{"student_name": "学生甲", "reason": "家长沟通结果待核实"}], "long_unrecorded_students": []},
        "execution": {"teachers": [{"teacher_id": "teacher-1", "teacher_name": "示例老师", "student_count": 12, "open_task_count": 1, "stale_students": ["周同学"], "missing_growth_report_students": []}], "managers": []},
    }


def _boss() -> dict:
    return {
        "role": "boss", "display_name": "机构负责人", "generated_at": NOW,
        "summary": {"student_count": 68, "today_valid_records": 14, "open_task_count": 4, "high_risk_count": 1},
        "hermes_employee": {"open_questions": [{"question": "是否批准数学基础提升项目进入小范围验证？", "status": "open", "focus_key": "opportunity:math"}], "focus_brief": {"title": "提升家校沟通完成率", "status": "active", "phase": "证据收集", "blocker": "两名学生还缺家长反馈", "next_action": "先向责任老师核实结果"}, "daily_reports": {"morning": {"status": "sent", "sent_at": NOW}, "evening": {"status": "pending"}}, "work_items": [{"title": "家校沟通目标推进", "status": "active", "phase": "执行中", "next_action": "等待两条沟通结果"}], "relationship_presence": {"items": [{"target_name": "示例老师", "status": "sent", "reason": "核实任务结果", "suggested_send_at": NOW}]}},
        "task_status": {"safety_risks": []},
        "execution": {"teachers": [{"teacher_id": "teacher-1", "teacher_name": "示例老师", "student_count": 12, "open_task_count": 1, "stale_students": ["周同学"]}], "managers": []},
        "project_opportunities": {"data_state": "current", "source_updated_at": NOW, "visible_count": 1, "items": [{"opportunity_id": "opp-1", "title": "数学基础提升", "hypothesis": "多个学生在计算与审题上出现共同薄弱点。", "status": "decision_pending", "affected_student_count": 8, "teacher_count": 3, "coverage_rate": 72, "evidence_period": "8月2日—8月23日", "xiaoyou_judgement": "问题跨学生、跨老师持续出现，值得先做低成本验证。", "missing_facts": ["家长付费意愿"], "validation_plan": {"summary": "抽样评估8名学生并访谈3名责任老师"}, "requires_owner_decision": True, "boundary": "待验证候选，不等于正式项目"}]},
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        token = parse_qs(parsed.query).get("token", ["boss"])[0]
        role = token if token in {"teacher", "manager", "boss"} else "boss"
        if parsed.path == "/tuoguan/dashboard":
            self._send(DASHBOARD_WORKBENCH_V1_HTML.replace("__TOKEN__", role), "text/html; charset=utf-8")
            return
        if parsed.path == "/tuoguan/api/me":
            payload = {"user_id": role + "-preview", "role": role, "role_label": {"teacher": "老师", "manager": "店长", "boss": "老板"}[role], "display_name": {"teacher": "示例老师", "manager": "示例店长", "boss": "机构负责人"}[role], "default_program_id": "global" if role == "boss" else "regular_tuoguan", "program_scope": ["正式托管"]}
            self._send(json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if parsed.path == "/tuoguan/api/teacher" and role == "teacher":
            self._send(json.dumps(_teacher(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if parsed.path == "/tuoguan/api/boss" and role in {"manager", "boss"}:
            self._send(json.dumps(_manager() if role == "manager" else _boss(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        self.send_error(404)

    def _send(self, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Xiaoyou H5 preview: http://{args.host}:{args.port}/tuoguan/dashboard?token=boss", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
