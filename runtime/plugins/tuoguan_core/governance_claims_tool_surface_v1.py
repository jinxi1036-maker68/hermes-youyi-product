"""Public, model-visible Tool surface for progressive governance claims.

This adapter is intentionally thin. It makes explicit capability Tools
available to Hermes, but it never reads a user sentence, decides an intent,
chooses a Tool or manufactures a business reply. Trusted identity, tenant and
operation identity are supplied by the surrounding public Runtime Contract.
"""

from __future__ import annotations

import os
from pathlib import Path
from hashlib import sha256
import json
from typing import Any, Callable

try:
    from .governance_claims_v1 import ClaimError, GovernanceClaimService
    from .personnel_service_governance_v1 import GovernanceError, PersonnelServiceGovernance
except ImportError:  # standalone certification import
    from governance_claims_v1 import ClaimError, GovernanceClaimService
    from personnel_service_governance_v1 import GovernanceError, PersonnelServiceGovernance


def _schema(description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


QUERY_REFERENCE_SCHEMA = _schema(
    "读取一条旧人员、学生或历史 Work 参考，帮助你向有权限的人补问当前真实情况。旧字段不是事实，读取后不得自行迁移、合并、分配负责人或承诺已更新。遇到同名或同手机号多个结果，必须自然说明冲突并请人确认。",
    {
        "kind": {"type": "string", "enum": ["person", "student", "work"]},
        "query": {"type": "string", "description": "用户明确说出的姓名、手机号或历史 Work 编号；不得编造。"},
    },
    ["kind", "query"],
)

STUDENT_MASTER_CLAIM_SCHEMA = _schema(
    "记录老板或店长已经明确确认的学生公共主档事实。只用于学生本人主档，不包含午托、晚托等服务、班级或负责人。若当前句子说的是午托/晚托/周末班/暑假班、班级课程或谁负责，必须使用学生服务责任 Tool，不能使用本 Tool。这个 Tool 会先留审计 Claim，再通过同一可信身份、Permission、Receipt 和 writeback 写入；只有完整明确事实、无冲突时才可调用。旧资料仅作 reference_id 参考；不得自动迁移。",
    {
        "reference_ids": {"type": "array", "items": {"type": "string"}, "description": "可选：只可使用本轮旧参考 Tool 返回的 reference_id。没有旧资料或它不可靠时留空；当前有权人员的明确陈述仍可作为待审计的事实来源。"},
        "name": {"type": "string", "description": "当前有权人员明确确认的学生姓名。"},
        "campus_id": {"type": "string", "description": "当前有权人员明确确认的校区。"},
        "phone": {"type": "string", "description": "可选；仅在当前人明确说出时填写。"},
        "ordinary_profile": {
            "type": "object",
            "properties": {
                "school": {"type": "string"},
                "grade": {"type": "string"},
                "class_name": {"type": "string"},
                "ordinary_contact": {"type": "string"},
                "ordinary_note": {"type": "string"},
            },
            "additionalProperties": False,
            "description": "可选普通基础资料。不得填入学生状态、服务、负责人、接送、紧急联系人、过敏、饮食禁忌或安全事项。",
        },
    },
    ["reference_ids", "name", "campus_id"],
)

STUDENT_SERVICE_CLAIM_SCHEMA = _schema(
    "记录老板或店长已经明确确认的一条学生服务责任。只用于已经存在的权威学生：午托、晚托、周末班或暑假班，以及其班级/课程和当前负责人。只要当前事实包含某项服务、班级/课程或“由谁负责/归谁”，应优先使用本 Tool，而不是学生主档 Tool。这个 Tool 会留下审计 Claim 并在 Receipt/writeback 验证后写入。不要把旧 teacher 字段当成负责人；负责人必须已在权威人员中唯一且当前在职，否则先追问。",
    {
        "reference_ids": {"type": "array", "items": {"type": "string"}, "description": "可选旧参考；不得编造。已经由权威学生和明确人类陈述确定时可留空。"},
        "student_id": {"type": "string", "description": "从权威学生查询返回的 student_id，不能编造。"},
        "campus_id": {"type": "string"},
        "service_type": {"type": "string", "enum": ["lunch_care", "evening_care", "weekend_class", "summer_class"]},
        "class_or_course_id": {"type": "string", "description": "当前有权人员明确确认的班级或课程标识。"},
        "assignee_name": {"type": "string", "description": "当前有权人员明确确认的负责人姓名；只可匹配唯一当前权威人员。"},
        "assignee_user_id": {"type": "string", "description": "可选；仅使用权威人员查询返回的 staff_user_id。"},
    },
    ["reference_ids", "student_id", "campus_id", "service_type", "class_or_course_id", "assignee_name"],
)

PERSON_STATUS_CLAIM_SCHEMA = _schema(
    "记录老板或店长已经明确确认的当前人员状态。只用于已经存在于权威人员中的在职、暂停或离职等事实；首次启用服务器记录为 pending 的企业微信账号必须使用 pending identity activation Tool，不能用本 Tool 组合恢复状态和任职。必须先从权威人员查询取得 staff_user_id。旧 staff 记录本身不能作为当前状态。若离职仍缺交接信息，应先追问，不能调用。",
    {
        "reference_ids": {"type": "array", "items": {"type": "string"}, "description": "可选旧参考；不得编造。"},
        "staff_user_id": {"type": "string", "description": "从权威人员查询返回的 staff_user_id。"},
        "campus_id": {"type": "string", "description": "该人员当前权威任职所在校区。"},
        "state": {"type": "string", "enum": ["active", "suspended", "left"]},
        "handover_id": {"type": "string", "description": "仅当 state=left 时必填；必须来自已完成的当前责任交接，不能编造。没有交接时先询问接手人或关闭决定。"},
    },
    ["staff_user_id", "campus_id", "state"],
)

PERSON_ASSIGNMENT_CLAIM_SCHEMA = _schema(
    "记录老板或有权限店长已经明确确认的一条人员任职关系。当前已认证有权人本轮给出的明确、对象唯一且事实完整的指令本身就是确认：应直接调用本 Tool，在同一受保护写入中完成审计、确认和必要的身份启用；不得先创建候选再向同一人重复确认。对象不唯一、缺少必需事实、存在冲突、无权或属于额外确认的高风险操作时，才自然追问。人员主状态与任职关系分开；调店或调班不把人员变成 transferred。不得用旧字段自动赋予角色。",
    {
        "reference_ids": {"type": "array", "items": {"type": "string"}, "description": "可选旧参考；不得编造。"},
        "staff_user_id": {"type": "string"},
        "person_name": {"type": "string", "description": "新增人员时必填：当前有权人员明确确认的正式业务展示姓名；只用于展示，不作为身份匹配依据。已存在的当前权威人员可留空。"},
        "role": {"type": "string", "enum": ["boss", "manager", "teacher"]},
        "campus_id": {"type": "string"},
    },
    ["reference_ids", "staff_user_id", "role", "campus_id"],
)

PENDING_IDENTITY_ACTIVATION_SCHEMA = _schema(
    "仅当当前已认证的老板明确要求把一个已由服务器记录为 pending 的企业微信账号正式确认为人员并启用角色时使用。这个单一受保护 Tool 会原子完成 pending 身份批准、正式展示姓名、当前任职关系和角色启用，并通过 Receipt/writeback 验证。staff_user_id 必须来自本轮可信人员目录/待确认身份结果；不得用姓名或旧 staff 资料猜 userid。不要把这件事拆成恢复状态、创建任职或多个 Claim。",
    {
        "staff_user_id": {"type": "string", "description": "本轮可信目录或 pending 身份结果返回的企业微信 userid。"},
        "person_name": {"type": "string", "description": "老板明确确认的正式业务展示姓名；不用于身份匹配。"},
        "role": {"type": "string", "enum": ["boss", "manager", "teacher"]},
        "campus_id": {"type": "string", "description": "老板明确确认的当前任职校区。"},
    },
    ["staff_user_id", "person_name", "role", "campus_id"],
)

HANDOVER_OPEN_SCHEMA = _schema(
    "记录老板或有权限店长明确确认的人员交接启动。只在当前离职、暂停或紧急停权已经明确，且已从权威人员查询取得 staff_user_id 时使用。它会盘点当前服务责任、未完成 Work 和跨服务关注，供后续人类指定接手人；不会自动猜接手人或结束离职。",
    {
        "reference_ids": {"type": "array", "items": {"type": "string"}, "description": "可选旧参考；不得编造。"},
        "outgoing_user_id": {"type": "string", "description": "必须来自权威人员查询。"},
        "campus_id": {"type": "string"},
        "kind": {"type": "string", "enum": ["normal", "emergency"]},
    },
    ["outgoing_user_id", "campus_id", "kind"],
)

HANDOVER_COMPLETE_SCHEMA = _schema(
    "记录已经由老板或有权限店长明确指定的交接结果。每一项当前服务责任和未完成 Work 都必须有接手人，或被明确关闭；不允许遗漏、猜测或覆盖历史负责人。成功后才可以再记录人员离职。",
    {
        "reference_ids": {"type": "array", "items": {"type": "string"}, "description": "可选旧参考；不得编造。"},
        "handover_id": {"type": "string", "description": "必须来自已启动交接的权威返回。"},
        "campus_id": {"type": "string"},
        "relation_recipients": {"type": "object", "description": "每个当前 service relation_id 映射到已确认接手 staff_user_id。"},
        "work_recipients": {"type": "object", "description": "每个当前 work_id 映射到已确认接手 staff_user_id。"},
        "close_relation_ids": {"type": "array", "items": {"type": "string"}},
        "close_work_ids": {"type": "array", "items": {"type": "string"}},
    },
    ["handover_id", "campus_id", "relation_recipients", "work_recipients", "close_relation_ids", "close_work_ids"],
)

GOVERNANCE_DECISION_CLAIM_SCHEMA = _schema(
    "记录一项已经明确的人为治理决定：旧 teacher 字段解释、同名/同手机号疑似重复处理、历史未分层记录保留、未完成 Work 转交/关闭，或跨服务关注关闭。此 Tool 绝不自动合并学生、分配服务负责人或把旧资料升级成事实。只在当前有权人员已经明确作出该决定时使用。",
    {
        "claim_type": {"type": "string", "enum": ["legacy_teacher_interpretation", "duplicate_decision", "historical_record_classification", "work_disposition", "attention_disposition", "manager_scope"]},
        "reference_ids": {"type": "array", "items": {"type": "string"}, "description": "只可使用本轮参考 Tool 返回的 id；无参考时可留空，但不得把空白当成同意合并或分配。"},
        "payload": {"type": "object", "description": "只放本轮明确的人为决定。work_disposition 需要 work_id 与 disposition=close 或 transfer；attention_disposition 需要 attention_id、relation_id、outcome；manager_scope 需要 employment_id、managed_campus_ids。"},
    },
    ["claim_type", "reference_ids", "payload"],
)

CONFIRM_CLAIM_SCHEMA = _schema(
    "在当前已认证的老板或店长明确确认该事实后，将一条待确认 Claim 写入治理权威 Workspace。必须使用 submit 返回的 claim_id。只因读到旧字段、模型推断或候选搜索命中时不得确认。写入通过 Receipt/writeback 验证；失败时不得宣称完成。",
    {"claim_id": {"type": "string"}},
    ["claim_id"],
)

QUERY_PENDING_SCHEMA = _schema(
    "查询当前可信身份可见范围内仍待确认或冲突的治理 Claim。只读，不改变任何人员、学生、服务责任或历史记录。",
    {"states": {"type": "array", "items": {"type": "string"}}},
    [],
)

QUERY_AUTHORITY_SCHEMA = _schema(
    "读取已经确认写入新治理模型的当前人员或学生。只返回当前权威事实，用于继续一条已经明确的认领；不读取旧资料、不推断服务关系、不改变任何数据。若没有唯一权威对象，应自然追问，不得使用旧字段猜测。",
    {
        "kind": {"type": "string", "enum": ["person", "student"]},
        "name": {"type": "string", "description": "用户明确说出的当前姓名；可为空。"},
        "campus_id": {"type": "string", "description": "用户明确确认的校区；可为空。"},
    },
    ["kind"],
)


def _legacy_dir_for(service: Any) -> Path:
    """Require an explicit read-only legacy source, never an implicit import."""
    configured = str(os.environ.get("XIAOYOU_GOVERNANCE_LEGACY_REFERENCE_DIR") or "").strip()
    if not configured:
        raise ClaimError("legacy_reference_source_not_configured")
    legacy = Path(configured).resolve()
    authority = Path(service.store.data_dir).resolve()
    if legacy == authority:
        raise ClaimError("legacy_reference_source_must_be_separate_from_authority_workspace")
    if not legacy.is_dir():
        raise ClaimError("legacy_reference_source_not_available")
    return legacy


def build_governance_claim_tools(
    *,
    service_provider: Callable[[], Any],
    activate_turn: Callable[..., Any],
    current_turn: Callable[[], Any],
    tool_result: Callable[[dict[str, Any]], str],
) -> tuple[tuple[str, dict[str, Any], Callable[..., str]], ...]:
    """Build the isolated candidate Tool surface through public plugin seams."""

    def handler(action: str, fixed_claim_type: str | None = None) -> Callable[..., str]:
        def run(args: dict[str, Any] | None = None, **runtime_kwargs: Any) -> str:
            activate_turn(
                session_id=runtime_kwargs.get("session_id"),
                turn_id=runtime_kwargs.get("turn_id"),
            )
            turn = current_turn()
            service = service_provider()
            if turn is None or service is None:
                return tool_result({
                    "ok": False,
                    "error": "missing_trusted_turn_context",
                    "message": "当前认领操作缺少服务端可信身份上下文，本轮未执行。",
                })
            message_id = str(getattr(turn, "message_id", "") or "").strip()
            tenant_id = str(getattr(turn, "tenant_id", "") or "").strip()
            if not message_id or not tenant_id:
                return tool_result({
                    "ok": False,
                    "error": "missing_trusted_claim_correlation",
                    "message": "当前认领操作缺少可信消息或机构关联，本轮未执行。",
                })
            payload = dict(args or {})
            try:
                claims = GovernanceClaimService(
                    governance=PersonnelServiceGovernance(service.store),
                    legacy_data_dir=_legacy_dir_for(service),
                )
                # Each distinct declared Tool request inside one authenticated
                # message has a deterministic id. This supports a user
                # explicitly confirming two independent service relations in
                # one sentence while a retry of the same structured request
                # remains idempotent.
                arg_digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
                operation_id = message_id + ":governance_claim:" + action + ":" + arg_digest
                def invoke() -> dict[str, Any]:
                    if action == "query_reference":
                        return claims.observe_legacy(
                            identity=service.identity, tenant_id=tenant_id,
                            kind=str(payload.get("kind") or ""),
                            query=str(payload.get("query") or ""),
                            operation_id=operation_id,
                        )
                    if action == "submit":
                        claim_payload = dict(payload.get("payload") or {}) if fixed_claim_type is None else {
                            key: value for key, value in payload.items()
                            if key not in {"reference_ids", "claim_type"}
                        }
                        return claims.submit_claim(
                            identity=service.identity, tenant_id=tenant_id,
                            claim_type=str(fixed_claim_type or payload.get("claim_type") or ""),
                            reference_ids=list(payload.get("reference_ids") or []),
                            payload=claim_payload,
                            operation_id=operation_id,
                        )
                    if action == "record":
                        claim_payload = {
                            key: value for key, value in payload.items()
                            if key not in {"reference_ids", "claim_type"}
                        }
                        if fixed_claim_type is None:
                            claim_payload = dict(payload.get("payload") or {})
                        return claims.record_confirmed_claim(
                            identity=service.identity, tenant_id=tenant_id,
                            claim_type=str(fixed_claim_type or payload.get("claim_type") or ""),
                            reference_ids=list(payload.get("reference_ids") or []),
                            payload=claim_payload,
                            operation_id=operation_id,
                        )
                    if action == "activate_pending_identity":
                        return claims.activate_confirmed_pending_identity(
                            identity=service.identity,
                            tenant_id=tenant_id,
                            staff_user_id=str(payload.get("staff_user_id") or ""),
                            person_name=str(payload.get("person_name") or ""),
                            role=str(payload.get("role") or ""),
                            campus_id=str(payload.get("campus_id") or ""),
                            operation_id=operation_id,
                        )
                    if action == "confirm":
                        return claims.confirm_claim(
                            identity=service.identity, tenant_id=tenant_id,
                            claim_id=str(payload.get("claim_id") or ""),
                            operation_id=operation_id,
                        )
                    raise ClaimError("unsupported_claim_write_action")

                if action in {"query_reference", "submit", "record", "confirm", "activate_pending_identity"}:
                    # A progressive claim is a real, audited Workspace change
                    # even when it only records an unconfirmed observation.
                    # Route it through the product's public Capability write
                    # seam so the usual trusted turn, CommandBus fence,
                    # idempotency ledger and receipt/writeback truth apply.
                    def execute() -> dict[str, Any]:
                        candidate = invoke()
                        if bool(candidate.get("execution_failed")):
                            # The terminal Claim mutation only records an
                            # audit/failure fact.  It is not the requested
                            # governance write and must never satisfy the
                            # Capability receipt's writeback proof.
                            return {
                                "ok": False,
                                "error": str(candidate.get("error") or "governance_execution_failed"),
                                "data": candidate,
                                "writeback_verified": False,
                                "message": "当前已确认的治理操作执行失败；失败已留审计，但没有形成待确认事项。",
                            }
                        candidate_receipt = candidate.get("execution_receipt") if isinstance(candidate, dict) else None
                        verified = bool(
                            (candidate_receipt or {}).get("writeback_verified")
                            or (candidate.get("writeback_verified") if isinstance(candidate, dict) else False)
                        )
                        return {
                            "ok": verified,
                            "data": candidate,
                            "writeback_verified": verified,
                            "message": "治理认领事实已按可信操作处理。" if verified else "治理认领事实未完成写后验证。",
                        }
                    result = service.execute_capability_write(
                        operation_id=operation_id,
                        operation="governance_claim_" + action,
                        execute=execute,
                    )
                    return tool_result(result)
                if action == "query_pending":
                    requested = payload.get("states") or []
                    result = {
                        "claims": claims.query_claims(
                            identity=service.identity, tenant_id=tenant_id,
                            states={str(item) for item in requested if str(item)},
                        ),
                    }
                elif action == "query_authority":
                    result = {
                        "objects": claims.query_authoritative_governance(
                            identity=service.identity, tenant_id=tenant_id,
                            kind=str(payload.get("kind") or ""),
                            name=str(payload.get("name") or ""),
                            campus_id=str(payload.get("campus_id") or ""),
                        ),
                    }
                elif action != "query_authority":
                    raise ClaimError("unsupported_claim_tool_action")
            except (ClaimError, GovernanceError) as exc:
                return tool_result({
                    "ok": False,
                    "error": str(exc),
                    "message": "当前认领事实未被写入；请根据可信范围和缺失信息继续确认。",
                })
            receipt = result.get("execution_receipt") if isinstance(result, dict) else None
            return tool_result({
                "ok": True,
                "action": "governance_claim_" + action,
                "data": result,
                "writeback_verified": bool((receipt or {}).get("writeback_verified")),
                "execution_receipt": receipt,
            })
        return run

    return (
        ("tuoguan_query_legacy_governance_reference", QUERY_REFERENCE_SCHEMA, handler("query_reference")),
        ("tuoguan_record_confirmed_student_master", STUDENT_MASTER_CLAIM_SCHEMA, handler("record", "student_master")),
        ("tuoguan_record_confirmed_student_service", STUDENT_SERVICE_CLAIM_SCHEMA, handler("record", "student_service")),
        ("tuoguan_record_confirmed_person_status", PERSON_STATUS_CLAIM_SCHEMA, handler("record", "person_status")),
        ("tuoguan_record_confirmed_person_assignment", PERSON_ASSIGNMENT_CLAIM_SCHEMA, handler("record", "person_assignment")),
        ("tuoguan_activate_confirmed_pending_identity", PENDING_IDENTITY_ACTIVATION_SCHEMA, handler("activate_pending_identity")),
        ("tuoguan_record_confirmed_handover_open", HANDOVER_OPEN_SCHEMA, handler("record", "handover_open")),
        ("tuoguan_record_confirmed_handover_complete", HANDOVER_COMPLETE_SCHEMA, handler("record", "handover_complete")),
        ("tuoguan_record_confirmed_governance_decision", GOVERNANCE_DECISION_CLAIM_SCHEMA, handler("record")),
        ("tuoguan_query_pending_governance_claims", QUERY_PENDING_SCHEMA, handler("query_pending")),
        ("tuoguan_query_authoritative_governance", QUERY_AUTHORITY_SCHEMA, handler("query_authority")),
    )
