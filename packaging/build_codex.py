#!/usr/bin/env python3
"""Build a commit-bound, descriptor-safely published Codex plugin projection."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath

ALLOWED_OUTPUT_NAMES = frozenset({"failure-memory", "failure-memory.new"})
ALLOWED_ROOT_FILES = frozenset(
    {
        ".mcp.json",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
    }
)
ALLOWED_ROOT_DIRECTORIES = frozenset(
    {
        ".claude-plugin",
        ".codex-plugin",
        ".cursor-plugin",
        ".plugin",
        "evals",
        "hooks",
        "scripts",
        "skills",
        "src",
    }
)
INTENDED_EXECUTABLES = frozenset(
    {
        "scripts/failure_memory_hook.py",
        "scripts/failure_memory_mcp.py",
        "scripts/install_harness.py",
    }
)
REQUIRED_FILES = frozenset(
    {
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        ".cursor-plugin/plugin.json",
        ".mcp.json",
        ".plugin/plugin.json",
        "LICENSE",
        "README.md",
        "hooks/hooks.json",
        "scripts/failure_memory_mcp.py",
    }
)
FORBIDDEN_DIRECTORY_NAMES = frozenset(
    {
        ".adapters",
        ".aws",
        ".azure",
        ".cache",
        ".cert",
        ".certificate",
        ".certificates",
        ".certs",
        ".credential",
        ".credentials",
        ".docker",
        ".gnupg",
        ".key",
        ".keys",
        ".kube",
        ".models",
        ".private",
        ".privates",
        ".runtime",
        ".secret",
        ".secrets",
        ".ssh",
        "__pycache__",
        "build",
        "cert",
        "certificate",
        "certificates",
        "certs",
        "credential",
        "credentials",
        "dist",
        "key",
        "keys",
        "out",
        "plugin-data",
        "private",
        "privates",
        "runtime",
        "secret",
        "secrets",
    }
)
SENSITIVE_STORE_NAMES = frozenset(
    {
        ".cert",
        ".certificate",
        ".certificates",
        ".certs",
        ".credential",
        ".credentials",
        ".key",
        ".keys",
        ".private",
        ".privates",
        ".secret",
        ".secrets",
        "cert",
        "certificate",
        "certificates",
        "certs",
        "credential",
        "credentials",
        "key",
        "keys",
        "private",
        "privates",
        "secret",
        "secrets",
    }
)
FORBIDDEN_CREDENTIAL_NAMES = frozenset(
    {
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "accesstokens.json",
        "auth.json",
        "credentials.json",
        "credentials.local.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "secrets.local.json",
    }
)
FORBIDDEN_SECRET_SUFFIXES = (
    ".asc",
    ".cer",
    ".crt",
    ".der",
    ".gpg",
    ".jks",
    ".kbx",
    ".kdbx",
    ".key",
    ".keystore",
    ".p12",
    ".p7b",
    ".p7c",
    ".p8",
    ".pem",
    ".pfx",
    ".pgp",
    ".ppk",
    ".pvk",
)
FORBIDDEN_RUNTIME_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
)

Inventory = dict[str, dict[str, str]]


class BuildError(RuntimeError):
    """A safe, user-actionable packaging failure."""

    def __init__(
        self,
        message: str,
        *,
        recovery_paths: tuple[Path, ...] = (),
        trust_anchor: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.recovery_paths = recovery_paths
        self.trust_anchor = trust_anchor


@dataclass(frozen=True, slots=True)
class LiveFingerprint:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class SourceFile:
    relative: PurePosixPath
    data: bytes
    output_mode: int
    git_mode: str | None = None
    fingerprint: LiveFingerprint | None = None


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    commit: str
    dirty: bool
    status: bytes
    live: bool
    files: tuple[SourceFile, ...]


@dataclass(frozen=True, slots=True)
class OpenParent:
    path: Path
    fd: int
    device: int
    inode: int
    anchor: Path


@dataclass(frozen=True, slots=True)
class TrustedDirectory:
    path: Path
    fd: int
    device: int
    inode: int
    anchor: Path


@dataclass(frozen=True, slots=True)
class BuildResult:
    output: Path
    rollback: Path | None
    recovery: Path | None


@dataclass(frozen=True, slots=True)
class EntryIdentity:
    device: int
    inode: int
    file_type: int


@dataclass(frozen=True, slots=True)
class IdentityLocations:
    paths: tuple[Path, ...]
    state: str


@dataclass(frozen=True, slots=True)
class DescriptorPathProbe:
    path: Path | None
    state: str


class PublicationState(Enum):
    STAGED = "staged"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    RECOVERING = "recovering"
    ROLLED_BACK = "rolled_back"
    RECOVERY_FAILED = "recovery_failed"
    COMMITTED = "committed"


@dataclass(slots=True)
class PinnedOutput:
    existed: bool
    fd: int | None
    identity: EntryIdentity | None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _run_git(repository: Path, *arguments: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repository), *arguments],
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace").strip()
        raise BuildError(f"git {' '.join(arguments)} failed: {detail}") from error


def _repository_root() -> Path:
    script_directory = Path(__file__).resolve().parent
    raw = _run_git(script_directory, "rev-parse", "--show-toplevel")
    return Path(os.fsdecode(raw).strip()).resolve(strict=True)


def _current_commit(repository: Path) -> str:
    return os.fsdecode(_run_git(repository, "rev-parse", "HEAD")).strip()


def _status(repository: Path) -> bytes:
    return _run_git(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )


def _decode_git_path(raw: bytes) -> PurePosixPath:
    text = os.fsdecode(raw)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise BuildError("git path is not valid UTF-8") from error
    relative = PurePosixPath(text)
    if (
        not text
        or relative.is_absolute()
        or "." in relative.parts
        or ".." in relative.parts
        or "\\" in text
    ):
        raise BuildError(f"git returned an unsafe source path: {text!r}")
    return relative


def _forbidden_reason(relative: PurePosixPath) -> str | None:
    lowered_parts = tuple(part.casefold() for part in relative.parts)
    filename = lowered_parts[-1]
    if filename.startswith(".env"):
        return "environment/secrets file"
    if filename in SENSITIVE_STORE_NAMES:
        return "sensitive credential/key/certificate store"
    if filename in FORBIDDEN_CREDENTIAL_NAMES:
        return "credential file"
    if filename.endswith(FORBIDDEN_SECRET_SUFFIXES):
        return "key/certificate file"
    if filename.endswith(FORBIDDEN_RUNTIME_SUFFIXES):
        return "runtime/database/bytecode file"
    forbidden_directory = next(
        (part for part in lowered_parts[:-1] if part in FORBIDDEN_DIRECTORY_NAMES),
        None,
    )
    if forbidden_directory is not None:
        return f"forbidden output/private/runtime directory {forbidden_directory!r}"
    return None


def _validate_source_policy(relative_paths: set[PurePosixPath]) -> None:
    for relative in sorted(relative_paths, key=lambda path: path.as_posix()):
        reason = _forbidden_reason(relative)
        if reason is not None:
            raise BuildError(f"forbidden package source {relative.as_posix()!r}: {reason}")


def _is_installable(relative: PurePosixPath) -> bool:
    path = relative.as_posix()
    if path in ALLOWED_ROOT_FILES:
        return True
    return bool(relative.parts and relative.parts[0] in ALLOWED_ROOT_DIRECTORIES)


def _output_mode(relative: PurePosixPath, git_mode: str | None) -> int:
    path = relative.as_posix()
    if git_mode is not None and git_mode not in {"100644", "100755"}:
        raise BuildError(f"unsupported Git file mode {git_mode} for {path!r}")
    if path in INTENDED_EXECUTABLES:
        if git_mode is not None and git_mode != "100755":
            raise BuildError(f"launcher must be tracked executable (100755): {path!r}")
        return 0o755
    return 0o644


def _clean_sources(repository: Path, commit: str) -> tuple[SourceFile, ...]:
    raw_tree = _run_git(repository, "ls-tree", "-r", "-z", "--full-tree", commit)
    tree_entries: list[tuple[str, str, str, PurePosixPath]] = []
    all_paths: set[PurePosixPath] = set()
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry:
            continue
        try:
            descriptor, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, raw_kind, raw_oid = descriptor.split(b" ", 2)
        except ValueError as error:
            raise BuildError("git returned a malformed tree entry") from error
        relative = _decode_git_path(raw_path)
        mode = raw_mode.decode("ascii")
        kind = raw_kind.decode("ascii")
        oid = raw_oid.decode("ascii")
        tree_entries.append((mode, kind, oid, relative))
        all_paths.add(relative)
    _validate_source_policy(all_paths)

    sources: list[SourceFile] = []
    for mode, kind, oid, relative in tree_entries:
        if not _is_installable(relative):
            continue
        if kind != "blob":
            raise BuildError(
                f"installable Git entry is not a regular blob: {relative.as_posix()!r}"
            )
        sources.append(
            SourceFile(
                relative=relative,
                data=_run_git(repository, "cat-file", "blob", oid),
                output_mode=_output_mode(relative, mode),
                git_mode=mode,
            )
        )
    return tuple(sources)


def _parse_index(repository: Path) -> dict[PurePosixPath, str]:
    raw_index = _run_git(repository, "ls-files", "--stage", "-z")
    entries: dict[PurePosixPath, str] = {}
    for raw_entry in raw_index.split(b"\0"):
        if not raw_entry:
            continue
        try:
            descriptor, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, _raw_oid, raw_stage = descriptor.split(b" ", 2)
        except ValueError as error:
            raise BuildError("git returned a malformed index entry") from error
        relative = _decode_git_path(raw_path)
        if raw_stage != b"0" or relative in entries:
            raise BuildError(
                f"unmerged or duplicate index entry cannot be packaged: {relative.as_posix()!r}"
            )
        entries[relative] = raw_mode.decode("ascii")
    return entries


def _untracked_paths(repository: Path) -> set[PurePosixPath]:
    raw_paths = _run_git(repository, "ls-files", "--others", "--exclude-standard", "-z")
    return {_decode_git_path(raw_path) for raw_path in raw_paths.split(b"\0") if raw_path}


def _deleted_tracked_paths(repository: Path) -> set[PurePosixPath]:
    raw_paths = _run_git(repository, "ls-files", "--deleted", "-z")
    return {_decode_git_path(raw_path) for raw_path in raw_paths.split(b"\0") if raw_path}


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_read_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _require_descriptor_primitives() -> None:
    required_functions = (os.open, os.mkdir, os.rename, os.stat)
    if (
        not getattr(os, "O_DIRECTORY", 0)
        or not getattr(os, "O_NOFOLLOW", 0)
        or any(function not in os.supports_dir_fd for function in required_functions)
    ):
        raise BuildError(
            "safe plugin publication requires POSIX dir_fd, O_DIRECTORY, and O_NOFOLLOW"
        )


def _validate_component(component: str) -> None:
    if not component or component in {".", ".."} or "/" in component or "\0" in component:
        raise BuildError(f"unsafe filesystem path component: {component!r}")


def _assert_safe_live_directory(directory_fd: int, label: str) -> None:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise BuildError(f"live source directory is not a directory: {label!r}")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BuildError(f"live source directory is group/world-writable: {label!r}")
    if metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise BuildError(f"live source directory has unsafe special permissions: {label!r}")


def _open_relative_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        _assert_safe_live_directory(current, "repository root")
        for component in parts:
            _validate_component(component)
            next_fd: int | None = None
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current)
                _assert_safe_live_directory(next_fd, component)
            except BaseException:
                if next_fd is not None:
                    with suppress(BaseException):
                        os.close(next_fd)
                raise
            try:
                os.close(current)
            except BaseException:
                with suppress(BaseException):
                    os.close(next_fd)
                raise
            current = next_fd
        return current
    except BaseException:
        with suppress(BaseException):
            os.close(current)
        raise


def _fingerprint(metadata: os.stat_result) -> LiveFingerprint:
    return LiveFingerprint(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _read_all(file_fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(file_fd, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _read_live_source(
    repository_fd: int,
    relative: PurePosixPath,
    git_mode: str | None,
) -> SourceFile:
    parent_fd = _open_relative_directory(repository_fd, relative.parts[:-1])
    try:
        filename = relative.parts[-1]
        _validate_component(filename)
        before = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise BuildError(f"live package source is not a regular file: {relative.as_posix()!r}")
        if stat.S_IMODE(before.st_mode) & 0o022:
            raise BuildError(
                f"live package source is group/world-writable: {relative.as_posix()!r}"
            )
        if before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise BuildError(
                f"live package source has unsafe special permissions: {relative.as_posix()!r}"
            )
        file_fd = os.open(filename, _file_read_flags(), dir_fd=parent_fd)
        try:
            opened = os.fstat(file_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise BuildError(
                    f"live package source changed while opening: {relative.as_posix()!r}"
                )
            data = _read_all(file_fd)
            after = os.fstat(file_fd)
        finally:
            _close_or_warn(
                lambda: os.close(file_fd),
                label="live source file descriptor",
            )
        if _fingerprint(opened) != _fingerprint(after):
            raise BuildError(f"live package source changed while reading: {relative.as_posix()!r}")
        return SourceFile(
            relative=relative,
            data=data,
            output_mode=_output_mode(relative, git_mode),
            git_mode=git_mode,
            fingerprint=_fingerprint(after),
        )
    except (FileNotFoundError, NotADirectoryError, OSError) as error:
        if isinstance(error, BuildError):
            raise
        raise BuildError(
            f"cannot safely read live package source {relative.as_posix()!r}: {error}"
        ) from error
    finally:
        _close_or_warn(
            lambda: os.close(parent_fd),
            label="live source parent descriptor",
        )


def _live_sources(repository: Path, repository_fd: int) -> tuple[SourceFile, ...]:
    tracked = _parse_index(repository)
    deleted = _deleted_tracked_paths(repository)
    untracked = _untracked_paths(repository)
    all_paths = (set(tracked) - deleted) | untracked
    _validate_source_policy(all_paths)
    sources = [
        _read_live_source(repository_fd, relative, tracked.get(relative))
        for relative in sorted(all_paths, key=lambda path: path.as_posix())
        if _is_installable(relative)
    ]
    return tuple(sources)


def _source_snapshot(
    repository: Path,
    repository_fd: int | None,
    *,
    allow_dirty: bool,
) -> SourceSnapshot:
    commit = _current_commit(repository)
    status = _status(repository)
    if not allow_dirty and status:
        raise BuildError("repository is dirty; commit changes or pass --allow-dirty")
    if allow_dirty:
        if repository_fd is None:
            raise BuildError("live source snapshot requires a repository descriptor")
        files = _live_sources(repository, repository_fd)
    else:
        files = _clean_sources(repository, commit)
    return SourceSnapshot(
        commit=commit,
        dirty=allow_dirty or bool(status),
        status=status,
        live=allow_dirty,
        files=files,
    )


def _verify_source_state(
    repository: Path,
    repository_fd: int | None,
    snapshot: SourceSnapshot,
) -> None:
    if _current_commit(repository) != snapshot.commit:
        raise BuildError("Git HEAD changed during package build")
    current_status = _status(repository)
    if snapshot.live:
        if current_status != snapshot.status:
            raise BuildError("Git status changed during live snapshot")
        if repository_fd is None:
            raise BuildError("live source verification requires a repository descriptor")
        for source in snapshot.files:
            current = _read_live_source(
                repository_fd,
                source.relative,
                source.git_mode,
            )
            if (
                current.data != source.data
                or current.fingerprint != source.fingerprint
                or current.output_mode != source.output_mode
            ):
                raise BuildError(
                    f"live source changed during live snapshot: {source.relative.as_posix()!r}"
                )
    elif current_status:
        raise BuildError("worktree became dirty during clean package build")


def _reject_existing_symlink_components(path: Path) -> None:
    candidates = list(path.parents)
    candidates.reverse()
    candidates.append(path)
    for candidate in candidates:
        if candidate.is_symlink():
            raise BuildError(f"output path contains a symbolic link: {candidate}")


def _validated_output(repository: Path, requested: Path) -> Path:
    if requested.name not in ALLOWED_OUTPUT_NAMES:
        allowed = ", ".join(sorted(ALLOWED_OUTPUT_NAMES))
        raise BuildError(f"output name must be one of: {allowed}")

    absolute = Path(os.path.abspath(os.fspath(requested.expanduser())))
    _reject_existing_symlink_components(absolute)
    home = Path(os.path.abspath(os.fspath(Path.home())))
    root = Path("/")
    if absolute in {root, home, repository}:
        raise BuildError(f"refusing unsafe output path: {absolute}")
    if absolute.parent in {root, home, repository}:
        raise BuildError(f"refusing unsafe output parent: {absolute.parent}")
    if absolute.is_relative_to(repository):
        allowed_repository_parent = repository / "packaging" / "out"
        if not absolute.is_relative_to(allowed_repository_parent):
            raise BuildError("repository output must be under packaging/out")
    if absolute.is_symlink():
        raise BuildError(f"output path is a symbolic link: {absolute}")
    if absolute.exists() and not absolute.is_dir():
        raise BuildError(f"existing output is not a directory: {absolute}")
    return absolute


def _open_absolute_directory(path: Path, *, create: bool) -> int:
    if not path.is_absolute():
        raise BuildError(f"directory path must be absolute: {path}")
    current = os.open("/", _directory_flags())
    try:
        for component in path.parts[1:]:
            _validate_component(component)
            created = False
            next_fd: int | None = None
            try:
                try:
                    next_fd = os.open(component, _directory_flags(), dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o755, dir_fd=current)
                    created = True
                    next_fd = os.open(component, _directory_flags(), dir_fd=current)
                if created:
                    os.fchmod(next_fd, 0o755)
            except BaseException:
                if next_fd is not None:
                    with suppress(BaseException):
                        os.close(next_fd)
                raise
            try:
                os.close(current)
            except BaseException:
                with suppress(BaseException):
                    os.close(next_fd)
                raise
            current = next_fd
        return current
    except BaseException:
        with suppress(BaseException):
            os.close(current)
        raise


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return None if getuid is None else int(getuid())


def _assert_real_directory(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise BuildError(f"trusted path component is not a real directory: {path}")


def _assert_private_directory(metadata: os.stat_result, path: Path) -> None:
    _assert_real_directory(metadata, path)
    uid = _current_uid()
    if uid is not None and metadata.st_uid != uid:
        raise BuildError(
            f"private trust-domain directory is not owned by current uid {uid}: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BuildError(f"private trust-domain directory is group/world-writable: {path}")


def _visible_directory_metadata(path: Path) -> os.stat_result:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise BuildError(f"cannot inspect trusted path component {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise BuildError(f"trusted path component is a symbolic link: {path}")
    _assert_real_directory(metadata, path)
    return metadata


def _select_trust_anchor(path: Path) -> Path:
    home = Path(os.path.abspath(os.fspath(Path.home())))
    if path == home or path.is_relative_to(home):
        metadata = _visible_directory_metadata(home)
        _assert_private_directory(metadata, home)
        return home

    for candidate in (path, *path.parents):
        try:
            metadata = os.stat(candidate, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise BuildError(
                f"cannot inspect trust-anchor candidate {candidate}: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            continue
        uid = _current_uid()
        if (uid is None or metadata.st_uid == uid) and not stat.S_IMODE(metadata.st_mode) & 0o022:
            return candidate
    raise BuildError(
        f"no private trust anchor owned by current uid with safe mode for path: {path}"
    )


def _open_trusted_directory(
    path: Path,
    *,
    create: bool,
    anchor: Path | None = None,
) -> TrustedDirectory:
    if not path.is_absolute():
        raise BuildError(f"trusted directory path must be absolute: {path}")
    selected_anchor = anchor or _select_trust_anchor(path)
    if path != selected_anchor and not path.is_relative_to(selected_anchor):
        raise BuildError(
            f"stored trust anchor is not an ancestor of trusted path: {selected_anchor}"
        )
    try:
        for ancestor in reversed(selected_anchor.parents):
            _visible_directory_metadata(ancestor)
        anchor_fd = _open_absolute_directory(selected_anchor, create=False)
    except BaseException as error:
        raise BuildError(
            f"{error}; trust_anchor={str(selected_anchor)!r}",
            recovery_paths=(error.recovery_paths if isinstance(error, BuildError) else ()),
            trust_anchor=selected_anchor,
        ) from error
    current = anchor_fd
    try:
        anchor_metadata = os.fstat(current)
        _assert_private_directory(anchor_metadata, selected_anchor)
        current_path = selected_anchor
        for component in path.relative_to(selected_anchor).parts:
            _validate_component(component)
            next_path = current_path / component
            next_fd: int | None = None
            try:
                try:
                    next_fd = os.open(component, _directory_flags(), dir_fd=current)
                except FileNotFoundError as error:
                    if not create:
                        raise BuildError(
                            f"trusted directory does not exist: {next_path}"
                        ) from error
                    os.mkdir(component, 0o700, dir_fd=current)
                    next_fd = os.open(
                        component,
                        _directory_flags(),
                        dir_fd=current,
                    )
                    os.fchmod(next_fd, 0o700)
                metadata = os.fstat(next_fd)
                _assert_private_directory(metadata, next_path)
                visible = _visible_directory_metadata(next_path)
                if _identity(visible) != _identity(metadata):
                    raise BuildError(f"trusted directory identity changed: {next_path}")
            except BaseException:
                if next_fd is not None:
                    with suppress(BaseException):
                        os.close(next_fd)
                raise
            try:
                os.close(current)
            except BaseException:
                with suppress(BaseException):
                    os.close(next_fd)
                raise
            current = next_fd
            current_path = next_path
        metadata = os.fstat(current)
        return TrustedDirectory(
            path=path,
            fd=current,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            anchor=selected_anchor,
        )
    except BaseException as error:
        with suppress(BaseException):
            os.close(current)
        raise BuildError(
            f"{error}; trust_anchor={str(selected_anchor)!r}",
            recovery_paths=(error.recovery_paths if isinstance(error, BuildError) else ()),
            trust_anchor=selected_anchor,
        ) from error


def _open_parent(path: Path) -> OpenParent:
    try:
        trusted = _open_trusted_directory(path, create=True)
    except (OSError, BuildError) as error:
        if isinstance(error, BuildError):
            raise
        raise BuildError(f"cannot safely open output parent {path}: {error}") from error
    return OpenParent(
        path=path,
        fd=trusted.fd,
        device=trusted.device,
        inode=trusted.inode,
        anchor=trusted.anchor,
    )


def _assert_trusted_directory_identity(
    directory: TrustedDirectory | OpenParent,
    *,
    label: str,
) -> None:
    try:
        held = os.fstat(directory.fd)
        _assert_private_directory(held, directory.path)
        visible = _open_trusted_directory(
            directory.path,
            create=False,
            anchor=directory.anchor,
        )
    except (OSError, BuildError) as error:
        raise BuildError(
            f"{label} changed during build: trust domain changed: {error}",
            trust_anchor=directory.anchor,
        ) from error
    try:
        if (
            held.st_dev != directory.device
            or held.st_ino != directory.inode
            or visible.device != directory.device
            or visible.inode != directory.inode
        ):
            raise BuildError(
                f"{label} changed during build",
                trust_anchor=directory.anchor,
            )
    finally:
        _close_or_warn(
            lambda: os.close(visible.fd),
            label=f"revalidated {label} descriptor",
        )


def _assert_parent_identity(parent: OpenParent) -> None:
    _assert_trusted_directory_identity(parent, label="output parent")


def _normalized_absolute_path(path: Path) -> Path | None:
    if not path.is_absolute():
        return None
    return Path(os.path.normpath(os.path.abspath(os.fspath(path))))


def _descriptor_path_probe(file_descriptor: int) -> DescriptorPathProbe:
    """Resolve and verify a descriptor path without adopting stale names."""
    held = os.fstat(file_descriptor)
    descriptor_path: Path | None = None
    try:
        if sys.platform == "darwin":
            command = getattr(fcntl, "F_GETPATH", None)
            if command is not None:
                raw = fcntl.fcntl(file_descriptor, command, b"\0" * 1024)
                if isinstance(raw, bytes):
                    encoded = raw.split(b"\0", 1)[0]
                    if encoded:
                        descriptor_path = Path(os.fsdecode(encoded))
        elif sys.platform.startswith("linux"):
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{file_descriptor}"))
    except (OSError, TypeError, ValueError):
        descriptor_path = None
    if descriptor_path is None:
        state = "unlinked" if held.st_nlink == 0 else "unavailable"
        return DescriptorPathProbe(path=None, state=state)

    normalized = _normalized_absolute_path(descriptor_path)
    if normalized is None:
        return DescriptorPathProbe(path=None, state="unavailable")
    visible = _lstat_at_path(normalized)
    if visible is None:
        return DescriptorPathProbe(path=None, state="unlinked")
    if (
        visible.st_dev == held.st_dev
        and visible.st_ino == held.st_ino
        and stat.S_IFMT(visible.st_mode) == stat.S_IFMT(held.st_mode)
    ):
        return DescriptorPathProbe(path=normalized, state="verified")
    return DescriptorPathProbe(path=None, state="mismatch")


def _verified_descriptor_path(file_descriptor: int) -> Path | None:
    return _descriptor_path_probe(file_descriptor).path


def _held_directory_link_state(
    file_descriptor: int,
    expected: EntryIdentity,
) -> str:
    """Classify whether a held directory still has a name in its actual parent."""
    if expected.file_type != stat.S_IFDIR:
        return "unknown"
    parent_fd: int | None = None
    try:
        parent_fd = os.open("..", _directory_flags(), dir_fd=file_descriptor)
        for name in os.listdir(parent_fd):
            _validate_component(name)
            metadata = _lstat_at(parent_fd, name)
            if metadata is not None and _identity(metadata) == expected:
                return "linked"
        return "unlinked"
    except (BuildError, OSError):
        return "unknown"
    finally:
        if parent_fd is not None:
            with suppress(BaseException):
                os.close(parent_fd)


def _resolved_parent_path(parent: OpenParent) -> Path:
    """Resolve the current absolute name of a held parent directory."""
    held = os.fstat(parent.fd)
    descriptor_path = _verified_descriptor_path(parent.fd)
    if descriptor_path is not None and (
        stat.S_ISDIR(held.st_mode) and held.st_dev == parent.device and held.st_ino == parent.inode
    ):
        return descriptor_path

    visible = _lstat_at_path(parent.path)
    if visible is not None and (
        stat.S_ISDIR(visible.st_mode)
        and visible.st_dev == parent.device
        and visible.st_ino == parent.inode
    ):
        return parent.path

    container_fd: int | None = None
    try:
        container_fd = _open_absolute_directory(parent.path.parent, create=False)
        matches: list[Path] = []
        for name in os.listdir(container_fd):
            _validate_component(name)
            metadata = _lstat_at(container_fd, name)
            if metadata is not None and (
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_dev == parent.device
                and metadata.st_ino == parent.inode
            ):
                matches.append(parent.path.parent / name)
        if len(matches) == 1:
            return matches[0]
    except (OSError, BuildError):
        pass
    finally:
        if container_fd is not None:
            with suppress(BaseException):
                os.close(container_fd)
    raise BuildError(
        "cannot resolve held output parent absolute path; "
        f"intended={str(parent.path)!r} device={parent.device} inode={parent.inode}",
        trust_anchor=parent.anchor,
    )


def _lstat_at_path(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path, follow_symlinks=False)
    except OSError:
        return None


def _lstat_at(directory_fd: int, name: str) -> os.stat_result | None:
    _validate_component(name)
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _identity(metadata: os.stat_result) -> EntryIdentity:
    return EntryIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        file_type=stat.S_IFMT(metadata.st_mode),
    )


def _assert_entry_identity(
    parent_fd: int,
    name: str,
    expected: EntryIdentity,
    *,
    label: str,
) -> None:
    metadata = _lstat_at(parent_fd, name)
    actual = None if metadata is None else _identity(metadata)
    if metadata is None or not stat.S_ISDIR(metadata.st_mode) or actual != expected:
        raise BuildError(
            "publication entry identity changed: "
            f"{label}={name!r}, expected device={expected.device} inode={expected.inode}, "
            f"observed={actual!r}"
        )


def _assert_entry_absent(parent_fd: int, name: str, *, label: str) -> None:
    if _lstat_at(parent_fd, name) is not None:
        raise BuildError(f"publication entry identity changed: expected absent {label}={name!r}")


def _validate_output_entry(parent_fd: int, output_name: str) -> None:
    metadata = _lstat_at(parent_fd, output_name)
    if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
        if stat.S_ISLNK(metadata.st_mode):
            raise BuildError(f"output path is a symbolic link: {output_name}")
        raise BuildError(f"existing output is not a directory: {output_name}")


def _pin_output(
    parent_fd: int,
    output_name: str,
) -> PinnedOutput:
    metadata = _lstat_at(parent_fd, output_name)
    if metadata is None:
        return PinnedOutput(existed=False, fd=None, identity=None)
    if not stat.S_ISDIR(metadata.st_mode):
        if stat.S_ISLNK(metadata.st_mode):
            raise BuildError(f"output path is a symbolic link: {output_name}")
        raise BuildError(f"existing output is not a directory: {output_name}")
    initial_identity = _identity(metadata)
    try:
        output_fd = os.open(output_name, _directory_flags(), dir_fd=parent_fd)
    except OSError as error:
        raise BuildError(f"initial output identity changed: {output_name!r}") from error
    try:
        identity = _identity(os.fstat(output_fd))
        if identity != initial_identity:
            raise BuildError(
                "initial output identity changed: "
                f"{output_name!r}, expected device={initial_identity.device} "
                f"inode={initial_identity.inode}, observed={identity!r}"
            )
        _assert_entry_identity(
            parent_fd,
            output_name,
            initial_identity,
            label="initial prior output",
        )
    except BaseException:
        _close_or_warn(
            lambda: os.close(output_fd),
            label="failed pinned output descriptor",
        )
        raise
    return PinnedOutput(existed=True, fd=output_fd, identity=initial_identity)


def _create_stage(
    parent: OpenParent,
    output_name: str,
    commit: str,
) -> tuple[str, int, EntryIdentity]:
    stage_name = f".{output_name}.previous-{commit[:12]}-{uuid.uuid4().hex}"
    stage_label = Path(stage_name)
    stage_fd: int | None = None
    try:
        try:
            os.mkdir(stage_name, 0o700, dir_fd=parent.fd)
        except FileExistsError as error:
            raise BuildError(
                "generated transaction name already exists; inspect it and move it to "
                f"Trash only after verification: transaction={stage_name!r}",
            ) from error
        metadata = _lstat_at(parent.fd, stage_name)
        if metadata is None or not stat.S_ISDIR(metadata.st_mode):
            raise BuildError(f"created transaction entry is not a directory: {stage_name!r}")
        _assert_private_directory(metadata, stage_label)
        created_identity = _identity(metadata)
        stage_fd = os.open(stage_name, _directory_flags(), dir_fd=parent.fd)
        opened_identity = _identity(os.fstat(stage_fd))
        if opened_identity != created_identity:
            raise BuildError(
                "created transaction identity changed before open; "
                f"transaction={stage_name!r} expected={created_identity!r} "
                f"observed={opened_identity!r}",
                trust_anchor=parent.anchor,
            )
        _assert_entry_identity(
            parent.fd,
            stage_name,
            created_identity,
            label="newly created transaction",
        )
        os.fchmod(stage_fd, 0o700)
        secured_metadata = os.fstat(stage_fd)
        _assert_private_directory(secured_metadata, stage_label)
        if (
            _identity(secured_metadata) != created_identity
            or stat.S_IMODE(secured_metadata.st_mode) != 0o700
        ):
            raise BuildError("created transaction mode or identity changed during setup")
    except BaseException as error:
        if stage_fd is not None:
            _close_or_warn(
                lambda: os.close(stage_fd),
                label="failed transaction descriptor",
            )
        retained = False
        with suppress(BaseException):
            os.stat(
                stage_name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            retained = True
        recovery_path = _resolved_parent_path(parent) / stage_name if retained else None
        recovery_paths = (
            (recovery_path,)
            if recovery_path is not None
            else (error.recovery_paths if isinstance(error, BuildError) else ())
        )
        recovery_detail = (
            f"; retained recovery={str(recovery_path)!r}" if recovery_path is not None else ""
        )
        raise BuildError(
            f"transaction setup failed: {error}{recovery_detail}",
            recovery_paths=recovery_paths,
            trust_anchor=parent.anchor,
        ) from error
    return stage_name, stage_fd, created_identity


def _ensure_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for component in parts:
            _validate_component(component)
            created = False
            next_fd: int | None = None
            try:
                try:
                    next_fd = os.open(component, _directory_flags(), dir_fd=current)
                except FileNotFoundError:
                    os.mkdir(component, 0o755, dir_fd=current)
                    created = True
                    next_fd = os.open(component, _directory_flags(), dir_fd=current)
                if created:
                    os.fchmod(next_fd, 0o755)
                elif stat.S_IMODE(os.fstat(next_fd).st_mode) != 0o755:
                    raise BuildError(f"staged directory mode is unsafe: {component!r}")
            except BaseException:
                if next_fd is not None:
                    with suppress(BaseException):
                        os.close(next_fd)
                raise
            try:
                os.close(current)
            except BaseException:
                with suppress(BaseException):
                    os.close(next_fd)
                raise
            current = next_fd
        return current
    except BaseException:
        with suppress(BaseException):
            os.close(current)
        raise


def _write_all(file_fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(file_fd, remaining)
        if written <= 0:
            raise BuildError("short write while staging plugin file")
        remaining = remaining[written:]


def _write_file_at(root_fd: int, relative: PurePosixPath, data: bytes, mode: int) -> None:
    parent_fd = _ensure_directory(root_fd, relative.parts[:-1])
    try:
        filename = relative.parts[-1]
        _validate_component(filename)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(filename, flags, mode, dir_fd=parent_fd)
        try:
            _write_all(file_fd, data)
            os.fchmod(file_fd, mode)
            os.fsync(file_fd)
        finally:
            _close_or_warn(
                lambda: os.close(file_fd),
                label="staged file descriptor",
            )
    finally:
        _close_or_warn(
            lambda: os.close(parent_fd),
            label="staged file parent descriptor",
        )


def _package_version(files: tuple[SourceFile, ...]) -> str:
    manifest = next(
        (
            source.data
            for source in files
            if source.relative.as_posix() == ".codex-plugin/plugin.json"
        ),
        None,
    )
    try:
        value = json.loads(manifest)["version"] if manifest is not None else None
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise BuildError("plugin manifest has no readable package version") from error
    if not isinstance(value, str) or not value:
        raise BuildError("plugin manifest version must be a non-empty string")
    return value


def _stage_projection(stage_fd: int, snapshot: SourceSnapshot) -> None:
    inventory: Inventory = {}
    for source in snapshot.files:
        _write_file_at(
            stage_fd,
            source.relative,
            source.data,
            source.output_mode,
        )
        inventory[source.relative.as_posix()] = {
            "sha256": hashlib.sha256(source.data).hexdigest(),
            "mode": f"{source.output_mode:04o}",
        }
    missing = sorted(REQUIRED_FILES - inventory.keys())
    if missing:
        raise BuildError(f"install projection is missing required files: {missing}")
    build_manifest = {
        "commit": snapshot.commit,
        "version": _package_version(snapshot.files),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "dirty": snapshot.dirty,
        "files": dict(sorted(inventory.items())),
    }
    _write_file_at(
        stage_fd,
        PurePosixPath("build-manifest.json"),
        (json.dumps(build_manifest, indent=2, sort_keys=True) + "\n").encode(),
        0o644,
    )


def _atomic_exchange(parent_fd: int, left_name: str, right_name: str) -> None:
    """Atomically exchange two entries in one held directory."""
    _validate_component(left_name)
    _validate_component(right_name)
    library = ctypes.CDLL(None, use_errno=True)
    left = os.fsencode(left_name)
    right = os.fsencode(right_name)
    result: int
    if sys.platform == "darwin":
        try:
            rename_swap = library.renameatx_np
        except AttributeError as error:
            raise BuildError("atomic directory exchange is unavailable on this platform") from error
        rename_swap.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_swap.restype = ctypes.c_int
        result = rename_swap(parent_fd, left, parent_fd, right, 0x00000002)
    elif sys.platform.startswith("linux"):
        try:
            rename_exchange = library.renameat2
        except AttributeError as error:
            raise BuildError("atomic directory exchange is unavailable on this platform") from error
        rename_exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exchange.restype = ctypes.c_int
        result = rename_exchange(parent_fd, left, parent_fd, right, 0x00000002)
    else:
        raise BuildError("atomic directory exchange is unavailable on this platform")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }:
        raise BuildError(f"atomic directory exchange is unavailable: {os.strerror(error_number)}")
    raise BuildError(f"atomic directory exchange failed: {os.strerror(error_number)}")


def _atomic_rename_noreplace(parent_fd: int, source_name: str, target_name: str) -> None:
    """Atomically rename one held-directory entry only if the target is absent."""
    _validate_component(source_name)
    _validate_component(target_name)
    library = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    target = os.fsencode(target_name)
    result: int
    if sys.platform == "darwin":
        try:
            rename_exclusive = library.renameatx_np
        except AttributeError as error:
            raise BuildError("atomic no-replace rename is unavailable on this platform") from error
        rename_exclusive.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(parent_fd, source, parent_fd, target, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename_exclusive = library.renameat2
        except AttributeError as error:
            raise BuildError("atomic no-replace rename is unavailable on this platform") from error
        rename_exclusive.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(parent_fd, source, parent_fd, target, 0x00000001)
    else:
        raise BuildError("atomic no-replace rename is unavailable on this platform")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise BuildError(f"atomic no-replace target already exists: {target_name!r}")
    if error_number in {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }:
        raise BuildError(f"atomic no-replace rename is unavailable: {os.strerror(error_number)}")
    raise BuildError(f"atomic no-replace rename failed: {os.strerror(error_number)}")


def _unresolved_artifacts(parent: OpenParent, output_name: str) -> tuple[Path, ...]:
    prefixes = (
        f".{output_name}.previous-",
        f".{output_name}.recovery-",
        f".{output_name}.stage-",
    )
    names = [
        name
        for name in os.listdir(parent.fd)
        if name.startswith(prefixes) or name.startswith(".publication-recovery-")
    ]
    if not names:
        return ()
    parent_path = _resolved_parent_path(parent)
    paths = [parent_path / name for name in names]
    return tuple(sorted(paths, key=os.fspath))


def _assert_no_unresolved_artifacts(parent: OpenParent, output_name: str) -> None:
    paths = _unresolved_artifacts(parent, output_name)
    if paths:
        rendered = ", ".join(repr(str(path)) for path in paths)
        raise BuildError(
            "unresolved builder rollback/recovery artifacts require inspection; "
            f"move them to Trash only after verification: {rendered}",
            recovery_paths=paths,
            trust_anchor=parent.anchor,
        )


def _retained_failure(
    error: BaseException,
    recovery_path: Path,
    *,
    trust_anchor: Path,
) -> BuildError:
    return BuildError(
        f"{error}; retained recovery={str(recovery_path)!r}",
        recovery_paths=(recovery_path,),
        trust_anchor=trust_anchor,
    )


def _identity_locations(
    parent: OpenParent,
    expected: EntryIdentity,
    held_fd: int,
) -> IdentityLocations:
    """Resolve retained names from raw identities, including before construction."""
    held_metadata = os.fstat(held_fd)
    held_identity = _identity(held_metadata)
    if held_identity != expected:
        raise BuildError(
            "held publication descriptor identity changed: "
            f"expected={expected!r} observed={held_identity!r}",
            trust_anchor=parent.anchor,
        )

    sibling_names: list[str] = []
    for name in os.listdir(parent.fd):
        _validate_component(name)
        metadata = _lstat_at(parent.fd, name)
        if metadata is not None and _identity(metadata) == expected:
            sibling_names.append(name)

    matches: set[Path] = set()
    if sibling_names:
        with suppress(BuildError):
            parent_path = _resolved_parent_path(parent)
            for name in sibling_names:
                candidate = _normalized_absolute_path(parent_path / name)
                visible = None if candidate is None else _lstat_at_path(candidate)
                if candidate is not None and visible is not None and _identity(visible) == expected:
                    matches.add(candidate)

    descriptor_probe = _descriptor_path_probe(held_fd)
    descriptor_path = descriptor_probe.path
    if descriptor_path is not None:
        visible = _lstat_at_path(descriptor_path)
        if visible is not None and _identity(visible) == expected:
            matches.add(descriptor_path)

    paths = tuple(sorted(matches, key=os.fspath))
    link_state = _held_directory_link_state(held_fd, expected)
    state = (
        "linked"
        if paths
        else (
            "unlinked"
            if link_state == "unlinked" or (link_state == "unknown" and held_metadata.st_nlink == 0)
            else "linked-path-unresolved"
        )
    )
    return IdentityLocations(paths=paths, state=state)


@dataclass(slots=True)
class PublicationTransaction:
    """A retained, reversible publication through one held parent descriptor."""

    parent: OpenParent
    stage_name: str
    stage_fd: int
    stage_identity: EntryIdentity
    output_name: str
    had_prior_output: bool
    prior_fd: int | None
    prior_identity: EntryIdentity | None
    state: PublicationState = PublicationState.STAGED

    @property
    def stage_path(self) -> Path:
        return _resolved_parent_path(self.parent) / self.stage_name

    def _verified_prior(self) -> tuple[int, EntryIdentity]:
        if self.prior_fd is None or self.prior_identity is None:
            raise BuildError("publication prior-output identity is unavailable")
        return self.prior_fd, self.prior_identity

    def _locations_for_identity(
        self,
        expected: EntryIdentity,
        held_fd: int,
    ) -> IdentityLocations:
        return _identity_locations(self.parent, expected, held_fd)

    def _single_location(self, expected: EntryIdentity, held_fd: int) -> Path | None:
        locations = self._locations_for_identity(expected, held_fd).paths
        return locations[0] if len(locations) == 1 else None

    def _rollback_complete(self) -> bool:
        stage_location = self._single_location(self.stage_identity, self.stage_fd)
        if stage_location != self.stage_path:
            return False
        output_metadata = _lstat_at(self.parent.fd, self.output_name)
        if self.had_prior_output:
            _prior_fd, prior_identity = self._verified_prior()
            return output_metadata is not None and _identity(output_metadata) == prior_identity
        return output_metadata is None

    def _published_layout(self) -> bool:
        output_metadata = _lstat_at(self.parent.fd, self.output_name)
        if output_metadata is None or _identity(output_metadata) != self.stage_identity:
            return False
        if self.had_prior_output:
            _prior_fd, prior_identity = self._verified_prior()
            stage_metadata = _lstat_at(self.parent.fd, self.stage_name)
            return stage_metadata is not None and _identity(stage_metadata) == prior_identity
        return _lstat_at(self.parent.fd, self.stage_name) is None

    def _diagnostic_error(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> BuildError:
        generated = self._locations_for_identity(
            self.stage_identity,
            self.stage_fd,
        )
        prior_paths: tuple[Path, ...] = ()
        prior_state = "not-applicable"
        prior_identity: EntryIdentity | None = None
        if self.had_prior_output:
            prior_fd, prior_identity = self._verified_prior()
            prior = self._locations_for_identity(prior_identity, prior_fd)
            prior_paths = prior.paths
            prior_state = prior.state
        prior_identity_detail = (
            "none"
            if prior_identity is None
            else (
                f"device={prior_identity.device} inode={prior_identity.inode} "
                f"type={prior_identity.file_type}"
            )
        )
        observed = _lstat_at(self.parent.fd, self.output_name)
        paths = tuple(sorted(set((*generated.paths, *prior_paths)), key=os.fspath))
        parent_path = _resolved_parent_path(self.parent)
        error = BuildError(
            f"{message}; parent={str(parent_path)!r} "
            f"device={self.parent.device} inode={self.parent.inode}; "
            f"generated_identity=device={self.stage_identity.device} "
            f"inode={self.stage_identity.inode} type={self.stage_identity.file_type}; "
            f"generated_state={generated.state!r}; "
            f"generated={[str(path) for path in generated.paths]!r}; "
            f"prior_identity={prior_identity_detail}; "
            f"prior_state={prior_state!r}; "
            f"prior={[str(path) for path in prior_paths]!r}; "
            f"intended_output={self.output_name!r}; "
            f"intended_transaction={self.stage_name!r}; "
            f"output_observed={None if observed is None else _identity(observed)!r}; "
            f"recovery_error={cause!r}",
            recovery_paths=paths,
            trust_anchor=self.parent.anchor,
        )
        if cause is not None:
            error.__cause__ = cause
        return error

    def verify_published_entries(self) -> None:
        if self.state is not PublicationState.PUBLISHED:
            raise BuildError("plugin output has not been published")
        _assert_entry_identity(
            self.parent.fd,
            self.output_name,
            self.stage_identity,
            label="published output",
        )
        if self.had_prior_output:
            _prior_fd, prior_identity = self._verified_prior()
            _assert_entry_identity(
                self.parent.fd,
                self.stage_name,
                prior_identity,
                label="staged prior output",
            )
        else:
            _assert_entry_absent(
                self.parent.fd,
                self.stage_name,
                label="retained transaction",
            )

    def publish(self) -> None:
        if self.state is not PublicationState.STAGED:
            raise BuildError(f"cannot publish plugin output from state {self.state.value!r}")
        _assert_parent_identity(self.parent)
        if _identity(os.fstat(self.stage_fd)) != self.stage_identity:
            raise BuildError("publication stage descriptor identity changed")
        _assert_entry_identity(
            self.parent.fd,
            self.stage_name,
            self.stage_identity,
            label="staged new output",
        )
        _validate_output_entry(self.parent.fd, self.output_name)
        if self.had_prior_output:
            _prior_fd, prior_identity = self._verified_prior()
            _assert_entry_identity(
                self.parent.fd,
                self.output_name,
                prior_identity,
                label="prior output",
            )
            self.state = PublicationState.PUBLISHING
            _atomic_exchange(self.parent.fd, self.stage_name, self.output_name)
            self.state = PublicationState.PUBLISHED
        else:
            _assert_entry_absent(
                self.parent.fd,
                self.output_name,
                label="prior output",
            )
            self.state = PublicationState.PUBLISHING
            _atomic_rename_noreplace(
                self.parent.fd,
                self.stage_name,
                self.output_name,
            )
            self.state = PublicationState.PUBLISHED

    def rollback(self) -> None:
        if self.state in {PublicationState.STAGED, PublicationState.ROLLED_BACK}:
            return
        if self.state is PublicationState.COMMITTED:
            raise BuildError("cannot roll back a committed publication")
        self.state = PublicationState.RECOVERING
        last_error: BaseException | None = None
        for _attempt in range(2):
            try:
                if self._rollback_complete():
                    self.state = PublicationState.ROLLED_BACK
                    return
                if not self._published_layout():
                    break
                if self.had_prior_output:
                    _atomic_exchange(
                        self.parent.fd,
                        self.stage_name,
                        self.output_name,
                    )
                else:
                    _atomic_rename_noreplace(
                        self.parent.fd,
                        self.output_name,
                        self.stage_name,
                    )
            except BaseException as error:
                last_error = error
        if self._rollback_complete():
            self.state = PublicationState.ROLLED_BACK
            return
        self.state = PublicationState.RECOVERY_FAILED
        raise self._diagnostic_error(
            "publication rollback incomplete after bounded identity reconciliation",
            cause=last_error,
        )

    def commit(self) -> None:
        if self.state is not PublicationState.PUBLISHED:
            raise BuildError("cannot commit an unpublished plugin output")
        self.verify_published_entries()
        _assert_parent_identity(self.parent)
        self.verify_published_entries()
        if self.had_prior_output:
            prior_fd, prior_identity = self._verified_prior()
            try:
                os.fchmod(prior_fd, 0o700)
                metadata = os.fstat(prior_fd)
                _assert_private_directory(metadata, self.stage_path)
                if _identity(metadata) != prior_identity:
                    raise BuildError("retained prior-output descriptor identity changed")
            except (OSError, BuildError) as error:
                raise BuildError(
                    f"cannot secure retained rollback {str(self.stage_path)!r}: {error}",
                    recovery_paths=(self.stage_path,),
                    trust_anchor=self.parent.anchor,
                ) from error
        self.state = PublicationState.COMMITTED


def _warn_cleanup(label: str, error: BaseException) -> None:
    with suppress(BaseException):
        print(f"warning: could not close {label}: {error}", file=sys.stderr)


def _close_or_warn(close: Callable[[], None], *, label: str) -> None:
    try:
        close()
    except BaseException as error:
        _warn_cleanup(label, error)


def build(
    *,
    requested_output: Path | None,
    allow_dirty: bool,
) -> BuildResult:
    """Build and descriptor-safely publish one validated-name plugin directory."""
    _require_descriptor_primitives()
    repository = _repository_root()
    requested = requested_output or repository / "packaging" / "out" / "failure-memory"
    output = _validated_output(repository, requested)

    publication: PublicationTransaction | None = None
    trusted_repository = _open_trusted_directory(repository, create=False)
    repository_fd = trusted_repository.fd
    try:
        snapshot = _source_snapshot(
            repository,
            repository_fd,
            allow_dirty=allow_dirty,
        )
        if snapshot.live:
            _verify_source_state(repository, repository_fd, snapshot)

        parent = _open_parent(output.parent)
        pinned_output: PinnedOutput | None = None
        try:
            _assert_parent_identity(parent)
            _assert_no_unresolved_artifacts(parent, output.name)
            pinned_output = _pin_output(parent.fd, output.name)
            stage_name, stage_fd, stage_identity = _create_stage(
                parent,
                output.name,
                snapshot.commit,
            )
            try:
                publication = PublicationTransaction(
                    parent=parent,
                    stage_name=stage_name,
                    stage_fd=stage_fd,
                    stage_identity=stage_identity,
                    output_name=output.name,
                    had_prior_output=pinned_output.existed,
                    prior_fd=pinned_output.fd,
                    prior_identity=pinned_output.identity,
                )
                _stage_projection(stage_fd, snapshot)
                _verify_source_state(repository, repository_fd, snapshot)
                _assert_trusted_directory_identity(
                    trusted_repository,
                    label="repository",
                )
                _assert_parent_identity(parent)
                publication.publish()
                _assert_trusted_directory_identity(
                    trusted_repository,
                    label="repository",
                )
                _assert_parent_identity(parent)
                publication.verify_published_entries()
                publication.commit()
            except BaseException as original:
                if publication is None:
                    try:
                        raw_locations = _identity_locations(
                            parent,
                            stage_identity,
                            stage_fd,
                        )
                    except BaseException as recovery_error:
                        raise BuildError(
                            "publication transaction construction failed and retained "
                            "generated output could not be resolved; "
                            f"original_error={original!r}; recovery_error={recovery_error!r}",
                            trust_anchor=parent.anchor,
                        ) from original
                    if len(raw_locations.paths) != 1:
                        raise BuildError(
                            "publication transaction construction failed and retained "
                            "generated output has no single verified recovery path; "
                            f"state={raw_locations.state!r}; "
                            f"paths={[str(path) for path in raw_locations.paths]!r}; "
                            f"original_error={original!r}",
                            recovery_paths=raw_locations.paths,
                            trust_anchor=parent.anchor,
                        ) from original
                    raise _retained_failure(
                        original,
                        raw_locations.paths[0],
                        trust_anchor=parent.anchor,
                    ) from original
                if publication.state in {
                    PublicationState.PUBLISHING,
                    PublicationState.PUBLISHED,
                    PublicationState.RECOVERING,
                }:
                    try:
                        publication.rollback()
                    except BaseException as rollback_error:
                        if not isinstance(rollback_error, BuildError):
                            rollback_error = publication._diagnostic_error(
                                "publication rollback raised unexpectedly",
                                cause=rollback_error,
                            )
                        raise BuildError(
                            f"{rollback_error}; original_error={original!r}",
                            recovery_paths=rollback_error.recovery_paths,
                            trust_anchor=rollback_error.trust_anchor,
                        ) from original
                recovery_path = publication._single_location(
                    stage_identity,
                    stage_fd,
                )
                if recovery_path is None:
                    raise publication._diagnostic_error(
                        "cannot locate retained generated output after failure",
                        cause=original,
                    ) from original
                raise _retained_failure(
                    original,
                    recovery_path,
                    trust_anchor=parent.anchor,
                ) from original
            finally:
                _close_or_warn(
                    lambda: os.close(stage_fd),
                    label="published output descriptor",
                )
            if publication is None:
                raise AssertionError("publication transaction was not constructed")
            return BuildResult(
                output=output,
                rollback=publication.stage_path if publication.had_prior_output else None,
                recovery=None,
            )
        finally:
            if pinned_output is not None:
                _close_or_warn(
                    pinned_output.close,
                    label=(
                        "prior output descriptor"
                        if pinned_output.existed
                        else "initial output descriptor"
                    ),
                )
            _close_or_warn(
                lambda: os.close(parent.fd),
                label="output parent descriptor",
            )
    except (BuildError, OSError) as error:
        if not isinstance(error, BuildError) or error.trust_anchor is None:
            raise BuildError(
                str(error),
                recovery_paths=(error.recovery_paths if isinstance(error, BuildError) else ()),
                trust_anchor=trusted_repository.anchor,
            ) from error
        raise
    finally:
        _close_or_warn(
            lambda: os.close(repository_fd),
            label="repository descriptor",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the Failure Memory Codex plugin bundle.",
        epilog=(
            "The builder performs no deletion. Unresolved rollback/recovery artifacts "
            "block later builds and require inspection before a release operator "
            "moves them to Trash. Concurrent mutation of the private transaction tree "
            "by a hostile process running as the same user is outside the security boundary."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory, named failure-memory or failure-memory.new.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Snapshot nonignored live files and permit a dirty source tree.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = build(
            requested_output=arguments.output,
            allow_dirty=arguments.allow_dirty,
        )
    except (BuildError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        if isinstance(error, BuildError) and (
            error.recovery_paths or error.trust_anchor is not None
        ):
            detail: dict[str, object] = {}
            if error.recovery_paths:
                detail["recovery"] = [str(path) for path in error.recovery_paths]
            if error.trust_anchor is not None:
                detail["trust_anchor"] = str(error.trust_anchor)
            print(
                json.dumps(
                    {"failure_memory_builder": detail},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        return 2
    print(result.output)
    detail = {}
    if result.rollback is not None:
        detail["rollback"] = str(result.rollback)
    if result.recovery is not None:
        detail["recovery"] = str(result.recovery)
    if detail:
        print(
            json.dumps(
                {"failure_memory_builder": detail},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
