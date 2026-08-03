"""Shared data models for the tutoring-center business module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class UserIdentity:
    platform: str
    platform_user_id: str
    canonical_user_id: str
    person_name: str
    role: str
    approval_state: str


@dataclass(frozen=True)
class TaskReplyResult:
    action: str
    reply: str
    task_id: str = ""


@dataclass(frozen=True)
class EscalationDecision:
    action: str
    notify_roles: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Notification:
    task_id: str
    role: str
    touser: str
    action: str
    content: str


@dataclass(frozen=True)
class RouteResult:
    handled: bool
    reply: str = ""
    notifications: list[dict[str, Any]] = field(default_factory=list)
