#!/usr/bin/env python3
"""Render the two compact Failure Memory skills from their JSON contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

SKILL_NAMES = ("record-agent-failure", "recall-failure-lessons")


class ContractError(ValueError):
    """Raised when a skill contract cannot produce a safe bounded skill."""


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _require(document: dict[str, Any], key: str, expected_type: type[Any]) -> Any:
    value = document.get(key)
    if not isinstance(value, expected_type):
        raise ContractError(f"{key} must be {expected_type.__name__}")
    return value


def validate_contract(document: dict[str, Any]) -> None:
    if document.get("contract_version") != 5:
        raise ContractError("contract_version must be 5")
    kind = document.get("policy_kind")
    if kind not in {"record_failure", "recall_failure"}:
        raise ContractError("unsupported policy_kind")
    skill = _require(document, "skill", dict)
    policy = _require(document, "policy", dict)
    name = _require(skill, "name", str)
    _require(skill, "description", str)
    expected_name = (
        "record-agent-failure" if kind == "record_failure" else "recall-failure-lessons"
    )
    if name != expected_name:
        raise ContractError(f"skill.name must be {expected_name}")
    launcher = _require(policy, "fallback_launcher", str)
    if launcher != "scripts/failure_memory_cli.py":
        raise ContractError("fallback_launcher must use the bundled stable launcher")
    fallback_arguments = _require(policy, "fallback_arguments", list)
    if not fallback_arguments or not all(
        isinstance(argument, str) and argument for argument in fallback_arguments
    ):
        raise ContractError("fallback_arguments must be non-empty strings")
    if kind == "record_failure":
        if policy.get("tool") != "remember_failure" or policy.get("single_call") is not True:
            raise ContractError("recording must use remember_failure exactly once")
        if policy.get("record_qualification_attempts") is not True:
            raise ContractError("every recording classification must reach the one-call gate")
        if policy.get("fallback_call_limit") != 1:
            raise ContractError("recording fallback must be one call")
        if any(
            policy.get(key) is not False
            for key in ("inspect_implementation", "inspect_database", "temporary_files")
        ):
            raise ContractError("recording fallback cannot inspect internals or create files")
    else:
        if policy.get("tool") != "recall_failure_lessons" or policy.get("call_limit") != 1:
            raise ContractError("recall must use one bounded call")
        if policy.get("maximum_top_k") != 3:
            raise ContractError("recall must return at most three cautions")


def _frontmatter(skill: dict[str, Any]) -> str:
    return f"---\nname: {skill['name']}\ndescription: {skill['description']}\n---\n"


def _render_record(document: dict[str, Any]) -> str:
    skill = document["skill"]
    policy = document["policy"]
    classes = policy["classifications"]
    rows = "\n".join(f"| `{name}` | {criterion} |" for name, criterion in classes.items())
    exclusions = ", ".join(policy["sensitive_exclusions"])
    return (
        _frontmatter(skill)
        + "\n# Record Agent Failure\n\n"
        + "Decide from evidence available before the outcome. Corrective wording alone is not "
        + "a failure.\n\n"
        + "## Fast path\n\n"
        + "1. Choose one classification. For `mixed`, keep only the prior-invariant mismatch "
        + "in `failure_portion`.\n"
        + "2. For `real_failure` or `mixed`, provide compact `expectation`, `observed`, `cause`, "
        + "and `lesson` objects. Use `unknown` for an uninspectable cause.\n"
        + f"3. Call `{policy['tool']}` exactly once for every classification, including a "
        + "non-failure. For a non-failure, send only the compact classification evidence; do "
        + "not invent failure objects. The service records qualification telemetry and creates "
        + "a lesson only when warranted.\n"
        + "4. Report the returned status briefly. A recorded lesson remains `proposed`.\n\n"
        + "If the named tool is unavailable, resolve the bundled "
        + f"[CLI launcher]({policy['fallback_launcher']}) relative to this skill and execute it "
        + f"once with `{' '.join(policy['fallback_arguments'])}`, passing the same JSON on "
        + "standard input. If the launcher is unavailable, report one installation error and "
        + "stop.\n\n"
        + "Never search for the plugin, inspect source or SQLite, import private APIs, discover "
        + "runtimes, retry alternate commands, or create temporary files. "
        + f"Exclude {exclusions}.\n\n"
        + "## Classifications\n\n"
        + "| Class | Meaning |\n|---|---|\n"
        + rows
        + "\n"
    )


def _render_recall(document: dict[str, Any]) -> str:
    skill = document["skill"]
    policy = document["policy"]
    discriminators = ", ".join(f"`{value}`" for value in policy["discriminators"])
    exclusions = ", ".join(policy["sensitive_exclusions"])
    return (
        _frontmatter(skill)
        + "\n# Recall Failure Lessons\n\n"
        + "Use one bounded lookup only when the current task provides `text` plus at least one "
        + f"concrete discriminator: {discriminators}.\n\n"
        + f"Call `{policy['tool']}` once with `mode={policy['default_mode']}` and "
        + f"`top_k={policy['default_top_k']}`. Do not broaden or retry the query. Apply at most "
        + "three returned lessons as proposed cautions and validate them against the "
        + "current task.\n\n"
        + "If the named tool is unavailable, resolve the bundled "
        + f"[CLI launcher]({policy['fallback_launcher']}) relative to this skill and execute it "
        + f"once with `{' '.join(policy['fallback_arguments'])}`, passing the same JSON on "
        + "standard input. If the launcher is unavailable, continue without memory guidance.\n\n"
        + f"Do not install dependencies during recall or include {exclusions}. Similarity is "
        + "not proof, policy authority, or permission to merge lessons.\n"
    )


def render_skill(document: dict[str, Any]) -> str:
    validate_contract(document)
    content = (
        _render_record(document)
        if document["policy_kind"] == "record_failure"
        else _render_recall(document)
    )
    digest = _canonical_sha256(document)
    marker = (
        f"<!-- Generated from contract.json; policy sha256={digest}; "
        "DO NOT EDIT SKILL.md MANUALLY. -->\n\n"
    )
    frontmatter, separator, body = content.partition("---\n")
    del frontmatter
    header, separator, body = body.partition("---\n")
    if not separator:
        raise ContractError("renderer produced invalid frontmatter")
    return f"---\n{header}---\n\n{marker}{body.lstrip()}"


def _validated_root(root: Path) -> Path:
    if not root.is_absolute():
        root = root.absolute()
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ContractError("render root must be a real directory")
    return root


def _regular_bytes(path: Path, *, required: bool) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise ContractError(f"required file is missing: {path}") from None
        return None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ContractError(f"managed path must be a regular file: {path}")
    return path.read_bytes()


def _validate_skill_directory(root: Path, name: str) -> Path:
    skills = root / "skills"
    directory = skills / name
    for path in (skills, directory):
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ContractError(f"managed path must be a real directory: {path}")
    return directory


def _write_atomic(path: Path, content: bytes) -> None:
    _regular_bytes(path, required=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_all(root: Path) -> dict[str, str]:
    root = _validated_root(root)
    rendered: dict[str, str] = {}
    for name in SKILL_NAMES:
        directory = _validate_skill_directory(root, name)
        contract_path = directory / "contract.json"
        contract_bytes = _regular_bytes(contract_path, required=True)
        assert contract_bytes is not None
        document = json.loads(contract_bytes.decode("utf-8"))
        if not isinstance(document, dict):
            raise ContractError(f"{contract_path} must contain an object")
        rendered[name] = render_skill(document)
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    try:
        rendered = render_all(arguments.root)
        differences: list[Path] = []
        for name, content in rendered.items():
            path = arguments.root / "skills" / name / "SKILL.md"
            current = _regular_bytes(path, required=False)
            expected = content.encode("utf-8")
            if current != expected:
                differences.append(path)
                if not arguments.check:
                    _write_atomic(path, expected)
        if arguments.check and differences:
            for path in differences:
                print(path.relative_to(arguments.root), file=os.sys.stderr)
            return 1
        return 0
    except (ContractError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"failure-memory skill rendering failed: {error}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
