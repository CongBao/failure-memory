from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPOSITORY_ROOT / "packaging" / "build_codex.py"
VALIDATOR = (
    Path.home()
    / ".codex"
    / "skills"
    / ".system"
    / "plugin-creator"
    / "scripts"
    / "validate_plugin.py"
)


def _set_permissive_umask() -> None:
    os.umask(0)


def _run_builder(
    output: Path, *, allow_dirty: bool, check: bool = True
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(BUILDER), "--output", str(output)]
    if allow_dirty:
        command.append("--allow-dirty")
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT, text=True
    ).strip()


def _bundle_paths(bundle: Path) -> set[str]:
    return {path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file()}


def _tree_state(root: Path) -> dict[str, tuple[str, int, str | None]]:
    state: dict[str, tuple[str, int, str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.stat(follow_symlinks=False).st_mode & 0o777
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[relative] = ("file" if path.is_file() else "directory", mode, digest)
    return state


def test_bundle_contains_only_installable_runtime_projection(tmp_path: Path) -> None:
    output = tmp_path / "bundle" / "failure-memory"

    _run_builder(output, allow_dirty=True)

    paths = _bundle_paths(output)
    required = {
        ".codex-plugin/plugin.json",
        ".claude-plugin/plugin.json",
        ".cursor-plugin/plugin.json",
        ".plugin/plugin.json",
        ".mcp.json",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "evals/v0.5-core.json",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "scripts/failure_memory_cli.py",
        "scripts/failure_memory_mcp.py",
        "scripts/failure_memory_hook.py",
        "scripts/install_harness.py",
        "skills/record-agent-failure/SKILL.md",
        "skills/record-agent-failure/contract.json",
        "skills/record-agent-failure/scripts/failure_memory_cli.py",
        "skills/recall-failure-lessons/SKILL.md",
        "skills/recall-failure-lessons/contract.json",
        "skills/recall-failure-lessons/scripts/failure_memory_cli.py",
        "src/failure_memory/bootstrap/server.py",
        ("src/failure_memory/adapters/event_store/sqlite/migrations/0001_initial.sql"),
        "build-manifest.json",
    }
    assert required <= paths
    assert not any(path == ".git" or path.startswith(".git/") for path in paths)
    assert not any(path == ".venv" or path.startswith(".venv/") for path in paths)
    assert not any(path.startswith("tests/") for path in paths)
    assert not any(path.startswith("packaging/out/") for path in paths)
    assert not any(
        path.endswith((".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm")) for path in paths
    )
    readme = (output / "README.md").read_text(encoding="utf-8")
    relative_links = [
        target.split("#", 1)[0]
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme)
        if target and "://" not in target and not target.startswith("#")
    ]
    assert relative_links
    assert all((output / target).is_file() for target in relative_links)

    manifest = json.loads((output / "build-manifest.json").read_text())
    assert manifest["commit"] == _head()
    assert manifest["dirty"] is True
    source_plugin = json.loads((REPOSITORY_ROOT / ".codex-plugin/plugin.json").read_text())
    assert manifest["version"] == source_plugin["version"]
    assert datetime.fromisoformat(manifest["built_at"].replace("Z", "+00:00")).tzinfo

    inventory = manifest["files"]
    expected_hashed_paths = paths - {"build-manifest.json"}
    assert set(inventory) == expected_hashed_paths
    for relative in expected_hashed_paths:
        entry = inventory[relative]
        executable = relative in {
            "scripts/failure_memory_cli.py",
            "scripts/failure_memory_hook.py",
            "scripts/failure_memory_mcp.py",
            "scripts/install_harness.py",
            "skills/recall-failure-lessons/scripts/failure_memory_cli.py",
            "skills/record-agent-failure/scripts/failure_memory_cli.py",
        }
        assert entry == {
            "sha256": hashlib.sha256((output / relative).read_bytes()).hexdigest(),
            "mode": "0755" if executable else "0644",
        }

    for path in output.rglob("*"):
        expected_mode = 0o755 if path.is_dir() else 0o644
        if path.relative_to(output).as_posix() in {
            "scripts/failure_memory_cli.py",
            "scripts/failure_memory_hook.py",
            "scripts/failure_memory_mcp.py",
            "scripts/install_harness.py",
            "skills/recall-failure-lessons/scripts/failure_memory_cli.py",
            "skills/record-agent-failure/scripts/failure_memory_cli.py",
        }:
            expected_mode = 0o755
        assert path.stat(follow_symlinks=False).st_mode & 0o777 == expected_mode
        assert path.stat(follow_symlinks=False).st_uid == os.getuid()

    mcp_config = json.loads((output / ".mcp.json").read_text())
    server = mcp_config["mcpServers"]["failure-memory"]
    assert server["command"] == "python3"
    assert server["args"] == ["scripts/failure_memory_mcp.py"]
    assert server["cwd"] == "."
    assert server["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}


def test_dirty_tree_requires_opt_in_and_includes_only_nonignored_untracked_files(
    tmp_path: Path,
) -> None:
    source_probe = (
        REPOSITORY_ROOT / "skills" / "record-agent-failure" / "packaging-untracked-probe.txt"
    )
    ignored_probe = REPOSITORY_ROOT / ".runtime" / "packaging-probe.sqlite"
    source_probe.write_text("include this source probe\n")
    ignored_probe.parent.mkdir(exist_ok=True)
    ignored_probe.write_text("do not package runtime state\n")
    try:
        rejected = _run_builder(
            tmp_path / "reject" / "failure-memory", allow_dirty=False, check=False
        )
        assert rejected.returncode != 0
        assert "dirty" in rejected.stderr.lower()

        output = tmp_path / "allowed" / "failure-memory"
        _run_builder(output, allow_dirty=True)
        assert (
            output / "skills" / "record-agent-failure" / "packaging-untracked-probe.txt"
        ).read_text() == "include this source probe\n"
        assert not (output / ".runtime").exists()
        assert json.loads((output / "build-manifest.json").read_text())["dirty"] is True
    finally:
        source_probe.unlink(missing_ok=True)
        ignored_probe.unlink(missing_ok=True)
        with suppress(OSError):
            ignored_probe.parent.rmdir()


def test_builder_rejects_unsafe_output_before_creating_parent(tmp_path: Path) -> None:
    forbidden_parent = tmp_path / "must-not-exist"
    result = _run_builder(forbidden_parent / "arbitrary-name", allow_dirty=True, check=False)

    assert result.returncode != 0
    assert "output name" in result.stderr.lower()
    assert not forbidden_parent.exists()


def test_builder_never_follows_an_existing_output_symlink(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    marker = protected / "marker.txt"
    marker.write_text("preserve me\n")
    output = tmp_path / "target" / "failure-memory"
    output.parent.mkdir()
    output.symlink_to(protected, target_is_directory=True)

    result = _run_builder(output, allow_dirty=True, check=False)

    assert result.returncode != 0
    assert "symbolic link" in result.stderr.lower()
    assert marker.read_text() == "preserve me\n"
    assert output.is_symlink()


def test_cli_reports_retained_rollback_as_json_without_polluting_stdout(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle" / "failure-memory"
    output.mkdir(parents=True)
    (output / "prior-marker.txt").write_text("prior output\n")

    result = _run_builder(output, allow_dirty=True)

    assert result.stdout == f"{output}\n"
    diagnostic = json.loads(result.stderr)
    rollback = Path(diagnostic["failure_memory_builder"]["rollback"])
    assert rollback.parent == output.parent
    assert (rollback / "prior-marker.txt").read_text() == "prior output\n"


def test_cli_failure_reports_exact_recovery_paths_as_json_and_empty_stdout(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle" / "failure-memory"
    output.parent.mkdir(parents=True)
    unresolved = output.parent / ".failure-memory.previous-manual"
    unresolved.mkdir(mode=0o700)

    result = _run_builder(output, allow_dirty=True, check=False)

    assert result.returncode != 0
    assert result.stdout == ""
    diagnostic = json.loads(result.stderr.splitlines()[-1])
    detail = diagnostic["failure_memory_builder"]
    assert detail["recovery"] == [str(unresolved)]
    assert detail["trust_anchor"] == str(output.parent)


def test_builder_help_and_contributor_guide_state_the_recovery_boundary() -> None:
    help_result = subprocess.run(
        [sys.executable, str(BUILDER), "--help"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    surfaces = (
        help_result.stdout,
        (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(),
    )
    for surface in surfaces:
        normalized = surface.casefold()
        assert "no deletion" in normalized
        assert "same user" in normalized
        assert "unresolved" in normalized
        assert "trash" in normalized
        assert "release operator" in normalized


def test_packaged_launcher_completes_strict_mcp_handshake(tmp_path: Path) -> None:
    output = tmp_path / "bundle" / "failure-memory"
    _run_builder(output, allow_dirty=True)
    state_before = _tree_state(output)
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "package-test", "version": "1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "remember_failure",
                "arguments": {
                    "summary": "The user added a new requirement.",
                    "classification": "requirement_update",
                },
            },
        },
    ]
    mcp_config = json.loads((output / ".mcp.json").read_text())
    server = mcp_config["mcpServers"]["failure-memory"]
    environment = os.environ.copy()
    environment.update(server["env"])
    environment.update(
        {
            "FAILURE_MEMORY_HOME": str(tmp_path / "runtime"),
            "PLUGIN_DATA": str(tmp_path / "plugin-data"),
            "PYTHONPATH": "",
        }
    )

    process = subprocess.run(
        [server["command"], *server["args"]],
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        env=environment,
        cwd=output / server["cwd"],
        timeout=15,
        check=False,
    )

    assert process.returncode == 0, process.stderr
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert responses[0]["result"]["protocolVersion"] == "2025-11-25"
    tool_names = [tool["name"] for tool in responses[1]["result"]["tools"]]
    assert tool_names == [
        "remember_failure",
        "recall_failure_lessons",
    ]
    assert len(tool_names) == len(set(tool_names)) == 2
    result = responses[2]["result"]["structuredContent"]
    assert result["status"] == "not_failure"
    assert result["decision"] == "reject"
    assert str(tmp_path) not in json.dumps(result)
    assert _tree_state(output) == state_before


def test_fresh_packaged_hosts_share_one_global_store_identity(tmp_path: Path) -> None:
    output = tmp_path / "bundle" / "failure-memory"
    _run_builder(output, allow_dirty=True)
    server = json.loads((output / ".mcp.json").read_text())["mcpServers"]["failure-memory"]
    runtime = tmp_path / "global-runtime"
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "cross-host-test", "version": "1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "failure_memory_doctor", "arguments": {}},
        },
    ]
    store_ids: set[str] = set()
    for harness in ("codex", "claude-code", "copilot-cli", "copilot-vscode", "cursor"):
        environment = {
            **os.environ,
            **server["env"],
            "FAILURE_MEMORY_HOME": str(runtime),
            "FAILURE_MEMORY_HARNESS": harness,
            "FAILURE_MEMORY_SESSION_ID": f"fresh-{harness}",
            "FAILURE_MEMORY_MCP_PROFILE": "admin",
            "PYTHONPATH": "",
        }
        process = subprocess.run(
            [server["command"], *server["args"]],
            input="".join(json.dumps(message) + "\n" for message in messages),
            capture_output=True,
            text=True,
            env=environment,
            cwd=output / server["cwd"],
            timeout=15,
            check=False,
        )

        assert process.returncode == 0, process.stderr
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        doctor = responses[1]["result"]["structuredContent"]
        assert doctor["integrity_check"] == "ok"
        store_ids.add(doctor["store"]["target_store_id"])

    assert len(store_ids) == 1
    databases = list(runtime.rglob("failure-memory.sqlite3"))
    assert len(databases) == 1


def test_packaged_launcher_exits_nonzero_with_sanitized_startup_failure(
    tmp_path: Path,
) -> None:
    """Would fail if the installed Codex launcher swallowed MCP startup failures."""
    output = tmp_path / "bundle" / "failure-memory"
    _run_builder(output, allow_dirty=True)
    invalid_root = tmp_path / "secret-invalid-runtime-root"
    invalid_root.write_text("not a directory\n", encoding="utf-8")
    environment = {
        **os.environ,
        "FAILURE_MEMORY_HOME": str(invalid_root),
        "PYTHONPATH": "",
    }

    process = subprocess.run(
        [sys.executable, str(output / "scripts" / "failure_memory_mcp.py")],
        capture_output=True,
        text=True,
        env=environment,
        cwd=output,
        timeout=15,
        check=False,
    )

    assert process.returncode != 0
    assert process.stdout == ""
    assert "Failure-memory MCP server could not start" in process.stderr
    assert str(invalid_root) not in process.stderr
    assert "Traceback" not in process.stderr


def test_packaged_launcher_sanitizes_pre_server_global_store_failure(
    tmp_path: Path,
) -> None:
    """Would fail if Codex data-root setup leaked a traceback before server.main."""
    output = tmp_path / "bundle" / "failure-memory"
    _run_builder(output, allow_dirty=True)
    invalid_global_root = tmp_path / "secret-invalid-global-root"
    invalid_global_root.write_text("not a directory\n", encoding="utf-8")
    environment = {
        **os.environ,
        "FAILURE_MEMORY_HOME": str(invalid_global_root),
        "PYTHONPATH": "",
    }
    for name in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        environment.pop(name, None)

    process = subprocess.run(
        [sys.executable, str(output / "scripts" / "failure_memory_mcp.py")],
        capture_output=True,
        text=True,
        env=environment,
        cwd=output,
        timeout=15,
        check=False,
    )

    assert process.returncode != 0
    assert process.stdout == ""
    assert "Failure-memory MCP server could not start" in process.stderr
    assert str(invalid_global_root) not in process.stderr
    assert "Traceback" not in process.stderr


@pytest.mark.parametrize("explicit_codex_home", [True, False])
def test_packaged_launcher_uses_private_platform_global_store(
    tmp_path: Path, explicit_codex_home: bool
) -> None:
    output = tmp_path / "bundle" / "failure-memory"
    _run_builder(output, allow_dirty=True)
    mcp_config = json.loads((output / ".mcp.json").read_text())
    server = mcp_config["mcpServers"]["failure-memory"]
    home = tmp_path / "home"
    codex_home = tmp_path / "explicit-codex-home" if explicit_codex_home else home / ".codex"
    shared_plugin_data = codex_home / "plugin-data"
    shared_plugin_data.mkdir(parents=True, mode=0o755)
    shared_plugin_data.chmod(0o755)
    environment = os.environ.copy()
    environment.update(server["env"])
    environment.update(
        {
            "HOME": str(home),
            "FAILURE_MEMORY_MCP_PROFILE": "admin",
            "PYTHONPATH": "",
        }
    )
    for name in ("FAILURE_MEMORY_HOME", "PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        environment.pop(name, None)
    if explicit_codex_home:
        environment["CODEX_HOME"] = str(codex_home)
    else:
        environment.pop("CODEX_HOME", None)

    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "codex-data-test", "version": "1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "failure_memory_doctor", "arguments": {}},
        },
    ]
    process = subprocess.run(
        [server["command"], *server["args"]],
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        env=environment,
        cwd=output / server["cwd"],
        timeout=15,
        check=False,
        preexec_fn=_set_permissive_umask if os.name != "nt" else None,
    )

    assert process.returncode == 0, process.stderr
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    doctor = responses[1]["result"]["structuredContent"]
    data_root = (
        home / "Library" / "Application Support" / "failure-memory"
        if sys.platform == "darwin"
        else home / ".local" / "share" / "failure-memory"
    )
    assert doctor["integrity_check"] == "ok"
    assert "database_path" not in doctor
    assert str(tmp_path) not in json.dumps(doctor)
    if os.name != "nt":
        assert stat.S_IMODE(shared_plugin_data.stat().st_mode) == 0o755
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o700
            for path in (data_root, *(item for item in data_root.rglob("*") if item.is_dir()))
        )
        runtime_files = [item for item in data_root.rglob("*") if item.is_file()]
        assert {item.name for item in runtime_files} >= {
            "identity.key",
            "failure-memory.sqlite3",
        }
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in runtime_files)


def test_bundle_passes_official_codex_plugin_validator(tmp_path: Path) -> None:
    if not VALIDATOR.is_file():
        pytest.skip("official Codex plugin validator is unavailable")
    yaml_available = subprocess.run(
        [sys.executable, "-c", "import yaml"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert yaml_available.returncode == 0, (
        "PyYAML must be installed in the development environment so the official "
        f"validator cannot silently skip: {yaml_available.stderr}"
    )
    output = tmp_path / "bundle" / "failure-memory"
    _run_builder(output, allow_dirty=True)

    validated = subprocess.run(
        [sys.executable, str(VALIDATOR), str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 0, validated.stdout + validated.stderr
