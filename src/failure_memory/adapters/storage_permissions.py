from __future__ import annotations

import errno
import os
import stat
from contextlib import suppress
from pathlib import Path

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_IS_WINDOWS = os.name == "nt"


def absolute_path(path: Path) -> Path:
    """Normalize dots and user expansion without following the owned leaf's symlinks."""
    return Path(os.path.abspath(path.expanduser()))


def ensure_private_directory(path: Path, *, create_parents: bool = False) -> Path:
    """Create or harden one Failure Memory-owned directory without chmodding ancestors."""
    owned = absolute_path(path)
    if create_parents:
        owned.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata = owned.lstat()
    except FileNotFoundError:
        with suppress(FileExistsError):
            os.mkdir(owned, _PRIVATE_DIRECTORY_MODE)
        metadata = owned.lstat()
    _reject_symlink(metadata, owned)
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "owned path is not a directory", owned)
    _assert_current_owner(metadata, owned)
    if _IS_WINDOWS:
        return owned

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(owned, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise OSError(errno.ESTALE, "owned directory identity changed", owned)
        _assert_current_owner(opened, owned)
        if not _IS_WINDOWS:
            os.fchmod(descriptor, _PRIVATE_DIRECTORY_MODE)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != _PRIVATE_DIRECTORY_MODE:
                raise PermissionError(
                    errno.EACCES,
                    "could not enforce owner-only directory permissions",
                    owned,
                )
    finally:
        os.close(descriptor)
    return owned


def ensure_private_tree(root: Path, *relative_directories: str) -> Path:
    """Harden the owned root and each named descendant, leaving its parent untouched."""
    current = ensure_private_directory(root, create_parents=True)
    for component in relative_directories:
        if component in {"", ".", ".."} or Path(component).name != component:
            raise ValueError(f"invalid owned directory component: {component!r}")
        current = ensure_private_directory(current / component)
    return current


def ensure_private_file(path: Path, *, required: bool = True) -> bool:
    """Reject symlinks/non-files and enforce owner-only mode on one owned file."""
    owned = absolute_path(path)
    try:
        metadata = owned.lstat()
    except FileNotFoundError:
        if required:
            raise
        return False
    _reject_symlink(metadata, owned)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(errno.EINVAL, "owned path is not a regular file", owned)
    _assert_current_owner(metadata, owned)

    descriptor = os.open(owned, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise OSError(errno.ESTALE, "owned file identity changed", owned)
        _assert_current_owner(opened, owned)
        if not _IS_WINDOWS:
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != _PRIVATE_FILE_MODE:
                raise PermissionError(
                    errno.EACCES,
                    "could not enforce owner-only file permissions",
                    owned,
                )
    finally:
        os.close(descriptor)
    return True


def read_private_file(path: Path) -> bytes:
    """Read one already-owned private file through a no-follow descriptor."""
    ensure_private_file(path)
    descriptor = os.open(
        absolute_path(path),
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _reject_symlink(metadata: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise OSError(errno.ELOOP, "owned path must not be a symbolic link", path)


def _assert_current_owner(metadata: os.stat_result, path: Path) -> None:
    if not _IS_WINDOWS and metadata.st_uid != os.geteuid():
        raise PermissionError(errno.EACCES, "owned path belongs to another user", path)
