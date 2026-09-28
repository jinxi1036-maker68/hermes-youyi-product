from __future__ import annotations

import json
import os
from pathlib import Path


def _inputs(tmp_path: Path):
    config = tmp_path / "config.yaml"
    env = tmp_path / "runtime.env"
    unit = tmp_path / "gateway.service"
    config.write_bytes(b"api_key: super-secret\n")
    env.write_bytes(b"SECRET_VALUE=hidden\n")
    unit.write_bytes(b"[Service]\nExecStart=/opt/candidate/.venv/bin/hermes\n")
    return [
        ("config_yaml", config),
        ("runtime_env", env),
        ("gateway_unit", unit),
    ]


def test_gateway_snapshot_is_dry_run_by_default_and_never_exposes_contents(tmp_path):
    from scripts.xiaoyou_gateway_rollback_snapshot import create_gateway_snapshot

    inputs = _inputs(tmp_path)
    output = tmp_path / "snapshots"
    result = create_gateway_snapshot(inputs=inputs, output_root=output, apply=False)

    assert result["ok"] is True
    assert result["snapshot_created"] is False
    assert result["runtime_inputs_changed"] is False
    assert not output.exists()
    assert "super-secret" not in str(result)
    assert "SECRET_VALUE" not in str(result)


def test_gateway_snapshot_copies_exact_bytes_and_verifies_hashes(tmp_path):
    from scripts.xiaoyou_gateway_rollback_snapshot import (
        create_gateway_snapshot,
        verify_gateway_snapshot,
    )

    inputs = _inputs(tmp_path)
    output = tmp_path / "snapshots"
    result = create_gateway_snapshot(
        inputs=inputs,
        output_root=output,
        snapshot_name="gate-test",
        apply=True,
    )

    assert result["ok"] is True
    assert result["snapshot_created"] is True
    manifest_path = Path(result["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["runtime_inputs_changed"] is False
    assert manifest["secret_content_exposed"] is False
    assert (manifest_path.stat().st_mode & 0o777) == 0o600
    assert (manifest_path.parent.stat().st_mode & 0o777) == 0o700

    by_label = {label: path for label, path in inputs}
    for row in manifest["files"]:
        copied = manifest_path.parent / row["snapshot_relative_path"]
        assert copied.read_bytes() == by_label[row["label"]].read_bytes()
        assert (copied.stat().st_mode & 0o777) == 0o600

    verified = verify_gateway_snapshot(manifest_path)
    assert verified["ok"] is True
    assert verified["file_count"] == 3


def test_gateway_snapshot_verifier_detects_tampering(tmp_path):
    from scripts.xiaoyou_gateway_rollback_snapshot import (
        create_gateway_snapshot,
        verify_gateway_snapshot,
    )

    result = create_gateway_snapshot(
        inputs=_inputs(tmp_path),
        output_root=tmp_path / "snapshots",
        snapshot_name="tamper-test",
        apply=True,
    )
    manifest_path = Path(result["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = manifest_path.parent / manifest["files"][0]["snapshot_relative_path"]
    target.write_bytes(b"tampered\n")
    os.chmod(target, 0o600)

    verified = verify_gateway_snapshot(manifest_path)
    assert verified["ok"] is False
    assert any("snapshot_hash_mismatch" in error for error in verified["errors"])


def test_gateway_snapshot_rejects_symlink_input(tmp_path):
    from scripts.xiaoyou_gateway_rollback_snapshot import create_gateway_snapshot

    inputs = _inputs(tmp_path)
    link = tmp_path / "runtime-link.env"
    os.symlink(inputs[1][1], link)
    result = create_gateway_snapshot(
        inputs=[("runtime_env", link)],
        output_root=tmp_path / "snapshots",
        apply=False,
    )

    assert result["ok"] is False
    assert "input_symlink_forbidden:runtime_env" in result["errors"]


def test_gateway_snapshot_rejects_duplicate_labels(tmp_path):
    from scripts.xiaoyou_gateway_rollback_snapshot import create_gateway_snapshot

    inputs = _inputs(tmp_path)
    result = create_gateway_snapshot(
        inputs=[("same", inputs[0][1]), ("same", inputs[1][1])],
        output_root=tmp_path / "snapshots",
        apply=False,
    )

    assert result["ok"] is False
    assert "duplicate_label:same" in result["errors"]
