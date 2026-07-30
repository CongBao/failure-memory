from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from failure_memory.adapters.event_store.sqlite import migrate as migrate_module
from failure_memory.application import service as service_module
from failure_memory.application.errors import StorageBusyError
from failure_memory.bootstrap import server as server_module
from failure_memory.bootstrap.server import (
    SUPPORTED_PROTOCOLS,
    McpServer,
    _maybe_reexec_with_adapter_runtime,
    create_local_service,
    negotiate_protocol,
    serve,
)

EXPECTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")


class FakeService:
    pass


def _server() -> McpServer:
    return McpServer(
        cast(object, FakeService()),
        dispatch=lambda name, arguments, service: {
            "name": name,
            "arguments": dict(arguments),
            "service": type(service).__name__,
        },
    )


def _request(
    request_id: int | str | None,
    method: str,
    params: Mapping[str, object] | None = None,
) -> str:
    message: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = dict(params)
    return json.dumps(message)


def _initialize(server: McpServer, request_id: int | str = 1) -> dict[str, object]:
    response = server.handle_line(
        _request(
            request_id,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
    )
    assert response is not None
    return response


def _complete_handshake(server: McpServer, request_id: int | str = 1) -> dict[str, object]:
    response = _initialize(server, request_id)
    assert server.handle_line(_request(None, "notifications/initialized", {})) is None
    assert server.initialized is True
    return response


def test_supported_protocols_are_exactly_the_three_published_revisions() -> None:
    assert SUPPORTED_PROTOCOLS == EXPECTED_PROTOCOLS


@pytest.mark.parametrize("protocol", EXPECTED_PROTOCOLS)
def test_negotiate_protocol_keeps_every_supported_revision(protocol: str) -> None:
    assert negotiate_protocol(protocol) == protocol


def test_negotiate_protocol_falls_back_to_latest() -> None:
    assert negotiate_protocol("not-a-protocol") == "2025-11-25"


def test_invalid_json_returns_parse_error_with_null_id() -> None:
    response = _server().handle_line("{not json")

    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }


def test_invalid_request_returns_invalid_request_and_preserves_zero_id() -> None:
    response = _server().handle_line('{"jsonrpc":"1.0","id":0,"method":"ping"}')

    assert response == {
        "jsonrpc": "2.0",
        "id": 0,
        "error": {"code": -32600, "message": "Invalid Request"},
    }


def test_invalid_request_rejects_missing_method_and_invalid_id() -> None:
    missing_method = _server().handle_line('{"jsonrpc":"2.0","id":""}')
    invalid_id = _server().handle_line('{"jsonrpc":"2.0","id":true,"method":"ping"}')

    assert missing_method is not None and missing_method["error"] == {
        "code": -32600,
        "message": "Invalid Request",
    }
    assert invalid_id is not None and invalid_id["error"] == {
        "code": -32600,
        "message": "Invalid Request",
    }


def test_malformed_idless_object_is_not_a_notification() -> None:
    response = _server().handle_line('{"jsonrpc":"1.0","method":"ping"}')

    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }


@pytest.mark.parametrize("request_id", [None, 1.5])
def test_valid_null_and_fractional_ids_are_preserved(request_id: int | float | None) -> None:
    response = _server().handle_line(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "ping", "params": {}})
    )

    assert response == {"jsonrpc": "2.0", "id": request_id, "result": {}}


def test_boolean_id_is_not_a_json_rpc_number() -> None:
    response = _server().handle_line('{"jsonrpc":"2.0","id":true,"method":"ping"}')

    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_json_numbers_return_parse_error(constant: str) -> None:
    response = _server().handle_line(
        f'{{"jsonrpc":"2.0","id":1,"method":"ping","params":{{"value":{constant}}}}}'
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }


def test_notification_does_not_get_a_response() -> None:
    assert _server().handle_line(_request(None, "ping", {})) is None


