from __future__ import annotations

import ast
import errno
import importlib.util
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUILDER_RELATIVE = Path("packaging/build_codex.py")
INSTALLABLE_INPUTS = (
    Path(".codex-plugin"),
    Path(".mcp.json"),
    Path(".gitignore"),
    Path("CHANGELOG.md"),
    Path("CONTRIBUTING.md"),
    Path("LICENSE"),
    Path("README.md"),
    Path("SECURITY.md"),
    Path("scripts"),
    Path("skills"),
    Path("src"),
)
SENSITIVE_STORE_BASENAMES = (
    "secret",
    "secrets",
    "private",
    "privates",
    "credential",
    "credentials",
    "key",
    "keys",
    "cert",
    "certs",
    "certificate",
    "certificates",
    ".secret",
    ".secrets",
    ".private",
    ".privates",
    ".credential",
    ".credentials",
    ".key",
    ".keys",
    ".cert",
    ".certs",
    ".certificate",
    ".certificates",
)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _copy_source(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _fixture_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    for relative in INSTALLABLE_INPUTS:
        _copy_source(REPOSITORY_ROOT / relative, repository / relative)
    _copy_source(REPOSITORY_ROOT / BUILDER_RELATIVE, repository / BUILDER_RELATIVE)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Packaging test")
    _git(repository, "config", "user.email", "packaging@example.invalid")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "fixture")
    return repository


def _run_builder(
    repository: Path,
    output: Path,
    *,
    allow_dirty: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(repository / BUILDER_RELATIVE),
        "--output",
        str(output),
    ]
    if allow_dirty:
        command.append("--allow-dirty")
    return subprocess.run(
        command,
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )


def _load_builder(repository: Path) -> ModuleType:
    module_path = repository / BUILDER_RELATIVE
    module_name = f"_failure_memory_builder_{id(repository)}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _existing_output(tmp_path: Path) -> tuple[Path, Path]:
    output = tmp_path / "publish" / "failure-memory"
    output.mkdir(parents=True)
    marker = output / "existing-marker.txt"
    marker.write_text("preserve existing output\n")
    return output, marker


def _assert_no_internal_artifacts(parent: Path) -> None:
    assert not any(
        child.name.startswith(
            (
                ".failure-memory.previous-",
                ".failure-memory.recovery-",
                ".failure-memory.stage-",
                ".publication-recovery-",
            )
        )
        for child in parent.iterdir()
    )


def test_build_returns_structured_result_for_initially_absent_output(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "private-publish" / "failure-memory"
    builder = _load_builder(repository)

    result = builder.build(requested_output=output, allow_dirty=False)

    assert isinstance(result, builder.BuildResult)
    assert result.output == output
    assert result.rollback is None
    assert result.recovery is None


def test_allow_dirty_omits_a_tracked_file_deleted_from_the_worktree(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    optional_document = repository / "docs" / "optional.md"
    optional_document.parent.mkdir()
    optional_document.write_text("optional public documentation\n")
    _git(repository, "add", "docs/optional.md")
    _git(repository, "commit", "-m", "add optional documentation")
    optional_document.unlink()
    output = tmp_path / "private-publish" / "failure-memory"

    completed = _run_builder(repository, output, allow_dirty=True)

    assert completed.returncode == 0, completed.stderr
    assert not (output / "docs" / "optional.md").exists()


def test_group_writable_output_parent_is_rejected(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "unsafe-publish"
    parent.mkdir()
    os.chmod(parent, 0o770)
    output = parent / "failure-memory"
    builder = _load_builder(repository)

    try:
        with pytest.raises(builder.BuildError, match="group/world-writable"):
            builder.build(requested_output=output, allow_dirty=False)
    finally:
        os.chmod(parent, 0o700)

    assert not output.exists()


def test_group_writable_repository_root_is_rejected_for_clean_build(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    os.chmod(repository, 0o770)
    output = tmp_path / "private-publish" / "failure-memory"
    builder = _load_builder(repository)

    try:
        with pytest.raises(builder.BuildError, match="group/world-writable"):
            builder.build(requested_output=output, allow_dirty=False)
    finally:
        os.chmod(repository, 0o755)

    assert not output.exists()


def test_wrong_owner_private_trust_domain_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "private-publish" / "failure-memory"
    builder = _load_builder(repository)
    actual_uid = os.getuid()
    monkeypatch.setattr(builder.os, "getuid", lambda: actual_uid + 1)

    with pytest.raises(builder.BuildError, match="owned by current uid"):
        builder.build(requested_output=output, allow_dirty=False)

    assert not output.exists()


def test_temp_private_trust_anchor_is_reported_by_directory_opener(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)
    private_anchor = tmp_path / "private-anchor"
    private_anchor.mkdir(mode=0o700)
    target = private_anchor / "nested" / "publish"

    trusted = builder._open_trusted_directory(target, create=True)
    try:
        assert trusted.path == target
        assert trusted.anchor == private_anchor
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
    finally:
        os.close(trusted.fd)


def test_source_snapshot_failure_records_selected_repository_trust_anchor(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    (repository / "README.md").write_text("dirty source\n")
    output = tmp_path / "private-publish" / "failure-memory"
    builder = _load_builder(repository)

    with pytest.raises(builder.BuildError, match="repository is dirty") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert caught.value.trust_anchor == repository
    assert not output.exists()


def test_postopen_repository_oserror_reports_selected_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "private-publish" / "failure-memory"
    builder = _load_builder(repository)
    fstat = builder.os.fstat
    injected = False

    def fail_first_postopen_fstat(file_descriptor: int) -> os.stat_result:
        nonlocal injected
        if not injected:
            injected = True
            raise OSError("injected repository post-open fstat failure")
        return fstat(file_descriptor)

    monkeypatch.setattr(builder.os, "fstat", fail_first_postopen_fstat)

    with pytest.raises(builder.BuildError, match="repository post-open") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert caught.value.trust_anchor == repository
    assert not output.exists()


def test_postopen_output_parent_oserror_reports_selected_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "private-publish"
    parent.mkdir(mode=0o700)
    builder = _load_builder(repository)
    fstat = builder.os.fstat
    injected = False

    def fail_first_postopen_fstat(file_descriptor: int) -> os.stat_result:
        nonlocal injected
        if not injected:
            injected = True
            raise OSError("injected output-parent post-open fstat failure")
        return fstat(file_descriptor)

    monkeypatch.setattr(builder.os, "fstat", fail_first_postopen_fstat)

    with pytest.raises(builder.BuildError, match="output-parent post-open") as caught:
        builder._open_parent(parent)

    assert injected
    assert caught.value.trust_anchor == parent


def test_descendant_postopen_fstat_failure_closes_the_descendant_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)
    private_anchor = tmp_path / "private-anchor"
    private_anchor.mkdir(mode=0o700)
    descendant = private_anchor / "descendant"
    descendant.mkdir(mode=0o700)
    fstat = builder.os.fstat
    calls = 0
    descendant_fd: int | None = None

    def fail_descendant_fstat(file_descriptor: int) -> os.stat_result:
        nonlocal calls, descendant_fd
        calls += 1
        if calls == 2:
            descendant_fd = file_descriptor
            raise OSError("injected descendant fstat failure")
        return fstat(file_descriptor)

    monkeypatch.setattr(builder.os, "fstat", fail_descendant_fstat)

    with pytest.raises(builder.BuildError, match="descendant fstat failure") as caught:
        builder._open_trusted_directory(
            descendant,
            create=False,
            anchor=private_anchor,
        )

    assert caught.value.trust_anchor == private_anchor
    assert descendant_fd is not None
    with pytest.raises(OSError):
        os.fstat(descendant_fd)


def test_live_relative_opener_closes_descendant_when_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    descendant = repository / "safe-directory"
    descendant.mkdir(mode=0o755)
    builder = _load_builder(repository)
    repository_fd = os.open(repository, builder._directory_flags())
    assert_safe = builder._assert_safe_live_directory
    descendant_fd: int | None = None

    def fail_descendant_validation(file_descriptor: int, label: str) -> None:
        nonlocal descendant_fd
        if label == descendant.name:
            descendant_fd = file_descriptor
            raise builder.BuildError("injected live descendant validation failure")
        assert_safe(file_descriptor, label)

    monkeypatch.setattr(
        builder,
        "_assert_safe_live_directory",
        fail_descendant_validation,
    )

    try:
        with pytest.raises(builder.BuildError, match="descendant validation failure"):
            builder._open_relative_directory(repository_fd, (descendant.name,))
    finally:
        os.close(repository_fd)

    assert descendant_fd is not None
    with pytest.raises(OSError):
        os.fstat(descendant_fd)


def test_staging_directory_opener_closes_created_child_when_fchmod_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)
    staging_root = tmp_path / "staging-root"
    staging_root.mkdir(mode=0o700)
    root_fd = os.open(staging_root, builder._directory_flags())
    fchmod = builder.os.fchmod
    child_fd: int | None = None

    def fail_created_child_mode(file_descriptor: int, mode: int) -> None:
        nonlocal child_fd
        if mode == 0o755:
            child_fd = file_descriptor
            raise OSError("injected staged child fchmod failure")
        fchmod(file_descriptor, mode)

    monkeypatch.setattr(builder.os, "fchmod", fail_created_child_mode)

    try:
        with pytest.raises(OSError, match="staged child fchmod failure"):
            builder._ensure_directory(root_fd, ("child",))
    finally:
        os.close(root_fd)

    assert child_fd is not None
    with pytest.raises(OSError):
        os.fstat(child_fd)


def test_absolute_directory_opener_closes_created_child_when_fchmod_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)
    target = tmp_path / "new-absolute-directory"
    fchmod = builder.os.fchmod
    child_fd: int | None = None

    def fail_created_child_mode(file_descriptor: int, mode: int) -> None:
        nonlocal child_fd
        if mode == 0o755:
            child_fd = file_descriptor
            raise OSError("injected absolute child fchmod failure")
        fchmod(file_descriptor, mode)

    monkeypatch.setattr(builder.os, "fchmod", fail_created_child_mode)

    with pytest.raises(OSError, match="absolute child fchmod failure"):
        builder._open_absolute_directory(target, create=True)

    assert child_fd is not None
    with pytest.raises(OSError):
        os.fstat(child_fd)


def test_staged_file_close_failure_does_not_mask_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)
    staging_root = tmp_path / "staging-root"
    staging_root.mkdir(mode=0o700)
    root_fd = os.open(staging_root, builder._directory_flags())
    close = builder.os.close
    file_fd: int | None = None

    def fail_write(open_file_fd: int, data: bytes) -> None:
        nonlocal file_fd
        del data
        file_fd = open_file_fd
        raise builder.BuildError("injected staged file write failure")

    def close_then_fail(file_descriptor: int) -> None:
        close(file_descriptor)
        if file_descriptor == file_fd:
            raise OSError("injected staged file close failure")

    monkeypatch.setattr(builder, "_write_all", fail_write)
    monkeypatch.setattr(builder.os, "close", close_then_fail)

    try:
        with pytest.raises(builder.BuildError, match="staged file write failure"):
            builder._write_file_at(
                root_fd,
                Path("probe.txt"),
                b"probe",
                0o644,
            )
    finally:
        os.close(root_fd)

    assert file_fd is not None
    with pytest.raises(OSError):
        os.fstat(file_fd)


def test_live_file_close_failure_does_not_mask_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)
    repository_fd = os.open(repository, builder._directory_flags())
    close = builder.os.close
    file_fd: int | None = None

    def fail_read(open_file_fd: int) -> bytes:
        nonlocal file_fd
        file_fd = open_file_fd
        raise builder.BuildError("injected live file read failure")

    def close_then_fail(file_descriptor: int) -> None:
        close(file_descriptor)
        if file_descriptor == file_fd:
            raise OSError("injected live file close failure")

    monkeypatch.setattr(builder, "_read_all", fail_read)
    monkeypatch.setattr(builder.os, "close", close_then_fail)

    try:
        with pytest.raises(builder.BuildError, match="live file read failure"):
            builder._read_live_source(
                repository_fd,
                Path("README.md"),
                "100644",
            )
    finally:
        os.close(repository_fd)

    assert file_fd is not None
    with pytest.raises(OSError):
        os.fstat(file_fd)


