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
SUPPORTED_TARGETS = ("codex", "claude-code", "copilot", "cursor")


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
            self.target in {"copilot", "cursor"}
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
            self.target in {"copilot", "cursor"}
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
        "copilot": ".plugin",
        "cursor": ".cursor-plugin",
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
    if target == "copilot":
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


def _state(bundle: Path, target: str) -> HostState:
    if target == "cursor":
        return _cursor_state(bundle)
    executable_name = {
        "codex": "codex",
        "claude-code": "claude",
        "copilot": "copilot",
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
            _build_commit(_copilot_install_root()) if target == "copilot" else None
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
        ("copilot", "copilot"),
    ):
        if shutil.which(executable_name) is not None:
            states.append(_state(bundle, target))
    cursor_root = _cursor_install_root()
    if cursor_root.exists() or cursor_root.is_symlink():
        states.append(_cursor_state(bundle))
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
    states = [_state(bundle, target) for target in targets]
    if arguments.apply:
        _enforce_shared_store_version_safety(bundle, set(targets))
        for state in states:
            _apply(bundle, state)
        states = [_state(bundle, state.target) for state in states]
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
