#!/usr/bin/env python3
"""Plan or apply duplicate-safe Failure Memory installation per agent host."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PLUGIN_NAME = "failure-memory"
SUPPORTED_TARGETS = ("codex", "copilot")


@dataclass(frozen=True, slots=True)
class HostState:
    target: str
    executable: str
    installed_versions: tuple[str, ...]
    desired_version: str
    installed_build_commit: str | None
    desired_build_commit: str | None

    @property
    def action(self) -> str:
        if len(self.installed_versions) > 1:
            return "conflict"
        if self.installed_versions != (self.desired_version,):
            return "install" if not self.installed_versions else "update"
        if (
            self.target == "copilot"
            and self.installed_build_commit is not None
            and self.desired_build_commit is not None
            and self.installed_build_commit != self.desired_build_commit
        ):
            return "update"
        if self.installed_versions == (self.desired_version,):
            return "noop"
        raise AssertionError("unreachable host state")

    @property
    def same_version_content_update(self) -> bool:
        return (
            self.target == "copilot"
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
    location = (
        bundle / ".codex-plugin" / "plugin.json"
        if target == "codex"
        else bundle / ".plugin" / "plugin.json"
    )
    value = json.loads(location.read_text(encoding="utf-8"))
    if value.get("name") != PLUGIN_NAME or not isinstance(value.get("version"), str):
        raise ValueError(f"invalid {target} plugin manifest")
    return str(value["version"])


def _build_commit(root: Path) -> str | None:
    manifest = root / "build-manifest.json"
    if not manifest.is_file():
        return None
    value = json.loads(manifest.read_text(encoding="utf-8"))
    commit = value.get("commit")
    return commit if isinstance(commit, str) and commit else None


def _copilot_install_root() -> Path:
    return Path.home() / ".copilot" / "installed-plugins" / "_direct" / PLUGIN_NAME


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


def _installed_versions(target: str, executable: str) -> tuple[str, ...]:
    output = _run(executable, "plugin", "list")
    if target == "codex":
        versions = []
        for line in output.splitlines():
            columns = re.split(r"\s{2,}", line.strip())
            if columns and columns[0].startswith(f"{PLUGIN_NAME}@") and len(columns) >= 3:
                versions.append(columns[2])
    else:
        versions = re.findall(
            rf"^\s*[•*]?\s*{re.escape(PLUGIN_NAME)}(?:@\S+)?\s+\(v([^)]+)\)",
            output,
            flags=re.MULTILINE,
        )
    return tuple(versions)


def _state(bundle: Path, target: str) -> HostState:
    executable = shutil.which(target) if target == "codex" else shutil.which("copilot")
    if executable is None:
        raise RuntimeError(f"{target} executable is not installed")
    return HostState(
        target=target,
        executable=executable,
        installed_versions=_installed_versions(target, executable),
        desired_version=_manifest_version(bundle, target),
        installed_build_commit=(
            _build_commit(_copilot_install_root()) if target == "copilot" else None
        ),
        desired_build_commit=_build_commit(bundle),
    )


def _apply(bundle: Path, state: HostState) -> None:
    if state.action == "noop":
        return
    if state.action == "conflict":
        raise RuntimeError(
            f"{state.target} has duplicate {PLUGIN_NAME} installations; resolve them first"
        )
    if state.target == "codex":
        identity = f"{PLUGIN_NAME}@personal"
        if state.action == "update":
            _run(state.executable, "plugin", "remove", identity)
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
    states = [_state(bundle, target) for target in dict.fromkeys(arguments.target)]
    if arguments.apply:
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
            }
            for state in states
        ],
    }
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if all(state.action == "noop" for state in states) or not arguments.apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
