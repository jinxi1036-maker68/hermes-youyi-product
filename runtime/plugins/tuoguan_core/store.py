"""Atomic JSON persistence for the Hermes tutoring-center module."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from utils import atomic_json_write


class TuoguanStoreError(RuntimeError):
    """Raised when tutoring-center data cannot be read or written safely."""


def _owner_for_root_write(path: Path) -> tuple[int, int] | None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    try:
        source = path if path.exists() else path.parent
        metadata = source.stat()
        return metadata.st_uid, metadata.st_gid
    except OSError:
        return None


def resolve_tuoguan_data_dir(path: str | Path | None = None) -> Path:
    if path is not None and str(path).strip():
        return Path(path).expanduser()
    env_path = os.getenv("HERMES_TUOGUAN_DATA_DIR", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return get_hermes_home() / "tuoguan-data"


class TuoguanStore:
    """JSON-backed store with legacy task-container compatibility."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.data_dir = resolve_tuoguan_data_dir(data_dir)
        self._lock = threading.RLock()

    def path_for(self, name: str) -> Path:
        candidate = self.data_dir / name
        if candidate.parent != self.data_dir or Path(name).name != name:
            raise TuoguanStoreError(f"Invalid data file name: {name}")
        return candidate

    def read_json(self, name: str, fallback: Any) -> Any:
        path = self.path_for(name)
        with self._lock:
            if not path.exists():
                return deepcopy(fallback)
            try:
                return json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise TuoguanStoreError(f"Unable to read {name}: {exc}") from exc

    def _backup_existing(self, path: Path) -> None:
        if not path.exists():
            return
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup = self.data_dir / "backup" / "prewrite" / timestamp / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)

    @contextmanager
    def _process_write_lock(self, path: Path):
        lock_path = self.data_dir / f".{path.name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            except FileExistsError:
                if time.monotonic() > deadline:
                    try:
                        if time.time() - lock_path.stat().st_mtime > 30:
                            lock_path.unlink()
                            continue
                    except OSError:
                        pass
                    raise TuoguanStoreError(f"Timed out waiting for data lock: {path.name}")
                time.sleep(0.05)
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def write_json(self, name: str, data: Any) -> None:
        from .write_guard import assert_business_write_allowed
        path = self.path_for(name)
        with self._lock:
            try:
                assert_business_write_allowed(self.data_dir, name)
                with self._process_write_lock(path):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    preserved_owner = _owner_for_root_write(path)
                    self._backup_existing(path)
                    atomic_json_write(path, data)
                    if preserved_owner is not None:
                        os.chown(path, *preserved_owner)
            except (OSError, TypeError, ValueError, PermissionError) as exc:
                raise TuoguanStoreError(f"Unable to write {name}: {exc}") from exc

    def load_tasks(self) -> list[dict[str, Any]]:
        container = self.read_json("tasks.json", [])
        if isinstance(container, list):
            tasks = container
        elif isinstance(container, dict) and isinstance(container.get("tasks"), list):
            tasks = container["tasks"]
        else:
            raise TuoguanStoreError("tasks.json must be a list or contain a tasks list")
        return deepcopy(tasks)

    def save_tasks(self, tasks: list[dict[str, Any]]) -> None:
        existing = self.read_json("tasks.json", [])
        if isinstance(existing, dict) and isinstance(existing.get("tasks"), list):
            container = deepcopy(existing)
            container["tasks"] = deepcopy(tasks)
        elif isinstance(existing, list):
            container = deepcopy(tasks)
        else:
            raise TuoguanStoreError("tasks.json must be a list or contain a tasks list")
        self.write_json("tasks.json", container)