def test_initialize_returns_capabilities_and_preserves_empty_string_id() -> None:
    response = _initialize(_server(), "")

    assert response["id"] == ""
    assert response["result"] == {
        "protocolVersion": "2025-11-25",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "failure-memory", "version": "0.4.0"},
        "instructions": (
            "Use failure memory only for real failures: an established expectation, "
            "an observed mismatch, and material impact or recurrence risk."
        ),
    }


def test_initialize_rejects_bad_params() -> None:
    response = _server().handle_line(_request(1, "initialize", {"protocolVersion": 1}))

    assert response is not None and response["error"] == {
        "code": -32602,
        "message": "Invalid params",
    }


def test_tool_call_with_non_object_params_returns_invalid_params() -> None:
    server = _server()
    _complete_handshake(server)

    response = server.handle_line('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":[]}')

    assert response is not None and response["error"] == {
        "code": -32602,
        "message": "Invalid params",
    }


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"_meta": {}},
        {
            "_meta": {
                "com.example/trace-id": "trace-123",
                "nested": {"sampled": True},
            },
            "com.example/extension": "allowed",
        },
    ],
    ids=["omitted", "empty", "empty-meta", "metadata-and-extension"],
)
def test_protocol_valid_initialized_notification_advances_lifecycle_without_response(
    params: Mapping[str, object] | None,
) -> None:
    """Would fail if valid NotificationParams metadata stalled the MCP lifecycle."""
    server = _server()
    _initialize(server)

    assert server.handle_line(_request(None, "notifications/initialized", params)) is None
    assert server.initialized is True


@pytest.mark.parametrize(
    "params",
    ["[]", '{"_meta":null}', '{"_meta":[]}', '{"_meta":"invalid"}'],
    ids=["array-params", "null-meta", "array-meta", "string-meta"],
)
def test_malformed_initialized_notification_does_not_complete_handshake(params: str) -> None:
    server = _server()
    _initialize(server)

    assert (
        server.handle_line(
            f'{{"jsonrpc":"2.0","method":"notifications/initialized","params":{params}}}'
        )
        is None
    )
    response = server.handle_line(_request(2, "tools/list", {}))

    assert server.initialized is False
    assert response is not None and response["error"] == {
        "code": -32002,
        "message": "Server not initialized",
    }


@pytest.mark.parametrize("method,params", [("tools/list", {}), ("tools/call", {"name": "x"})])
def test_tools_reject_requests_before_completed_handshake(
    method: str, params: Mapping[str, object]
) -> None:
    response = _server().handle_line(_request(0, method, params))

    assert response is not None and response["error"] == {
        "code": -32002,
        "message": "Server not initialized",
    }


@pytest.mark.parametrize("method,params", [("tools/list", {}), ("tools/call", {"name": "x"})])
def test_tools_reject_requests_after_initialize_but_before_initialized_notification(
    method: str, params: Mapping[str, object]
) -> None:
    server = _server()
    _initialize(server)

    response = server.handle_line(_request(0, method, params))

    assert response is not None and response["error"] == {
        "code": -32002,
        "message": "Server not initialized",
    }


def test_tools_list_returns_all_immutable_tools_after_initialize() -> None:
    server = _server()
    _complete_handshake(server)

    response = server.handle_line(_request(2, "tools/list", {}))

    assert response is not None
    tools = cast(dict[str, list[dict[str, object]]], response["result"])["tools"]
    assert len(tools) == 17
    assert {tool["name"] for tool in tools} == {
        "evaluate_failure_candidate",
        "review_failure_recording",
        "record_failure_incident",
        "find_related_failures",
        "recall_failure_lessons",
        "record_recall_outcome",
        "get_failure_memory_metrics",
        "get_failure_recall_metrics",
        "failure_memory_retrieval_status",
        "build_failure_memory_index",
        "get_failure_learning_metrics",
        "failure_memory_store_status",
        "transition_failure_lesson",
        "run_failure_ranking_experiment",
        "propose_failure_lesson_clusters",
        "failure_memory_setup_status",
        "failure_memory_doctor",
    }


