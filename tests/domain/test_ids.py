from failure_memory.domain.ids import new_id


def test_new_id_is_sortable_and_deterministic_with_injected_entropy() -> None:
    first = new_id("inc", timestamp_ms=1_700_000_000_000, randomness=b"\x00" * 10)
    second = new_id("inc", timestamp_ms=1_700_000_000_001, randomness=b"\x00" * 10)
    assert first == "inc_01HF7YAT000000000000000000"
    assert first < second


def test_new_id_rejects_invalid_prefix() -> None:
    try:
        new_id("Incident!")
    except ValueError as exc:
        assert str(exc) == "prefix must match [a-z][a-z0-9_]{1,15}"
    else:
        raise AssertionError("invalid prefix was accepted")
