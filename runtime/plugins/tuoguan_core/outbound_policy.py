"""XiaoYou-owned final-delivery metadata; no Hermes private reply helper."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class FinalDeliveryEnvelope:
    source_message_id: str
    message_owner_id: str
    reply_owner: str
    reply_text: str
    business_result: dict[str, Any] | None
    trusted_deterministic: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def prepare_final_delivery(
    *,
    source_message_id: str,
    message_owner_id: str,
    reply_owner: str,
    reply_text: str,
    business_result: dict[str, Any] | None = None,
    trusted_deterministic: bool = False,
) -> FinalDeliveryEnvelope:
    """Attach audit metadata without changing a reply or deciding business."""

    return FinalDeliveryEnvelope(
        source_message_id=str(source_message_id or ""),
        message_owner_id=str(message_owner_id or ""),
        reply_owner=str(reply_owner or ""),
        reply_text=str(reply_text or ""),
        business_result=business_result if isinstance(business_result, dict) else None,
        trusted_deterministic=bool(trusted_deterministic),
    )
