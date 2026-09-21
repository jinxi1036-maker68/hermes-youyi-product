"""Cross-platform identity and role resolution."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore


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
        # exposing the transport userid (for example ``owner_test``).  This is
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
    ) -> UserIdentity:
        platform_key = self._platform_key(platform)
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
