"""Signed dashboard access tokens for the tutoring-center H5 views."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore


TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60
PARENT_REPORT_TOKEN_TTL_SECONDS = 14 * 24 * 60 * 60
_SECRET_FILE = "dashboard_secret.json"
_ROLE_ALIASES = {
    "admin": "boss",
    "super_admin": "boss",
    "boss": "boss",
    "manager": "manager",
    "teacher": "teacher",
}


class DashboardAuthError(RuntimeError):
    """Raised when a dashboard token is missing, stale, or unauthorized."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DashboardPrincipal:
    user_id: str
    role: str
    expires_at: int


@dataclass(frozen=True)
class ParentReportPrincipal:
    report_id: str
    student_name: str
    expires_at: int


def _now_ts(now: datetime | None = None) -> int:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _read_secret(store: TuoguanStore) -> str:
    data = store.read_json(_SECRET_FILE, {})
    if not isinstance(data, dict):
        return ""
    return str(data.get("secret") or "").strip()


def get_or_create_dashboard_secret(store: TuoguanStore) -> str:
    """Return the HMAC secret, creating it only when a token is being signed."""
    secret = _read_secret(store)
    if secret:
        return secret
    secret = secrets.token_hex(32)
    store.write_json(
        _SECRET_FILE,
        {
            "secret": secret,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "purpose": "tuoguan_dashboard_hmac",
        },
    )
    return secret


def _signature(secret: str, payload_part: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)


def _current_wecom_role(store: TuoguanStore, user_id: str) -> str:
    data = store.read_json("wecom_whitelist.json", {})
    if not isinstance(data, dict):
        return ""
    if user_id in set(data.get("rejected_users") or []):
        return ""
    super_users = {str(item) for item in data.get("super_users") or []}
    allowed_users = {str(item) for item in data.get("allowed_users") or []}
    if user_id not in super_users and user_id not in allowed_users:
        return ""
    roles = data.get("user_roles") or {}
    raw_role = str(roles.get(user_id) or ("boss" if user_id in super_users else "teacher"))
    role = _ROLE_ALIASES.get(raw_role, "")
    summer_managers = {str(item) for item in data.get("summer_manager_ids") or []}
    if user_id in summer_managers and role != "boss":
        role = "manager"
    return role


def sign_dashboard_token(
    identity: UserIdentity,
    store: TuoguanStore,
    *,
    ttl_seconds: int = TOKEN_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    role = _ROLE_ALIASES.get(identity.role, "")
    if role not in {"teacher", "manager", "boss"}:
        raise DashboardAuthError("forbidden", "User role cannot access dashboard")
    uid = str(identity.canonical_user_id or identity.platform_user_id).strip()
    if not uid:
        raise DashboardAuthError("forbidden", "User id is empty")
    current_role = _current_wecom_role(store, uid)
    if current_role != role:
        raise DashboardAuthError("forbidden", "User role is no longer authorized")
    issued_at = _now_ts(now)
    payload: dict[str, Any] = {
        "uid": uid,
        "role": role,
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
    }
    payload_part = _b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    secret = get_or_create_dashboard_secret(store)
    return f"{payload_part}.{_signature(secret, payload_part)}"


def sign_parent_report_token(
    store: TuoguanStore,
    *,
    report_id: str,
    student_name: str,
    ttl_seconds: int = PARENT_REPORT_TOKEN_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    report = _find_growth_report(store, report_id)
    if report is None or report.get("status") != "approved":
        raise DashboardAuthError("forbidden", "Parent report is not approved")
    if str(report.get("student_name") or "") != str(student_name or ""):
        raise DashboardAuthError("forbidden", "Parent report student does not match")
    issued_at = _now_ts(now)
    payload: dict[str, Any] = {
        "scope": "parent_growth_report",
        "report_id": str(report_id).strip(),
        "student_name": str(student_name).strip(),
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
    }
    payload_part = _b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    secret = get_or_create_dashboard_secret(store)
    return f"{payload_part}.{_signature(secret, payload_part)}"


def verify_dashboard_token(
    token: str,
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> DashboardPrincipal:
    if not token:
        raise DashboardAuthError("missing_token", "Token is required")
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError as exc:
        raise DashboardAuthError("bad_token", "Token format is invalid") from exc
    secret = _read_secret(store)
    if not secret:
        raise DashboardAuthError("bad_token", "Dashboard signing secret is missing")
    expected = _signature(secret, payload_part)
    if not hmac.compare_digest(signature_part, expected):
        raise DashboardAuthError("bad_signature", "Token signature is invalid")
    try:
        payload = json.loads(_b64decode(payload_part).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DashboardAuthError("bad_token", "Token payload is invalid") from exc
    uid = str(payload.get("uid") or "").strip()
    role = _ROLE_ALIASES.get(str(payload.get("role") or ""), "")
    exp = int(payload.get("exp") or 0)
    if not uid or role not in {"teacher", "manager", "boss"}:
        raise DashboardAuthError("bad_token", "Token payload is incomplete")
    if exp <= _now_ts(now):
        raise DashboardAuthError("expired", "Token has expired")
    current_role = _current_wecom_role(store, uid)
    if current_role != role:
        raise DashboardAuthError("forbidden", "User is no longer authorized")
    return DashboardPrincipal(user_id=uid, role=role, expires_at=exp)


def verify_parent_report_token(
    token: str,
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> ParentReportPrincipal:
    if not token:
        raise DashboardAuthError("missing_token", "Token is required")
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError as exc:
        raise DashboardAuthError("bad_token", "Token format is invalid") from exc
    secret = _read_secret(store)
    if not secret:
        raise DashboardAuthError("bad_token", "Dashboard signing secret is missing")
    expected = _signature(secret, payload_part)
    if not hmac.compare_digest(signature_part, expected):
        raise DashboardAuthError("bad_signature", "Token signature is invalid")
    try:
        payload = json.loads(_b64decode(payload_part).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DashboardAuthError("bad_token", "Token payload is invalid") from exc
    scope = str(payload.get("scope") or "")
    report_id = str(payload.get("report_id") or "").strip()
    student_name = str(payload.get("student_name") or "").strip()
    exp = int(payload.get("exp") or 0)
    if scope != "parent_growth_report" or not report_id or not student_name:
        raise DashboardAuthError("bad_token", "Token payload is incomplete")
    if exp <= _now_ts(now):
        raise DashboardAuthError("expired", "Token has expired")
    report = _find_growth_report(store, report_id)
    if report is None or report.get("status") != "approved":
        raise DashboardAuthError("forbidden", "Parent report is not approved")
    if str(report.get("student_name") or "") != student_name:
        raise DashboardAuthError("forbidden", "Parent report student does not match")
    return ParentReportPrincipal(
        report_id=report_id,
        student_name=student_name,
        expires_at=exp,
    )


def _find_growth_report(store: TuoguanStore, report_id: str) -> dict[str, Any] | None:
    reports = store.read_json("growth_reports.json", [])
    if not isinstance(reports, list):
        return None
    for report in reports:
        if isinstance(report, dict) and str(report.get("id") or "") == str(report_id):
            return report
    return None


def token_expiry_datetime(now: datetime | None = None) -> datetime:
    base = now or datetime.now()
    return base + timedelta(seconds=TOKEN_TTL_SECONDS)


def parent_report_token_expiry_datetime(now: datetime | None = None) -> datetime:
    base = now or datetime.now()
    return base + timedelta(seconds=PARENT_REPORT_TOKEN_TTL_SECONDS)
