from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]


def _installer_module() -> object:
    path = REPOSITORY / "scripts" / "install_harness.py"
    spec = importlib.util.spec_from_file_location("failure_memory_install_harness_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_host_manifests_share_one_identity_version_skills_and_mcp() -> None:
    manifests = [
        REPOSITORY / ".codex-plugin" / "plugin.json",
        REPOSITORY / ".claude-plugin" / "plugin.json",
        REPOSITORY / ".plugin" / "plugin.json",
        REPOSITORY / ".cursor-plugin" / "plugin.json",
    ]
    values = [json.loads(path.read_text(encoding="utf-8")) for path in manifests]

    assert {value["name"] for value in values} == {"failure-memory"}
    assert {value["version"] for value in values} == {"0.4.0"}
    assert {value["skills"] for value in values} == {"./skills/"}
    assert {value["mcpServers"] for value in values} == {"./.mcp.json"}
    assert {value["repository"] for value in values} == {
        "https://github.com/CongBao/failure-memory"
    }
    assert {value["homepage"] for value in values} == {
        "https://github.com/CongBao/failure-memory#readme"
    }
    assert "FAILURE_MEMORY_HARNESS" not in (REPOSITORY / ".mcp.json").read_text(encoding="utf-8")


def test_public_marketplaces_resolve_the_root_plugin() -> None:
    codex = json.loads(
        (REPOSITORY / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    claude = json.loads(
        (REPOSITORY / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    copilot = json.loads(
        (REPOSITORY / ".github" / "plugin" / "marketplace.json").read_text(encoding="utf-8")
    )

    assert codex["name"] == "failure-memory"
    assert codex["plugins"][0]["name"] == "failure-memory"
    assert codex["plugins"][0]["source"] == {
        "source": "url",
        "url": "https://github.com/CongBao/failure-memory.git",
        "ref": "main",
    }

    for marketplace in (claude, copilot):
        assert marketplace["name"] == "failure-memory"
        assert marketplace["plugins"][0]["name"] == "failure-memory"
        assert marketplace["plugins"][0]["source"] == {
            "source": "github",
            "repo": "CongBao/failure-memory",
            "ref": "main",
        }


def test_readme_documents_supported_host_install_paths() -> None:
    readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")

    expected = (
        "codex plugin marketplace add CongBao/failure-memory",
        "codex plugin add failure-memory@failure-memory",
        "claude plugin marketplace add CongBao/failure-memory",
        "claude plugin install failure-memory@failure-memory",
        "copilot plugin install CongBao/failure-memory",
        "~/.cursor/plugins/local/failure-memory",
    )
    assert all(command in readme for command in expected)


@pytest.mark.parametrize("harness", ["codex", "claude-code", "copilot", "cursor"])
def test_session_hook_emits_bounded_guidance_without_creating_state(
    tmp_path: Path, harness: str
) -> None:
    script = REPOSITORY / "scripts" / "failure_memory_hook.py"
    environment = {**os.environ, "HOME": str(tmp_path)}
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--harness",
            harness,
            "--event",
            "session-start",
        ],
        input=json.dumps({"session_id": "test", "prompt": "must not persist"}),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert len(completed.stdout) < 1_500
    assert "must not persist" not in completed.stdout
    assert "Failure Memory" in completed.stdout
    assert list(tmp_path.rglob("*")) == []
    assert isinstance(payload, dict)


def test_installer_detects_stable_versions_and_duplicate_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    outputs = {
        "codex": (
            "PLUGIN  STATUS  VERSION  PATH\n"
            "failure-memory@personal  installed, enabled  0.4.0+codex.1  /plugin\n"
        ),
        "copilot": (
            "Installed plugins:\n  • failure-memory (v0.4.0)\n  • failure-memory@other (v0.3.0)\n"
        ),
    }

    monkeypatch.setattr(
        installer,
        "_run",
        lambda executable, *_arguments: outputs[
            "codex" if executable.endswith("codex") else "copilot"
        ],
    )

    assert installer._installed_versions("codex", "/usr/bin/codex") == ("0.4.0+codex.1",)
    assert installer._installed_versions("copilot", "/usr/bin/copilot") == (
        "0.4.0",
        "0.3.0",
    )


def test_installer_reinstalls_changed_copilot_content_at_same_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    state = installer.HostState(
        target="copilot",
        executable="/usr/bin/copilot",
        installed_versions=("0.4.0",),
        desired_version="0.4.0",
        installed_build_commit="old-commit",
        desired_build_commit="new-commit",
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        installer,
        "_run",
        lambda executable, *arguments: calls.append((executable, *arguments)) or "",
    )

    assert state.action == "update"
    assert state.same_version_content_update is True
    installer._apply(tmp_path, state)
    assert calls == [
        ("/usr/bin/copilot", "plugin", "uninstall", "failure-memory"),
        ("/usr/bin/copilot", "plugin", "install", str(tmp_path)),
    ]
