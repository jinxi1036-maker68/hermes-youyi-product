"""Gateway configuration primitives used by plugin tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Platform(str, Enum):
    WECOM_CALLBACK = "wecom_callback"


@dataclass
class PlatformConfig:
    platform: Platform | str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)

