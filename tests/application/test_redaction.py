from failure_memory.application.redaction import redact_text

GITHUB_TOKEN = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
MINIMUM_GITHUB_TOKEN = "ghp_" + "abcdefghijklmnopqrst"
OPENAI_PROJECT_KEY = "sk-" + "proj-abcdefghijklmnop"


def test_redacts_github_and_openai_tokens() -> None:
    """Would fail if either token matcher were removed or shortened."""
    value = f"token {GITHUB_TOKEN} and {OPENAI_PROJECT_KEY}"

    result = redact_text(value)

    assert result.text == "token [REDACTED:github_token] and [REDACTED:openai_key]"
    assert result.state == "redacted"
    assert result.kinds == ("github_token", "openai_key")


def test_redacts_private_keys_and_bearer_tokens() -> None:
    """Would fail if multiline private keys or bearer credentials reached storage unchanged."""
    value = (
        "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY----- "
        "Bearer abcdefghijklmnopqrstuvwxyz123456"
    )

    result = redact_text(value)

    assert result.text == "[REDACTED:private_key] [REDACTED:bearer_token]"
    assert result.state == "redacted"
    assert result.kinds == ("private_key", "bearer_token")


def test_mismatched_private_key_labels_do_not_over_redact_intervening_text() -> None:
    """Would fail if an unmatched PEM boundary consumed unrelated intervening text."""
    value = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "ordinary text that must remain visible\n"
        "-----END EC PRIVATE KEY-----"
    )

    result = redact_text(value)

    assert result.text == value
    assert result.state == "clean"
    assert result.kinds == ()


def test_leaves_short_token_like_text_clean() -> None:
    """Would fail if the bounded matchers over-redacted ordinary short identifiers."""
    value = "Reference ghp_short and sk-short are not credentials."

    result = redact_text(value)

    assert result.text == value
    assert result.state == "clean"
    assert result.kinds == ()


def test_redacts_github_token_next_to_underscore_and_hyphen_punctuation() -> None:
    """Would fail if word boundaries let punctuation-adjacent GitHub tokens bypass scanning."""
    token = MINIMUM_GITHUB_TOKEN

    result = redact_text(f"prefix_{token}-suffix")

    assert result.text == "prefix_[REDACTED:github_token]-suffix"
    assert result.state == "redacted"
    assert result.kinds == ("github_token",)


def test_redacts_minimum_openai_key_ending_with_hyphen() -> None:
    """Would fail if a valid minimum-length OpenAI key ending in '-' required a word boundary."""
    token = "sk-abcdefghijklmno-"

    result = redact_text(f"prefix_{token}!")

    assert result.text == "prefix_[REDACTED:openai_key]!"
    assert result.state == "redacted"
    assert result.kinds == ("openai_key",)


def test_redacts_bearer_token_next_to_underscore_and_hyphen_punctuation() -> None:
    """Would fail if an underscore before Bearer prevented its credential from being redacted."""
    token = "Bearer abcdefghijklmnopqrstuvwxyz123456"

    result = redact_text(f"prefix_{token}-suffix")

    assert "Bearer " not in result.text
    assert "abcdefghijklmnopqrstuvwxyz123456" not in result.text
    assert result.state == "redacted"
    assert result.kinds == ("bearer_token",)
