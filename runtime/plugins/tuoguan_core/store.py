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
from typing import Any, Callable

from hermes_constants import get_hermes_home
from utils import atomic_json_write


class TuoguanStoreError(RuntimeError):
    """Raised when tutoring-center data cannot be read or written safely."""


JSON_NO_CHANGE = object()
_RESOURCE_LOCKS_GUARD = threading.Lock()
_RESOURCE_LOCKS: dict[str, threading.RLock] = {}


def _resource_lock(path: Path) -> threading.RLock:
    """Share one in-process lock across all store instances for a file."""

    key = str(path.resolve())
    with _RESOURCE_LOCKS_GUARD:
        return _RESOURCE_LOCKS.setdefault(key, threading.RLock())


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

    def _read_json_unlocked(self, path: Path, name: str, fallback: Any) -> Any:
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
        with _resource_lock(path):
            deadline = time.monotonic() + 10
            fd: int | None = None
            while fd is None:
                try:
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
                except (FileExistsError, PermissionError):
                    if time.monotonic() > deadline:
                        try:
                            if lock_path.exists() and time.time() - lock_path.stat().st_mtime > 30:
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
                    preserved_mode = (path.stat().st_mode & 0o777) if path.exists() else None
                    self._backup_existing(path)
                    atomic_json_write(path, data)
                    if preserved_mode is not None:
                        os.chmod(path, preserved_mode)
                    if preserved_owner is not None:
                        os.chown(path, *preserved_owner)
            except (OSError, TypeError, ValueError, PermissionError) as exc:
                raise TuoguanStoreError(f"Unable to write {name}: {exc}") from exc

    def update_json(self, name: str, fallback: Any, updater: Callable[[Any], Any]) -> Any:
        """Read, modify, and atomically write one JSON resource under one process lock."""

        from .write_guard import assert_business_write_allowed

        path = self.path_for(name)
        with self._lock:
            try:
                assert_business_write_allowed(self.data_dir, name)
                with self._process_write_lock(path):
                    current = self._read_json_unlocked(path, name, fallback)
                    updated = updater(deepcopy(current))
                    if updated is JSON_NO_CHANGE:
                        return deepcopy(current)
                    data = current if updated is None else updated
                    path.parent.mkdir(parents=True, exist_ok=True)
                    preserved_owner = _owner_for_root_write(path)
                    preserved_mode = (path.stat().st_mode & 0o777) if path.exists() else None
                    self._backup_existing(path)
                    atomic_json_write(path, data)
                    if preserved_mode is not None:
                        os.chmod(path, preserved_mode)
                    if preserved_owner is not None:
                        os.chown(path, *preserved_owner)
                    return deepcopy(data)
            except (OSError, TypeError, ValueError, PermissionError) as exc:
                raise TuoguanStoreError(f"Unable to update {name}: {exc}") from exc

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
        def replace(existing: Any) -> Any:
            if isinstance(existing, dict) and isinstance(existing.get("tasks"), list):
                container = deepcopy(existing)
                container["tasks"] = deepcopy(tasks)
                return container
            if isinstance(existing, list):
                return deepcopy(tasks)
            raise TuoguanStoreError("tasks.json must be a list or contain a tasks list")

        self.update_json("tasks.json", [], replace)

    def update_tasks(self, updater: Callable[[list[dict[str, Any]]], Any]) -> list[dict[str, Any]]:
        """Atomically mutate the task list while preserving its legacy container shape."""

        def mutate(existing: Any) -> Any:
            if isinstance(existing, dict) and isinstance(existing.get("tasks"), list):
                container = deepcopy(existing)
                tasks = deepcopy(existing["tasks"])
                updated = updater(tasks)
                container["tasks"] = tasks if updated is None else updated
                return container
            if isinstance(existing, list):
                tasks = deepcopy(existing)
                updated = updater(tasks)
                return tasks if updated is None else updated
            raise TuoguanStoreError("tasks.json must be a list or contain a tasks list")

        container = self.update_json("tasks.json", [], mutate)
        if isinstance(container, dict):
            return deepcopy(container.get("tasks") or [])
        return deepcopy(container)

    def update_task(
        self,
        task_id: str,
        updater: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any] | None:
        """Atomically update one task and return the persisted task."""

        wanted = str(task_id or "").strip()
        if not wanted:
            raise TuoguanStoreError("task_id is required")
        persisted: dict[str, Any] | None = None

        def mutate(tasks: list[dict[str, Any]]) -> None:
            nonlocal persisted
            for index, task in enumerate(tasks):
                if not isinstance(task, dict) or str(task.get("id") or "") != wanted:
                    continue
                working = deepcopy(task)
                updated = updater(working)
                tasks[index] = working if updated is None else updated
                persisted = deepcopy(tasks[index])
                return

        self.update_tasks(mutate)
        if persisted is None:
            return None
        reread = next(
            (task for task in self.load_tasks() if str(task.get("id") or "") == wanted),
            None,
        )
        return deepcopy(reread) if isinstance(reread, dict) else None

    def append_jsonl_verified(self, name: str, row: dict[str, Any]) -> bool:
        """Append one JSONL row under a process lock and verify it from disk."""

        from .write_guard import assert_business_write_allowed

        if not isinstance(row, dict):
            raise TuoguanStoreError("JSONL row must be an object")
        path = self.path_for(name)
        serialized = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                assert_business_write_allowed(self.data_dir, name)
                with self._process_write_lock(path):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    preserved_owner = _owner_for_root_write(path)
                    with path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(serialized + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    if preserved_owner is not None:
                        os.chown(path, *preserved_owner)
                    verified = False
                    for line in path.read_text(encoding="utf-8-sig").splitlines()[-200:]:
                        if not line.strip():
                            continue
                        try:
                            candidate = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if candidate == row:
                            verified = True
                            break
                    if not verified:
                        raise TuoguanStoreError(f"JSONL writeback verification failed: {name}")
                    return True
            except (OSError, TypeError, ValueError, PermissionError) as exc:
                raise TuoguanStoreError(f"Unable to append {name}: {exc}") from exc

    def update_jsonl_verified(
        self,
        name: str,
        updater: Callable[[list[dict[str, Any]]], Any],
    ) -> list[dict[str, Any]]:
        """Atomically update a JSONL resource without racing concurrent appends."""

        from .write_guard import assert_business_write_allowed

        path = self.path_for(name)
        with self._lock:
            try:
                assert_business_write_allowed(self.data_dir, name)
                with self._process_write_lock(path):
                    rows: list[dict[str, Any]] = []
                    if path.exists():
                        for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                            if not line.strip():
                                continue
                            try:
                                row = json.loads(line)
                            except json.JSONDecodeError as exc:
                                raise TuoguanStoreError(
                                    f"Unable to update {name}: invalid JSON on line {line_number}"
                                ) from exc
                            if not isinstance(row, dict):
                                raise TuoguanStoreError(
                                    f"Unable to update {name}: line {line_number} is not an object"
                                )
                            rows.append(row)
                    updated = updater(deepcopy(rows))
                    if updated is JSON_NO_CHANGE:
                        return deepcopy(rows)
                    output = rows if updated is None else updated
                    if not isinstance(output, list) or any(not isinstance(row, dict) for row in output):
                        raise TuoguanStoreError(f"Unable to update {name}: updater must return object rows")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    preserved_owner = _owner_for_root_write(path)
                    preserved_mode = (path.stat().st_mode & 0o777) if path.exists() else None
                    self._backup_existing(path)
                    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                    with temp.open("w", encoding="utf-8", newline="\n") as handle:
                        for row in output:
                            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, path)
                    if preserved_mode is not None:
                        os.chmod(path, preserved_mode)
                    if preserved_owner is not None:
                        os.chown(path, *preserved_owner)
                    verified: list[dict[str, Any]] = []
                    for line in path.read_text(encoding="utf-8-sig").splitlines():
                        if line.strip():
                            candidate = json.loads(line)
                            if not isinstance(candidate, dict):
                                raise TuoguanStoreError(f"JSONL writeback verification failed: {name}")
                            verified.append(candidate)
                    if verified != output:
                        raise TuoguanStoreError(f"JSONL writeback verification failed: {name}")
                    return deepcopy(verified)
            except (OSError, TypeError, ValueError, PermissionError) as exc:
                raise TuoguanStoreError(f"Unable to update {name}: {exc}") from exc
