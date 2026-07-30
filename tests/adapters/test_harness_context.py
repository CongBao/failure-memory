import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from failure_memory.adapters import storage_permissions
from failure_memory.adapters.harness import context
from failure_memory.adapters.harness.context import HarnessContext, resolve_data_root


def test_harness_plugin_data_does_not_partition_the_global_store(tmp_path: Path) -> None:
    root = resolve_data_root(
        {
            "PLUGIN_DATA": str(tmp_path / "codex"),
            "CLAUDE_PLUGIN_DATA": str(tmp_path / "claude"),
            "FAILURE_MEMORY_HOME": str(tmp_path / "global"),
        }
    )
    assert root == tmp_path / "global"


def test_context_uses_keyed_stable_fingerprints(tmp_path: Path) -> None:
    one = HarnessContext.create(tmp_path, tmp_path / "repo", "codex", "session-1")
    two = HarnessContext.create(tmp_path, tmp_path / "repo", "codex", "session-1")
    assert one.workspace_fingerprint == two.workspace_fingerprint
    assert one.session_fingerprint == two.session_fingerprint
    assert str(tmp_path / "repo") not in one.workspace_fingerprint
    assert (tmp_path / "bootstrap" / "identity.key").stat().st_mode & 0o077 == 0


def test_context_restricts_an_existing_identity_key(tmp_path: Path) -> None:
    key_path = tmp_path / "bootstrap" / "identity.key"
    key_path.parent.mkdir()
    key_path.write_bytes(b"x" * 32)
    key_path.chmod(0o644)

    HarnessContext.create(tmp_path, tmp_path / "repo", "codex", None)

    assert key_path.stat().st_mode & 0o077 == 0


def test_context_hardens_only_failure_memory_owned_descendants_under_open_umask(
    tmp_path: Path,
) -> None:
    """Would fail if the data root stayed public or a shared ancestor was chmodded."""
    shared = tmp_path / "shared-host-data"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    root = shared / "failure-memory"
    previous_umask = os.umask(0)
    try:
        HarnessContext.create(root, tmp_path / "repo", "codex", None)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(shared.stat().st_mode) == 0o777
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "bootstrap").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "bootstrap" / "identity.key").stat().st_mode) == 0o600


def test_context_avoids_platform_incompatible_directory_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate Windows CRT rejecting os.open on directories during service startup."""
    real_open = storage_permissions.os.open

    def reject_directory_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        if Path(path).is_dir():
            raise PermissionError("simulated Windows directory open rejection")
        return real_open(path, flags, mode)

    monkeypatch.setattr(storage_permissions, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage_permissions.os, "open", reject_directory_open)

    created = HarnessContext.create(tmp_path / "data", tmp_path, "codex", None)

    assert created.data_root == tmp_path / "data"
    assert (created.data_root / "bootstrap" / "identity.key").is_file()


def test_context_rejects_symlink_data_root_without_chmodding_target(
    tmp_path: Path,
) -> None:
    """Would fail if a configured root symlink redirected private-state hardening."""
    external = tmp_path / "unrelated"
    external.mkdir(mode=0o755)
    external.chmod(0o755)
    linked_root = tmp_path / "failure-memory"
    linked_root.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError, match="symbolic link"):
        HarnessContext.create(linked_root, tmp_path / "repo", "codex", None)

    assert stat.S_IMODE(external.stat().st_mode) == 0o755
    assert not (external / "bootstrap").exists()


def test_context_rejects_symlink_identity_key_without_chmodding_target(
    tmp_path: Path,
) -> None:
    """Would fail if an owned-key path followed a symlink into unrelated data."""
    root = tmp_path / "failure-memory"
    bootstrap = root / "bootstrap"
    bootstrap.mkdir(parents=True)
    external = tmp_path / "unrelated.key"
    external.write_bytes(b"x" * 32)
    external.chmod(0o644)
    (bootstrap / "identity.key").symlink_to(external)

    with pytest.raises(OSError, match="symbolic link"):
        HarnessContext.create(root, tmp_path / "repo", "codex", None)

    assert stat.S_IMODE(external.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX inaccessible-mode regression")
def test_context_rejects_inaccessible_owned_key_without_changing_it(
    tmp_path: Path,
) -> None:
    """Define fail-closed behavior when a pre-existing owned file cannot be opened."""
    root = tmp_path / "failure-memory"
    bootstrap = root / "bootstrap"
    bootstrap.mkdir(parents=True)
    key = bootstrap / "identity.key"
    key.write_bytes(b"x" * 32)
    key.chmod(0)
    try:
        with pytest.raises(PermissionError):
            HarnessContext.create(root, tmp_path / "repo", "codex", None)
        assert stat.S_IMODE(key.stat().st_mode) == 0
    finally:
        key.chmod(0o600)


def test_concurrent_first_contexts_share_one_identity_key(tmp_path: Path) -> None:
    def create_context() -> HarnessContext:
        return HarnessContext.create(tmp_path, tmp_path / "repo", "codex", "session-1")

    with ThreadPoolExecutor(max_workers=8) as executor:
        contexts = list(executor.map(lambda _: create_context(), range(8)))

    assert {item.workspace_fingerprint for item in contexts} == {contexts[0].workspace_fingerprint}
    assert {item.session_fingerprint for item in contexts} == {contexts[0].session_fingerprint}


def test_failed_key_write_removes_temporary_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context.os, "write", lambda _descriptor, _key: 0)

    with pytest.raises(OSError, match="failed to write complete identity key"):
        HarnessContext.create(tmp_path, tmp_path / "repo", "codex", None)

    assert list((tmp_path / "bootstrap").glob(".identity.key.*.tmp")) == []


def test_windows_permission_protection_uses_icacls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "identity.key"
    key_path.write_bytes(b"x" * 32)
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> None:
        commands.append(command)

    monkeypatch.setattr(context.subprocess, "run", run)

    context._restrict_key_permissions(key_path, is_windows=True)

    assert commands == [
        [
            "icacls",
            str(key_path),
            "/inheritance:r",
            "/grant:r",
            f"{context.getpass.getuser()}:(R,W)",
        ],
    ]
