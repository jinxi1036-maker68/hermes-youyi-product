#!/usr/bin/env python3
"""Version-neutral runtime topology contract for XiaoYou/Hermes releases."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any


DEFAULT_RUNTIME_HOME = Path("/var/lib/hermes-youyi/hermes-home")
DEFAULT_CONFIG_ROOT = Path("/etc/hermes-youyi")


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def is_within(path: Path, parent: Path) -> bool:
    child = _resolved(path)
    root = _resolved(parent)
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class RuntimeTopology:
    release_root: str
    runtime_home: str
    config_root: str | None
    workspace_root: str | None
    agenda_root: str | None
    release_runtime_separated: bool
    runtime_home_direct_directory: bool
    config_outside_release: bool
    workspace_outside_release_and_home: bool
    agenda_outside_release_and_home: bool

    @property
    def ok(self) -> bool:
        return all(
            (
                self.release_runtime_separated,
                self.runtime_home_direct_directory,
                self.config_outside_release,
                self.workspace_outside_release_and_home,
                self.agenda_outside_release_and_home,
            )
        )


def validate_runtime_topology(
    *,
    release_root: Path,
    runtime_home: Path,
    config_root: Path | None = None,
    workspace_root: Path | None = None,
    agenda_root: Path | None = None,
    require_existing: bool = True,
) -> dict[str, Any]:
    release = _resolved(release_root)
    home = _resolved(runtime_home)
    config = _resolved(config_root) if config_root is not None else None
    workspace = _resolved(workspace_root) if workspace_root is not None else None
    agenda = _resolved(agenda_root) if agenda_root is not None else None

    errors: list[str] = []
    if require_existing and not release.is_dir():
        errors.append("release_root_missing")
    if require_existing and not home.is_dir():
        errors.append("runtime_home_missing")

    home_is_direct = not runtime_home.is_symlink()
    if not home_is_direct:
        errors.append("runtime_home_must_not_be_symlink")

    separated = not is_within(home, release) and not is_within(release, home)
    if not separated:
        errors.append("runtime_home_must_be_version_neutral")

    config_ok = config is None or not is_within(config, release)
    if not config_ok:
        errors.append("config_root_inside_release")

    workspace_ok = (
        workspace is None
        or (
            not is_within(workspace, release)
            and not is_within(workspace, home)
            and not is_within(home, workspace)
        )
    )
    if not workspace_ok:
        errors.append("workspace_must_be_separate_from_release_and_runtime_home")

    agenda_ok = (
        agenda is None
        or (
            not is_within(agenda, release)
            and not is_within(agenda, home)
            and not is_within(home, agenda)
        )
    )
    if not agenda_ok:
        errors.append("agenda_must_be_separate_from_release_and_runtime_home")

    topology = RuntimeTopology(
        release_root=str(release),
        runtime_home=str(home),
        config_root=str(config) if config is not None else None,
        workspace_root=str(workspace) if workspace is not None else None,
        agenda_root=str(agenda) if agenda is not None else None,
        release_runtime_separated=separated,
        runtime_home_direct_directory=home_is_direct,
        config_outside_release=config_ok,
        workspace_outside_release_and_home=workspace_ok,
        agenda_outside_release_and_home=agenda_ok,
    )
    return {
        "ok": topology.ok and not errors,
        "error": "" if topology.ok and not errors else "runtime_topology_invalid",
        "errors": errors,
        "topology": asdict(topology),
        "runtime_model": "version_neutral_persistent_home",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the XiaoYou/Hermes runtime topology.")
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--agenda-root", type=Path)
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args()

    result = validate_runtime_topology(
        release_root=args.release_root,
        runtime_home=args.runtime_home,
        config_root=args.config_root,
        workspace_root=args.workspace_root,
        agenda_root=args.agenda_root,
        require_existing=not args.allow_missing,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