def test_trusted_opener_overwrites_injected_wrong_anchor_with_selected_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)
    wrong_anchor = tmp_path / "wrong-anchor"
    fstat = builder.os.fstat
    injected = False

    def fail_with_wrong_anchor(file_descriptor: int) -> os.stat_result:
        nonlocal injected
        if not injected:
            injected = True
            raise builder.BuildError(
                "injected wrong-anchor failure",
                trust_anchor=wrong_anchor,
            )
        return fstat(file_descriptor)

    monkeypatch.setattr(builder.os, "fstat", fail_with_wrong_anchor)

    with pytest.raises(builder.BuildError, match="wrong-anchor failure") as caught:
        builder._open_trusted_directory(repository, create=False)

    assert injected
    assert caught.value.trust_anchor == repository


def test_postopen_stage_mode_failure_reports_recovery_and_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "private-publish"
    parent.mkdir(mode=0o700)
    output = parent / "failure-memory"
    builder = _load_builder(repository)
    real_fchmod = builder.os.fchmod
    stage_fd: int | None = None

    def fail_stage_mode(file_descriptor: int, mode: int) -> None:
        nonlocal stage_fd
        if stage_fd is None and mode == 0o700:
            stage_fd = file_descriptor
            raise OSError("injected transaction fchmod failure")
        real_fchmod(file_descriptor, mode)

    monkeypatch.setattr(builder.os, "fchmod", fail_stage_mode)

    with pytest.raises(builder.BuildError, match="transaction setup failed") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert stage_fd is not None
    with pytest.raises(OSError):
        os.fstat(stage_fd)
    assert caught.value.trust_anchor == parent
    assert len(caught.value.recovery_paths) == 1
    recovery = caught.value.recovery_paths[0]
    assert recovery.is_dir()
    assert recovery.parent == parent
    assert not output.exists()


