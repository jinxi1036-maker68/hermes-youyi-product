from __future__ import annotations

import pytest


def test_timer_parser_never_returns_activated_oneshot_service():
    from scripts.xiaoyou_deploy_guard import extract_timer_units

    output = """
Wed 2026-08-12 13:30:00 CST  12min left Wed 2026-08-12 13:00:00 CST hermes-youyi-autonomous-wakeup.timer hermes-youyi-autonomous-wakeup.service
Wed 2026-08-12 21:00:00 CST  7h left Tue 2026-08-11 21:00:00 CST hermes-youyi-daily-evening-report.timer hermes-youyi-daily-evening-report.service
"""

    assert extract_timer_units(output) == [
        "hermes-youyi-autonomous-wakeup.timer",
        "hermes-youyi-daily-evening-report.timer",
    ]


def test_deploy_guard_rejects_starting_any_oneshot_service():
    from scripts.xiaoyou_deploy_guard import systemctl

    with pytest.raises(ValueError, match="unsafe_systemctl_target"):
        systemctl("start", "hermes-youyi-daily-evening-report.service")


def test_deploy_guard_allows_main_service_and_timer_only():
    from scripts.xiaoyou_deploy_guard import systemctl

    assert systemctl("restart", "hermes-youyi-019.service")["executed"] is False
    assert systemctl("start", "hermes-youyi-daily-evening-report.timer")["executed"] is False
