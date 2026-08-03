"""Stage-aware semantic evidence proposals with deterministic contract checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class SemanticResolution:
    matched: bool
    confidence: float
    candidate_facts: dict[str, Any]
    evidence_signature: dict[str, Any]
    reason_code: str


class PendingEvidenceSemanticResolver:
    """LLM proposes meaning; this validator alone decides contract satisfaction."""

    def __init__(self, propose: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._propose = propose

    def resolve(self, payload: dict[str, Any]) -> SemanticResolution:
        proposal = self._propose(payload)
        confidence = float(proposal.get("confidence") or 0)
        signature = {
            "observation_completed": bool(proposal.get("observation_completed")),
            "condition_change": str(proposal.get("condition_change") or "unknown"),
            "activity_state": str(proposal.get("activity_state") or "unknown"),
        }
        allowed_condition = {"not_worsened", "improved", "worsened", "stable", "unknown"}
        allowed_activity = {"normal", "limited", "unable", "unknown"}
        if signature["condition_change"] not in allowed_condition or signature["activity_state"] not in allowed_activity:
            return SemanticResolution(False, confidence, {}, signature, "invalid_semantic_enum")
        missing = set(payload.get("missing_evidence") or [])
        after_review = payload.get("review_return_reason") == "request_more_observation" and "followup_result_after_review" in missing
        ordinary_followup = payload.get("current_stage") == "followup" and "followup_result" in missing
        observation_update = payload.get("current_stage") == "manager_review" and bool(payload.get("verified_evidence_signatures"))
        target = "followup_result_after_review" if after_review else "followup_result" if ordinary_followup else "observation_update"
        signature["target_evidence"] = target
        signature["review_round"] = int(payload.get("review_round") or 1)
        contract_active = bool(after_review or ordinary_followup or observation_update)
        has_result = signature["condition_change"] != "unknown" or signature["activity_state"] != "unknown"
        matched = bool(contract_active and signature["observation_completed"] and has_result and not proposal.get("future_only") and confidence >= 0.7)
        facts = {target: str(payload.get("current_message") or "")} if matched else {}
        return SemanticResolution(matched, confidence, facts, signature, "matched" if matched else "contract_not_satisfied")


def signature_key(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        bool(value.get("observation_completed")),
        str(value.get("condition_change") or "unknown"),
        str(value.get("activity_state") or "unknown"),
        str(value.get("target_evidence") or ""),
        int(value.get("review_round") or 1),
    )
