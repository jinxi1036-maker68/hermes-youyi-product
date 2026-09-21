"""Public Hermes Platform Adapter used only by the isolated Agenda POC."""

from __future__ import annotations

from .adapter import AgendaServiceWorkAdapter, check_agenda_service_work_requirements, validate_agenda_service_work_config


def _build_adapter(config):
    return AgendaServiceWorkAdapter(config)


def register(ctx) -> None:
    """Register a server-only Agenda ingress through Hermes' public platform API."""

    ctx.register_platform(
        name="agenda_service_work",
        label="XiaoYou Agenda service work (isolated POC)",
        adapter_factory=_build_adapter,
        check_fn=check_agenda_service_work_requirements,
        validate_config=validate_agenda_service_work_config,
        emoji="⏱️",
        # Hermes' public Gateway authorization asks the platform registry for
        # this allowlist.  The deployment sets one canonical server-issued
        # service identity here; the adapter still accepts only an opaque,
        # signed durable ticket, so this is not a generic client ingress.
        allowed_users_env="XIAOYOU_AGENDA_ALLOWED_USERS",
        allow_update_command=False,
    )
