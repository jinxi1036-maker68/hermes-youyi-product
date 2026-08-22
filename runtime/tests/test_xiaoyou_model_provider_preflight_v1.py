from __future__ import annotations

import httpx


def test_provider_preflight_reports_capability_without_secrets_or_text():
    from scripts.xiaoyou_model_provider_preflight import ModelRoute, probe_routes

    def handler(request: httpx.Request) -> httpx.Response:
        if "working" in request.url.host:
            return httpx.Response(200, json={
                "choices": [{"message": {"tool_calls": [{"id": "call-1", "function": {"name": "health_ping"}}]}}],
            })
        return httpx.Response(429, json={"error": "limited-private-body"})

    report = probe_routes([
        ModelRoute(0, "custom", "limited-model", "https://limited.test/v1", "limited-secret"),
        ModelRoute(1, "custom", "working-model", "https://working.test/v1", "working-secret"),
    ], transport=httpx.MockTransport(handler))

    assert report["status"] == "pass"
    assert report["passing_route_count"] == 1
    assert report["routes"][0]["http_status"] == 429
    assert report["routes"][1]["tool_call_present"] is True
    assert "secret" not in str(report)
    assert "limited-private-body" not in str(report)
