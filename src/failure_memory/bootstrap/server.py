"""Dependency-free Model Context Protocol server over standard input and output."""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO, cast

from failure_memory import __version__
from failure_memory.adapters.dependency_runtime.manager import AdapterRuntimeManager
from failure_memory.adapters.harness.context import resolve_data_root
from failure_memory.application.service import FailureMemoryService, create_local_service
from failure_memory.mcp.dispatcher import dispatch_tool
from failure_memory.mcp.tools import TOOLS

from .stdio import decode_json, write_message

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]
_LOGGER = logging.getLogger(__name__)
_INTERNAL_ERROR = {"code": -32603, "message": "Internal error"}

Dispatch = Callable[[str, Mapping[str, object], FailureMemoryService], dict[str, object]]
JsonRpcId = str | int | float | None


def negotiate_protocol(requested: str) -> str:
    """Use a supported client revision, otherwise select the newest revision."""
    return requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL


class McpServer:
    """Own the small MCP lifecycle and dispatch requests to one local service."""

    def __init__(
        self, service: FailureMemoryService, *, dispatch: Dispatch = dispatch_tool
    ) -> None:
        self._service = service
        self._dispatch = dispatch
        self._initialize_received = False
        self.initialized = False
        self._closed = False

    def handle_line(self, line: str) -> dict[str, object] | None:
        """Handle one input line, with only JSON-RPC-safe errors escaping this boundary."""
        try:
            value = decode_json(line)
        except (json.JSONDecodeError, ValueError):
            return _error(None, -32700, "Parse error")
        if not isinstance(value, Mapping):
            return _error(None, -32600, "Invalid Request")
        request = cast(Mapping[str, object], value)
        request_id = _request_identity(request)
        if not _is_valid_request(request):
            return _error(request_id, -32600, "Invalid Request")
        if "id" not in request:
            self._handle_notification(request)
            return None
        method = cast(str, request["method"])
        try:
            response = self._handle_request(request_id, method, request)
            if not _is_strict_json_value(response):
                _LOGGER.error("Failure-memory MCP server produced a non-standard JSON response")
                return _internal_error(request_id)
            return response
        except Exception:
            _LOGGER.exception("Unexpected failure-memory MCP server error for %s", method)
            return _internal_error(request_id)

    def _handle_notification(self, request: Mapping[str, object]) -> None:
        if (
            request["method"] == "notifications/initialized"
            and self._initialize_received
            and _valid_initialized_notification(request)
        ):
            self.initialized = True

    def _handle_request(
        self, request_id: JsonRpcId, method: str, request: Mapping[str, object]
    ) -> dict[str, object]:
        if method == "initialize":
            return self._initialize(request_id, request)
        if method == "ping":
            if not _params_are_object(request):
                return _error(request_id, -32602, "Invalid params")
            return _result(request_id, {})
        if method == "tools/list":
            return self._tools_list(request_id, request)
        if method == "tools/call":
            return self._tools_call(request_id, request)
        return _error(request_id, -32601, "Method not found")

    def _initialize(
        self, request_id: JsonRpcId, request: Mapping[str, object]
    ) -> dict[str, object]:
        if self._initialize_received:
            return _error(request_id, -32600, "Invalid Request")
        params = _object_params(request)
        if params is None or not _valid_initialize_params(params):
            return _error(request_id, -32602, "Invalid params")
        self._initialize_received = True
        requested = cast(str, params["protocolVersion"])
        return _result(
            request_id,
            {
                "protocolVersion": negotiate_protocol(requested),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "failure-memory", "version": __version__},
                "instructions": (
                    "Use failure memory only for real failures: an established expectation, "
                    "an observed mismatch, and material impact or recurrence risk."
                ),
            },
        )

    def _tools_list(
        self, request_id: JsonRpcId, request: Mapping[str, object]
    ) -> dict[str, object]:
        if not self.initialized:
            return _error(request_id, -32002, "Server not initialized")
        if not _params_are_object(request):
            return _error(request_id, -32602, "Invalid params")
        return _result(request_id, {"tools": [tool.as_mcp_dict() for tool in TOOLS]})

    def _tools_call(
        self, request_id: JsonRpcId, request: Mapping[str, object]
    ) -> dict[str, object]:
        if not self.initialized:
            return _error(request_id, -32002, "Server not initialized")
        params = _object_params(request)
        if params is None:
            return _error(request_id, -32602, "Invalid params")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
            return _error(request_id, -32602, "Invalid params")
        result = self._dispatch(name, cast(Mapping[str, object], arguments), self._service)
        return _result(request_id, result)

    def close(self) -> None:
        """Close the SQLite connection that this server owns, at most once."""
        if self._closed:
            return
        self._closed = True
        close = getattr(self._service, "close", None)
        if not callable(close):
            connection = getattr(getattr(self._service, "store", None), "connection", None)
            close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                _LOGGER.exception("Failure-memory MCP server could not close its SQLite connection")


