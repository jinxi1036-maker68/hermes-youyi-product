"""Process-local session environment helpers."""

from __future__ import annotations

import os
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Iterator


_SESSION_ENV: ContextVar[dict[str, str]] = ContextVar("hermes_session_env", default={})


def get_session_env(name: str, default: str = "") -> str:
    values = _SESSION_ENV.get()
    if name in values:
        return values[name]
    return str(os.getenv(name) or default)


@contextmanager
def session_env(values: dict[str, str]) -> Iterator[None]:
    merged = dict(_SESSION_ENV.get())
    merged.update({str(key): str(value) for key, value in values.items()})
    token = _SESSION_ENV.set(merged)
    try:
        yield
    finally:
        _SESSION_ENV.reset(token)