def test_publication_transaction_construction_baseexception_reports_recovery_and_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Would fail if transaction construction escaped the retained-recovery scope."""
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "private-publish"
    parent.mkdir(mode=0o700)
    output = parent / "failure-memory"
    builder = _load_builder(repository)
    stage_fd: int | None = None

    def interrupt_construction(**arguments: object) -> object:
        nonlocal stage_fd
        value = arguments["stage_fd"]
        assert isinstance(value, int)
        stage_fd = value
        raise KeyboardInterrupt("injected transaction construction interruption")

    monkeypatch.setattr(builder, "PublicationTransaction", interrupt_construction)

    with pytest.raises(builder.BuildError, match="retained recovery") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert stage_fd is not None
    with pytest.raises(OSError):
        os.fstat(stage_fd)
    assert caught.value.trust_anchor == parent
    assert len(caught.value.recovery_paths) == 1
    recovery = caught.value.recovery_paths[0]
    assert recovery.parent == parent
    assert recovery.is_dir()
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o700
    assert not output.exists()


def test_stage_mkdir_then_baseexception_reports_exact_retained_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "private-publish"
    parent.mkdir(mode=0o700)
    output = parent / "failure-memory"
    builder = _load_builder(repository)
    mkdir = builder.os.mkdir
    injected = False

    def mkdir_then_interrupt(
        path: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal injected
        mkdir(path, mode, dir_fd=dir_fd)
        if not injected and path.startswith(".failure-memory.previous-"):
            injected = True
            raise KeyboardInterrupt("injected after real transaction mkdir")

    monkeypatch.setattr(builder, "_require_descriptor_primitives", lambda: None)
    monkeypatch.setattr(builder.os, "mkdir", mkdir_then_interrupt)

    with pytest.raises(builder.BuildError, match="transaction setup failed") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert caught.value.trust_anchor == parent
    assert len(caught.value.recovery_paths) == 1
    recovery = caught.value.recovery_paths[0]
    assert recovery.is_dir()
    assert recovery.parent == parent
    assert not output.exists()


def test_stage_setup_reports_actual_recovery_after_held_parent_relocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent_path = tmp_path / "private-publish"
    parent_path.mkdir(mode=0o700)
    relocation_root = tmp_path / "relocated"
    relocation_root.mkdir(mode=0o700)
    relocated_parent = relocation_root / "publish-displaced"
    builder = _load_builder(repository)
    parent = builder._open_parent(parent_path)
    mkdir = builder.os.mkdir
    injected = False

    def mkdir_relocate_then_interrupt(
        path: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal injected
        mkdir(path, mode, dir_fd=dir_fd)
        if not injected and path.startswith(".failure-memory.previous-"):
            injected = True
            parent_path.rename(relocated_parent)
            raise KeyboardInterrupt("injected after held parent relocation")

    monkeypatch.setattr(builder.os, "mkdir", mkdir_relocate_then_interrupt)

    try:
        with pytest.raises(builder.BuildError, match="transaction setup failed") as caught:
            builder._create_stage(parent, "failure-memory", "a" * 40)
    finally:
        os.close(parent.fd)

    assert injected
    assert len(caught.value.recovery_paths) == 1
    recovery = caught.value.recovery_paths[0]
    assert recovery.parent == relocated_parent
    assert recovery.is_dir()
    assert not (parent_path / recovery.name).exists()


def test_first_postmkdir_stage_probe_baseexception_reports_exact_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "private-publish"
    parent.mkdir(mode=0o700)
    output = parent / "failure-memory"
    builder = _load_builder(repository)
    lstat_at = builder._lstat_at
    injected = False

    def interrupt_first_transaction_probe(directory_fd: int, name: str) -> object:
        nonlocal injected
        if not injected and name.startswith(".failure-memory.previous-"):
            injected = True
            raise KeyboardInterrupt("injected first post-mkdir stage probe interruption")
        return lstat_at(directory_fd, name)

    monkeypatch.setattr(builder, "_lstat_at", interrupt_first_transaction_probe)

    with pytest.raises(builder.BuildError, match="transaction setup failed") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert caught.value.trust_anchor == parent
    assert len(caught.value.recovery_paths) == 1
    recovery = caught.value.recovery_paths[0]
    assert recovery.parent == parent
    assert recovery.is_dir()
    assert not output.exists()


def test_output_parent_becoming_writable_after_exchange_rolls_back_and_retains_generated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    parent = output.parent
    builder = _load_builder(repository)
    real_exchange = builder._atomic_exchange
    injected = False

    def exchange_then_weaken_parent(parent_fd: int, left: str, right: str) -> None:
        nonlocal injected
        real_exchange(parent_fd, left, right)
        if not injected:
            injected = True
            os.chmod(parent, 0o770)

    monkeypatch.setattr(builder, "_atomic_exchange", exchange_then_weaken_parent)

    try:
        with pytest.raises(builder.BuildError, match="trust domain changed") as caught:
            builder.build(requested_output=output, allow_dirty=False)
    finally:
        os.chmod(parent, 0o700)

    assert injected
    assert marker.read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    assert (caught.value.recovery_paths[0] / ".codex-plugin" / "plugin.json").is_file()


def test_original_output_trust_anchor_becoming_writable_blocks_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    private_anchor = tmp_path / "private-anchor"
    private_anchor.mkdir(mode=0o700)
    output = private_anchor / "nested" / "failure-memory"
    builder = _load_builder(repository)
    write_file = builder._write_file_at
    injected = False

    def weaken_original_anchor_before_first_stage_write(
        root_fd: int,
        relative: object,
        data: bytes,
        mode: int,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            os.chmod(private_anchor, 0o770)
        write_file(root_fd, relative, data, mode)

    monkeypatch.setattr(
        builder,
        "_write_file_at",
        weaken_original_anchor_before_first_stage_write,
    )

    try:
        with pytest.raises(builder.BuildError, match="trust domain changed") as caught:
            builder.build(requested_output=output, allow_dirty=False)
    finally:
        os.chmod(private_anchor, 0o700)

    assert injected
    assert not output.exists()
    assert caught.value.trust_anchor == private_anchor
    assert len(caught.value.recovery_paths) == 1
    assert caught.value.recovery_paths[0].is_dir()


def test_repository_becoming_writable_after_snapshot_blocks_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    write_file = builder._write_file_at
    injected = False

    def weaken_repository_before_first_stage_write(
        root_fd: int,
        relative: object,
        data: bytes,
        mode: int,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            os.chmod(repository, 0o770)
        write_file(root_fd, relative, data, mode)

    monkeypatch.setattr(
        builder,
        "_write_file_at",
        weaken_repository_before_first_stage_write,
    )

    try:
        with pytest.raises(builder.BuildError, match="trust domain changed") as caught:
            builder.build(requested_output=output, allow_dirty=False)
    finally:
        os.chmod(repository, 0o755)

    assert injected
    assert marker.read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    assert caught.value.recovery_paths[0].is_dir()


def test_repository_becoming_writable_after_real_exchange_rolls_back_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    real_exchange = builder._atomic_exchange
    injected = False

    def exchange_then_weaken_repository(parent_fd: int, left: str, right: str) -> None:
        nonlocal injected
        real_exchange(parent_fd, left, right)
        if not injected:
            injected = True
            os.chmod(repository, 0o770)

    monkeypatch.setattr(builder, "_atomic_exchange", exchange_then_weaken_repository)

    try:
        with pytest.raises(builder.BuildError, match="trust domain changed") as caught:
            builder.build(requested_output=output, allow_dirty=False)
    finally:
        os.chmod(repository, 0o755)

    assert injected
    assert marker.read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    assert (caught.value.recovery_paths[0] / ".codex-plugin" / "plugin.json").is_file()


def test_pinned_output_close_failure_does_not_mask_identity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    output_metadata = output.stat()
    builder = _load_builder(repository)
    assert_entry_identity = builder._assert_entry_identity
    close = builder.os.close
    injected_close = False

    def fail_initial_identity(
        parent_fd: int,
        name: str,
        expected: object,
        *,
        label: str,
    ) -> None:
        if label == "initial prior output":
            raise builder.BuildError("injected pinned output identity failure")
        assert_entry_identity(parent_fd, name, expected, label=label)

    def close_output_then_fail(file_descriptor: int) -> None:
        nonlocal injected_close
        try:
            metadata = os.fstat(file_descriptor)
        except OSError:
            metadata = None
        is_output = (
            metadata is not None
            and metadata.st_dev == output_metadata.st_dev
            and metadata.st_ino == output_metadata.st_ino
        )
        close(file_descriptor)
        if is_output and not injected_close:
            injected_close = True
            raise OSError("injected pinned output close failure")

    monkeypatch.setattr(builder, "_assert_entry_identity", fail_initial_identity)
    monkeypatch.setattr(builder.os, "close", close_output_then_fail)

    with pytest.raises(builder.BuildError, match="pinned output identity failure"):
        builder.build(requested_output=output, allow_dirty=False)

    assert injected_close
    assert marker.read_text() == "preserve existing output\n"
    warning = capsys.readouterr().err
    assert "injected pinned output close failure" in warning


def test_builder_exposes_no_race_hook_or_destructive_cleanup_api(tmp_path: Path) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)

    assert "race_hook" not in inspect.signature(builder.build).parameters
    assert not hasattr(builder, "_call_hook")

    tree = ast.parse(Path(builder.__file__).read_text())
    destructive_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"unlink", "rmdir", "rmtree"}
    }
    assert destructive_calls == set()


def _forbid_builder_deletion(
    builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_delete(*arguments: object, **keywords: object) -> None:
        del arguments, keywords
        raise AssertionError("builder invoked destructive cleanup")

    monkeypatch.setattr(builder.os, "unlink", forbidden_delete)
    monkeypatch.setattr(builder.os, "rmdir", forbidden_delete)
    monkeypatch.setattr(shutil, "rmtree", forbidden_delete)


def test_existing_output_success_retains_exact_private_rollback_without_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    _forbid_builder_deletion(builder, monkeypatch)
    commit_prefix = _git(repository, "rev-parse", "HEAD").stdout.strip()[:12]

    result = builder.build(requested_output=output, allow_dirty=False)

    assert result.output == output
    assert result.rollback is not None
    assert result.recovery is None
    rollback = result.rollback
    assert rollback.parent == output.parent
    assert rollback.name.startswith(f".failure-memory.previous-{commit_prefix}-")
    assert marker == output / marker.name
    assert not marker.exists()
    assert (rollback / marker.name).read_text() == "preserve existing output\n"
    assert (output / ".codex-plugin" / "plugin.json").is_file()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(rollback.stat().st_mode) == 0o700
    assert output.stat().st_uid == os.getuid()
    assert rollback.stat().st_uid == os.getuid()


def test_initially_absent_success_uses_no_deletion_and_leaves_no_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "private-publish" / "failure-memory"
    builder = _load_builder(repository)
    _forbid_builder_deletion(builder, monkeypatch)

    result = builder.build(requested_output=output, allow_dirty=False)

    assert result == builder.BuildResult(output=output, rollback=None, recovery=None)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert not list(output.parent.glob(".failure-memory.previous-*"))


def test_prepublication_failure_retains_exact_private_recovery_without_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "private-publish" / "failure-memory"
    builder = _load_builder(repository)
    _forbid_builder_deletion(builder, monkeypatch)

    def fail_source_recheck(*arguments: object, **keywords: object) -> None:
        del arguments, keywords
        raise builder.BuildError("injected prepublication failure")

    monkeypatch.setattr(builder, "_verify_source_state", fail_source_recheck)

    with pytest.raises(builder.BuildError, match="injected prepublication failure") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert not output.exists()
    assert len(caught.value.recovery_paths) == 1
    recovery = caught.value.recovery_paths[0]
    assert recovery.parent == output.parent
    assert recovery.name.startswith(".failure-memory.previous-")
    assert recovery.is_dir()
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o700
    assert (recovery / ".codex-plugin" / "plugin.json").is_file()


def test_unresolved_rollback_blocks_repeated_build_with_exact_instruction(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, _marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    first = builder.build(requested_output=output, allow_dirty=False)
    assert first.rollback is not None

    with pytest.raises(builder.BuildError, match=r"inspect.*Trash") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert first.rollback in caught.value.recovery_paths
    assert str(first.rollback) in str(caught.value)
    assert (output / ".codex-plugin" / "plugin.json").is_file()


def test_unresolved_artifact_reports_actual_path_after_held_parent_relocation(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent_path = tmp_path / "private-publish"
    parent_path.mkdir(mode=0o700)
    relocation_root = tmp_path / "relocated"
    relocation_root.mkdir(mode=0o700)
    relocated_parent = relocation_root / "publish-displaced"
    builder = _load_builder(repository)
    parent = builder._open_parent(parent_path)
    artifact_name = ".failure-memory.previous-old"
    os.mkdir(artifact_name, 0o700, dir_fd=parent.fd)
    marker = parent_path / artifact_name / "marker.txt"
    marker.write_text("preserve retained artifact\n")
    parent_path.rename(relocated_parent)

    try:
        with pytest.raises(builder.BuildError, match=r"inspect.*Trash") as caught:
            builder._assert_no_unresolved_artifacts(parent, "failure-memory")
    finally:
        os.close(parent.fd)

    actual = relocated_parent / artifact_name
    assert caught.value.recovery_paths == (actual,)
    assert str(actual) in str(caught.value)
    assert actual.joinpath("marker.txt").read_text() == "preserve retained artifact\n"
    assert not (parent_path / artifact_name).exists()


def test_existing_output_interrupt_after_real_exchange_restores_prior_and_retains_generated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    real_exchange = builder._atomic_exchange
    calls = 0

    def exchange_then_interrupt(parent_fd: int, left: str, right: str) -> None:
        nonlocal calls
        calls += 1
        real_exchange(parent_fd, left, right)
        if calls == 1:
            raise KeyboardInterrupt("injected after real publication exchange")

    monkeypatch.setattr(builder, "_atomic_exchange", exchange_then_interrupt)

    with pytest.raises(builder.BuildError, match="retained recovery") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert calls == 2
    assert marker.read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    recovery = caught.value.recovery_paths[0]
    assert recovery.parent == output.parent
    assert (recovery / ".codex-plugin" / "plugin.json").is_file()
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o700


def test_absent_output_interrupt_after_real_noreplace_restores_absence_and_retains_generated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "private-publish" / "failure-memory"
    builder = _load_builder(repository)
    real_noreplace = builder._atomic_rename_noreplace
    calls = 0

    def rename_then_interrupt(parent_fd: int, source: str, target: str) -> None:
        nonlocal calls
        calls += 1
        real_noreplace(parent_fd, source, target)
        if calls == 1:
            raise KeyboardInterrupt("injected after real no-replace publication")

    monkeypatch.setattr(builder, "_atomic_rename_noreplace", rename_then_interrupt)

    with pytest.raises(builder.BuildError, match="retained recovery") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert calls == 2
    assert not output.exists()
    assert len(caught.value.recovery_paths) == 1
    recovery = caught.value.recovery_paths[0]
    assert recovery.parent == output.parent
    assert (recovery / ".codex-plugin" / "plugin.json").is_file()


def test_interrupt_after_real_rollback_exchange_is_reconciled_from_held_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    real_exchange = builder._atomic_exchange
    calls = 0

    def always_interrupt_after_exchange(parent_fd: int, left: str, right: str) -> None:
        nonlocal calls
        calls += 1
        real_exchange(parent_fd, left, right)
        raise KeyboardInterrupt(f"injected after real exchange {calls}")

    monkeypatch.setattr(builder, "_atomic_exchange", always_interrupt_after_exchange)

    with pytest.raises(builder.BuildError, match="retained recovery") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert calls == 2
    assert marker.read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    recovery = caught.value.recovery_paths[0]
    assert (recovery / ".codex-plugin" / "plugin.json").is_file()


def test_repeated_repair_failure_reports_actual_identity_locations_and_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    real_exchange = builder._atomic_exchange
    calls = 0

    def publish_then_fail_repairs(parent_fd: int, left: str, right: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_exchange(parent_fd, left, right)
            raise KeyboardInterrupt("injected after real publication exchange")
        raise builder.BuildError("injected repair failure")

    monkeypatch.setattr(builder, "_atomic_exchange", publish_then_fail_repairs)

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert calls == 3
    error = caught.value
    rollback_candidates = tuple(
        path for path in error.recovery_paths if path.name.startswith(".failure-memory.previous-")
    )
    assert len(rollback_candidates) == 1
    rollback = rollback_candidates[0]
    assert rollback.parent == output.parent
    assert output in error.recovery_paths
    assert (output / ".codex-plugin" / "plugin.json").is_file()
    assert (rollback / marker.name).read_text() == "preserve existing output\n"
    assert f"parent={str(output.parent)!r}" in str(error)
    assert "device=" in str(error)
    assert "inode=" in str(error)
    assert str(output) in str(error)
    assert str(rollback) in str(error)
    assert len(error.recovery_paths) == len(set(error.recovery_paths)) == 2


def test_existing_output_generated_move_and_parent_relocation_report_actual_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    parent = output.parent
    relocation_root = tmp_path / "recovery paths Ω"
    relocation_root.mkdir()
    generated = relocation_root / "generated output Ω"
    relocated_parent = relocation_root / "held parent Ω"
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    injected = False

    def exchange_move_generated_and_parent(
        parent_fd: int,
        left_name: str,
        right_name: str,
    ) -> None:
        nonlocal injected
        atomic_exchange(parent_fd, left_name, right_name)
        if not injected:
            injected = True
            output.rename(generated)
            parent.rename(relocated_parent)
            raise KeyboardInterrupt("injected generated and parent relocation")

    monkeypatch.setattr(
        builder,
        "_atomic_exchange",
        exchange_move_generated_and_parent,
    )

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    prior = next(relocated_parent.glob(".failure-memory.previous-*"))
    assert generated in caught.value.recovery_paths
    assert prior in caught.value.recovery_paths
    assert (generated / ".codex-plugin" / "plugin.json").is_file()
    assert (prior / marker.name).read_text() == "preserve existing output\n"
    assert str(generated) in str(caught.value)
    assert str(relocated_parent) in str(caught.value)


def test_initially_absent_generated_move_reports_descriptor_actual_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "publish" / "failure-memory"
    relocation_root = tmp_path / "recovery paths Ω"
    relocation_root.mkdir()
    generated = relocation_root / "generated output Ω"
    builder = _load_builder(repository)
    atomic_noreplace = builder._atomic_rename_noreplace
    injected = False

    def publish_move_then_interrupt(
        parent_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal injected
        atomic_noreplace(parent_fd, source_name, target_name)
        if not injected:
            injected = True
            output.rename(generated)
            raise KeyboardInterrupt("injected initially-absent generated relocation")

    monkeypatch.setattr(
        builder,
        "_atomic_rename_noreplace",
        publish_move_then_interrupt,
    )

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert caught.value.recovery_paths == (generated,)
    assert (generated / ".codex-plugin" / "plugin.json").is_file()
    assert str(generated) in str(caught.value)
    assert not output.exists()


def test_existing_output_prior_move_reports_descriptor_actual_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    relocation_root = tmp_path / "recovery paths Ω"
    relocation_root.mkdir()
    prior = relocation_root / "prior output Ω"
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    injected = False

    def exchange_move_prior_then_interrupt(
        parent_fd: int,
        left_name: str,
        right_name: str,
    ) -> None:
        nonlocal injected
        atomic_exchange(parent_fd, left_name, right_name)
        if not injected:
            injected = True
            (output.parent / left_name).rename(prior)
            raise KeyboardInterrupt("injected prior-output relocation")

    monkeypatch.setattr(
        builder,
        "_atomic_exchange",
        exchange_move_prior_then_interrupt,
    )

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert output in caught.value.recovery_paths
    assert prior in caught.value.recovery_paths
    assert (output / ".codex-plugin" / "plugin.json").is_file()
    assert (prior / marker.name).read_text() == "preserve existing output\n"
    assert str(prior) in str(caught.value)


def test_generated_move_after_real_rollback_reports_descriptor_actual_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    relocation_root = tmp_path / "rollback recovery Ω"
    relocation_root.mkdir()
    generated = relocation_root / "generated after rollback Ω"
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    calls = 0

    def exchange_interrupt_and_move_after_rollback(
        parent_fd: int,
        left_name: str,
        right_name: str,
    ) -> None:
        nonlocal calls
        calls += 1
        atomic_exchange(parent_fd, left_name, right_name)
        if calls == 1:
            raise KeyboardInterrupt("injected after publication exchange")
        if calls == 2:
            (output.parent / left_name).rename(generated)
            raise KeyboardInterrupt("injected generated move after real rollback")

    monkeypatch.setattr(
        builder,
        "_atomic_exchange",
        exchange_interrupt_and_move_after_rollback,
    )

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert calls == 2
    assert generated in caught.value.recovery_paths
    assert output in caught.value.recovery_paths
    assert (generated / ".codex-plugin" / "plugin.json").is_file()
    assert (output / marker.name).read_text() == "preserve existing output\n"
    assert str(generated) in str(caught.value)


def test_simulated_linux_descriptor_path_reports_cross_parent_generated_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "publish" / "failure-memory"
    relocation_root = tmp_path / "linux recovery Ω"
    relocation_root.mkdir()
    generated = relocation_root / "generated output Ω"
    builder = _load_builder(repository)
    atomic_noreplace = builder._atomic_rename_noreplace
    injected = False

    def descriptor_link(path: str) -> str:
        assert path.startswith("/proc/self/fd/")
        return str(generated)

    def publish_move_then_switch_platform(
        parent_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal injected
        atomic_noreplace(parent_fd, source_name, target_name)
        if not injected:
            injected = True
            output.rename(generated)
            monkeypatch.setattr(builder.sys, "platform", "linux")
            raise KeyboardInterrupt("injected simulated Linux generated relocation")

    monkeypatch.setattr(builder.os, "readlink", descriptor_link)
    monkeypatch.setattr(
        builder,
        "_atomic_rename_noreplace",
        publish_move_then_switch_platform,
    )

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert caught.value.recovery_paths == (generated,)
    assert (generated / ".codex-plugin" / "plugin.json").is_file()


def test_stale_descriptor_path_is_rejected_with_linked_path_unresolved_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, _marker = _existing_output(tmp_path)
    relocation_root = tmp_path / "recovery paths"
    relocation_root.mkdir()
    generated = relocation_root / "actual generated"
    stale = relocation_root / "stale substitute"
    stale.mkdir()
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    injected = False

    def stale_descriptor_path(
        file_descriptor: int,
        command: int,
        buffer: bytes,
    ) -> bytes:
        del file_descriptor, command
        encoded = os.fsencode(stale)
        return encoded + b"\0" * (len(buffer) - len(encoded))

    def exchange_move_generated_then_interrupt(
        parent_fd: int,
        left_name: str,
        right_name: str,
    ) -> None:
        nonlocal injected
        atomic_exchange(parent_fd, left_name, right_name)
        if not injected:
            injected = True
            output.rename(generated)
            raise KeyboardInterrupt("injected with stale descriptor path")

    monkeypatch.setattr(builder.fcntl, "fcntl", stale_descriptor_path)
    monkeypatch.setattr(
        builder,
        "_atomic_exchange",
        exchange_move_generated_then_interrupt,
    )

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert stale not in caught.value.recovery_paths
    assert generated not in caught.value.recovery_paths
    assert "generated_state='linked-path-unresolved'" in str(caught.value)
    assert "generated_identity=device=" in str(caught.value)


def test_nonexistent_stale_descriptor_path_does_not_mislabel_linked_inode_unlinked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, _marker = _existing_output(tmp_path)
    relocation_root = tmp_path / "recovery paths"
    relocation_root.mkdir()
    generated = relocation_root / "actual generated"
    stale = relocation_root / "nonexistent stale name"
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    injected = False

    def nonexistent_stale_descriptor_path(
        file_descriptor: int,
        command: int,
        buffer: bytes,
    ) -> bytes:
        del file_descriptor, command
        encoded = os.fsencode(stale)
        return encoded + b"\0" * (len(buffer) - len(encoded))

    def exchange_move_generated_then_interrupt(
        parent_fd: int,
        left_name: str,
        right_name: str,
    ) -> None:
        nonlocal injected
        atomic_exchange(parent_fd, left_name, right_name)
        if not injected:
            injected = True
            output.rename(generated)
            raise KeyboardInterrupt("injected with nonexistent stale descriptor path")

    monkeypatch.setattr(
        builder.fcntl,
        "fcntl",
        nonexistent_stale_descriptor_path,
    )
    monkeypatch.setattr(
        builder,
        "_atomic_exchange",
        exchange_move_generated_then_interrupt,
    )

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert stale not in caught.value.recovery_paths
    assert generated not in caught.value.recovery_paths
    assert generated.is_dir()
    assert "generated_state='linked-path-unresolved'" in str(caught.value)
    assert "generated_state='unlinked'" not in str(caught.value)


def test_cross_filesystem_style_copy_is_not_adopted_for_unlinked_generated_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, _marker = _existing_output(tmp_path)
    substitute = tmp_path / "copied cross-filesystem substitute"
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    injected = False

    def exchange_copy_unlink_then_interrupt(
        parent_fd: int,
        left_name: str,
        right_name: str,
    ) -> None:
        nonlocal injected
        atomic_exchange(parent_fd, left_name, right_name)
        if not injected:
            injected = True
            shutil.copytree(output, substitute)
            shutil.rmtree(output)
            raise KeyboardInterrupt("injected cross-filesystem-style substitution")

    monkeypatch.setattr(
        builder,
        "_atomic_exchange",
        exchange_copy_unlink_then_interrupt,
    )

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert substitute not in caught.value.recovery_paths
    assert (substitute / ".codex-plugin" / "plugin.json").is_file()
    assert "generated_state='unlinked'" in str(caught.value)
    assert "generated_identity=device=" in str(caught.value)


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/.env",
        "scripts/.env.example",
        "scripts/.envrc",
        "scripts/.env-test",
        "scripts/.ENV-PRODUCTION",
        "scripts/private.pem",
        "scripts/private.ppk",
        "scripts/keyring.kdbx",
        "scripts/store.jks",
        "scripts/store.keystore",
        "scripts/armored-private-key.asc",
        "scripts/pubring.kbx",
        "scripts/secring.gpg",
        "scripts/certificate.crt",
        "scripts/runtime.db",
        "scripts/secret/token.txt",
        "scripts/secrets/token.txt",
        "scripts/private/token.txt",
        "scripts/privates/token.txt",
        "scripts/credential/token.txt",
        "scripts/credentials/token.txt",
        "scripts/key/token.txt",
        "scripts/KEYS/token.txt",
        "scripts/cert/token.txt",
        "scripts/certs/token.txt",
        "scripts/certificate/token.txt",
        "scripts/CERTIFICATES/token.txt",
        "scripts/.ssh/id_example",
        "scripts/.GnUpG/private-keys-v1.d/key",
        "scripts/.AWS/credentials",
        "scripts/.azure/accessTokens.json",
        "scripts/.kube/config",
        "scripts/.docker/config.json",
        "skills/record-agent-failure/out/generated.txt",
        "src/failure_memory/build/generated.py",
    ],
)
def test_tracked_sensitive_or_runtime_paths_fail_closed_without_replacing_output(
    tmp_path: Path, relative: str
) -> None:
    repository = _fixture_repository(tmp_path)
    suspicious = repository / relative
    suspicious.parent.mkdir(parents=True, exist_ok=True)
    secret_value = "sentinel-sensitive-content-must-not-leak"
    suspicious.write_text(f"{secret_value}\n")
    _git(repository, "add", "-f", relative)
    _git(repository, "commit", "-m", f"add forbidden {relative}")
    output, marker = _existing_output(tmp_path)

    completed = _run_builder(repository, output)

    assert completed.returncode != 0
    assert "forbidden package source" in completed.stderr.lower()
    assert relative in completed.stderr
    assert secret_value not in completed.stdout
    assert secret_value not in completed.stderr
    assert marker.read_text() == "preserve existing output\n"
    assert {path.name for path in output.iterdir()} == {"existing-marker.txt"}
    _assert_no_internal_artifacts(output.parent)


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/.envrc",
        "scripts/.env-test",
        "scripts/PRIVATE.PPK",
        "scripts/PRIVATE.ASC",
        "scripts/PUBRING.KBX",
        "scripts/secret/token.txt",
        "scripts/PRIVATES/token.txt",
        "scripts/KEYS/token.txt",
        "scripts/Certificate/token.txt",
        "scripts/.SSH/id_example",
        "scripts/.AWS/credentials",
    ],
)
def test_allow_dirty_rejects_sensitive_tracked_inputs_without_leaking_contents(
    tmp_path: Path, relative: str
) -> None:
    repository = _fixture_repository(tmp_path)
    suspicious = repository / relative
    suspicious.parent.mkdir(parents=True, exist_ok=True)
    suspicious.write_text("committed-sensitive-content\n")
    _git(repository, "add", "-f", relative)
    _git(repository, "commit", "-m", f"add sensitive input {relative}")
    secret_value = "updated-sensitive-content-must-not-leak"
    suspicious.write_text(f"{secret_value}\n")
    output, marker = _existing_output(tmp_path)

    completed = _run_builder(repository, output, allow_dirty=True)

    assert completed.returncode != 0
    assert "forbidden package source" in completed.stderr.lower()
    assert relative in completed.stderr
    assert secret_value not in completed.stdout
    assert secret_value not in completed.stderr
    assert marker.read_text() == "preserve existing output\n"
    _assert_no_internal_artifacts(output.parent)


@pytest.mark.parametrize("basename", SENSITIVE_STORE_BASENAMES)
def test_clean_build_rejects_sensitive_store_terminal_basenames(
    tmp_path: Path,
    basename: str,
) -> None:
    repository = _fixture_repository(tmp_path)
    relative = f"scripts/{basename}"
    suspicious = repository / relative
    suspicious.write_text("terminal-sensitive-store\n")
    _git(repository, "add", "-f", relative)
    _git(repository, "commit", "-m", f"add terminal sensitive store {basename}")
    output, marker = _existing_output(tmp_path)

    completed = _run_builder(repository, output)

    assert completed.returncode != 0
    assert "forbidden package source" in completed.stderr.lower()
    assert relative in completed.stderr
    assert marker.read_text() == "preserve existing output\n"
    _assert_no_internal_artifacts(output.parent)


@pytest.mark.parametrize("basename", SENSITIVE_STORE_BASENAMES)
def test_allow_dirty_rejects_sensitive_store_terminal_basenames(
    tmp_path: Path,
    basename: str,
) -> None:
    repository = _fixture_repository(tmp_path)
    rendered_basename = basename.upper()
    relative = f"scripts/{rendered_basename}"
    suspicious = repository / relative
    suspicious.write_text("committed-terminal-sensitive-store\n")
    _git(repository, "add", "-f", relative)
    _git(repository, "commit", "-m", f"add terminal sensitive store {rendered_basename}")
    suspicious.write_text("dirty-terminal-sensitive-store\n")
    output, marker = _existing_output(tmp_path)

    completed = _run_builder(repository, output, allow_dirty=True)

    assert completed.returncode != 0
    assert "forbidden package source" in completed.stderr.lower()
    assert relative in completed.stderr
    assert marker.read_text() == "preserve existing output\n"
    _assert_no_internal_artifacts(output.parent)


@pytest.mark.parametrize("allow_dirty", [False, True])
def test_sensitive_path_policy_does_not_reject_similarly_named_safe_inputs(
    tmp_path: Path, allow_dirty: bool
) -> None:
    repository = _fixture_repository(tmp_path)
    safe_relatives = [
        "scripts/environment.py",
        "scripts/certificate_utils.py",
        "scripts/monkey/handler.py",
        "scripts/privateer/notes.txt",
        "scripts/secretary/notes.txt",
        "scripts/keynote/notes.txt",
        "scripts/certifiable/notes.txt",
        "scripts/awsome/notes.txt",
    ]
    for relative in safe_relatives:
        safe_input = repository / relative
        safe_input.parent.mkdir(parents=True, exist_ok=True)
        safe_input.write_text(f"safe input: {relative}\n")
    _git(repository, "add", *safe_relatives)
    _git(repository, "commit", "-m", "add safe policy controls")
    output = tmp_path / "publish" / "failure-memory"

    completed = _run_builder(repository, output, allow_dirty=allow_dirty)

    assert completed.returncode == 0, completed.stderr
    for relative in safe_relatives:
        assert (output / relative).is_file()


def test_clean_build_reads_commit_blobs_even_if_worktree_changes_after_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "publish" / "failure-memory"
    committed_readme = _git(repository, "show", "HEAD:README.md").stdout
    builder = _load_builder(repository)
    write_file = builder._write_file_at
    injected = False

    def write_while_worktree_differs(
        root_fd: int,
        relative: object,
        data: bytes,
        mode: int,
    ) -> None:
        nonlocal injected
        if injected:
            write_file(root_fd, relative, data, mode)
            return
        injected = True
        readme = repository / "README.md"
        original = readme.read_bytes()
        readme.write_text("raced live worktree bytes\n")
        try:
            write_file(root_fd, relative, data, mode)
        finally:
            readme.write_bytes(original)

    monkeypatch.setattr(builder, "_write_file_at", write_while_worktree_differs)

    result = builder.build(requested_output=output, allow_dirty=False)

    assert result.output == output
    assert injected
    assert (output / "README.md").read_text() == committed_readme
    manifest = json.loads((output / "build-manifest.json").read_text())
    assert manifest["commit"] == _git(repository, "rev-parse", "HEAD").stdout.strip()
    assert manifest["dirty"] is False


def test_clean_build_aborts_if_head_changes_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    write_file = builder._write_file_at
    injected = False

    def commit_before_first_stage_write(
        root_fd: int,
        relative: object,
        data: bytes,
        mode: int,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            (repository / "README.md").write_text("new committed head\n")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "-m", "race head")
        write_file(root_fd, relative, data, mode)

    monkeypatch.setattr(builder, "_write_file_at", commit_before_first_stage_write)

    with pytest.raises(builder.BuildError, match="HEAD changed") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert marker.read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    assert caught.value.recovery_paths[0].is_dir()


def test_allow_dirty_aborts_when_a_live_input_changes_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    (repository / "README.md").write_text("first dirty live value\n")
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    write_file = builder._write_file_at
    injected = False

    def mutate_live_input_before_first_stage_write(
        root_fd: int,
        relative: object,
        data: bytes,
        mode: int,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            (repository / "README.md").write_text("second dirty live value with a different size\n")
        write_file(root_fd, relative, data, mode)

    monkeypatch.setattr(
        builder,
        "_write_file_at",
        mutate_live_input_before_first_stage_write,
    )

    with pytest.raises(builder.BuildError, match="changed during live snapshot") as caught:
        builder.build(requested_output=output, allow_dirty=True)

    assert injected
    assert marker.read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    assert caught.value.recovery_paths[0].is_dir()


def test_allow_dirty_marks_even_a_clean_live_snapshot_non_commit_pinned(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "publish" / "failure-memory"

    completed = _run_builder(repository, output, allow_dirty=True)

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output / "build-manifest.json").read_text())
    assert manifest["dirty"] is True


def test_clean_build_ignores_unsafe_live_chmod_and_normalizes_bundle_modes(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    _git(repository, "config", "core.fileMode", "false")
    os.chmod(repository / "README.md", 0o777)
    assert _git(repository, "status", "--porcelain").stdout == ""
    output = tmp_path / "publish" / "failure-memory"

    completed = _run_builder(repository, output)

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output / "build-manifest.json").read_text())
    assert manifest["files"]["README.md"]["mode"] == "0644"
    assert stat.S_IMODE((output / "README.md").stat().st_mode) == 0o644
    assert stat.S_IMODE((output / "scripts/failure_memory_mcp.py").stat().st_mode) == 0o755
    for path in output.rglob("*"):
        expected = 0o755 if path.is_dir() else 0o644
        if path.relative_to(output).as_posix() == "scripts/failure_memory_mcp.py":
            expected = 0o755
        assert stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) == expected
        assert path.stat(follow_symlinks=False).st_uid == os.getuid()


def test_allow_dirty_rejects_group_or_world_writable_live_inputs(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    os.chmod(repository / "README.md", 0o666)
    output, marker = _existing_output(tmp_path)

    completed = _run_builder(repository, output, allow_dirty=True)

    assert completed.returncode != 0
    assert "group/world-writable" in completed.stderr.lower()
    assert marker.read_text() == "preserve existing output\n"
    _assert_no_internal_artifacts(output.parent)


def test_allow_dirty_rejects_group_or_world_writable_source_directories(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    os.chmod(repository / "skills" / "record-agent-failure", 0o777)
    output, marker = _existing_output(tmp_path)

    completed = _run_builder(repository, output, allow_dirty=True)

    assert completed.returncode != 0
    assert "group/world-writable" in completed.stderr.lower()
    assert marker.read_text() == "preserve existing output\n"
    _assert_no_internal_artifacts(output.parent)


# The Round-4 threat model intentionally excludes hostile same-UID mutation of the
# owner-only transaction tree. Its old stage-substitution and recursive-cleanup
# tests were retired: the builder now retains artifacts and never deletes them.
def test_conflicting_reserved_artifact_blocks_build_with_exact_trash_instruction(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    collision = output.parent / ".failure-memory.stage-fixed"
    collision.write_text("unrelated collision\n")

    with pytest.raises(builder.BuildError, match=r"inspection.*Trash") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert caught.value.recovery_paths == (collision,)
    assert str(collision) in str(caught.value)
    assert collision.read_text() == "unrelated collision\n"
    assert marker.read_text() == "preserve existing output\n"
    assert {path.name for path in output.iterdir()} == {"existing-marker.txt"}


def test_swapped_output_parent_is_detected_without_touching_victim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    parent = output.parent
    displaced_parent = tmp_path / "publish-original"
    victim = tmp_path / "victim"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("untouched\n")
    builder = _load_builder(repository)
    open_parent = builder._open_parent

    def open_then_swap_parent(path: Path) -> object:
        opened = open_parent(path)
        parent.rename(displaced_parent)
        parent.symlink_to(victim, target_is_directory=True)
        return opened

    monkeypatch.setattr(builder, "_open_parent", open_then_swap_parent)
    with pytest.raises(builder.BuildError, match="output parent changed"):
        builder.build(requested_output=output, allow_dirty=False)

    assert victim_marker.read_text() == "untouched\n"
    assert not (victim / "failure-memory").exists()
    assert (displaced_parent / "failure-memory" / marker.name).read_text() == (
        "preserve existing output\n"
    )
    _assert_no_internal_artifacts(displaced_parent)


def test_late_parent_swap_after_publication_rolls_back_through_held_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    parent = output.parent
    displaced_parent = tmp_path / "publish-original"
    victim = tmp_path / "victim"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("untouched\n")
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    injected = False

    def exchange_then_swap_parent(parent_fd: int, left: str, right: str) -> None:
        nonlocal injected
        atomic_exchange(parent_fd, left, right)
        if not injected:
            injected = True
            parent.rename(displaced_parent)
            parent.symlink_to(victim, target_is_directory=True)

    monkeypatch.setattr(builder, "_atomic_exchange", exchange_then_swap_parent)
    with pytest.raises(builder.BuildError, match="output parent changed"):
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert victim_marker.read_text() == "untouched\n"
    assert not (victim / "failure-memory").exists()
    restored = displaced_parent / "failure-memory" / marker.name
    assert restored.read_text() == "preserve existing output\n"
    recoveries = list(displaced_parent.glob(".failure-memory.previous-*"))
    assert len(recoveries) == 1
    assert (recoveries[0] / ".codex-plugin" / "plugin.json").is_file()


def test_existing_output_is_present_immediately_after_atomic_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, _marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    observed = False

    def exchange_then_observe(parent_fd: int, left: str, right: str) -> None:
        nonlocal observed
        atomic_exchange(parent_fd, left, right)
        observed = True
        assert output.is_dir()
        assert (output / ".codex-plugin" / "plugin.json").is_file()

    monkeypatch.setattr(builder, "_atomic_exchange", exchange_then_observe)
    result = builder.build(requested_output=output, allow_dirty=False)

    assert result.output == output
    assert result.rollback is not None
    assert observed
    assert (output / ".codex-plugin" / "plugin.json").is_file()


def test_existing_output_replacement_fails_closed_when_exchange_is_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)

    def unsupported_exchange(parent_fd: int, left_name: str, right_name: str) -> None:
        del parent_fd, left_name, right_name
        raise builder.BuildError("atomic directory exchange is unavailable")

    monkeypatch.setattr(builder, "_atomic_exchange", unsupported_exchange, raising=False)

    with pytest.raises(
        builder.BuildError,
        match="atomic directory exchange is unavailable",
    ) as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert marker.read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    assert (caught.value.recovery_paths[0] / ".codex-plugin" / "plugin.json").is_file()


def test_existing_output_replacement_fails_closed_when_exchange_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)

    def failed_exchange(parent_fd: int, left_name: str, right_name: str) -> None:
        del parent_fd, left_name, right_name
        raise builder.BuildError("atomic directory exchange failed")

    monkeypatch.setattr(builder, "_atomic_exchange", failed_exchange)

    with pytest.raises(builder.BuildError, match="atomic directory exchange failed") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert marker.read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    assert (caught.value.recovery_paths[0] / ".codex-plugin" / "plugin.json").is_file()


@pytest.mark.parametrize(
    ("platform", "symbol"),
    [("darwin", "renameatx_np"), ("linux", "renameat2")],
)
def test_atomic_exchange_ctypes_adapter_dispatches_the_platform_abi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    symbol: str,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)
    calls: list[tuple[object, ...]] = []

    class FakeExchange:
        argtypes: object = None
        restype: object = None

        def __call__(self, *arguments: object) -> int:
            calls.append(arguments)
            return 0

    class FakeLibrary:
        pass

    fake_exchange = FakeExchange()
    fake_library = FakeLibrary()
    setattr(fake_library, symbol, fake_exchange)
    monkeypatch.setattr(builder.sys, "platform", platform)
    monkeypatch.setattr(
        builder.ctypes,
        "CDLL",
        lambda name, *, use_errno: fake_library,
    )

    builder._atomic_exchange(41, "staged-entry", "output-entry")

    assert calls == [(41, b"staged-entry", 41, b"output-entry", 0x00000002)]
    assert fake_exchange.argtypes == [
        builder.ctypes.c_int,
        builder.ctypes.c_char_p,
        builder.ctypes.c_int,
        builder.ctypes.c_char_p,
        builder.ctypes.c_uint,
    ]
    assert fake_exchange.restype is builder.ctypes.c_int


@pytest.mark.parametrize(
    ("error_number", "message"),
    [(errno.ENOSYS, "unavailable"), (errno.EIO, "failed")],
)
def test_atomic_exchange_ctypes_adapter_maps_errno_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    message: str,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)

    class FailedExchange:
        argtypes: object = None
        restype: object = None

        def __call__(self, *arguments: object) -> int:
            del arguments
            return -1

    class FakeLibrary:
        renameat2 = FailedExchange()

    monkeypatch.setattr(builder.sys, "platform", "linux")
    monkeypatch.setattr(
        builder.ctypes,
        "CDLL",
        lambda name, *, use_errno: FakeLibrary(),
    )
    monkeypatch.setattr(builder.ctypes, "get_errno", lambda: error_number)

    with pytest.raises(builder.BuildError, match=message):
        builder._atomic_exchange(41, "staged-entry", "output-entry")


@pytest.mark.parametrize(
    ("platform", "symbol", "exclusive_flag"),
    [
        ("darwin", "renameatx_np", 0x00000004),
        ("linux", "renameat2", 0x00000001),
    ],
)
def test_atomic_noreplace_ctypes_adapter_dispatches_the_platform_abi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    symbol: str,
    exclusive_flag: int,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)
    calls: list[tuple[object, ...]] = []

    class FakeRename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *arguments: object) -> int:
            calls.append(arguments)
            return 0

    class FakeLibrary:
        pass

    fake_rename = FakeRename()
    fake_library = FakeLibrary()
    setattr(fake_library, symbol, fake_rename)
    monkeypatch.setattr(builder.sys, "platform", platform)
    monkeypatch.setattr(
        builder.ctypes,
        "CDLL",
        lambda name, *, use_errno: fake_library,
    )

    builder._atomic_rename_noreplace(41, "source-entry", "recovery-entry")

    assert calls == [(41, b"source-entry", 41, b"recovery-entry", exclusive_flag)]
    assert fake_rename.argtypes == [
        builder.ctypes.c_int,
        builder.ctypes.c_char_p,
        builder.ctypes.c_int,
        builder.ctypes.c_char_p,
        builder.ctypes.c_uint,
    ]
    assert fake_rename.restype is builder.ctypes.c_int


@pytest.mark.parametrize(
    ("error_number", "message"),
    [
        (errno.EEXIST, "target already exists"),
        (errno.ENOSYS, "unavailable"),
        (errno.EIO, "failed"),
    ],
)
def test_atomic_noreplace_ctypes_adapter_maps_errno_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    message: str,
) -> None:
    repository = _fixture_repository(tmp_path)
    builder = _load_builder(repository)

    class FailedRename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *arguments: object) -> int:
            del arguments
            return -1

    class FakeLibrary:
        renameat2 = FailedRename()

    monkeypatch.setattr(builder.sys, "platform", "linux")
    monkeypatch.setattr(
        builder.ctypes,
        "CDLL",
        lambda name, *, use_errno: FakeLibrary(),
    )
    monkeypatch.setattr(builder.ctypes, "get_errno", lambda: error_number)

    with pytest.raises(builder.BuildError, match=message):
        builder._atomic_rename_noreplace(41, "source-entry", "recovery-entry")


def test_existing_output_replacement_rolls_back_with_a_second_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    exchange_calls = 0

    atomic_exchange = builder._atomic_exchange

    def exchange_then_fail_once(parent_fd: int, left_name: str, right_name: str) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        atomic_exchange(parent_fd, left_name, right_name)
        if exchange_calls == 1:
            raise RuntimeError("stop after publication exchange")

    monkeypatch.setattr(builder, "_atomic_exchange", exchange_then_fail_once)

    with pytest.raises(builder.BuildError, match="retained recovery") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert exchange_calls == 2
    assert marker.read_text() == "preserve existing output\n"
    assert {path.name for path in output.iterdir()} == {"existing-marker.txt"}
    assert len(caught.value.recovery_paths) == 1
    assert (caught.value.recovery_paths[0] / ".codex-plugin" / "plugin.json").is_file()


def test_existing_output_substitution_during_staging_is_never_adopted_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    parent = output.parent
    displaced_prior = parent / "displaced-prior-output"
    victim = parent / "victim-entry"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("never delete this directory\n")
    builder = _load_builder(repository)
    write_file = builder._write_file_at
    injected = False

    def swap_output_on_first_stage_write(
        root_fd: int,
        relative: object,
        data: bytes,
        mode: int,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            output.rename(displaced_prior)
            victim.rename(output)
        write_file(root_fd, relative, data, mode)

    monkeypatch.setattr(builder, "_write_file_at", swap_output_on_first_stage_write)

    with pytest.raises(
        builder.BuildError,
        match="publication entry identity changed",
    ) as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert (output / victim_marker.name).read_text() == "never delete this directory\n"
    assert not (output / ".codex-plugin").exists()
    assert (displaced_prior / marker.name).read_text() == "preserve existing output\n"
    assert len(caught.value.recovery_paths) == 1
    assert (caught.value.recovery_paths[0] / ".codex-plugin" / "plugin.json").is_file()


def test_existing_output_substitution_after_initial_stat_is_never_adopted_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    parent = output.parent
    displaced_prior = parent / "displaced-prior-output"
    victim = parent / "victim-entry"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("never delete this directory\n")
    builder = _load_builder(repository)
    lstat_at = builder._lstat_at
    injected = False

    def swap_after_output_stat(directory_fd: int, name: str) -> object:
        nonlocal injected
        metadata = lstat_at(directory_fd, name)
        if name == output.name and metadata is not None and not injected:
            injected = True
            output.rename(displaced_prior)
            victim.rename(output)
        return metadata

    monkeypatch.setattr(builder, "_lstat_at", swap_after_output_stat)
    with pytest.raises(builder.BuildError, match="initial output identity changed"):
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert (output / victim_marker.name).read_text() == "never delete this directory\n"
    assert not (output / ".codex-plugin").exists()
    assert (displaced_prior / marker.name).read_text() == "preserve existing output\n"
    _assert_no_internal_artifacts(parent)


def test_initially_absent_output_created_during_staging_is_never_adopted_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "publish"
    parent.mkdir()
    output = parent / "failure-memory"
    victim = parent / "victim-entry"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("never delete this directory\n")
    builder = _load_builder(repository)
    write_file = builder._write_file_at
    injected = False

    def create_output_on_first_stage_write(
        root_fd: int,
        relative: object,
        data: bytes,
        mode: int,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            victim.rename(output)
        write_file(root_fd, relative, data, mode)

    monkeypatch.setattr(builder, "_write_file_at", create_output_on_first_stage_write)

    with pytest.raises(builder.BuildError, match="expected absent prior output") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert (output / victim_marker.name).read_text() == "never delete this directory\n"
    assert not (output / ".codex-plugin").exists()
    assert len(caught.value.recovery_paths) == 1
    assert (caught.value.recovery_paths[0] / ".codex-plugin" / "plugin.json").is_file()


def test_initially_absent_output_created_after_initial_stat_is_never_adopted_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "publish"
    parent.mkdir()
    output = parent / "failure-memory"
    victim = parent / "victim-entry"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("never delete this directory\n")
    builder = _load_builder(repository)
    lstat_at = builder._lstat_at
    injected = False

    def create_output_after_absent_probe(directory_fd: int, name: str) -> object:
        nonlocal injected
        metadata = lstat_at(directory_fd, name)
        if name == output.name and metadata is None and not injected:
            injected = True
            victim.rename(output)
        return metadata

    monkeypatch.setattr(builder, "_lstat_at", create_output_after_absent_probe)

    with pytest.raises(builder.BuildError, match="expected absent prior output") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert (output / victim_marker.name).read_text() == "never delete this directory\n"
    assert not (output / ".codex-plugin").exists()
    assert len(caught.value.recovery_paths) == 1


def test_same_parent_output_substitution_before_rollback_is_never_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    parent = output.parent
    victim = parent / "victim-entry"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("never delete this directory\n")
    displaced_new_output = parent / "displaced-new-output"
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    injected = False

    def publish_then_substitute_output(parent_fd: int, left: str, right: str) -> None:
        nonlocal injected
        atomic_exchange(parent_fd, left, right)
        if not injected:
            injected = True
            output.rename(displaced_new_output)
            victim.rename(output)
            raise KeyboardInterrupt("force rollback after output substitution")

    monkeypatch.setattr(builder, "_atomic_exchange", publish_then_substitute_output)

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert (output / victim_marker.name).read_text() == "never delete this directory\n"
    assert (displaced_new_output / ".codex-plugin" / "plugin.json").is_file()
    prior_locations = [
        path for path in caught.value.recovery_paths if (path / marker.name).is_file()
    ]
    assert len(prior_locations) == 1
    assert str(displaced_new_output) in str(caught.value)


def test_rollback_exchange_failure_reports_the_recoverable_output_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, _marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    exchange_calls = 0
    atomic_exchange = builder._atomic_exchange

    def publish_then_fail_rollback(parent_fd: int, left_name: str, right_name: str) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            atomic_exchange(parent_fd, left_name, right_name)
            raise KeyboardInterrupt("force rollback after real publication")
        raise builder.BuildError("injected rollback exchange failure")

    monkeypatch.setattr(builder, "_atomic_exchange", publish_then_fail_rollback)

    with pytest.raises(
        builder.BuildError,
        match="rollback incomplete",
    ) as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert exchange_calls == 3
    assert (output / ".codex-plugin" / "plugin.json").is_file()
    stages = list(output.parent.glob(".failure-memory.previous-*"))
    assert len(stages) == 1
    assert (stages[0] / "existing-marker.txt").read_text() == "preserve existing output\n"
    diagnostic = str(caught.value)
    assert str(output.parent) in diagnostic
    assert str(output) in diagnostic
    assert str(stages[0]) in diagnostic
    assert "device=" in diagnostic
    assert "inode=" in diagnostic


def test_new_output_publication_rolls_back_when_final_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "publish" / "failure-memory"
    builder = _load_builder(repository)
    atomic_noreplace = builder._atomic_rename_noreplace
    calls = 0

    def publish_then_fail(parent_fd: int, source: str, target: str) -> None:
        nonlocal calls
        calls += 1
        atomic_noreplace(parent_fd, source, target)
        if calls == 1:
            assert output.is_dir()
            raise RuntimeError("stop after no-replace publication")

    monkeypatch.setattr(builder, "_atomic_rename_noreplace", publish_then_fail)

    with pytest.raises(builder.BuildError, match="retained recovery") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert calls == 2
    assert not output.exists()
    assert len(caught.value.recovery_paths) == 1
    assert (caught.value.recovery_paths[0] / ".codex-plugin" / "plugin.json").is_file()


def test_initially_absent_publication_never_replaces_syscall_window_victim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "publish"
    parent.mkdir()
    output = parent / "failure-memory"
    victim = parent / "victim-entry"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("preserve publication-window victim\n")
    builder = _load_builder(repository)
    atomic_noreplace = builder._atomic_rename_noreplace
    injected = False

    def inject_before_publication_syscall(
        parent_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal injected
        if target_name == output.name and not injected:
            injected = True
            victim.rename(output)
        atomic_noreplace(parent_fd, source_name, target_name)

    monkeypatch.setattr(
        builder,
        "_atomic_rename_noreplace",
        inject_before_publication_syscall,
    )

    with pytest.raises(builder.BuildError, match="target already exists") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert (output / victim_marker.name).read_text() == ("preserve publication-window victim\n")
    assert not (output / ".codex-plugin").exists()
    assert len(caught.value.recovery_paths) == 1
    assert (caught.value.recovery_paths[0] / ".codex-plugin" / "plugin.json").is_file()


def test_new_output_late_parent_swap_rolls_back_without_touching_victim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "publish" / "failure-memory"
    parent = output.parent
    displaced_parent = tmp_path / "publish-original"
    victim = tmp_path / "victim"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("untouched\n")
    builder = _load_builder(repository)
    atomic_noreplace = builder._atomic_rename_noreplace
    injected = False

    def publish_then_swap_parent(parent_fd: int, source: str, target: str) -> None:
        nonlocal injected
        atomic_noreplace(parent_fd, source, target)
        if not injected:
            injected = True
            assert output.is_dir()
            parent.rename(displaced_parent)
            parent.symlink_to(victim, target_is_directory=True)

    monkeypatch.setattr(
        builder,
        "_atomic_rename_noreplace",
        publish_then_swap_parent,
    )
    with pytest.raises(builder.BuildError, match="output parent changed"):
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert victim_marker.read_text() == "untouched\n"
    assert not (victim / "failure-memory").exists()
    assert not (displaced_parent / "failure-memory").exists()
    recoveries = list(displaced_parent.glob(".failure-memory.previous-*"))
    assert len(recoveries) == 1
    assert (recoveries[0] / ".codex-plugin" / "plugin.json").is_file()


def test_visible_output_swap_after_publication_wrapper_is_reported_without_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    output, marker = _existing_output(tmp_path)
    parent = output.parent
    victim = parent / "victim-entry"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("preserve victim\n")
    builder = _load_builder(repository)
    atomic_exchange = builder._atomic_exchange
    injected = False

    def exchange_then_swap_visible_output(parent_fd: int, left: str, right: str) -> None:
        nonlocal injected
        atomic_exchange(parent_fd, left, right)
        if not injected:
            injected = True
            temporary = parent / ".test-output-swap"
            output.rename(temporary)
            victim.rename(output)
            temporary.rename(victim)
            raise KeyboardInterrupt("stop after visible output substitution")

    monkeypatch.setattr(builder, "_atomic_exchange", exchange_then_swap_visible_output)

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert (output / victim_marker.name).read_text() == "preserve victim\n"
    assert (victim / ".codex-plugin" / "plugin.json").is_file()
    assert not (output / ".codex-plugin").exists()
    prior_locations = [
        path for path in caught.value.recovery_paths if (path / marker.name).is_file()
    ]
    assert len(prior_locations) == 1


def test_initially_absent_output_swap_after_publication_wrapper_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _fixture_repository(tmp_path)
    parent = tmp_path / "publish"
    parent.mkdir()
    output = parent / "failure-memory"
    victim = parent / "victim-entry"
    victim.mkdir()
    victim_marker = victim / "victim-marker.txt"
    victim_marker.write_text("preserve victim\n")
    builder = _load_builder(repository)
    atomic_noreplace = builder._atomic_rename_noreplace
    injected = False

    def publish_then_swap_visible_output(parent_fd: int, source: str, target: str) -> None:
        nonlocal injected
        atomic_noreplace(parent_fd, source, target)
        if not injected:
            injected = True
            temporary = parent / ".test-output-swap"
            output.rename(temporary)
            victim.rename(output)
            temporary.rename(victim)
            raise KeyboardInterrupt("stop after absent-output substitution")

    monkeypatch.setattr(
        builder,
        "_atomic_rename_noreplace",
        publish_then_swap_visible_output,
    )

    with pytest.raises(builder.BuildError, match="rollback incomplete") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert injected
    assert (output / victim_marker.name).read_text() == "preserve victim\n"
    assert (victim / ".codex-plugin" / "plugin.json").is_file()
    assert victim in caught.value.recovery_paths


def test_postcommit_descriptor_close_failure_is_warning_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _fixture_repository(tmp_path)
    output, _marker = _existing_output(tmp_path)
    builder = _load_builder(repository)
    real_close = builder.PinnedOutput.close
    injected = False

    def close_then_fail(pinned_output: object) -> None:
        nonlocal injected
        real_close(pinned_output)
        if not injected:
            injected = True
            raise OSError("injected committed prior descriptor close failure")

    monkeypatch.setattr(builder.PinnedOutput, "close", close_then_fail)

    result = builder.build(requested_output=output, allow_dirty=False)

    assert result.output == output
    assert result.rollback is not None
    assert (result.rollback / "existing-marker.txt").is_file()
    assert injected
    assert (output / ".codex-plugin" / "plugin.json").is_file()
    warning = capsys.readouterr().err
    assert "warning: could not close prior output descriptor" in warning
    assert "injected committed prior descriptor close failure" in warning


def test_prepublication_stage_close_failure_does_not_mask_retained_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _fixture_repository(tmp_path)
    output = tmp_path / "publish" / "failure-memory"
    builder = _load_builder(repository)
    close = builder.os.close
    stage_fd: int | None = None

    def fail_projection(open_stage_fd: int, snapshot: object) -> None:
        nonlocal stage_fd
        del snapshot
        stage_fd = open_stage_fd
        raise builder.BuildError("injected projection failure")

    def close_then_fail(file_descriptor: int) -> None:
        close(file_descriptor)
        if file_descriptor == stage_fd:
            raise OSError("injected stage descriptor close failure")

    monkeypatch.setattr(builder, "_stage_projection", fail_projection)
    monkeypatch.setattr(builder.os, "close", close_then_fail)

    with pytest.raises(builder.BuildError, match="injected projection failure") as caught:
        builder.build(requested_output=output, allow_dirty=False)

    assert len(caught.value.recovery_paths) == 1
    assert caught.value.recovery_paths[0].is_dir()
    warning = capsys.readouterr().err
    assert "warning: could not close published output descriptor" in warning
    assert "injected stage descriptor close failure" in warning
