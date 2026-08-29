from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


CN_TZ = timezone(timedelta(hours=8))


def _write_json(root: Path, name: str, payload) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _store(root: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(root, "write_guard_config.json", {"enabled": True})
    _write_json(root, "notification_outbox.json", [])
    _write_json(root, "tasks.json", [])
    _write_json(root, "students.json", {})
    _write_json(root, "records.json", [])
    _write_json(root, "staff.json", {})
    _write_json(root, "wecom_whitelist.json", {"super_users": ["boss1"], "user_roles": {"boss1": "boss"}})
    _write_json(root, "teacher_wecom_map.json", {"金总": "boss1"})
    return TuoguanStore(root)


def test_daily_delivery_in_progress_is_a_successful_handoff(tmp_path):
    from plugins.tuoguan_core.daily_reporter import _await_daily_delivery_terminal

    store = _store(tmp_path)
    _write_json(tmp_path, "notification_outbox.json", [{
        "id": "autonomous_daily_report:20260828:morning",
        "status": "sending",
        "lease_expires_at": (datetime.now().astimezone() + timedelta(minutes=2)).isoformat(timespec="seconds"),
    }])

    result = asyncio.run(_await_daily_delivery_terminal(
        store,
        "autonomous_daily_report:20260828:morning",
        wait_seconds=0,
        poll_interval_seconds=0.05,
    ))

    assert result["ok"] is True
    assert result["terminal"] is False
    assert result["delivery_status"] == "delivery_in_progress"
    assert result["lease_state"] == "sending_valid_lease"


def test_dashboard_freshness_uses_snapshot_time_not_h5_request_time():
    from plugins.tuoguan_core.dashboard_builder import dashboard_cache_freshness

    now = datetime(2026, 8, 28, 10, 0, tzinfo=CN_TZ)
    fresh = dashboard_cache_freshness(
        {"generated_at": "2026-08-28T09:20:00+08:00"},
        now=now,
        max_age_seconds=45 * 60,
    )
    stale = dashboard_cache_freshness(
        {"generated_at": "2026-08-28T09:00:00+08:00"},
        now=now,
        max_age_seconds=45 * 60,
    )

    assert fresh["freshness_state"] == "current"
    assert stale["freshness_state"] == "stale"


def test_dashboard_runner_only_writes_projection(monkeypatch, tmp_path):
    import plugins.tuoguan_core.dashboard_refresh_runner as runner

    store = _store(tmp_path)
    monkeypatch.setattr(runner, "refresh_dashboard_cache", lambda *_args, **_kwargs: {
        "generated_at": "2026-08-28T10:00:00+08:00",
        "freshness_state": "current",
        "source_versions": {"tasks.json": "1:2"},
    })

    result = runner.run_dashboard_refresh_once(store, now=datetime(2026, 8, 28, 10, 0, tzinfo=CN_TZ))

    assert result["ok"] is True
    assert result["model_called"] is False
    assert result["outbound_count"] == 0
    assert result["writes"] == ["dashboard_cache.json"]


def test_autonomous_preflight_skips_model_without_recovery_material():
    from plugins.tuoguan_core.autonomous_employee_loop import autonomous_model_preflight

    result = autonomous_model_preflight({"timestamp": "2026-08-28T10:00:00+08:00", "materials_summary": {}})

    assert result["model_required"] is False
    assert result["reasons"] == []


def test_autonomous_preflight_keeps_due_goal_actions_visible_to_model():
    from plugins.tuoguan_core.autonomous_employee_loop import autonomous_model_preflight

    result = autonomous_model_preflight({"materials_summary": {"due_goal_action_count": 1}})

    assert result["model_required"] is True
    assert result["reasons"] == ["due_goal_action_count"]


def test_autonomous_timeout_detector_uses_reports_not_chat_trace_only(tmp_path):
    from plugins.tuoguan_core.supervision import _autonomous_timeout_findings

    store = _store(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    now = datetime(2026, 8, 28, 10, 0, tzinfo=CN_TZ)
    for index in range(3):
        stamp = now - timedelta(minutes=(2 - index) * 30)
        (reports / f"autonomous-wakeup-v1-20260828-0{8 + index}0000.json").write_text(json.dumps({
            "generated_at": stamp.isoformat(timespec="seconds"),
            "ok": False,
            "run_status": "degraded_model_timeout",
            "model_error_class": "timeout",
        }), encoding="utf-8")

    findings = _autonomous_timeout_findings(store, now=now)

    assert len(findings) == 1
    assert findings[0]["category"] == "autonomous_consecutive_timeout"
    assert findings[0]["severity"] == "p1"


def test_runtime_config_audit_never_reads_or_reports_secret(monkeypatch, tmp_path):
    from plugins.tuoguan_core.supervision import _runtime_config_drift_findings

    store = _store(tmp_path / "data")
    config = tmp_path / "config.yaml"
    config.write_text(
        "model:\n"
        "  model: agnes-2.5-flash\n"
        "  base_url: https://apihub.agnes-ai.cn/v1\n"
        "  api_key: private-value\n"
        "  request_timeout_seconds: 9\n"
        "providers:\n"
        "  custom:\n"
        "    models:\n"
        "      agnes-2.5-flash:\n"
        "        timeout_seconds: 9\n"
        "compression:\n"
        "  threshold_tokens: 24000\n"
        "fallback_providers: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(config))

    findings = _runtime_config_drift_findings(store)

    assert findings == []
    assert "private-value" not in str(findings)


def test_runtime_config_audit_requires_an_explicit_path_when_enabled(monkeypatch, tmp_path):
    from plugins.tuoguan_core.supervision import _runtime_config_drift_findings

    store = _store(tmp_path)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HERMES_SUPERVISION_REQUIRE_CONFIG_PATH", "1")

    findings = _runtime_config_drift_findings(store)

    assert findings[0]["category"] == "runtime_config_drift"
    assert findings[0]["scope"] == "HERMES_CONFIG_PATH"


def test_supervision_verifies_a_stale_dashboard_finding_after_a_fresh_projection(tmp_path):
    from plugins.tuoguan_core.dashboard_builder import refresh_dashboard_cache
    from plugins.tuoguan_core.supervision_runner import run_supervision_once

    store = _store(tmp_path)
    first_now = datetime(2026, 8, 28, 10, 0, tzinfo=CN_TZ)
    _write_json(tmp_path, "dashboard_cache.json", {"schema_version": 1, "generated_at": "2026-08-28T08:00:00+08:00"})

    first = run_supervision_once(store, now=first_now, apply_repairs=False, write_report=False)
    assert any(row["category"] == "dashboard_projection_stale" for row in first["findings"])

    refresh_dashboard_cache(store, now=first_now)
    second = run_supervision_once(
        store,
        now=first_now + timedelta(minutes=1),
        apply_repairs=False,
        write_report=False,
    )

    assert not any(row["category"] == "dashboard_projection_stale" for row in second["findings"])
    assert any(row["category"] == "dashboard_projection_stale" for row in second["verified_findings"])


def test_supervision_health_does_not_count_terminal_p0_history_as_open(tmp_path):
    from plugins.tuoguan_core.supervision import SUPERVISION_FINDINGS_FILE, query_supervision_status
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _store(tmp_path)
    with authorized_system_write(store.data_dir, job_name="test_supervision_history", allowed_files={SUPERVISION_FINDINGS_FILE}):
        store.append_jsonl_verified(SUPERVISION_FINDINGS_FILE, {
            "record_type": "supervision_finding",
            "finding_id": "historical-p0",
            "tenant_id": "youyi_tuoguan",
            "fingerprint": "historical-p0",
            "category": "dashboard_projection_stale",
            "severity": "p0",
            "state": "verified",
            "summary": "historical resolved finding",
            "created_at": "2026-08-28T10:00:00+08:00",
        })

    result = query_supervision_status(store)

    assert result["active_finding_count"] == 0
    assert result["p0_open_count"] == 0
