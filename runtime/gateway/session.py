"""Session primitives shared by local gateway tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SessionSource:
    platform: Any
    chat_id: str
    chat_name: str | None = None
    chat_type: str = "dm"
    user_id: str | None = None
    user_name: str | None = None
    thread_id: str | None = None
    chat_topic: str | None = None
    user_id_alt: str | None = None
    chat_id_alt: str | None = None
    is_bot: bool = False
    scope_id: str | None = None
    guild_id: str | None = None
    parent_chat_id: str | None = None
    message_id: str | None = None
    profile: str | None = None
    role_authorized: bool = False
    auto_thread_created: bool = False
    auto_thread_initial_name: str | None = None


def build_session_key(source: SessionSource) -> str:
    platform = str(getattr(source.platform, "value", source.platform) or "")
    return ":".join(
        part
        for part in (
            platform,
            str(source.chat_type or "dm"),
            str(source.chat_id or ""),
            str(source.thread_id or ""),
            str(source.user_id or ""),
        )
        if part
    )

