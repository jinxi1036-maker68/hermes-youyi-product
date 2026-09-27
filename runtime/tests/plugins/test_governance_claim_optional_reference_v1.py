from __future__ import annotations


def test_person_assignment_reference_ids_are_optional_transport_metadata():
    from plugins.tuoguan_core.governance_claims_tool_surface_v1 import (
        PERSON_ASSIGNMENT_CLAIM_SCHEMA,
    )

    params = PERSON_ASSIGNMENT_CLAIM_SCHEMA["parameters"]
    required = set(params.get("required") or [])

    # The handler already normalizes an omitted reference list to [] and the
    # Tool description explicitly calls it optional. Requiring it only creates
    # a corrective model round with no new business information.
    assert "reference_ids" not in required

    # Business facts remain model/human-owned.
    assert {"staff_user_id", "role", "campus_id"} <= required


def test_pending_identity_keeps_business_fields_model_owned_and_campus_optional():
    from plugins.tuoguan_core.governance_claims_tool_surface_v1 import (
        PENDING_IDENTITY_ACTIVATION_SCHEMA,
    )

    required = set(PENDING_IDENTITY_ACTIVATION_SCHEMA["parameters"].get("required") or [])

    # The service may resolve campus only when current authority exposes one
    # unique campus. Identity/name/role are business facts and stay required.
    assert "campus_id" not in required
    assert {"staff_user_id", "person_name", "role"} <= required
