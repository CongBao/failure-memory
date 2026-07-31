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
    assert {value["version"] for value in values} == {"0.6.0"}
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


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_prompt_hook_injects_failure_check_without_echoing_or_persisting_prompt(
    tmp_path: Path,
    harness: str,
) -> None:
    script = REPOSITORY / "scripts" / "failure_memory_hook.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--harness",
            harness,
            "--event",
            "user-prompt-submit",
        ],
        input=json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "private correction must never be echoed",
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "HOME": str(tmp_path)},
    )

    assert completed.returncode == 0
    assert "private correction" not in completed.stdout
    assert "requirement" in completed.stdout
    assert "root cause" in completed.stdout
    assert list(tmp_path.rglob("*")) == []
    assert json.loads(completed.stdout)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_prompt_context_hooks_are_enabled_only_where_host_output_is_model_visible() -> None:
    codex = json.loads((REPOSITORY / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    claude = json.loads((REPOSITORY / "hooks" / "claude-hooks.json").read_text(encoding="utf-8"))
    copilot = json.loads((REPOSITORY / "hooks" / "copilot-hooks.json").read_text(encoding="utf-8"))
    cursor = json.loads((REPOSITORY / "hooks" / "cursor-hooks.json").read_text(encoding="utf-8"))

    assert "UserPromptSubmit" in codex["hooks"]
    assert "UserPromptSubmit" in claude["hooks"]
    assert set(copilot["hooks"]) == {"sessionStart"}
    copilot_hook = copilot["hooks"]["sessionStart"][0]
    assert "${PLUGIN_ROOT}" in copilot_hook["bash"]
    assert "${PLUGIN_ROOT}" in copilot_hook["powershell"]
    assert "CLAUDE_PLUGIN_ROOT" not in json.dumps(copilot_hook)
    assert set(cursor["hooks"]) == {"sessionStart"}


def test_installer_detects_stable_versions_and_duplicate_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    outputs = {
        "codex": json.dumps(
            {
                "installed": [
                    {
                        "pluginId": "failure-memory@personal",
                        "name": "failure-memory",
                        "version": "0.4.0+codex.1",
                    }
                ],
                "available": [],
            }
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
    assert installer._installed_identities("codex", "/usr/bin/codex") == (
        "failure-memory@personal",
    )
    assert installer._installed_versions("copilot", "/usr/bin/copilot") == (
        "0.4.0",
        "0.3.0",
    )


def test_installer_supports_all_published_harness_projections() -> None:
    installer = _installer_module()

    assert installer.SUPPORTED_TARGETS == (
        "codex",
        "claude-code",
        "copilot",
        "cursor",
    )


def test_installer_blocks_omitted_outdated_projection_for_shared_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    outdated = installer.HostState(
        target="copilot",
        executable="/usr/bin/copilot",
        installed_versions=("0.5.0",),
        desired_version="0.6.0",
        installed_build_commit="old",
        desired_build_commit="new",
    )
    monkeypatch.setattr(installer, "_installed_states", lambda _bundle: (outdated,))

    with pytest.raises(RuntimeError, match="shared-store version skew"):
        installer._enforce_shared_store_version_safety(REPOSITORY, {"codex"})

    installer._enforce_shared_store_version_safety(REPOSITORY, {"codex", "copilot"})


def test_installer_accepts_a_codex_cachebuster_for_the_same_shared_schema() -> None:
    installer = _installer_module()
    current = installer.HostState(
        target="codex",
        executable="/usr/bin/codex",
        installed_versions=("0.6.0+codex.local-20260731-120000",),
        desired_version="0.6.0",
        installed_build_commit=None,
        desired_build_commit=None,
    )
    outdated = installer.HostState(
        target="codex",
        executable="/usr/bin/codex",
        installed_versions=("0.5.0+codex.local-20260731-120000",),
        desired_version="0.6.0",
        installed_build_commit=None,
        desired_build_commit=None,
    )

    assert current.installed_matches_desired is True
    assert current.action == "noop"
    assert outdated.installed_matches_desired is False
    assert outdated.action == "update"


def test_installer_reads_claude_plugin_list_json_without_collapsing_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    output = json.dumps(
        [
            {
                "id": "failure-memory@failure-memory",
                "version": "0.5.0",
                "scope": "user",
            },
            {
                "id": "failure-memory@other-marketplace",
                "version": "0.4.0",
                "scope": "local",
            },
            {"id": "unrelated@marketplace", "version": "1.0.0", "scope": "user"},
        ]
    )
    monkeypatch.setattr(installer, "_run", lambda _executable, *_arguments: output)

    assert installer._installed_versions("claude-code", "/usr/bin/claude") == (
        "0.5.0",
        "0.4.0",
    )


def test_installer_creates_one_idempotent_cursor_local_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    destination = tmp_path / ".cursor" / "plugins" / "local" / "failure-memory"
    monkeypatch.setattr(installer, "_cursor_install_root", lambda: destination)

    before = installer._state(REPOSITORY, "cursor")
    assert before.action == "install"

    installer._apply(REPOSITORY, before)

    assert destination.is_symlink()
    assert destination.resolve() == REPOSITORY
    after = installer._state(REPOSITORY, "cursor")
    assert after.action == "noop"
    assert after.installed_versions == (before.desired_version,)
    assert list(destination.parent.glob("failure-memory*")) == [destination]


def test_installer_uses_claude_marketplace_identity_for_install_and_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        installer,
        "_run",
        lambda executable, *arguments: calls.append((executable, *arguments)) or "",
    )
    install = installer.HostState(
        target="claude-code",
        executable="/usr/bin/claude",
        installed_versions=(),
        desired_version="0.5.0",
        installed_build_commit=None,
        desired_build_commit="new-commit",
        marketplace_present=False,
    )

    installer._apply(tmp_path, install)

    assert calls == [
        (
            "/usr/bin/claude",
            "plugin",
            "marketplace",
            "add",
            str(tmp_path),
        ),
        (
            "/usr/bin/claude",
            "plugin",
            "install",
            "failure-memory@failure-memory",
            "--scope",
            "user",
        ),
    ]
    calls.clear()
    update = installer.HostState(
        target="claude-code",
        executable="/usr/bin/claude",
        installed_versions=("0.4.0",),
        desired_version="0.5.0",
        installed_build_commit=None,
        desired_build_commit="new-commit",
        marketplace_present=True,
    )

    installer._apply(tmp_path, update)

    assert calls == [
        (
            "/usr/bin/claude",
            "plugin",
            "marketplace",
            "update",
            "failure-memory",
        ),
        (
            "/usr/bin/claude",
            "plugin",
            "update",
            "failure-memory@failure-memory",
            "--scope",
            "user",
        ),
    ]


def test_installer_updates_the_existing_codex_marketplace_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        installer,
        "_run",
        lambda executable, *arguments: calls.append((executable, *arguments)) or "",
    )
    state = installer.HostState(
        target="codex",
        executable="/usr/bin/codex",
        installed_versions=("0.4.0+codex.1",),
        desired_version="0.5.0+codex.2",
        installed_build_commit=None,
        desired_build_commit="new-commit",
        marketplace_present=True,
        installed_identities=("failure-memory@failure-memory",),
    )

    installer._apply(tmp_path, state)

    assert calls == [
        (
            "/usr/bin/codex",
            "plugin",
            "marketplace",
            "update",
            "failure-memory",
        ),
        (
            "/usr/bin/codex",
            "plugin",
            "remove",
            "failure-memory@failure-memory",
        ),
        (
            "/usr/bin/codex",
            "plugin",
            "add",
            "failure-memory@failure-memory",
        ),
    ]


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
