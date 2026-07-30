from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from failure_memory.adapters.dependency_runtime.manager import AdapterRuntimeManager


def test_runtime_plan_and_status_do_not_create_or_download_anything(tmp_path: Path) -> None:
    data_root = tmp_path / "failure-memory"
    manager = AdapterRuntimeManager(data_root)

    plan = manager.plan()
    status = manager.status()

    assert plan["automatic_install"] is False
    assert plan["requirements"] == [
        "truststore==0.10.4",
        "sqlite-vec==0.1.9",
        "fastembed==0.8.0",
    ]
    assert status["ready"] is False
    assert manager.ready_python_executable() is None
    assert not data_root.exists()


def test_explicit_install_uses_private_adapter_paths_and_a_validated_marker(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "failure-memory"
    commands: list[list[str]] = []
    manager: AdapterRuntimeManager

    def runner(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert capture_output is True
        assert text is True
        rendered = list(command)
        commands.append(rendered)
        if rendered[1:3] == ["-m", "venv"]:
            if os.name == "nt":
                site_packages = Path(rendered[3]) / "Lib" / "site-packages"
                runtime_python = Path(rendered[3]) / "Scripts" / "python.exe"
            else:
                version = f"python{sys.version_info.major}.{sys.version_info.minor}"
                site_packages = Path(rendered[3]) / "lib" / version / "site-packages"
                runtime_python = Path(rendered[3]) / "bin" / "python"
            site_packages.mkdir(parents=True)
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_bytes(b"validated-runtime")
        if "-c" in rendered:
            assert env is not None
            assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
            assert env["HF_HUB_DISABLE_XET"] == "1"
            manager.model_root.mkdir(parents=True, exist_ok=True)
            (manager.model_root / "model.onnx").write_bytes(b"validated")
        return subprocess.CompletedProcess(rendered, 0, "", "")

    manager = AdapterRuntimeManager(data_root, runner=runner)

    status = manager.install()

    assert status["ready"] is True
    assert len(commands) == 3
    assert commands[1][-3:] == [
        "truststore==0.10.4",
        "sqlite-vec==0.1.9",
        "fastembed==0.8.0",
    ]
    assert manager.ready_marker.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(manager.ready_marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(manager.runtime_root.stat().st_mode) == 0o700
    assert manager.activate() is True
    assert manager.ready_python_executable() == manager.python_executable
