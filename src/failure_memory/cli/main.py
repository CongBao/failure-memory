"""Machine-oriented command-line interface for local failure memory."""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Never, TextIO, cast

from failure_memory.adapters.dependency_runtime.manager import AdapterRuntimeManager
from failure_memory.adapters.harness.context import resolve_data_root
from failure_memory.application.errors import (
    ADAPTER_SETUP_FAILED_MESSAGE,
    SEMANTIC_SETUP_MESSAGE,
    STORAGE_BUSY_MESSAGE,
    AdapterSetupError,
    SemanticSetupRequiredError,
    StorageBusyError,
)
from failure_memory.application.service import FailureMemoryService, create_local_service
from failure_memory.domain.records import LessonState
from failure_memory.json_codec import load_json
from failure_memory.mcp.dispatcher import dispatch_tool

_COMMAND_TO_TOOL = {
    "setup-status": "failure_memory_setup_status",
    "doctor": "failure_memory_doctor",
    "metrics": "get_failure_memory_metrics",
    "recall-metrics": "get_failure_recall_metrics",
    "learning-metrics": "get_failure_learning_metrics",
    "evaluate": "evaluate_failure_candidate",
    "diagnose": "diagnose_failure_cause",
    "review": "review_failure_recording",
    "record": "record_failure_incident",
    "repair-feedback": "record_failure_repair_outcome",
    "recall": "recall_failure_lessons",
    "feedback": "record_recall_outcome",
}


class CliUsageError(ValueError):
    """A concise, safe-to-expose command-line contract error."""


class CliOperationRejectedError(RuntimeError):
    """A domain or adapter operation was safely rejected."""


class _MachineArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CliUsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _MachineArgumentParser(prog="failure-memory")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("setup-status", help="report available local capabilities")
    subparsers.add_parser("doctor", help="check local storage health")
    subparsers.add_parser("metrics", help="return append-ledger record counts")
    subparsers.add_parser("recall-metrics", help="return recall telemetry record counts")
    subparsers.add_parser("learning-metrics", help="return measured recall quality")
    for command in (
        "evaluate",
        "diagnose",
        "review",
        "record",
        "repair-feedback",
        "recall",
        "feedback",
    ):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument(
            "--input",
            required=True,
            metavar="FILE|-",
            help="read one JSON object from FILE or standard input",
        )
    index_parser = subparsers.add_parser("index", help="inspect or rebuild the derived index")
    index_subparsers = index_parser.add_subparsers(dest="index_command", required=True)
    index_subparsers.add_parser("status", help="report retrieval index availability")
    index_subparsers.add_parser("build", help="synchronize accepted lessons into the index")
    adapters_parser = subparsers.add_parser(
        "adapters",
        help="plan or install optional retrieval dependencies",
    )
    adapters_subparsers = adapters_parser.add_subparsers(
        dest="adapters_command",
        required=True,
    )
    adapters_subparsers.add_parser("list", help="list supported optional adapters")
    adapters_subparsers.add_parser("plan", help="show paths, pins, and expected downloads")
    adapters_subparsers.add_parser("status", help="check private adapter runtime readiness")
    adapters_subparsers.add_parser(
        "install",
        help="explicitly install pinned sqlite-vec/FastEmbed dependencies and model",
    )
    store_parser = subparsers.add_parser(
        "store",
        help="inspect or consolidate harness-local ledgers into global personal memory",
    )
    store_subparsers = store_parser.add_subparsers(dest="store_command", required=True)
    store_subparsers.add_parser("discover", help="discover known legacy local stores")
    store_subparsers.add_parser("status", help="report completed copy-only imports")
    for command in ("plan", "import", "verify"):
        command_parser = store_subparsers.add_parser(command)
        command_parser.add_argument("--source", required=True, type=Path)
    lesson_parser = subparsers.add_parser("lesson", help="review lesson lifecycle")
    lesson_subparsers = lesson_parser.add_subparsers(dest="lesson_command", required=True)
    transition_parser = lesson_subparsers.add_parser(
        "transition", help="append a reviewed lifecycle transition"
    )
    transition_parser.add_argument("--lesson-id", required=True)
    transition_parser.add_argument(
        "--to-state",
        required=True,
        choices=("verified", "deprecated", "superseded"),
    )
    transition_parser.add_argument("--rationale-code", required=True)
    learning_parser = subparsers.add_parser(
        "learning", help="run disabled-by-default learning experiments"
    )
    learning_subparsers = learning_parser.add_subparsers(dest="learning_command", required=True)
    learning_subparsers.add_parser("experiment", help="append a shadow feedback-ranking experiment")
    learning_subparsers.add_parser(
        "proposals",
        help="list reviewable lesson generalization proposals",
    )
    proposal_review_parser = learning_subparsers.add_parser(
        "review",
        help="append an explicit generalization proposal review",
    )
    proposal_review_parser.add_argument(
        "--input",
        required=True,
        metavar="FILE|-",
        help="read one JSON object from FILE or standard input",
    )
    evaluation_parser = learning_subparsers.add_parser(
        "evaluate",
        help="run a local offline shadow evaluation corpus",
    )
    evaluation_parser.add_argument(
        "--corpus",
        required=True,
        type=Path,
        help="read one public synthetic evaluation corpus",
    )
    cluster_parser = learning_subparsers.add_parser(
        "cluster", help="append proposal-only semantic lesson clusters"
    )
    cluster_parser.add_argument("--distance-threshold", type=float, default=0.2)
    return parser


