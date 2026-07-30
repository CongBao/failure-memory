from __future__ import annotations

import io
import json

import pytest

from failure_memory.bootstrap.stdio import read_message, write_message


def test_read_message_returns_one_object_from_each_input_line() -> None:
    stream = io.StringIO('{"jsonrpc":"2.0","id":0}\n{"method":"ping"}\n')

    assert read_message(stream) == {"jsonrpc": "2.0", "id": 0}
    assert read_message(stream) == {"method": "ping"}


def test_read_message_returns_none_at_eof() -> None:
    assert read_message(io.StringIO("")) is None


def test_read_message_rejects_non_object_json() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        read_message(io.StringIO("[]\n"))


def test_read_message_rejects_duplicate_object_keys() -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        read_message(io.StringIO('{"method":"ping","method":"tools/list"}\n'))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_read_message_rejects_non_finite_json_numbers(value: str) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        read_message(io.StringIO(f'{{"value":{value}}}\n'))


def test_write_message_is_one_json_line() -> None:
    output = io.StringIO()

    write_message(output, {"jsonrpc": "2.0", "id": 1, "result": {"text": "a\nb"}})

    assert output.getvalue().count("\n") == 1
    assert json.loads(output.getvalue()) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"text": "a\nb"},
    }


def test_write_message_preserves_non_ascii_utf8_text() -> None:
    output = io.StringIO()

    write_message(output, {"jsonrpc": "2.0", "id": "", "result": {"text": "失败"}})

    assert "失败" in output.getvalue()
    assert json.loads(output.getvalue())["id"] == ""


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_write_message_rejects_non_finite_json_numbers(value: float) -> None:
    output = io.StringIO()

    with pytest.raises(ValueError, match="Out of range float values"):
        write_message(output, {"jsonrpc": "2.0", "id": 1, "result": {"value": value}})

    assert output.getvalue() == ""
