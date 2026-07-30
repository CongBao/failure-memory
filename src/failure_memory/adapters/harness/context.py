from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import secrets
import subprocess
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from failure_memory.adapters.storage_permissions import (
    absolute_path,
    ensure_private_file,
    ensure_private_tree,
    read_private_file,
)


def resolve_data_root(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the owner-private global store, independent of an agent harness."""
    values = os.environ if env is None else env
    if values.get("FAILURE_MEMORY_HOME"):
        return absolute_path(Path(values["FAILURE_MEMORY_HOME"]))
    user_home = Path(values.get("HOME", Path.home()))
    if os.name == "nt":
        base = Path(values.get("LOCALAPPDATA", user_home / "AppData" / "Local"))
        return base / "FailureMemory"
    if sys.platform == "darwin":
        return user_home / "Library" / "Application Support" / "failure-memory"
    base = Path(values.get("XDG_DATA_HOME", user_home / ".local" / "share"))
    return base / "failure-memory"


def _restrict_key_permissions(path: Path, *, is_windows: bool | None = None) -> None:
    if is_windows is None:
        is_windows = os.name == "nt"
    if is_windows:
        ensure_private_file(path)
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(R,W)"],
            check=True,
            capture_output=True,
            text=True,
        )
        return
    ensure_private_file(path)


def _write_all(descriptor: int, value: bytes) -> None:
    remaining = memoryview(value)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("failed to write complete identity key")
        remaining = remaining[written:]


def _identity_key(data_root: Path) -> bytes:
    path = data_root / "bootstrap" / "identity.key"
    ensure_private_tree(data_root, "bootstrap")
    exists = ensure_private_file(path, required=False)
    if not exists:
        candidate = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                _write_all(descriptor, os.urandom(32))
                _restrict_key_permissions(candidate)
            finally:
                os.close(descriptor)
            with suppress(FileExistsError):
                os.link(candidate, path)
        finally:
            candidate.unlink(missing_ok=True)
    _restrict_key_permissions(path)
    value = read_private_file(path)
    if len(value) != 32:
        raise ValueError(f"invalid identity key at {path}")
    return value


def _fingerprint(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class OriginContext:
    """Describe event provenance without defining retrieval visibility."""

    harness: str
    data_root: Path
    workspace_fingerprint: str
    session_fingerprint: str | None

    def fingerprint(self, value: str) -> str:
        """Create a stable, local-only HMAC without retaining the source value."""
        return _fingerprint(_identity_key(self.data_root), value)

    @classmethod
    def create(
        cls,
        data_root: Path,
        cwd: Path,
        harness: str,
        session_id: str | None,
    ) -> OriginContext:
        root = ensure_private_tree(data_root)
        key = _identity_key(root)
        return cls(
            harness=harness,
            data_root=root,
            workspace_fingerprint=_fingerprint(key, str(cwd.resolve())),
            session_fingerprint=None if session_id is None else _fingerprint(key, session_id),
        )


# Compatibility alias for adapters compiled against the 0.1/0.2 public surface.
HarnessContext = OriginContext
