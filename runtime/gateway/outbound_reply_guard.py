"""Final reply envelope helpers used by the tutoring plugin."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class FinalReplyEnvelope:
    source_message_id: str
    message_owner_id: str
    reply_owner: str
    reply_text: str
    business_result: dict[str, Any] | None = None
    trusted_deterministic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_final_reply_envelope(
    *,
    source_message_id: str,
    message_owner_id: str,
    reply_owner: str,
    reply_text: str,
    business_result: dict[str, Any] | None = None,
    trusted_deterministic: bool = False,
) -> FinalReplyEnvelope:
    return FinalReplyEnvelope(
        source_message_id=str(source_message_id or ""),
        message_owner_id=str(message_owner_id or ""),
        reply_owner=str(reply_owner or ""),
        reply_text=str(reply_text or ""),
        business_result=business_result,
        trusted_deterministic=trusted_deterministic,
    )

