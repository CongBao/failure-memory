from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RedactionResult:
    text: str
    state: str
    kinds: tuple[str, ...]


_PATTERNS = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?P<private_key_label>(?:[A-Z0-9]+ )*PRIVATE KEY)-----"
            r".*?-----END (?P=private_key_label)-----",
            re.DOTALL,
        ),
    ),
    (
        "github_token",
        re.compile(
            r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
            r"(?![A-Za-z0-9])"
        ),
    ),
    (
        "openai_key",
        re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])"),
    ),
    (
        "bearer_token",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])Bearer\s+[A-Za-z0-9._~+/=-]{20,}"
            r"(?![A-Za-z0-9._~+/=-])"
        ),
    ),
)


def redact_text(value: str) -> RedactionResult:
    text = value
    found: list[str] = []
    for kind, pattern in _PATTERNS:
        text, count = pattern.subn(f"[REDACTED:{kind}]", text)
        if count:
            found.append(kind)
    return RedactionResult(
        text=text,
        state="redacted" if found else "clean",
        kinds=tuple(found),
    )
