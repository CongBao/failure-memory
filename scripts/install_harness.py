#!/usr/bin/env python3
"""Plan or apply duplicate-safe Failure Memory installation per agent host."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PLUGIN_NAME = "failure-memory"
SUPPORTED_TARGETS = (
    "codex",
    "claude-code",
    "copilot-cli",
    "copilot-vscode",
    "cursor",
    "generic",
)


@dataclass(frozen=True, slots=True)
class HostState:
    target: str
    executable: str
    installed_versions: tuple[str, ...]
    desired_version: str
    installed_build_commit: str | None
    desired_build_commit: str | None
    marketplace_present: bool = False
    managed_projection: bool = True
    installed_identities: tuple[str, ...] = ()
    skills_dir: str | None = None
    mcp_config: str | None = None
    agent_name: str | None = None

    @property
    def installed_matches_desired(self) -> bool:
        if self.installed_versions == (self.desired_version,):
            return True
        if (
            self.target == "codex"
            and "+codex." not in self.desired_version
            and len(self.installed_versions) == 1
        ):
            installed_base, separator, _cachebuster = self.installed_versions[0].partition(
                "+codex."
            )
            return bool(separator) and installed_base == self.desired_version
        return False

    @property
    def action(self) -> str:
        if len(self.installed_versions) > 1:
            return "conflict"
        if not self.managed_projection and not self.installed_matches_desired:
            return "conflict"
        if not self.installed_matches_desired:
            return "install" if not self.installed_versions else "update"
        if (
            self.target in {"copilot-cli", "copilot-vscode", "cursor", "generic"}
            and self.installed_build_commit is not None
            and self.desired_build_commit is not None
            and self.installed_build_commit != self.desired_build_commit
        ):
            return "update"
        if self.installed_matches_desired:
            return "noop"
        raise AssertionError("unreachable host state")

    @property
    def same_version_content_update(self) -> bool:
        return (
            self.target in {"copilot-cli", "copilot-vscode", "cursor", "generic"}
            and self.installed_versions == (self.desired_version,)
            and self.installed_build_commit is not None
            and self.desired_build_commit is not None
            and self.installed_build_commit != self.desired_build_commit
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Identify existing stable plugin identities before installing or updating; "
            "all hosts keep the same platform-global memory store."
        )
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=SUPPORTED_TARGETS,
        required=True,
        help="host to inspect; repeat for multiple hosts",
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        help="skill root for the generic target; defaults to ~/.agents/skills",
    )
    parser.add_argument(
        "--mcp-config",
        type=Path,
        help="optional MCP JSON file for the generic target",
    )
    parser.add_argument(
        "--agent-name",
        default="generic",
        help="source-harness label for a generic projection",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="installable plugin bundle root",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply planned native host operations; default is read-only",
    )
    return parser


def _manifest_version(bundle: Path, target: str) -> str:
    manifest_directories = {
        "codex": ".codex-plugin",
        "claude-code": ".claude-plugin",
        "copilot-cli": ".plugin",
        "copilot-vscode": ".plugin",
        "cursor": ".cursor-plugin",
        "generic": ".codex-plugin",
    }
    location = bundle / manifest_directories[target] / "plugin.json"
    value = json.loads(location.read_text(encoding="utf-8"))
    if value.get("name") != PLUGIN_NAME or not isinstance(value.get("version"), str):
        raise ValueError(f"invalid {target} plugin manifest")
    return str(value["version"])


def _desired_version(bundle: Path) -> str:
    versions = {_manifest_version(bundle, target) for target in SUPPORTED_TARGETS}
    if len(versions) != 1:
        raise ValueError("all harness manifests must publish the same plugin version")
    return versions.pop()


def _build_commit(root: Path) -> str | None:
    manifest = root / "build-manifest.json"
    if not manifest.is_file():
        return None
    value = json.loads(manifest.read_text(encoding="utf-8"))
    commit = value.get("commit")
    return commit if isinstance(commit, str) and commit else None


def _copilot_install_root() -> Path:
    return Path.home() / ".copilot" / "installed-plugins" / "_direct" / PLUGIN_NAME


def _cursor_install_root() -> Path:
    return Path.home() / ".cursor" / "plugins" / "local" / PLUGIN_NAME


def _vscode_user_mcp_config() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "Code" / "User" / "mcp.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Code" / "User" / "mcp.json"


def _vscode_skills_root() -> Path:
    return Path.home() / ".copilot" / "skills"


def _generic_marker(skills_dir: Path) -> Path:
    return skills_dir / ".failure-memory-install.json"


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read JSON configuration at {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON configuration at {path} must contain an object")
    return value


def _projection_skills_match(bundle: Path, skills_dir: Path) -> bool:
    for name in ("record-agent-failure", "recall-failure-lessons"):
        destination = skills_dir / name
        if not destination.is_symlink():
            return False
        try:
            if destination.resolve(strict=True) != (bundle / "skills" / name).resolve(strict=True):
                return False
        except OSError:
            return False
    return True


def _projection_skills_conflict(bundle: Path, skills_dir: Path) -> bool:
    for name in ("record-agent-failure", "recall-failure-lessons"):
        destination = skills_dir / name
        if not (destination.exists() or destination.is_symlink()):
            continue
        if not destination.is_symlink():
            return True
        try:
            if destination.resolve(strict=True) != (bundle / "skills" / name).resolve(strict=True):
                return True
        except OSError:
            return True
    return False


def _projection_state(
    bundle: Path,
    target: str,
    *,
    skills_dir: Path,
    mcp_config: Path | None,
    agent_name: str,
) -> HostState:
    desired_version = _manifest_version(bundle, target)
    managed = True
    installed_version: str | None = None
    installed_commit: str | None = None
    identity_present = False
    if mcp_config is not None:
        document = _read_json_object(mcp_config)
        servers = document.get("servers")
        if not isinstance(servers, dict):
            servers = document.get("mcpServers")
        entry = None if not isinstance(servers, dict) else servers.get(PLUGIN_NAME)
        if entry is not None:
            identity_present = True
            if not isinstance(entry, dict):
                managed = False
            else:
                environment = entry.get("env")
                if not isinstance(environment, dict) or environment.get(
                    "FAILURE_MEMORY_MANAGED"
                ) != "1":
                    managed = False
                else:
                    version = environment.get("FAILURE_MEMORY_PLUGIN_VERSION")
                    commit = environment.get("FAILURE_MEMORY_BUILD_COMMIT")
                    installed_version = version if isinstance(version, str) else None
                    installed_commit = commit if isinstance(commit, str) and commit else None
    else:
        marker = _read_json_object(_generic_marker(skills_dir))
        if marker:
            identity_present = True
            if marker.get("plugin") != PLUGIN_NAME or marker.get("managed") is not True:
                managed = False
            else:
                version = marker.get("version")
                commit = marker.get("build_commit")
                installed_version = version if isinstance(version, str) else None
                installed_commit = commit if isinstance(commit, str) and commit else None
    if _projection_skills_conflict(bundle, skills_dir):
        identity_present = True
        managed = False
    skills_match = _projection_skills_match(bundle, skills_dir)
    complete = identity_present and managed and installed_version is not None and skills_match
    versions = (installed_version,) if complete and installed_version is not None else ()
    if identity_present and not managed:
        versions = ("unmanaged",)
    return HostState(
        target=target,
        executable="",
        installed_versions=versions,
        desired_version=desired_version,
        installed_build_commit=installed_commit,
        desired_build_commit=_build_commit(bundle),
        managed_projection=managed,
        installed_identities=((PLUGIN_NAME,) if identity_present else ()),
        skills_dir=str(skills_dir),
        mcp_config=None if mcp_config is None else str(mcp_config),
        agent_name=agent_name,
    )


def _atomic_json_write(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.new"
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError(f"stale temporary configuration exists at {temporary}")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _project_skill_links(bundle: Path, skills_dir: Path) -> None:
    skills_dir.mkdir(parents=True, exist_ok=True)
    for name in ("record-agent-failure", "recall-failure-lessons"):
        source = (bundle / "skills" / name).resolve(strict=True)
        destination = skills_dir / name
        if destination.is_symlink() and destination.resolve(strict=False) == source:
            continue
        if destination.exists() or destination.is_symlink():
            raise RuntimeError(f"unmanaged skill projection exists at {destination}")
        temporary = skills_dir / f".{name}.{os.getpid()}.new"
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError(f"stale skill projection exists at {temporary}")
        temporary.symlink_to(source, target_is_directory=True)
        try:
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _apply_projection(bundle: Path, state: HostState) -> None:
    assert state.skills_dir is not None
    assert state.agent_name is not None
    skills_dir = Path(state.skills_dir)
    _project_skill_links(bundle, skills_dir)
    version = state.desired_version
    commit = state.desired_build_commit or ""
    if state.mcp_config is None:
        _atomic_json_write(
            _generic_marker(skills_dir),
            {
                "plugin": PLUGIN_NAME,
                "version": version,
                "build_commit": commit,
                "managed": True,
                "source_harness": state.agent_name,
            },
        )
        return
    mcp_config = Path(state.mcp_config)
    document = _read_json_object(mcp_config)
    key = "servers" if "mcpServers" not in document else "mcpServers"
    servers = document.get(key)
    if servers is None:
        servers = {}
        document[key] = servers
    if not isinstance(servers, dict):
        raise RuntimeError(f"{mcp_config} {key} value must be an object")
    existing = servers.get(PLUGIN_NAME)
    if existing is not None:
        if not isinstance(existing, dict):
            raise RuntimeError("existing failure-memory MCP entry is unmanaged")
        environment = existing.get("env")
        if not isinstance(environment, dict) or environment.get(
            "FAILURE_MEMORY_MANAGED"
        ) != "1":
            raise RuntimeError("existing failure-memory MCP entry is unmanaged")
    entry: dict[str, object] = {
        "command": "python3",
        "args": [str((bundle / "scripts" / "failure_memory_mcp.py").resolve(strict=True))],
        "cwd": str(bundle),
        "env": {
            "FAILURE_MEMORY_HARNESS": state.agent_name,
            "FAILURE_MEMORY_MANAGED": "1",
            "FAILURE_MEMORY_PLUGIN_VERSION": version,
            "FAILURE_MEMORY_BUILD_COMMIT": commit,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    }
    if key == "servers":
        entry["type"] = "stdio"
    servers[PLUGIN_NAME] = entry
    _atomic_json_write(mcp_config, document)


def _run(executable: str, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        [executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{executable} operation failed: {detail}")
    return result.stdout


def _installed_plugins(target: str, executable: str) -> tuple[tuple[str, str], ...]:
    output = _run(
        executable,
        "plugin",
        "list",
        *(("--json",) if target in {"codex", "claude-code"} else ()),
    )
    if target == "codex":
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise RuntimeError("codex plugin list did not return valid JSON") from error
        installed = value.get("installed") if isinstance(value, dict) else None
        if not isinstance(installed, list):
            raise RuntimeError("codex plugin list has an unsupported JSON shape")
        plugins = []
        for item in installed:
            if not isinstance(item, dict) or item.get("name") != PLUGIN_NAME:
                continue
            identity = item.get("pluginId")
            version = item.get("version")
            if isinstance(identity, str) and isinstance(version, str):
                plugins.append((identity, version))
        return tuple(plugins)
    if target == "copilot-cli":
        matches = re.findall(
            rf"^\s*[•*]?\s*({re.escape(PLUGIN_NAME)}(?:@\S+)?)\s+\(v([^)]+)\)",
            output,
            flags=re.MULTILINE,
        )
        return tuple((identity, version) for identity, version in matches)
    return tuple(_claude_plugins(output))


def _installed_versions(target: str, executable: str) -> tuple[str, ...]:
    return tuple(version for _identity, version in _installed_plugins(target, executable))


def _installed_identities(target: str, executable: str) -> tuple[str, ...]:
    return tuple(identity for identity, _version in _installed_plugins(target, executable))


def _claude_plugins(output: str) -> list[tuple[str, str]]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("claude plugin list did not return valid JSON") from error
    plugins: list[tuple[str, str]] = []

    def collect(item: object) -> None:
        if isinstance(item, list):
            for nested in item:
                collect(nested)
            return
        if not isinstance(item, dict):
            return
        identity = next(
            (item.get(key) for key in ("id", "name", "plugin") if isinstance(item.get(key), str)),
            None,
        )
        version = item.get("version")
        if (
            isinstance(identity, str)
            and identity.split("@", 1)[0] == PLUGIN_NAME
            and isinstance(version, str)
            and version
        ):
            plugins.append((identity, version))
            return
        for nested in item.values():
            collect(nested)

    collect(value)
    return plugins


def _marketplace_present(target: str, executable: str) -> bool:
    output = _run(executable, "plugin", "marketplace", "list", "--json")
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{target} marketplace list did not return valid JSON") from error

    def contains(item: object) -> bool:
        if isinstance(item, list):
            return any(contains(nested) for nested in item)
        if not isinstance(item, dict):
            return False
        if item.get("name") == PLUGIN_NAME or PLUGIN_NAME in item:
            return True
        return any(contains(nested) for nested in item.values())

    return contains(value)


def _cursor_state(bundle: Path) -> HostState:
    root = _cursor_install_root()
    installed = root.exists() or root.is_symlink()
    managed = not installed or root.is_symlink()
    versions: tuple[str, ...] = ()
    if installed:
        try:
            versions = (_manifest_version(root, "cursor"),)
        except (OSError, ValueError, json.JSONDecodeError):
            versions = ("unmanaged",)
            managed = False
    return HostState(
        target="cursor",
        executable="",
        installed_versions=versions,
        desired_version=_manifest_version(bundle, "cursor"),
        installed_build_commit=_build_commit(root) if installed else None,
        desired_build_commit=_build_commit(bundle),
        managed_projection=managed,
        installed_identities=((PLUGIN_NAME,) if installed else ()),
    )


def _state(
    bundle: Path,
    target: str,
    *,
    skills_dir: Path | None = None,
    mcp_config: Path | None = None,
    agent_name: str = "generic",
) -> HostState:
    if target == "cursor":
        return _cursor_state(bundle)
    if target == "copilot-vscode":
        return _projection_state(
            bundle,
            target,
            skills_dir=_vscode_skills_root(),
            mcp_config=_vscode_user_mcp_config(),
            agent_name="copilot-vscode",
        )
    if target == "generic":
        return _projection_state(
            bundle,
            target,
            skills_dir=skills_dir or Path.home() / ".agents" / "skills",
            mcp_config=mcp_config,
            agent_name=agent_name,
        )
    executable_name = {
        "codex": "codex",
        "claude-code": "claude",
        "copilot-cli": "copilot",
    }[target]
    executable = shutil.which(executable_name)
    if executable is None:
        raise RuntimeError(f"{target} executable is not installed")
    installed_plugins = _installed_plugins(target, executable)
    return HostState(
        target=target,
        executable=executable,
        installed_versions=tuple(version for _identity, version in installed_plugins),
        desired_version=_manifest_version(bundle, target),
        installed_build_commit=(
            _build_commit(_copilot_install_root()) if target == "copilot-cli" else None
        ),
        desired_build_commit=_build_commit(bundle),
        marketplace_present=(
            _marketplace_present(target, executable)
            if target in {"codex", "claude-code"}
            else False
        ),
        installed_identities=tuple(identity for identity, _version in installed_plugins),
    )


def _installed_states(bundle: Path) -> tuple[HostState, ...]:
    states: list[HostState] = []
    for target, executable_name in (
        ("codex", "codex"),
        ("claude-code", "claude"),
        ("copilot-cli", "copilot"),
    ):
        if shutil.which(executable_name) is not None:
            states.append(_state(bundle, target))
    cursor_root = _cursor_install_root()
    if cursor_root.exists() or cursor_root.is_symlink():
        states.append(_cursor_state(bundle))
    vscode = _state(bundle, "copilot-vscode")
    if vscode.installed_identities:
        states.append(vscode)
    return tuple(states)


def _enforce_shared_store_version_safety(
    bundle: Path,
    selected_targets: set[str],
) -> None:
    _desired_version(bundle)
    conflicts = [
        state
        for state in _installed_states(bundle)
        if state.target not in selected_targets
        and state.installed_versions
        and not state.installed_matches_desired
    ]
    if not conflicts:
        return
    details = ", ".join(
        f"{state.target}={','.join(state.installed_versions)}" for state in conflicts
    )
    missing = " ".join(f"--target {state.target}" for state in conflicts)
    raise RuntimeError(
        "shared-store version skew detected in installed Failure Memory projections "
        f"({details}); update them together by adding {missing}"
    )


def _apply(bundle: Path, state: HostState) -> None:
    if state.action == "noop":
        return
    if state.action == "conflict":
        detail = (
            "duplicate installations"
            if len(state.installed_versions) > 1
            else "an unmanaged conflicting projection"
        )
        raise RuntimeError(f"{state.target} has {detail}; resolve it first")
    if state.target == "cursor":
        destination = _cursor_install_root()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{PLUGIN_NAME}.new"
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError(f"stale Cursor projection exists at {temporary}")
        temporary.symlink_to(bundle, target_is_directory=True)
        try:
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return
    if state.target in {"copilot-vscode", "generic"}:
        _apply_projection(bundle, state)
        return
    if state.target == "claude-code":
        identity = (
            state.installed_identities[0]
            if state.installed_identities
            else f"{PLUGIN_NAME}@failure-memory"
        )
        if state.action == "install":
            if not state.marketplace_present:
                _run(
                    state.executable,
                    "plugin",
                    "marketplace",
                    "add",
                    str(bundle),
                )
            _run(
                state.executable,
                "plugin",
                "install",
                identity,
                "--scope",
                "user",
            )
            return
        marketplace = identity.partition("@")[2]
        _run(
            state.executable,
            "plugin",
            "marketplace",
            "update",
            marketplace,
        )
        _run(
            state.executable,
            "plugin",
            "update",
            identity,
            "--scope",
            "user",
        )
        return
    if state.target == "codex":
        identity = (
            state.installed_identities[0]
            if state.installed_identities
            else f"{PLUGIN_NAME}@failure-memory"
        )
        if state.action == "update":
            marketplace = identity.partition("@")[2]
            if marketplace != "personal":
                _run(
                    state.executable,
                    "plugin",
                    "marketplace",
                    "update",
                    marketplace,
                )
            _run(state.executable, "plugin", "remove", identity)
        elif not state.marketplace_present:
            _run(
                state.executable,
                "plugin",
                "marketplace",
                "add",
                str(bundle),
            )
        _run(state.executable, "plugin", "add", identity)
        return
    if state.same_version_content_update:
        _run(state.executable, "plugin", "uninstall", PLUGIN_NAME)
        _run(state.executable, "plugin", "install", str(bundle))
        return
    if state.action == "update":
        try:
            _run(state.executable, "plugin", "update", PLUGIN_NAME)
            return
        except RuntimeError:
            _run(state.executable, "plugin", "uninstall", PLUGIN_NAME)
    _run(state.executable, "plugin", "install", str(bundle))


def _global_store(bundle: Path) -> str:
    sys.path.insert(0, str(bundle / "src"))
    from failure_memory.adapters.harness.context import resolve_data_root

    return str(
        resolve_data_root()
        / "adapters"
        / "event-store"
        / "sqlite"
        / "primary"
        / "failure-memory.sqlite3"
    )


def main() -> int:
    arguments = _parser().parse_args()
    bundle = arguments.bundle.expanduser().resolve(strict=True)
    targets = tuple(dict.fromkeys(arguments.target))
    _desired_version(bundle)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", arguments.agent_name):
        raise RuntimeError("--agent-name must be a lowercase portable identifier")

    def inspect(target: str) -> HostState:
        return _state(
            bundle,
            target,
            skills_dir=arguments.skills_dir,
            mcp_config=arguments.mcp_config,
            agent_name=arguments.agent_name,
        )

    states = [inspect(target) for target in targets]
    if arguments.apply:
        _enforce_shared_store_version_safety(bundle, set(targets))
        for state in states:
            _apply(bundle, state)
        states = [inspect(state.target) for state in states]
    payload = {
        "plugin": PLUGIN_NAME,
        "bundle": str(bundle),
        "global_store": _global_store(bundle),
        "applied": bool(arguments.apply),
        "hosts": [
            {
                "target": state.target,
                "installed_versions": list(state.installed_versions),
                "desired_version": state.desired_version,
                "installed_build_commit": state.installed_build_commit,
                "desired_build_commit": state.desired_build_commit,
                "action": state.action,
                "duplicate_installation": len(state.installed_versions) > 1,
                "marketplace_present": state.marketplace_present,
                "managed_projection": state.managed_projection,
                "installed_identities": list(state.installed_identities),
            }
            for state in states
        ],
    }
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if all(state.action == "noop" for state in states) or not arguments.apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