def test_tools_call_returns_the_dispatcher_result_directly() -> None:
    server = _server()
    _complete_handshake(server)

    response = server.handle_line(
        _request(3, "tools/call", {"name": "failure_memory_doctor", "arguments": {}})
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "name": "failure_memory_doctor",
            "arguments": {},
            "service": "FakeService",
        },
    }


def test_tools_call_rejects_bad_params() -> None:
    server = _server()
    _complete_handshake(server)

    response = server.handle_line(_request(3, "tools/call", {"name": 1, "arguments": {}}))

    assert response is not None and response["error"] == {
        "code": -32602,
        "message": "Invalid params",
    }


def test_duplicate_initialize_is_rejected_without_resetting_completed_handshake() -> None:
    server = _server()
    _complete_handshake(server)

    response = _initialize(server, 2)

    assert response == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert server.initialized is True


def test_duplicate_initialize_before_initialized_notification_keeps_handshake_incomplete() -> None:
    server = _server()
    _initialize(server)

    response = _initialize(server, 2)

    assert response == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert server.initialized is False


def test_dispatch_exception_is_logged_and_sanitized(caplog: pytest.LogCaptureFixture) -> None:
    def broken_dispatch(
        name: str, arguments: Mapping[str, object], service: object
    ) -> dict[str, object]:
        raise RuntimeError("secret internal detail")

    server = McpServer(cast(object, FakeService()), dispatch=broken_dispatch)
    _complete_handshake(server)

    response = server.handle_line(
        _request(4, "tools/call", {"name": "failure_memory_doctor", "arguments": {}})
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 4,
        "error": {"code": -32603, "message": "Internal error"},
    }
    assert "secret internal detail" in caplog.text


def test_dispatch_result_with_non_finite_number_is_sanitized() -> None:
    server = McpServer(
        cast(object, FakeService()),
        dispatch=lambda name, arguments, service: {"value": float("nan")},
    )
    _complete_handshake(server)

    response = server.handle_line(
        _request(4, "tools/call", {"name": "failure_memory_doctor", "arguments": {}})
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 4,
        "error": {"code": -32603, "message": "Internal error"},
    }


def test_ping_returns_empty_result_before_initialization() -> None:
    response = _server().handle_line(_request(0, "ping", {}))

    assert response == {"jsonrpc": "2.0", "id": 0, "result": {}}


def test_unknown_method_returns_method_not_found() -> None:
    response = _server().handle_line(_request(1, "unknown", {}))

    assert response is not None and response["error"] == {
        "code": -32601,
        "message": "Method not found",
    }


def test_serve_reads_multiple_lines_writes_only_responses_and_stops_at_eof() -> None:
    server = _server()
    stdin = io.StringIO(
        "\n".join(
            (
                _request(
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                ),
                _request(None, "notifications/initialized", {}),
                _request(2, "tools/list", {}),
                _request(3, "ping", {}),
            )
        )
        + "\n"
    )
    stdout = io.StringIO()

    serve(server, stdin, stdout)

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]


def test_serve_closes_the_owned_sqlite_connection_after_eof() -> None:
    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Store:
        connection = Connection()

    class Service:
        store = Store()

    server = McpServer(cast(object, Service()))

    serve(server, io.StringIO(""), io.StringIO())

    assert Service.store.connection.closed is True


