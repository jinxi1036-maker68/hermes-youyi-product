"""Public Hermes platform registration for XiaoYou reply-only recovery."""

from __future__ import annotations

from .adapter import ReplyRecoveryAdapter, check_reply_recovery_requirements, validate_reply_recovery_config


def _build_adapter(config):
    return ReplyRecoveryAdapter(config)


def register(ctx) -> None:
    ctx.register_platform(
        name="reply_recovery",
        label="XiaoYou durable reply recovery",
        adapter_factory=_build_adapter,
        check_fn=check_reply_recovery_requirements,
        validate_config=validate_reply_recovery_config,
        emoji="↩️",
        allow_update_command=False,
    )
