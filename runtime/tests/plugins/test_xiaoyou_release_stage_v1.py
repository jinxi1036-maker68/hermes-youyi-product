from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _active_fixture(base: Path) -> dict[str, bytes]:
    files = {
        "runtime/plugins/tuoguan_core/__init__.py": b"old-tuoguan\n",
        "runtime/plugins/platforms/wecom/callback_adapter.py": b"old-wecom\n",
        "scripts/current_marker.py": b"OLD = True\n",
    }
    for relative, payload in files.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return files


def test_stage_only_release_never_mutates_active_links_or_files(tmp_path):
    from scripts.xiaoyou_release_installer import stage_release
    from scripts.xiaoyou_release_package import (
        build_release,
        verify_release_directory,
    )

    built = build_release(
        root=ROOT,
        output_dir=tmp_path / "dist",
        hermes_version="server-verified",
        model="server-verified",
        require_clean=False,
        source_commit="a" * 40,
    )
    release_root = Path(built["release_root"])
    base = tmp_path / "base"
    base.mkdir()
    active = _active_fixture(base)

    before = {
        relative: {
            "bytes": (base / relative).read_bytes(),
            "is_symlink": (base / relative).is_symlink(),
        }
        for relative in active
    }

    staged = stage_release(
        release_root=release_root,
        base=base,
        apply=True,
    )

    assert staged["ok"] is True
    assert staged["staged"] is True
    assert staged["applied"] is True
    assert staged["active_targets_modified"] is False

    canonical = Path(staged["canonical_release"])
    assert verify_release_directory(canonical)["ok"] is True
    assert (
        canonical
        / "payload/deploy/systemd/xiaoyou-wecom-holding-bridge.service.example"
    ).is_file()
    assert (
        canonical
        / "payload/deploy/config/runtime.wecom-holding.env.example"
    ).is_file()

    for relative, expected in before.items():
        current = base / relative
        assert current.read_bytes() == expected["bytes"]
        assert current.is_symlink() == expected["is_symlink"]

    again = stage_release(
        release_root=release_root,
        base=base,
        apply=True,
    )
    assert again["ok"] is True
    assert again["staged"] is True
    assert again["already_present"] is True
    assert again["applied"] is False
    assert again["active_targets_modified"] is False


def test_stage_only_rejects_tampered_existing_canonical_release(tmp_path):
    from scripts.xiaoyou_release_installer import stage_release
    from scripts.xiaoyou_release_package import build_release

    built = build_release(
        root=ROOT,
        output_dir=tmp_path / "dist",
        hermes_version="server-verified",
        model="server-verified",
        require_clean=False,
        source_commit="b" * 40,
    )
    release_root = Path(built["release_root"])
    base = tmp_path / "base"
    base.mkdir()
    active = _active_fixture(base)
    before = {
        relative: (base / relative).read_bytes()
        for relative in active
    }

    first = stage_release(
        release_root=release_root,
        base=base,
        apply=True,
    )
    assert first["ok"] is True

    canonical = Path(first["canonical_release"])
    target = canonical / "payload/runtime/plugins/platforms/wecom/holding_bridge.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# tampered\n",
        encoding="utf-8",
    )

    second = stage_release(
        release_root=release_root,
        base=base,
        apply=True,
    )
    assert second["ok"] is False
    assert second["error"] == "existing_canonical_release_invalid"
    assert second["active_targets_modified"] is False

    for relative, payload in before.items():
        assert (base / relative).read_bytes() == payload


def test_bridge_systemd_template_uses_staged_candidate_code_boundary():
    unit = (
        ROOT
        / "deploy/systemd/xiaoyou-wecom-holding-bridge.service.example"
    ).read_text(encoding="utf-8")

    assert "User=${XIAOYOU_SERVICE_USER}" in unit
    assert "Group=${XIAOYOU_SERVICE_GROUP}" in unit
    assert "WorkingDirectory=${XIAOYOU_RELEASE_PAYLOAD_ROOT}" in unit
    assert (
        "Environment=PYTHONPATH=${XIAOYOU_RELEASE_PAYLOAD_ROOT}/runtime"
        in unit
    )
    assert (
        "ExecStart=${HERMES_PYTHON} "
        "${XIAOYOU_RELEASE_PAYLOAD_ROOT}/scripts/"
        "xiaoyou_wecom_holding_bridge.py"
        in unit
    )
    assert "${HERMES_RUNTIME_ROOT}/scripts" not in unit
    assert "${HERMES_RUNTIME_ROOT}/.venv/bin/python" not in unit
