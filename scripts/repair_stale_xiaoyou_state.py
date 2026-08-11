from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any
import uuid


STALE_TERMS = ("昨晚", "一直发", "一直提醒", "李老师沟通结果", "明天10点")
STALE_FOCUS_KEYS = {"report:xiaojin_parent_comm_20260806"}
CLOSED_STATUSES = {"resolved", "superseded", "closed", "done", "completed"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_before_cutoff(row: dict[str, Any], cutoff: datetime) -> bool:
    for key in ("updated_at", "created_at", "queued_at", "sent_at"):
        parsed = _parse_dt(row.get(key))
        if parsed is None:
            continue
        if parsed.tzinfo is None and cutoff.tzinfo is not None:
            parsed = parsed.replace(tzinfo=cutoff.tzinfo)
        return parsed < cutoff
    return False


def _text(row: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in (
        "focus_key",
        "title",
        "focus_summary",
        "question_text",
        "source_text",
        "current_waiting",
        "blocked_by",
        "next_actions",
        "pending_judgements",
    ):
        value = row.get(key)
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, (dict, list)):
            chunks.append(json.dumps(value, ensure_ascii=False))
        elif value is not None:
            chunks.append(str(value))
    return "\n".join(chunks)


def _fold_latest_status(rows: list[dict[str, Any]], id_key: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for row in rows:
        item_id = str(row.get(id_key) or "").strip()
        if not item_id:
            continue
        status = str(row.get("status") or "").strip()
        if status:
            statuses[item_id] = status
    return statuses


def repair(data_dir: Path, *, cutoff: datetime, apply: bool) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    attention_path = data_dir / "attention_threads.jsonl"
    work_path = data_dir / "hermes_work_items.jsonl"
    attention_rows = _read_jsonl(attention_path)
    work_rows = _read_jsonl(work_path)
    attention_status = _fold_latest_status(attention_rows, "attention_id")
    work_status = _fold_latest_status(work_rows, "work_item_id")

    attention_matches: list[dict[str, Any]] = []
    attention_seen: set[str] = set()
    for row in attention_rows:
        if str(row.get("record_type") or "") == "attention_thread_update":
            continue
        attention_id = str(row.get("attention_id") or "").strip()
        if not attention_id or attention_status.get(attention_id, str(row.get("status") or "")) in CLOSED_STATUSES:
            continue
        if attention_id in attention_seen:
            continue
        text = _text(row)
        if not _is_before_cutoff(row, cutoff):
            continue
        if str(row.get("focus_key") or "") in STALE_FOCUS_KEYS or any(term in text for term in STALE_TERMS):
            attention_seen.add(attention_id)
            attention_matches.append(row)

    work_matches: list[dict[str, Any]] = []
    work_seen: set[str] = set()
    for row in work_rows:
        if str(row.get("record_type") or "") == "hermes_work_item_update":
            continue
        work_item_id = str(row.get("work_item_id") or "").strip()
        if not work_item_id or work_status.get(work_item_id, str(row.get("status") or "")) in CLOSED_STATUSES:
            continue
        if work_item_id in work_seen:
            continue
        text = _text(row)
        if not _is_before_cutoff(row, cutoff):
            continue
        # Work items can be long-running goals whose history contains stale
        # wording. Only close the explicit legacy report focus, not a broader
        # goal item that merely mentions an old question.
        if str(row.get("focus_key") or "") in STALE_FOCUS_KEYS:
            work_seen.add(work_item_id)
            work_matches.append(row)

    if apply:
        for row in attention_matches:
            _append_jsonl(
                attention_path,
                {
                    "record_type": "attention_thread_update",
                    "attention_id": str(row.get("attention_id") or ""),
                    "status": "superseded",
                    "resolution_note": "P0 repair: stale owner reminder no longer belongs in current daily report.",
                    "source_text": "repair_stale_xiaoyou_state.py",
                    "created_at": now,
                    "source": {"actor_user_id": "system", "operation_id": f"repair_stale_attention:{uuid.uuid4().hex[:12]}"},
                    "auto_effects": {"sends_parent_messages": False, "forces_next_action": False, "changes_router": False},
                },
            )
        for row in work_matches:
            _append_jsonl(
                work_path,
                {
                    "record_type": "hermes_work_item_update",
                    "work_item_id": str(row.get("work_item_id") or ""),
                    "status": "superseded",
                    "stop_reason": "P0 repair: stale daily-report material superseded by newer state.",
                    "update_text": "旧早报/晚报材料已收口，不再作为今日重点或需确认项。",
                    "source_text": "repair_stale_xiaoyou_state.py",
                    "created_at": now,
                    "source": {"actor_user_id": "system", "operation_id": f"repair_stale_work:{uuid.uuid4().hex[:12]}"},
                    "auto_effects": {"sends_parent_messages": False, "forces_next_action": False, "changes_router": False},
                },
            )

    return {
        "ok": True,
        "dry_run": not apply,
        "data_dir": str(data_dir),
        "cutoff": cutoff.isoformat(timespec="seconds"),
        "attention_match_count": len(attention_matches),
        "work_item_match_count": len(work_matches),
        "attention_ids": [str(row.get("attention_id") or "") for row in attention_matches],
        "work_item_ids": [str(row.get("work_item_id") or "") for row in work_matches],
        "attention_summaries": [
            {
                "attention_id": str(row.get("attention_id") or ""),
                "focus_key": str(row.get("focus_key") or ""),
                "status": str(row.get("status") or ""),
                "text": _text(row)[:160],
            }
            for row in attention_matches
        ],
        "work_item_summaries": [
            {
                "work_item_id": str(row.get("work_item_id") or ""),
                "focus_key": str(row.get("focus_key") or ""),
                "status": str(row.get("status") or ""),
                "text": _text(row)[:160],
            }
            for row in work_matches
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Mark stale Xiaoyou reminder/work material as superseded.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--cutoff", default="2026-08-10T00:00:00+08:00")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    cutoff = _parse_dt(args.cutoff)
    if cutoff is None:
        raise SystemExit(f"invalid --cutoff: {args.cutoff}")
    print(json.dumps(repair(Path(args.data_dir), cutoff=cutoff, apply=bool(args.apply)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
