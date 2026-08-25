from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_upstream_proxy_parser_does_not_require_or_expose_credentials():
    from scripts.agnes_egress_proxy import parse_upstream_proxy

    proxy = parse_upstream_proxy("https://user:private%2Dsecret@proxy.example:8443")

    assert proxy.scheme == "https"
    assert proxy.host == "proxy.example"
    assert proxy.port == 8443
    assert proxy.username == "user"
    assert proxy.password == "private-secret"


def test_connect_parser_only_accepts_connect_authority():
    from scripts.agnes_egress_proxy import parse_connect_target

    assert parse_connect_target(b"CONNECT apihub.agnes-ai.com:443 HTTP/1.1") == ("apihub.agnes-ai.com", 443)
    with pytest.raises(ValueError, match="https_connect_required"):
        parse_connect_target(b"GET https://apihub.agnes-ai.com/ HTTP/1.1")


def test_upstream_connect_request_uses_basic_auth_without_logging_secret():
    from scripts.agnes_egress_proxy import UpstreamProxy, build_upstream_connect_request

    request = build_upstream_connect_request(
        "apihub.agnes-ai.com", 443,
        UpstreamProxy("http", "proxy.example", 8080, "user", "secret"),
    ).decode("ascii")

    assert request.startswith("CONNECT apihub.agnes-ai.com:443 HTTP/1.1")
    assert "Proxy-Authorization: Basic dXNlcjpzZWNyZXQ=" in request


def test_secret_file_must_be_owner_only(tmp_path: Path):
    from scripts.agnes_egress_proxy import ProxyConfigurationError, read_secret_file

    secret = tmp_path / "proxy.secret"
    secret.write_text("http://proxy.example:8080", encoding="utf-8")
    os.chmod(secret, 0o644)
    if os.name != "nt":
        with pytest.raises(ProxyConfigurationError, match="permissions"):
            read_secret_file(secret)
    os.chmod(secret, 0o600)
    assert read_secret_file(secret) == "http://proxy.example:8080"


def test_systemd_egress_switch_is_loopback_only_and_requires_the_egress_service():
    root = Path(__file__).resolve().parents[2]
    text = (root / "systemd" / "hermes-youyi-019.service.d" / "30-agnes-egress.conf").read_text(encoding="utf-8")

    assert "Requires=hermes-youyi-agnes-egress.service" in text
    assert "After=hermes-youyi-agnes-egress.service" in text
    assert "http://127.0.0.1:18889" in text
    assert "HERMES_AGNES_EGRESS_LABEL=agnes_dedicated_upstream" in text
