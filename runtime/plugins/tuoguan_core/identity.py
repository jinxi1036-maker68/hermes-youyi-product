"""Cross-platform identity and role resolution."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import UserIdentity
from .personnel_identity_authority import (
    DATA_FILE as PERSONNEL_GOVERNANCE_DATA_FILE,
    authority_is_enforced,
    record_pending_runtime_identity,
    resolve_runtime_identity,
)
from .store import TuoguanStore
from .tenant_context import current_tenant_id


_ROLE_ALIASES = {
    "admin": "boss",
    "super_admin": "boss",
    "boss": "boss",
    "manager": "manager",
    "teacher": "teacher",
}


class IdentityService:
    def __init__(self, store: TuoguanStore) -> None:
        self.store = store

    @staticmethod
    def _platform_key(platform: str) -> str:
        value = str(platform or "").lower()
        return "feishu" if value == "feishu" else "wecom"

    def _maps(self) -> tuple[dict[str, str], dict[str, str]]:
        wecom = self.store.read_json("teacher_wecom_map.json", {})
        feishu = self.store.read_json("teacher_feishu_map.json", {})
        return (
            wecom if isinstance(wecom, dict) else {},
            feishu if isinstance(feishu, dict) else {},
        )

    def _person_name(self, platform_key: str, sender_id: str) -> str:
        wecom, feishu = self._maps()
        mapping = feishu if platform_key == "feishu" else wecom
        for name, user_id in mapping.items():
            if str(user_id) == sender_id:
                return str(name)
        # The legacy channel-name map is optional in the current Institution
        # Workspace.  A fresh Hermes session must still recover the person's
        # business display name from the trusted personnel record instead of
        # exposing the transport userid (for example ``owner-id``).  This is
        # read-only identity presentation; role/approval remain sourced from
        # the server-owned channel directory below.
        staff = self.store.read_json("staff.json", {})
        profile = staff.get(sender_id) if isinstance(staff, dict) else None
        if isinstance(profile, dict):
            business_name = str(profile.get("business_name") or profile.get("name") or "").strip()
            if business_name:
                return business_name
        return ""

    def _canonical_user_id(
        self,
        platform_key: str,
        sender_id: str,
        person_name: str,
    ) -> str:
        if platform_key != "feishu" or not person_name:
            return sender_id
        wecom, _ = self._maps()
        return str(wecom.get(person_name) or sender_id)

    def _record_pending(
        self,
        platform_key: str,
        sender_id: str,
        *,
        user_name: str,
        chat_id: str,
        message_text: str,
    ) -> None:
        name = f"{platform_key}_whitelist.json"
        data = self.store.read_json(name, {})
        if not isinstance(data, dict):
            data = {}
        pending_users = list(data.get("pending_users") or [])
        if sender_id not in pending_users:
            pending_users.append(sender_id)
        applications = list(data.get("pending_applications") or [])
        existing = next(
            (
                item
                for item in applications
                if isinstance(item, dict) and item.get("user_id") == sender_id
            ),
            None,
        )
        now = datetime.now().isoformat(timespec="seconds")
        payload: dict[str, Any] = {
            "user_id": sender_id,
            "user_name": user_name,
            "chat_id": chat_id,
            "platform": platform_key,
            "last_seen": now,
            "last_message": message_text[:200],
        }
        if existing is None:
            payload["first_seen"] = now
            applications.append(payload)
        else:
            existing.update(payload)
        data.setdefault("super_users", [])
        data.setdefault("allowed_users", [])
        data.setdefault("rejected_users", [])
        data.setdefault("user_roles", {})
        data["pending_users"] = pending_users
        data["pending_applications"] = applications
        self.store.write_json(name, data)

    def resolve(
        self,
        platform: str,
        sender_id: str,
        *,
        user_name: str = "",
        chat_id: str = "",
        message_text: str = "",
        tenant_id: str = "",
    ) -> UserIdentity:
        platform_key = self._platform_key(platform)
        trusted_tenant = str(tenant_id or current_tenant_id()).strip()
        authority_enabled = authority_is_enforced(
            self.store.read_json(PERSONNEL_GOVERNANCE_DATA_FILE, {}),
        )
        # The formal authority deliberately binds a *WeCom* verified userid.
        # A Feishu sender whose opaque ID happens to have the same characters
        # must not inherit a WeCom employee's role.  Robot is normalized by
        # the Runtime Contract to its already verified WeCom principal before
        # reaching this service.  A future non-WeCom bridge must be an
        # explicit, server-attested binding; it cannot revive the old
        # display-name bridge after authority cutover.
        if authority_enabled and platform_key != "wecom":
            return UserIdentity(
                platform=platform_key,
                platform_user_id=sender_id,
                canonical_user_id=sender_id,
                person_name="",
                role="unknown",
                approval_state="unmapped_platform_identity",
            )
        # ``sender_id`` is the verified channel userid.  Once the explicit
        # personnel migration has enabled the authority aggregate, it is the
        # only identity lookup key.  Display names, legacy aliases and model
        # text may decorate a response but cannot pick a person or role.
        authority = resolve_runtime_identity(
            self.store,
            tenant_id=trusted_tenant,
            user_id=sender_id,
        )
        if authority is not None:
            if authority.approval_state == "pending" and authority.reason == "unrecognized_verified_userid":
                record_pending_runtime_identity(
                    self.store,
                    tenant_id=trusted_tenant,
                    user_id=sender_id,
                    platform=platform_key,
                    user_name=user_name,
                    chat_id=chat_id,
                )
                authority = resolve_runtime_identity(
                    self.store,
                    tenant_id=trusted_tenant,
                    user_id=sender_id,
                ) or authority
            # Once the personnel authority is enforced, old staff profiles are
            # historical reference only.  In particular, a pending, rejected,
            # suspended, left, or otherwise unknown WeCom userid must not be
            # presented to the model as a recognised employee merely because a
            # legacy staff row happens to carry the same transport userid.
            #
            # A formally approved person may still use the legacy display
            # fallback for presentation compatibility when the authoritative
            # record has no display name.  The fallback never affects the
            # canonical userid, lifecycle, role, or approval decision.
            person_name = ""
            if authority.approval_state == "approved":
                person_name = authority.display_name or self._person_name(platform_key, sender_id)
            return UserIdentity(
                platform=platform_key,
                platform_user_id=sender_id,
                canonical_user_id=sender_id,
                person_name=person_name,
                role=authority.role,
                approval_state=authority.approval_state,
            )

        whitelist_name = f"{platform_key}_whitelist.json"
        data = self.store.read_json(whitelist_name, {})
        if not isinstance(data, dict):
            data = {}

        rejected = sender_id in set(data.get("rejected_users") or [])
        pending = sender_id in set(data.get("pending_users") or [])
        super_user = sender_id in set(data.get("super_users") or [])
        allowed = sender_id in set(data.get("allowed_users") or [])
        raw_role = str((data.get("user_roles") or {}).get(sender_id) or "")
        summer_managers = {str(item) for item in data.get("summer_manager_ids") or []}

        if rejected:
            role, state = "unknown", "rejected"
        elif super_user or allowed:
            role = _ROLE_ALIASES.get(raw_role or ("boss" if super_user else "teacher"), "unknown")
            if sender_id in summer_managers and role != "boss":
                role = "manager"
            state = "approved"
        else:
            if not pending:
                self._record_pending(
                    platform_key,
                    sender_id,
                    user_name=user_name,
                    chat_id=chat_id,
                    message_text=message_text,
                )
            role, state = "unknown", "pending"

        person_name = self._person_name(platform_key, sender_id)
        return UserIdentity(
            platform=platform_key,
            platform_user_id=sender_id,
            canonical_user_id=self._canonical_user_id(
                platform_key,
                sender_id,
                person_name,
            ),
            person_name=person_name or user_name,
            role=role,
            approval_state=state,
        )
