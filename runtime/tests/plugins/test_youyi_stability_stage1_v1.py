from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_atomic_json_write_retries_transient_replace_denial(tmp_path, monkeypatch):
    import utils

    target = tmp_path / "state.json"
    original_replace = utils.os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise PermissionError("transient file lock")
        return original_replace(source, destination)

    monkeypatch.setattr(utils.os, "replace", flaky_replace)
    utils.atomic_json_write(target, {"ok": True})

    assert attempts == 4
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_update_json_preserves_concurrent_outbox_appends(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.write_guard import authorized_system_write

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "notification_outbox.json", [])
    errors: list[BaseException] = []

    def worker(prefix: str) -> None:
        try:
            store = TuoguanStore(tmp_path)
            for index in range(30):
                row = {
                    "id": f"{prefix}:{index}",
                    "status": "pending",
                    "delivery_mode": "direct_wecom",
                    "touser": "boss1",
                    "content": "test",
                }

                def append(outbox):
                    outbox = outbox if isinstance(outbox, list) else []
                    outbox.append(row)
                    return outbox

                with authorized_system_write(
                    store.data_dir,
                    job_name="test_concurrent_outbox_append",
                    allowed_files={"notification_outbox.json"},
                ):
                    store.update_json("notification_outbox.json", [], append)
        except BaseException as exc:  # pragma: no cover - surfaced after join
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"worker{idx}",)) for idx in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    ids = {item["id"] for item in outbox}
    assert len(outbox) == 120
    assert len(ids) == 120


def test_expired_sending_outbox_item_becomes_result_unknown_not_resent(tmp_path, monkeypatch):
    from plugins.tuoguan_core import _drain_notification_outbox

    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(tmp_path))
    expired_start = datetime.now(timezone(timedelta(hours=8))) - timedelta(minutes=20)
    expired_end = expired_start + timedelta(minutes=5)
    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "task1:teacher:task_due",
                "status": "sending",
                "lease_id": "lease-before-crash",
                "lease_owner": "notification_outbox_delivery",
                "lease_started_at": expired_start.isoformat(timespec="seconds"),
                "lease_expires_at": expired_end.isoformat(timespec="seconds"),
                "delivery_mode": "direct_wecom",
                "notification_type": "task_notification",
                "action": "task_due",
                "task_id": "task1",
                "touser": "teacher1",
                "content": "李老师，这条任务需要回执。",
                "created_at": "2026-08-09T08:00:00+08:00",
                "attempt_count": 1,
            }
        ],
    )

    class Adapter:
        async def send(self, target, content, metadata=None):
            raise AssertionError("expired sending items must be marked result_unknown before retry")

    asyncio.run(_drain_notification_outbox(Adapter()))

    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox[0]["status"] == "result_unknown"
    assert outbox[0]["last_error"] == "send_result_unknown_after_lease_expired"


def test_drain_claims_before_send_and_writes_single_receipt(tmp_path, monkeypatch):
    from plugins.tuoguan_core import _drain_notification_outbox

    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(tmp_path))
    now = datetime.now(timezone(timedelta(hours=8)))
    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "task2:teacher:task_due",
                "status": "pending",
                "delivery_mode": "direct_wecom",
                "notification_type": "task_notification",
                "action": "task_due",
                "task_id": "task2",
                "touser": "teacher1",
                "content": "李老师，这条任务需要回执。",
                "created_at": now.isoformat(timespec="seconds"),
                "attempt_count": 0,
            }
        ],
    )

    class Result:
        success = True
        message_id = "msg_task2"
        error = ""

    class Adapter:
        async def send(self, target, content, metadata=None):
            outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
            assert outbox[0]["status"] == "sending"
            assert outbox[0]["lease_id"]
            assert metadata["outbox_lease_id"] == outbox[0]["lease_id"]
            return Result()

    asyncio.run(_drain_notification_outbox(Adapter()))

    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox[0]["status"] == "sent"
    assert outbox[0]["attempt_count"] == 1
    assert outbox[0]["message_id"] == "msg_task2"
    assert "lease_id" not in outbox[0]