def test_mcp_reexecs_with_ready_private_adapter_python(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_python = tmp_path / "adapter-runtime" / "bin" / "python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"runtime")
    current_python = tmp_path / "host" / "python"
    current_python.parent.mkdir(parents=True)
    current_python.write_bytes(b"host")

    class RuntimeManager:
        def __init__(self, data_root: Path) -> None:
            assert data_root == tmp_path / "memory"

        def ready_python_executable(self) -> Path:
            return runtime_python

    calls: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(server_module, "AdapterRuntimeManager", RuntimeManager)
    monkeypatch.setattr(server_module, "resolve_data_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(server_module.sys, "executable", str(current_python))
    monkeypatch.setattr(server_module.sys, "argv", ["failure_memory_mcp.py", "--future"])
    monkeypatch.setenv("PYTHONPATH", "existing-path")
    monkeypatch.setattr(
        server_module.os,
        "execve",
        lambda executable, arguments, environment: calls.append(
            (executable, list(arguments), dict(environment))
        ),
    )

    _maybe_reexec_with_adapter_runtime()

    assert len(calls) == 1
    executable, arguments, environment = calls[0]
    assert executable == str(runtime_python)
    assert arguments == [
        str(runtime_python),
        "-m",
        "failure_memory.bootstrap.server",
        "--future",
    ]
    assert environment["PYTHONPATH"].endswith(f"{os.pathsep}existing-path")
    assert str(Path(server_module.__file__).resolve().parents[2]) in environment["PYTHONPATH"]


def test_mcp_does_not_reexec_without_a_ready_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RuntimeManager:
        def __init__(self, data_root: Path) -> None:
            pass

        def ready_python_executable(self) -> None:
            return None

    monkeypatch.setattr(server_module, "AdapterRuntimeManager", RuntimeManager)
    monkeypatch.setattr(server_module, "resolve_data_root", lambda: tmp_path)
    monkeypatch.setattr(
        server_module.os,
        "execve",
        lambda *arguments: pytest.fail(f"unexpected execve: {arguments!r}"),
    )

    _maybe_reexec_with_adapter_runtime()


def test_create_local_service_uses_explicit_and_environment_data_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = create_local_service(data_root=tmp_path / "explicit", cwd=tmp_path, harness="test")
    database = Path(service.store.database_path())
    service.close()
    service.close()
    with pytest.raises(sqlite3.ProgrammingError):
        service.store.counts()
    assert database == (
        tmp_path
        / "explicit"
        / "adapters"
        / "event-store"
        / "sqlite"
        / "primary"
        / "failure-memory.sqlite3"
    )

    monkeypatch.setenv("FAILURE_MEMORY_HOME", str(tmp_path / "environment"))
    environment_service = create_local_service(cwd=tmp_path, harness="test")
    environment_database = Path(environment_service.store.database_path())
    environment_service.close()
    assert environment_database.is_relative_to(tmp_path / "environment")


@pytest.mark.parametrize(
    ("failure_point", "expected_stage_calls"),
    [
        ("migration", ["migration"]),
        ("store", ["migration", "store"]),
        ("service", ["migration", "store", "service"]),
    ],
)
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_create_local_service_closes_once_and_preserves_every_post_acquisition_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
    expected_stage_calls: list[str],
    error_type: type[BaseException],
) -> None:
    failure = error_type(f"{failure_point} construction stopped")
    stage_calls: list[str] = []

    class Connection:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    connection = Connection()
    monkeypatch.setattr(service_module, "connect_sqlite", lambda path: connection)

    def migrate(value: object) -> tuple[()]:
        stage_calls.append("migration")
        if failure_point == "migration":
            raise failure
        return ()

    def construct_store(value: object) -> object:
        stage_calls.append("store")
        if failure_point == "store":
            raise failure
        return object()

    def construct_service(*args: object, **kwargs: object) -> object:
        stage_calls.append("service")
        if failure_point == "service":
            raise failure
        return object()

    monkeypatch.setattr(service_module, "apply_migrations", migrate)
    monkeypatch.setattr(service_module, "SQLiteEventStore", construct_store)
    monkeypatch.setattr(service_module, "FailureMemoryService", construct_service)

    with pytest.raises(error_type) as raised:
        create_local_service(data_root=tmp_path, cwd=tmp_path)

    assert raised.value is failure
    assert stage_calls == expected_stage_calls
    assert stage_calls.count("migration") == 1
    assert stage_calls.count("store") == (0 if failure_point == "migration" else 1)
    assert stage_calls.count("service") == (1 if failure_point == "service" else 0)
    assert connection.close_calls == 1


def test_create_local_service_preserves_construction_failure_when_close_also_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = KeyboardInterrupt("store construction stopped")

    class Connection:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("secondary close failure")

    connection = Connection()
    monkeypatch.setattr(service_module, "connect_sqlite", lambda path: connection)
    monkeypatch.setattr(service_module, "apply_migrations", lambda value: ())
    monkeypatch.setattr(
        service_module,
        "SQLiteEventStore",
        lambda value: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        create_local_service(data_root=tmp_path, cwd=tmp_path)

    assert raised.value is failure
    assert connection.close_calls == 1


def test_create_local_service_rejects_newer_migration_before_store_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Would fail if a downgraded service reached adapter construction on a newer schema."""
    service = create_local_service(data_root=tmp_path, cwd=tmp_path)
    database = Path(service.store.database_path())
    service.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO schema_migration(version, name, checksum, applied_at)
            VALUES (999, '0999_future.sql', 'future-checksum', '2026-07-30T00:00:00Z')
            """
        )
        connection.commit()
    finally:
        connection.close()
    constructed = False

    def construct_store(_connection: object) -> object:
        nonlocal constructed
        constructed = True
        return object()

    monkeypatch.setattr(service_module, "SQLiteEventStore", construct_store)

    with pytest.raises(ValueError, match=r"unknown applied migration versions: 999"):
        create_local_service(data_root=tmp_path, cwd=tmp_path)

    assert constructed is False


def test_create_local_service_maps_pending_migration_contention_to_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Would fail if startup migration contention escaped as an internal error."""
    data_root = tmp_path / "data"
    service = create_local_service(data_root=data_root, cwd=tmp_path)
    database = Path(service.store.database_path())
    service.close()
    migrations = migrate_module._migration_files()
    monkeypatch.setattr(
        migrate_module,
        "_migration_files",
        lambda: [
            *migrations,
            (6, "0006_pending.sql", "CREATE TABLE pending_migration (id INTEGER) STRICT;"),
        ],
    )
    real_connect = service_module.connect_sqlite

    def fast_busy_connect(path: Path) -> sqlite3.Connection:
        connection = real_connect(path)
        connection.execute("PRAGMA busy_timeout = 1")
        return connection

    monkeypatch.setattr(service_module, "connect_sqlite", fast_busy_connect)
    blocker = sqlite3.connect(database, isolation_level=None)
    blocker.execute("PRAGMA journal_mode = WAL")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StorageBusyError):
            create_local_service(data_root=data_root, cwd=tmp_path)
    finally:
        blocker.rollback()
        blocker.close()

    verification = sqlite3.connect(database)
    try:
        assert (
            verification.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'pending_migration'"
            ).fetchone()
            is None
        )
    finally:
        verification.close()


def test_module_server_keeps_stdout_as_json_rpc_only(tmp_path: Path) -> None:
    environment = {**os.environ, "FAILURE_MEMORY_HOME": str(tmp_path)}
    requests = "\n".join(
        (
            _request(
                1,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            ),
            _request(None, "notifications/initialized", {}),
            _request(2, "tools/list", {}),
            _request(3, "ping", {}),
        )
    )

    completed = subprocess.run(
        [sys.executable, "-m", "failure_memory.bootstrap.server"],
        input=f"{requests}\n",
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert responses[0]["result"]["capabilities"]["tools"]["listChanged"] is False


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_mcp_entrypoints_exit_nonzero_with_sanitized_startup_failure(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    """Would fail if a supervisor mistook failed service construction for a clean exit."""
    invalid_root = tmp_path / "secret-runtime-root"
    invalid_root.write_text("not a directory\n", encoding="utf-8")
    environment = {**os.environ, "FAILURE_MEMORY_HOME": str(invalid_root)}
    command = (
        [sys.executable, "-m", "failure_memory.bootstrap.server"]
        if entrypoint == "module"
        else [str(Path(sys.executable).with_name("failure-memory-mcp"))]
    )

    completed = subprocess.run(
        command,
        input="",
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "Failure-memory MCP server could not start" in completed.stderr
    assert str(invalid_root) not in completed.stderr
    assert "Traceback" not in completed.stderr
