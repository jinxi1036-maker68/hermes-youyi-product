"""Minimal platform adapter primitives used by plugin tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from gateway.session import SessionSource


class MessageType(str, Enum):
    TEXT = "text"


@dataclass
class MessageEvent:
    text: str
    message_id: str
    source: SessionSource
    message_type: MessageType = MessageType.TEXT
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""


class BasePlatformAdapter:
    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        raise NotImplementedError

