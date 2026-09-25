from __future__ import annotations


def test_runtime_topology_accepts_version_neutral_persistent_home(tmp_path):
    from scripts.xiaoyou_runtime_topology import validate_runtime_topology

    release = tmp_path / "releases" / "abc123"
    home = tmp_path / "var-lib" / "hermes-home"
    config = tmp_path / "etc" / "hermes-youyi"
    workspace = tmp_path / "workspace"
    agenda = tmp_path / "agenda"
    for path in (release, home, config, workspace, agenda):
        path.mkdir(parents=True)

    result = validate_runtime_topology(
        release_root=release,
        runtime_home=home,
        config_root=config,
        workspace_root=workspace,
        agenda_root=agenda,
    )

    assert result["ok"] is True
    assert result["runtime_model"] == "version_neutral_persistent_home"
    assert result["topology"]["release_runtime_separated"] is True


def test_runtime_topology_rejects_release_local_home(tmp_path):
    from scripts.xiaoyou_runtime_topology import validate_runtime_topology

    release = tmp_path / "release"
    home = release / "home"
    home.mkdir(parents=True)

    result = validate_runtime_topology(
        release_root=release,
        runtime_home=home,
    )

    assert result["ok"] is False
    assert "runtime_home_must_be_version_neutral" in result["errors"]


def test_runtime_topology_rejects_workspace_inside_runtime_home(tmp_path):
    from scripts.xiaoyou_runtime_topology import validate_runtime_topology

    release = tmp_path / "release"
    home = tmp_path / "persistent-home"
    workspace = home / "workspace"
    release.mkdir()
    workspace.mkdir(parents=True)

    result = validate_runtime_topology(
        release_root=release,
        runtime_home=home,
        workspace_root=workspace,
    )

    assert result["ok"] is False
    assert "workspace_must_be_separate_from_release_and_runtime_home" in result["errors"]


def test_runtime_topology_rejects_symlinked_runtime_home(tmp_path):
    from scripts.xiaoyou_runtime_topology import validate_runtime_topology

    release = tmp_path / "release"
    actual_home = tmp_path / "actual-home"
    link_home = tmp_path / "home-link"
    release.mkdir()
    actual_home.mkdir()
    link_home.symlink_to(actual_home, target_is_directory=True)

    result = validate_runtime_topology(
        release_root=release,
        runtime_home=link_home,
    )

    assert result["ok"] is False
    assert "runtime_home_must_not_be_symlink" in result["errors"]