def _read_input(location: str, stdin: TextIO) -> Mapping[str, object]:
    try:
        if location == "-":
            value = load_json(stdin)
        else:
            with Path(location).open(encoding="utf-8") as stream:
                value = load_json(stream)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CliUsageError("Input must contain one valid JSON object.") from exc
    except OSError as exc:
        raise CliUsageError("Input file could not be read.") from exc
    if not isinstance(value, dict):
        raise CliUsageError("Input must contain one valid JSON object.")
    return cast(Mapping[str, object], value)


def _emit(payload: Mapping[str, object], stdout: TextIO) -> None:
    json.dump(dict(payload), stdout, ensure_ascii=False, sort_keys=True, allow_nan=False)
    stdout.write("\n")
    stdout.flush()


def _error(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}}


def _diagnose(message: str, stderr: TextIO) -> None:
    stderr.write(f"failure-memory: {message}\n")
    stderr.flush()


def _run(
    arguments: argparse.Namespace,
    service: FailureMemoryService,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    command = cast(str, arguments.command)
    if command == "store":
        store_command = cast(str, arguments.store_command)
        try:
            if store_command == "discover":
                payload: Mapping[str, object] = {
                    "scope": "global_personal",
                    "sources": list(service.discover_source_stores()),
                }
            elif store_command == "status":
                payload = service.store_status()
            elif store_command == "plan":
                payload = service.plan_store_import(cast(Path, arguments.source))
            elif store_command == "import":
                payload = service.import_source_store(cast(Path, arguments.source))
            else:
                payload = service.verify_source_store(cast(Path, arguments.source))
        except ValueError as exc:
            raise CliOperationRejectedError from exc
        _emit(payload, stdout)
        return 0
    if command == "lesson":
        try:
            payload = service.transition_lesson(
                cast(str, arguments.lesson_id),
                LessonState(cast(str, arguments.to_state)),
                cast(str, arguments.rationale_code),
            )
        except ValueError as exc:
            raise CliOperationRejectedError from exc
        _emit(payload, stdout)
        return 0
    if command == "learning":
        try:
            if arguments.learning_command == "cluster":
                payload = service.propose_lesson_clusters(
                    distance_threshold=float(arguments.distance_threshold)
                )
            elif arguments.learning_command == "proposals":
                payload = {
                    "scope": "global_personal",
                    "proposals": list(service.list_lesson_generalization_proposals()),
                }
            elif arguments.learning_command == "review":
                envelope = dispatch_tool(
                    "review_failure_generalization_proposal",
                    _read_input(cast(str, arguments.input), stdin),
                    service,
                    log_exceptions=False,
                )
                payload = cast(Mapping[str, object], envelope["structuredContent"])
                _emit(payload, stdout)
                if envelope["isError"] is True:
                    error = cast(Mapping[str, object], payload["error"])
                    _diagnose(cast(str, error["message"]), stderr)
                    return 2
                return 0
            elif arguments.learning_command == "evaluate":
                payload = service.run_offline_learning_evaluation(cast(Path, arguments.corpus))
            else:
                payload = service.run_shadow_ranking_experiment()
        except ValueError as exc:
            raise CliOperationRejectedError from exc
        _emit(payload, stdout)
        return 0
    tool_arguments: Mapping[str, object]
    if command in {
        "evaluate",
        "diagnose",
        "review",
        "record",
        "repair-feedback",
        "recall",
        "feedback",
    }:
        tool_arguments = _read_input(cast(str, arguments.input), stdin)
    else:
        tool_arguments = {}
    if command == "index":
        tool_name = (
            "build_failure_memory_index"
            if arguments.index_command == "build"
            else "failure_memory_retrieval_status"
        )
    else:
        tool_name = _COMMAND_TO_TOOL[command]
    envelope = dispatch_tool(
        tool_name,
        tool_arguments,
        service,
        log_exceptions=False,
    )
    payload = cast(Mapping[str, object], envelope["structuredContent"])
    _emit(payload, stdout)
    if envelope["isError"] is True:
        error = cast(Mapping[str, object], payload["error"])
        _diagnose(cast(str, error["message"]), stderr)
        if error["code"] == "internal_error":
            return 1
        return 3 if error["code"] == "busy" else 2
    return 0


def _run_adapters(
    arguments: argparse.Namespace,
    *,
    stdout: TextIO,
) -> int:
    manager = AdapterRuntimeManager(resolve_data_root())
    command = cast(str, arguments.adapters_command)
    if command == "list":
        payload: Mapping[str, object] = {
            "adapters": [
                {
                    "name": "sqlite-vec-fastembed",
                    "state": manager.status()["state"],
                    "semantic_search": "exact_knn",
                    "lexical_search": "fts5",
                    "hybrid_search": "application_rrf",
                }
            ]
        }
    elif command == "plan":
        payload = manager.plan()
    elif command == "status":
        payload = manager.status()
    else:
        payload = manager.install()
    _emit(payload, stdout)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one local operation and emit exactly one JSON result on stdout."""
    try:
        arguments = _parser().parse_args(argv)
    except CliUsageError as exc:
        message = str(exc)
        _emit(_error("invalid_input", message), sys.stdout)
        _diagnose(message, sys.stderr)
        return 2

    result_stdout = io.StringIO()
    result_stderr = io.StringIO()
    service: FailureMemoryService | None = None
    try:
        if arguments.command == "adapters":
            exit_code = _run_adapters(arguments, stdout=result_stdout)
        else:
            service = create_local_service()
            exit_code = _run(
                arguments,
                service,
                stdin=sys.stdin,
                stdout=result_stdout,
                stderr=result_stderr,
            )
    except CliUsageError as exc:
        result_stdout = io.StringIO()
        result_stderr = io.StringIO()
        message = str(exc)
        _emit(_error("invalid_input", message), result_stdout)
        _diagnose(message, result_stderr)
        exit_code = 2
    except StorageBusyError:
        result_stdout = io.StringIO()
        result_stderr = io.StringIO()
        _emit(_error("busy", STORAGE_BUSY_MESSAGE), result_stdout)
        _diagnose(STORAGE_BUSY_MESSAGE, result_stderr)
        exit_code = 3
    except AdapterSetupError:
        result_stdout = io.StringIO()
        result_stderr = io.StringIO()
        _emit(_error("setup_failed", ADAPTER_SETUP_FAILED_MESSAGE), result_stdout)
        _diagnose(ADAPTER_SETUP_FAILED_MESSAGE, result_stderr)
        exit_code = 2
    except SemanticSetupRequiredError:
        result_stdout = io.StringIO()
        result_stderr = io.StringIO()
        _emit(_error("setup_required", SEMANTIC_SETUP_MESSAGE), result_stdout)
        _diagnose(SEMANTIC_SETUP_MESSAGE, result_stderr)
        exit_code = 2
    except CliOperationRejectedError:
        result_stdout = io.StringIO()
        result_stderr = io.StringIO()
        message = "The failure-memory service rejected the operation."
        _emit(_error("operation_rejected", message), result_stdout)
        _diagnose(message, result_stderr)
        exit_code = 2
    except Exception:
        result_stdout = io.StringIO()
        result_stderr = io.StringIO()
        message = "Internal failure-memory error."
        _emit(_error("internal_error", message), result_stdout)
        _diagnose(message, result_stderr)
        exit_code = 1

    if service is not None:
        try:
            service.close()
        except Exception:
            result_stdout = io.StringIO()
            result_stderr = io.StringIO()
            message = "Internal failure-memory error."
            _emit(_error("internal_error", message), result_stdout)
            _diagnose(message, result_stderr)
            exit_code = 1

    sys.stdout.write(result_stdout.getvalue())
    sys.stdout.flush()
    sys.stderr.write(result_stderr.getvalue())
    sys.stderr.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
