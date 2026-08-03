#!/usr/bin/env python3
"""No-agent operational entrypoint for Hermes tutoring schedules."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.tuoguan_core.runtime import (
    REMINDERS,
    run_backup,
    run_escalation_delivery,
    run_report_delivery,
    build_growth_review_digest,
)
from plugins.tuoguan_core.dashboard_builder import refresh_dashboard_cache
from plugins.tuoguan_core.research import collect_industry_learning_candidates, collect_public_research, format_research_report
from plugins.tuoguan_core.data_upgrade import upgrade_business_data
from plugins.tuoguan_core.store import TuoguanStore


def _send(target: str, content: str):
    from tools.send_message_tool import send_message_tool

    try:
        raw = send_message_tool(
            {
                "action": "send",
                "target": f"wecom_callback:{target}",
                "message": content,
            }
        )
    except RuntimeError as exc:
        err = str(exc)
        if "No home channel" in err or "not connected" in err or "Could not resolve" in err:
            print(content)
            raise RuntimeError(f"message target unavailable; printed fallback only: {err}") from exc
        raise
    response = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(response, dict) and response.get("error"):
        err = str(response["error"])
        if "No home channel" in err or "not connected" in err or "Could not resolve" in err:
            print(content)
            raise RuntimeError(f"message target unavailable; printed fallback only: {err}")
        raise RuntimeError(err)
    return response


def _role_recipient(store: TuoguanStore, role: str) -> str:
    data = store.read_json("wecom_whitelist.json", {})
    roles = data.get("user_roles") or {} if isinstance(data, dict) else {}
    if role == "manager" and isinstance(data, dict):
        manager_id = str(data.get("manager_id") or "").strip()
        if manager_id:
            return manager_id
        manager_ids = data.get("manager_ids") or []
        if manager_ids:
            return str(manager_ids[0])
    for userid, configured_role in roles.items():
        if role == "manager" and configured_role == "manager":
            return str(userid)
        if role == "boss" and configured_role in {"boss", "admin", "super_admin"}:
            return str(userid)
    if role == "boss" and isinstance(data, dict) and data.get("super_users"):
        return str(data["super_users"][0])
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("escalation")
    subparsers.add_parser("backup")
    subparsers.add_parser("upgrade-data")
    subparsers.add_parser("growth-review")
    subparsers.add_parser("dashboard-refresh")
    research = subparsers.add_parser("research")
    research.add_argument("--kind", choices=("knowledge", "competitor"), required=True)
    subparsers.add_parser("industry-learning")
    shadow = subparsers.add_parser("multi-agent-shadow")
    shadow.add_argument("--agent-type", choices=("institution_audit_agent", "goal_review_agent", "industry_research_agent"), required=True)
    shadow.add_argument("--question", default="")
    shadow.add_argument("--parent-focus-key", default="")
    shadow.add_argument("--parent-goal-id", default="")
    shadow.add_argument("--parent-work-item-id", default="")
    shadow.add_argument("--no-dedupe", action="store_true")
    report = subparsers.add_parser("report")
    report.add_argument("--role", choices=("manager", "boss"), required=True)
    reminder = subparsers.add_parser("reminder")
    reminder.add_argument("--kind", choices=tuple(REMINDERS), required=True)
    args = parser.parse_args(argv)

    store = TuoguanStore()
    print_success = False
    if args.command == "escalation":
        result = run_escalation_delivery(store, send=_send)
    elif args.command == "backup":
        result = run_backup(store)
    elif args.command == "upgrade-data":
        result = upgrade_business_data(store)
    elif args.command == "growth-review":
        print(build_growth_review_digest(store))
        return 0
    elif args.command == "dashboard-refresh":
        result = refresh_dashboard_cache(store)
        print_success = True
    elif args.command == "research":
        from tools.web_tools import web_search_tool

        result = collect_public_research(
            store,
            kind=args.kind,
            search=lambda query, limit: web_search_tool(query, limit=limit),
        )
        print(format_research_report(result))
        return 0
    elif args.command == "industry-learning":
        from tools.web_tools import web_search_tool

        result = collect_industry_learning_candidates(
            store,
            search=lambda query, limit: web_search_tool(query, limit=limit),
        )
        print(format_research_report(result))
        candidate = result.get("industry_learning_candidate") or {}
        if candidate:
            print(candidate.get("rendered_text") or candidate.get("message") or "")
        return 0
    elif args.command == "multi-agent-shadow":
        from plugins.tuoguan_core.multi_agent_shadow_runner import run_multi_agent_shadow_once

        result = run_multi_agent_shadow_once(
            store,
            agent_type=args.agent_type,
            question=args.question,
            parent_focus_key=args.parent_focus_key,
            parent_goal_id=args.parent_goal_id,
            parent_work_item_id=args.parent_work_item_id,
            dedupe_per_day=not args.no_dedupe,
        )
        print(result.get("rendered_text") or json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    elif args.command == "report":
        recipient = _role_recipient(store, args.role)
        result = run_report_delivery(
            store,
            role=args.role,
            recipient=recipient,
            send=_send,
        )
    else:
        recipient, content = REMINDERS[args.kind]
        _send(recipient, content)
        result = {"sent": 1, "failed": 0}

    if result.get("failed"):
        print(json.dumps(result, ensure_ascii=False))
        return 1
    if print_success:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
