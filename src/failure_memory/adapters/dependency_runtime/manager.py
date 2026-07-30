from __future__ import annotations

import hashlib
import json
import os
import platform
import site
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from failure_memory.adapters.storage_permissions import (
    ensure_private_file,
    ensure_private_tree,
    read_private_file,
)
from failure_memory.application.errors import AdapterSetupError

SQLITE_VEC_VERSION = "0.1.9"
FASTEMBED_VERSION = "0.8.0"
TRUSTSTORE_VERSION = "0.10.4"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_REVISION = f"fastembed-{FASTEMBED_VERSION}"
DEFAULT_EMBEDDING_DIMENSIONS = 384
_REQUIREMENTS = (
    f"truststore=={TRUSTSTORE_VERSION}",
    f"sqlite-vec=={SQLITE_VEC_VERSION}",
    f"fastembed=={FASTEMBED_VERSION}",
)
_Runner = Callable[..., subprocess.CompletedProcess[str]]


class AdapterRuntimeManager:
    """Plan, install, validate, and activate optional retrieval dependencies."""

    def __init__(
        self,
        data_root: Path,
        *,
        runner: _Runner = subprocess.run,
    ) -> None:
        self.data_root = data_root
        self._runner = runner

    @property
    def lock_hash(self) -> str:
        payload = "\n".join(
            (
                *_REQUIREMENTS,
                f"python={sys.version_info.major}.{sys.version_info.minor}",
                f"platform={platform.system()}-{platform.machine()}",
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    @property
    def runtime_root(self) -> Path:
        return self.data_root / "adapters" / "runtime" / self.lock_hash

    @property
    def environment_root(self) -> Path:
        return self.runtime_root / "venv"

    @property
    def ready_marker(self) -> Path:
        return self.runtime_root / "ready.json"

    @property
    def python_executable(self) -> Path:
        """Return the interpreter owned by this immutable adapter runtime."""
        return self._environment_python()

    @property
    def model_root(self) -> Path:
        safe_model = DEFAULT_EMBEDDING_MODEL.replace("/", "--")
        return (
            self.data_root
            / "adapters"
            / "embedding"
            / "fastembed"
            / f"{safe_model}-{DEFAULT_EMBEDDING_REVISION}"
        )

    def plan(self) -> Mapping[str, object]:
        return {
            "adapter": "sqlite-vec-fastembed",
            "runtime_root": str(self.runtime_root),
            "model_root": str(self.model_root),
            "requirements": list(_REQUIREMENTS),
            "embedding_model": DEFAULT_EMBEDDING_MODEL,
            "embedding_revision": DEFAULT_EMBEDDING_REVISION,
            "embedding_dimensions": DEFAULT_EMBEDDING_DIMENSIONS,
            "downloads_required": True,
            "automatic_install": False,
        }

    def status(self) -> Mapping[str, object]:
        marker = self._read_ready_marker()
        model_ready = self.model_root.is_dir() and any(self.model_root.iterdir())
        ready = marker is not None and self._site_packages().is_dir() and model_ready
        return {
            **self.plan(),
            "state": "ready" if ready else "not_installed",
            "ready": ready,
            "model_ready": model_ready,
        }

    def install(self) -> Mapping[str, object]:
        runtime_root = ensure_private_tree(
            self.data_root,
            "adapters",
            "runtime",
            self.lock_hash,
        )
        ensure_private_tree(
            self.data_root,
            "adapters",
            "embedding",
            "fastembed",
            f"{DEFAULT_EMBEDDING_MODEL.replace('/', '--')}-{DEFAULT_EMBEDDING_REVISION}",
        )
        if self.status()["ready"] is True:
            return self.status()
        self._run(
            [sys.executable, "-m", "venv", str(self.environment_root)],
            "could not create the private adapter environment",
        )
        python = self._environment_python()
        self._run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *_REQUIREMENTS,
            ],
            "could not install optional retrieval dependencies",
        )
        self._run(
            [
                str(python),
                "-c",
                (
                    "import truststore;"
                    "truststore.inject_into_ssl();"
                    "from fastembed import TextEmbedding;"
                    "import sqlite3, sqlite_vec;"
                    f"TextEmbedding(model_name={DEFAULT_EMBEDDING_MODEL!r},"
                    f" cache_dir={str(self.model_root)!r});"
                    "c=sqlite3.connect(':memory:');"
                    "c.enable_load_extension(True);sqlite_vec.load(c);"
                    "c.execute('select vec_version()').fetchone()"
                ),
            ],
            "optional retrieval dependencies did not validate",
            environment={
                **os.environ,
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HUB_DISABLE_XET": "1",
            },
        )
        marker: dict[str, Any] = {
            "schema_version": 1,
            "lock_hash": self.lock_hash,
            "requirements": list(_REQUIREMENTS),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "model": DEFAULT_EMBEDDING_MODEL,
            "revision": DEFAULT_EMBEDDING_REVISION,
            "dimensions": DEFAULT_EMBEDDING_DIMENSIONS,
        }
        temporary = runtime_root / f".ready-{os.getpid()}.json"
        temporary.write_text(
            json.dumps(marker, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        ensure_private_file(temporary)
        os.replace(temporary, self.ready_marker)
        ensure_private_file(self.ready_marker)
        return self.status()

    def activate(self) -> bool:
        status = self.status()
        site_packages = self._site_packages()
        if status["ready"] is not True or not site_packages.is_dir():
            return False
        site.addsitedir(str(site_packages))
        return True

    def ready_python_executable(self) -> Path | None:
        """Return the validated runtime interpreter without installing anything."""
        python = self.python_executable
        if self.status()["ready"] is not True or not python.is_file():
            return None
        return python

    def _read_ready_marker(self) -> Mapping[str, object] | None:
        if not ensure_private_file(self.ready_marker, required=False):
            return None
        try:
            value = json.loads(read_private_file(self.ready_marker).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        if value.get("lock_hash") != self.lock_hash:
            return None
        if value.get("requirements") != list(_REQUIREMENTS):
            return None
        return value

    def _site_packages(self) -> Path:
        if os.name == "nt":
            return self.environment_root / "Lib" / "site-packages"
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        return self.environment_root / "lib" / version / "site-packages"

    def _environment_python(self) -> Path:
        if os.name == "nt":
            return self.environment_root / "Scripts" / "python.exe"
        return self.environment_root / "bin" / "python"

    def _run(
        self,
        command: Sequence[str],
        error_message: str,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        kwargs: dict[str, object] = {
            "check": False,
            "capture_output": True,
            "text": True,
        }
        if environment is not None:
            kwargs["env"] = dict(environment)
        completed = self._runner(list(command), **kwargs)
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            suffix = "" if not detail else f": {detail[-1]}"
            raise AdapterSetupError(f"{error_message}{suffix}")