def _request_identity(request: Mapping[str, object]) -> JsonRpcId:
    if "id" not in request:
        return None
    request_id = request["id"]
    if _valid_id(request_id):
        return cast(JsonRpcId, request_id)
    return None


def _is_valid_request(request: Mapping[str, object]) -> bool:
    if request.get("jsonrpc") != "2.0":
        return False
    method = request.get("method")
    if not isinstance(method, str) or not method:
        return False
    return "id" not in request or _valid_id(request["id"])


def _valid_id(value: object) -> bool:
    return (
        value is None
        or isinstance(value, str)
        or type(value) is int
        or (isinstance(value, float) and math.isfinite(value))
    )


def _object_params(request: Mapping[str, object]) -> Mapping[str, object] | None:
    params = request.get("params", {})
    return cast(Mapping[str, object], params) if isinstance(params, Mapping) else None


def _params_are_object(request: Mapping[str, object]) -> bool:
    return _object_params(request) is not None


def _valid_initialized_notification(request: Mapping[str, object]) -> bool:
    params = _object_params(request)
    return params is not None and ("_meta" not in params or isinstance(params["_meta"], Mapping))


def _valid_initialize_params(params: Mapping[str, object]) -> bool:
    protocol_version = params.get("protocolVersion")
    capabilities = params.get("capabilities")
    client_info = params.get("clientInfo")
    return (
        isinstance(protocol_version, str)
        and bool(protocol_version)
        and isinstance(capabilities, Mapping)
        and isinstance(client_info, Mapping)
        and isinstance(client_info.get("name"), str)
        and bool(client_info.get("name"))
        and isinstance(client_info.get("version"), str)
        and bool(client_info.get("version"))
    )


def _result(request_id: JsonRpcId, result: Mapping[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _error(request_id: JsonRpcId, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _internal_error(request_id: JsonRpcId) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": dict(_INTERNAL_ERROR)}


def _is_strict_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _is_strict_json_value(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_is_strict_json_value(item) for item in value)
    return False


def serve(server: McpServer, stdin: TextIO, stdout: TextIO) -> None:
    """Read until EOF and write a single JSON line for each request response."""
    try:
        while line := stdin.readline():
            response = server.handle_line(line)
            if response is not None:
                write_message(stdout, response)
    finally:
        server.close()


def _maybe_reexec_with_adapter_runtime() -> None:
    """Use the validated adapter Python when the host SQLite cannot load sqlite-vec."""
    runtime_python = AdapterRuntimeManager(resolve_data_root()).ready_python_executable()
    if runtime_python is None:
        return
    try:
        already_running = Path(sys.executable).resolve() == runtime_python.resolve()
    except OSError:
        already_running = os.path.abspath(sys.executable) == os.path.abspath(runtime_python)
    if already_running:
        return
    source_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root)
        if not existing_python_path
        else os.pathsep.join((str(source_root), existing_python_path))
    )
    os.execve(
        str(runtime_python),
        [
            str(runtime_python),
            "-m",
            "failure_memory.bootstrap.server",
            *sys.argv[1:],
        ],
        environment,
    )


def main() -> int:
    """Run the local MCP stdio server."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        _maybe_reexec_with_adapter_runtime()
        service = create_local_service()
        serve(McpServer(service), sys.stdin, sys.stdout)
    except Exception:
        _LOGGER.error("Failure-memory MCP server could not start")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
