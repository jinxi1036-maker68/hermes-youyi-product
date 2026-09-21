"""Hermes platform registration for the isolated Robot Channel text POC."""

from __future__ import annotations

from .adapter import RobotPocAdapter, check_robot_poc_requirements, validate_robot_poc_config


def _build_adapter(config):
    return RobotPocAdapter(config)


def register(ctx) -> None:
    """Register a loopback-only Robot POC platform without touching WeCom."""

    ctx.register_platform(
        name="robot_poc",
        label="Robot POC (local text)",
        adapter_factory=_build_adapter,
        check_fn=check_robot_poc_requirements,
        validate_config=validate_robot_poc_config,
        emoji="🤖",
        allow_update_command=False,
    )
