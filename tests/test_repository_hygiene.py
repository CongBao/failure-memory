from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_ignored(path: str) -> int:
    return subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path],
        cwd=ROOT,
        check=False,
    ).returncode


def test_local_and_sensitive_paths_are_ignored() -> None:
    ignored = [
        ".venv/bin/python",
        ".runtime/adapters/vector-index/sqlite-vec/vectors.sqlite3",
        "plugin-data/failure-memory.sqlite3-wal",
        "packaging/out/failure-memory/.codex-plugin/plugin.json",
        "docs/architecture.md",
        ".env",
        ".env.local",
        ".envrc",
        ".secrets/adapter-token",
        "private.key",
        "credentials.json",
        "credentials.local.json",
        "secrets.json",
        ".coverage",
        "htmlcov/index.html",
        ".DS_Store",
    ]
    assert {path: check_ignored(path) for path in ignored} == {path: 0 for path in ignored}


def test_public_inputs_are_not_ignored() -> None:
    tracked = [
        "uv.lock",
        ".python-version",
        ".env.example",
        "src/failure_memory/adapters/event_store/sqlite/migrations/0001_initial.sql",
        "tests/fixtures/capture-cases.json",
        "skills/record-agent-failure/SKILL.md",
        ".github/workflows/ci.yml",
    ]
    assert {path: check_ignored(path) for path in tracked} == {path: 1 for path in tracked}


def test_documentation_directory_is_not_tracked() -> None:
    listed = subprocess.check_output(["git", "ls-files", "-z", "docs"], cwd=ROOT)
    assert listed == b""


def test_tracked_text_has_no_private_checkout_or_process_artifacts() -> None:
    listed = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    paths = [Path(value.decode()) for value in listed.split(b"\0") if value]
    forbidden = {
        "personal_home": "/Users/" + "co" + "bao",
        "private_process_docs": "docs/" + "superpowers/",
        "internal_milestone": "Phase " + "1",
        "internal_release_task": "Task " + "13",
    }
    findings: dict[str, list[str]] = {}
    for relative in paths:
        path = ROOT / relative
        if not path.exists():
            # A public-readiness change may intentionally delete a tracked file
            # before the release commit is created.
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, fragment in forbidden.items():
            if fragment.casefold() in text.casefold():
                findings.setdefault(label, []).append(relative.as_posix())
    assert findings == {}


def test_public_versions_are_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert project["project"]["version"] == plugin["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"])


def test_tracked_markdown_relative_links_resolve() -> None:
    listed = subprocess.check_output(["git", "ls-files", "-z", "*.md"], cwd=ROOT)
    markdown_paths = [
        ROOT / value.decode()
        for value in listed.split(b"\0")
        if value and (ROOT / value.decode()).is_file()
    ]
    findings: dict[str, list[str]] = {}
    for markdown_path in markdown_paths:
        text = markdown_path.read_text(encoding="utf-8")
        targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
        for target in targets:
            local_target = target.split("#", 1)[0]
            if not local_target or "://" in local_target or target.startswith("#"):
                continue
            if not (markdown_path.parent / local_target).exists():
                relative = markdown_path.relative_to(ROOT).as_posix()
                findings.setdefault(relative, []).append(target)

    assert markdown_paths
    assert findings == {}
