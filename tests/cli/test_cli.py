from __future__ import annotations

import io
import json
import logging
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import failure_memory.application.service as service_module
import failure_memory.cli.main as cli_module
from failure_memory.adapters.event_store.sqlite.connection import connect_sqlite
from failure_memory.application.errors import AdapterSetupError, StorageBusyError

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _set_permissive_umask() -> None:
    os.umask(0)


def _assert_owner_only_runtime_tree(root: Path) -> None:
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    assert directories
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories)
    files = [path for path in root.rglob("*") if path.is_file()]
    assert {path.name for path in files} >= {"identity.key", "failure-memory.sqlite3"}
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)


def _run_cli(
    home: Path,
    *arguments: str,
    input_text: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "FAILURE_MEMORY_HOME": str(home),
        "FAILURE_MEMORY_HARNESS": "pytest-cli",
        "FAILURE_MEMORY_SESSION_ID": "cli-subprocess-tests",
    }
    return subprocess.run(
        [sys.executable, "-m", "failure_memory.cli.main", *arguments],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
        env=environment,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only mode regression")
def test_direct_cli_hardens_explicit_failure_memory_home_under_permissive_umask(
    tmp_path: Path,
) -> None:
    """Would fail if the direct CLI exposed its explicit runtime tree under umask 000."""
    home = tmp_path / "explicit-runtime"
    environment = {
        **os.environ,
        "FAILURE_MEMORY_HOME": str(home),
        "FAILURE_MEMORY_HARNESS": "pytest-cli-permissions",
    }

    completed = subprocess.run(
        [sys.executable, "-m", "failure_memory.cli.main", "metrics"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
        preexec_fn=_set_permissive_umask,
    )

    assert completed.returncode == 0, completed.stderr
    _assert_owner_only_runtime_tree(home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only mode regression")
def test_direct_cli_uses_explicit_global_root_and_ignores_harness_plugin_data(
    tmp_path: Path,
) -> None:
    """Would fail if a harness-specific root partitioned the global memory."""
    plugin_data = tmp_path / "shared-plugin-data"
    plugin_data.mkdir(mode=0o777)
    plugin_data.chmod(0o777)
    global_root = tmp_path / "global-failure-memory"
    environment = {
        **os.environ,
        "PLUGIN_DATA": str(plugin_data),
        "FAILURE_MEMORY_HOME": str(global_root),
        "FAILURE_MEMORY_HARNESS": "pytest-cli-permissions",
    }
    environment.pop("CLAUDE_PLUGIN_DATA", None)

    completed = subprocess.run(
        [sys.executable, "-m", "failure_memory.cli.main", "metrics"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
        preexec_fn=_set_permissive_umask,
    )

    assert completed.returncode == 0, completed.stderr
    assert stat.S_IMODE(plugin_data.stat().st_mode) == 0o777
    assert list(plugin_data.iterdir()) == []
    _assert_owner_only_runtime_tree(global_root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only mode regression")
def test_direct_cli_hardens_platform_default_under_permissive_umask(
    tmp_path: Path,
) -> None:
    """Would fail if the platform-default runtime hierarchy inherited public modes."""
    home = tmp_path / "home"
    xdg_data = tmp_path / "xdg-data"
    environment = {
        **os.environ,
        "HOME": str(home),
        "XDG_DATA_HOME": str(xdg_data),
        "FAILURE_MEMORY_HARNESS": "pytest-cli-permissions",
    }
    for name in ("FAILURE_MEMORY_HOME", "PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        environment.pop(name, None)
    root = (
        home / "Library" / "Application Support" / "failure-memory"
        if sys.platform == "darwin"
        else xdg_data / "failure-memory"
    )

    completed = subprocess.run(
        [sys.executable, "-m", "failure_memory.cli.main", "metrics"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
        preexec_fn=_set_permissive_umask,
    )

    assert completed.returncode == 0, completed.stderr
    _assert_owner_only_runtime_tree(root)


def _candidate(**changes: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "summary": "The user supplied a new requirement after the result.",
        "classification": "requirement_update",
        "expectation_source": "none",
        "observed_outcome_at": NOW.isoformat(),
        "outcome_mismatch": True,
        "material_impact_or_recurrence_risk": True,
        "controllable_with_prior_information": True,
        "durable_lesson": True,
    }
    candidate.update(changes)
    return candidate


def test_cluster_proposal_without_semantic_adapter_is_an_actionable_setup_error(
    tmp_path: Path,
) -> None:
    completed = _run_cli(tmp_path / "data", "learning", "cluster")

    assert completed.returncode == 2
    assert json.loads(completed.stdout)["error"]["code"] == "setup_required"
    assert "adapters install" in completed.stderr


def test_learning_proposals_lists_reviewable_clusters_as_machine_json(
    tmp_path: Path,
) -> None:
    completed = _run_cli(tmp_path / "data", "learning", "proposals")

    assert completed.returncode == 0, completed.stderr
    assert _json_stdout(completed) == {
        "scope": "global_personal",
        "proposals": [],
    }


def test_offline_learning_evaluation_is_private_shadow_evidence_only(
    tmp_path: Path,
) -> None:
    home = tmp_path / "data"
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "tiny-public-synthetic",
                "version": "1",
                "capture_cases": [
                    {
                        "id": "requirement-update",
                        "candidate": _candidate(),
                        "expected_decision": "reject",
                        "negative": True,
                    },
                    {
                        "id": "real-failure",
                        "candidate": _candidate(
                            summary="A migration skipped its established preflight.",
                            classification="real_failure",
                            expectation_source="accepted_design",
                            expectation_established_at=(NOW - timedelta(minutes=1)).isoformat(),
                        ),
                        "expected_decision": "accept",
                        "negative": False,
                    },
                ],
                "recall_cases": [
                    {
                        "id": "hybrid-positive",
                        "lexical": ["lesson-a"],
                        "semantic": ["lesson-a"],
                        "accepted_clusters": [],
                        "relevant": ["lesson-a"],
                        "top_k": 3,
                        "negative": False,
                    },
                    {
                        "id": "negative-no-injection",
                        "lexical": [],
                        "semantic": [],
                        "accepted_clusters": [],
                        "relevant": [],
                        "top_k": 3,
                        "negative": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    completed = _run_cli(
        home,
        "learning",
        "evaluate",
        "--corpus",
        str(corpus),
    )

    assert completed.returncode == 0, completed.stderr
    result = _json_stdout(completed)
    assert result["state"] == "shadow"
    assert result["corpus_name"] == "tiny-public-synthetic"
    assert result["case_count"] == 4
    assert result["negative_case_count"] == 2
    assert result["production_activated"] is False
    assert result["passed"] is False
    assert result["threshold_failures"] == [
        "minimum_case_count",
        "minimum_negative_case_count",
    ]
    assert result["metrics"] == {
        "capture_accuracy": 1.0,
        "requirement_update_false_positive_count": 0,
        "precision_at_1": 1.0,
        "precision_at_3": 1.0,
        "negative_no_injection_accuracy": 1.0,
    }
    evaluation_root = home / "adapters" / "evaluation" / "offline"
    reports = list(evaluation_root.glob("*/report.json"))
    assert len(reports) == 1
    if os.name != "nt":
        assert stat.S_IMODE(reports[0].stat().st_mode) == 0o600
    assert str(corpus) not in reports[0].read_text(encoding="utf-8")


def test_offline_learning_evaluation_rejects_unsafe_corpus_metadata(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "../private report",
                "version": "1",
                "capture_cases": [],
                "recall_cases": [],
            }
        ),
        encoding="utf-8",
    )

    completed = _run_cli(
        tmp_path / "data",
        "learning",
        "evaluate",
        "--corpus",
        str(corpus),
    )

    assert completed.returncode == 2
    assert _json_stdout(completed)["error"]["code"] == "operation_rejected"


def test_offline_learning_evaluation_rejects_duplicate_case_ids(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "duplicate-case-corpus",
                "version": "1",
                "capture_cases": [
                    {
                        "id": "duplicate",
                        "candidate": _candidate(),
                        "expected_decision": "reject",
                        "negative": True,
                    }
                ],
                "recall_cases": [
                    {
                        "id": "duplicate",
                        "lexical": [],
                        "semantic": [],
                        "accepted_clusters": [],
                        "relevant": [],
                        "top_k": 3,
                        "negative": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    completed = _run_cli(
        tmp_path / "data",
        "learning",
        "evaluate",
        "--corpus",
        str(corpus),
    )

    assert completed.returncode == 2
    assert _json_stdout(completed)["error"]["code"] == "operation_rejected"


def test_checked_in_core_corpus_meets_shadow_thresholds(tmp_path: Path) -> None:
    corpus = Path(__file__).parents[2] / "evals" / "v0.5-core.json"

    completed = _run_cli(
        tmp_path / "data",
        "learning",
        "evaluate",
        "--corpus",
        str(corpus),
    )

    assert completed.returncode == 0, completed.stderr
    result = _json_stdout(completed)
    assert result["case_count"] == 52
    assert result["negative_case_count"] == 21
    assert result["passed"] is True
    assert result["threshold_failures"] == []
    assert result["production_activated"] is False
    assert result["metrics"] == {
        "capture_accuracy": 1.0,
        "requirement_update_false_positive_count": 0,
        "precision_at_1": 26 / 30,
        "precision_at_3": 1.0,
        "negative_no_injection_accuracy": 1.0,
    }


def _record(capture_attempt_id: str) -> dict[str, object]:
    return {
        "capture_attempt_id": capture_attempt_id,
        "incident": {
            "outcome_summary": "The result did not include a newly requested field.",
            "expected_invariant": "Only requirements known before execution are binding.",
            "controllable_cause": "The new field was supplied after execution.",
            "material_impact": "No pre-existing contract was violated.",
            "recurrence_risk": "Requirement updates could be misclassified again.",
        },
        "lesson": {
            "title": "Separate requirement changes from failures",
            "rule": "Do not call a later requirement update a prior failure.",
            "prevention_action": "Check when the requirement became established.",
            "verification_action": "Compare requirement and outcome timestamps.",
            "applicability": "Agent outcome reviews.",
            "counterexamples": "A requirement established before execution.",
        },
    }


def _json_stdout(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def _run_json_input(
    tmp_path: Path,
    home: Path,
    input_text: str,
    *,
    from_file: bool,
) -> subprocess.CompletedProcess[str]:
    if from_file:
        input_path = tmp_path / "candidate.json"
        input_path.write_text(input_text, encoding="utf-8")
        return _run_cli(home, "evaluate", "--input", str(input_path))
    return _run_cli(home, "evaluate", "--input", "-", input_text=input_text)


def test_setup_status_returns_lexical_ready_json(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path / "data", "setup-status")

    assert completed.returncode == 0, completed.stderr
    assert _json_stdout(completed)["state"] == "lexical_ready"
    assert completed.stderr == ""


def test_metrics_returns_empty_append_ledger_counts(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path / "data", "metrics")

    assert completed.returncode == 0, completed.stderr
    assert _json_stdout(completed) == {
        "capture_attempt": 0,
        "incident": 0,
        "lesson": 0,
        "lesson_version": 0,
        "incident_lesson_relation": 0,
    }


def test_recall_metrics_and_index_status_are_machine_readable(tmp_path: Path) -> None:
    home = tmp_path / "data"

    recall_metrics = _run_cli(home, "recall-metrics")
    index_status = _run_cli(home, "index", "status")

    assert recall_metrics.returncode == 0, recall_metrics.stderr
    assert _json_stdout(recall_metrics)["recall_attempt"] == 0
    assert index_status.returncode == 0, index_status.stderr
    status = _json_stdout(index_status)
    assert status["lexical_available"] is True
    assert status["semantic_available"] is False
    assert status["indexed_documents"] == 0


def test_adapter_plan_is_explicit_and_does_not_install(tmp_path: Path) -> None:
    home = tmp_path / "data"

    completed = _run_cli(home, "adapters", "plan")

    assert completed.returncode == 0, completed.stderr
    plan = _json_stdout(completed)
    assert plan["adapter"] == "sqlite-vec-fastembed"
    assert plan["requirements"] == [
        "truststore==0.10.4",
        "sqlite-vec==0.1.9",
        "fastembed==0.8.0",
    ]
    assert plan["automatic_install"] is False
    assert not (home / "adapters" / "runtime").exists()


def test_adapter_install_failure_is_actionable_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailedManager:
        def __init__(self, _root: Path) -> None:
            pass

        def install(self) -> dict[str, object]:
            raise AdapterSetupError("secret certificate and path details")

    monkeypatch.setattr(cli_module, "AdapterRuntimeManager", FailedManager)

    exit_code = cli_module.main(["adapters", "install"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out)["error"]["code"] == "setup_failed"
    assert "verify network trust and retry" in captured.err
    assert "secret" not in captured.out + captured.err


def test_evaluate_accepts_stdin_and_rejects_requirement_update(tmp_path: Path) -> None:
    completed = _run_cli(
        tmp_path / "data",
        "evaluate",
        "--input",
        "-",
        input_text=json.dumps(_candidate()),
    )

    assert completed.returncode == 0, completed.stderr
    result = _json_stdout(completed)
    assert isinstance(result["capture_attempt_id"], str)
    assert result["capture_attempt_id"]
    assert result["decision"] == "reject"
    assert result["reason_codes"] == ["not_preexisting_requirement"]
    assert completed.stderr == ""


def test_record_from_file_refuses_a_rejected_capture(tmp_path: Path) -> None:
    home = tmp_path / "data"
    evaluated = _run_cli(
        home,
        "evaluate",
        "--input",
        "-",
        input_text=json.dumps(_candidate()),
    )
    capture_id = _json_stdout(evaluated)["capture_attempt_id"]
    assert isinstance(capture_id, str)
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(_record(capture_id)), encoding="utf-8")

    completed = _run_cli(home, "review", "--input", str(record_path))

    assert completed.returncode == 2
    assert _json_stdout(completed) == {
        "error": {
            "code": "operation_rejected",
            "message": "The failure-memory service rejected the operation.",
        }
    }
    assert completed.stderr == (
        "failure-memory: The failure-memory service rejected the operation.\n"
    )


def test_record_persists_an_accepted_failure_and_metrics_reflect_it(tmp_path: Path) -> None:
    home = tmp_path / "data"
    accepted_candidate = _candidate(
        summary="A migration skipped its established preflight.",
        classification="real_failure",
        expectation_source="accepted_design",
        expectation_established_at=(NOW - timedelta(minutes=1)).isoformat(),
    )
    evaluated = _run_cli(
        home,
        "evaluate",
        "--input",
        "-",
        input_text=json.dumps(accepted_candidate),
    )
    evaluated_result = _json_stdout(evaluated)
    assert evaluated_result["decision"] == "accept"
    capture_id = evaluated_result["capture_attempt_id"]
    assert isinstance(capture_id, str)

    reviewed = _run_cli(
        home,
        "review",
        "--input",
        "-",
        input_text=json.dumps(_record(capture_id)),
    )
    review_result = _json_stdout(reviewed)
    record_input = _record(capture_id)
    record_input.update(
        {
            "generalization_review_id": review_result["review_id"],
            "disposition": "create_distinct",
            "rationale_code": "no_related_lesson",
        }
    )
    recorded = _run_cli(
        home,
        "record",
        "--input",
        "-",
        input_text=json.dumps(record_input),
    )

    assert recorded.returncode == 0, recorded.stderr
    record_result = _json_stdout(recorded)
    assert record_result["relation"] == "novel"
    assert record_result["created_new_lesson"] is True
    metrics = _run_cli(home, "metrics")
    assert _json_stdout(metrics) == {
        "capture_attempt": 1,
        "incident": 1,
        "lesson": 1,
        "lesson_version": 1,
        "incident_lesson_relation": 1,
    }


def test_doctor_reports_integrity_without_revealing_private_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "secret-workspace-token"
    workspace.mkdir()
    home = tmp_path / "secret-data-root-token"

    completed = _run_cli(home, "doctor", cwd=workspace)

    assert completed.returncode == 0, completed.stderr
    result = _json_stdout(completed)
    assert result["integrity_check"] == "ok"
    assert "database_path" not in result
    for private_value in (
        str(workspace),
        str(home),
        "secret-workspace-token",
        "secret-data-root-token",
    ):
        assert private_value not in completed.stdout
        assert private_value not in completed.stderr
    assert completed.stderr == ""


def test_invalid_json_returns_machine_error_and_concise_stderr(tmp_path: Path) -> None:
    completed = _run_cli(
        tmp_path / "data",
        "evaluate",
        "--input",
        "-",
        input_text="{not-json",
    )

    assert completed.returncode == 2
    assert _json_stdout(completed) == {
        "error": {
            "code": "invalid_input",
            "message": "Input must contain one valid JSON object.",
        }
    }
    assert completed.stderr == "failure-memory: Input must contain one valid JSON object.\n"
    assert len(completed.stderr) < 100


def test_invalid_candidate_returns_boundary_error_on_stdout(tmp_path: Path) -> None:
    completed = _run_cli(
        tmp_path / "data",
        "evaluate",
        "--input",
        "-",
        input_text=json.dumps(_candidate(outcome_mismatch=1)),
    )

    assert completed.returncode == 2
    result = _json_stdout(completed)
    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["message"] == "outcome_mismatch must be a boolean"
    assert completed.stderr == "failure-memory: outcome_mismatch must be a boolean\n"


def test_missing_input_option_returns_machine_usage_error(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path / "data", "evaluate")

    assert completed.returncode == 2
    result = _json_stdout(completed)
    assert result["error"]["code"] == "invalid_input"
    assert "--input" in result["error"]["message"]
    assert completed.stderr.startswith("failure-memory: ")


@pytest.mark.parametrize("from_file", [False, True], ids=["stdin", "file"])
def test_duplicate_classification_is_invalid_and_never_persisted(
    tmp_path: Path, from_file: bool
) -> None:
    home = tmp_path / "data"
    accepted = _candidate(
        classification="real_failure",
        expectation_source="accepted_design",
        expectation_established_at=(NOW - timedelta(minutes=1)).isoformat(),
    )
    duplicate = json.dumps(accepted).replace(
        '"classification": "real_failure"',
        '"classification": "requirement_update", "classification": "real_failure"',
        1,
    )

    completed = _run_json_input(
        tmp_path,
        home,
        duplicate,
        from_file=from_file,
    )

    assert completed.returncode == 2
    assert _json_stdout(completed) == {
        "error": {
            "code": "invalid_input",
            "message": "Input must contain one valid JSON object.",
        }
    }
    metrics = _run_cli(home, "metrics")
    assert _json_stdout(metrics)["capture_attempt"] == 0


@pytest.mark.parametrize("from_file", [False, True], ids=["stdin", "file"])
@pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_json_is_invalid_for_every_input_source(
    tmp_path: Path, from_file: bool, non_finite: str
) -> None:
    encoded = json.dumps(_candidate()).replace(
        json.dumps(_candidate()["summary"]),
        non_finite,
        1,
    )

    completed = _run_json_input(
        tmp_path,
        tmp_path / "data",
        encoded,
        from_file=from_file,
    )

    assert completed.returncode == 2
    assert _json_stdout(completed) == {
        "error": {
            "code": "invalid_input",
            "message": "Input must contain one valid JSON object.",
        }
    }
    assert completed.stderr == "failure-memory: Input must contain one valid JSON object.\n"


def test_service_internal_failure_is_sanitized_without_dispatcher_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "token=private-runtime-secret /secret/workspace/path"

    class ExplodingService:
        closed = False

        def metrics(self) -> object:
            raise RuntimeError(secret)

        def close(self) -> None:
            self.closed = True

    service = ExplodingService()
    monkeypatch.setattr(cli_module, "create_local_service", lambda: service)
    caplog.set_level(logging.ERROR, logger="failure_memory.mcp.dispatcher")

    exit_code = cli_module.main(["metrics"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out) == {
        "error": {
            "code": "internal_error",
            "message": "Internal failure-memory error.",
        }
    }
    assert captured.err == "failure-memory: Internal failure-memory error.\n"
    assert secret not in captured.out + captured.err
    assert "Traceback" not in captured.err
    assert service.closed is True
    assert [
        record for record in caplog.records if record.name == "failure_memory.mcp.dispatcher"
    ] == []


def test_cli_serializes_busy_result_with_retryable_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Would fail if CLI callers could not distinguish retryable storage contention."""

    class Service:
        def close(self) -> None:
            pass

    envelope = {
        "content": [{"type": "text", "text": "Failure-memory storage is busy; retry."}],
        "structuredContent": {
            "error": {
                "code": "busy",
                "message": "Failure-memory storage is busy; retry the operation.",
            }
        },
        "isError": True,
    }
    monkeypatch.setattr(cli_module, "create_local_service", Service)
    monkeypatch.setattr(cli_module, "dispatch_tool", lambda *args, **kwargs: envelope)

    exit_code = cli_module.main(["metrics"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.out) == envelope["structuredContent"]
    assert captured.err == (
        "failure-memory: Failure-memory storage is busy; retry the operation.\n"
    )


def test_cli_real_write_contention_reaches_retryable_busy_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Would fail if service startup masked writer contention as an internal error."""
    home = tmp_path / "data"
    bootstrap = service_module.create_local_service(data_root=home)
    database = Path(bootstrap.store.database_path())
    bootstrap.close()
    blocker = connect_sqlite(database)
    real_connect = service_module.connect_sqlite

    def fast_busy_connect(path: Path):
        connection = real_connect(path)
        connection.execute("PRAGMA busy_timeout = 1")
        return connection

    monkeypatch.setattr(service_module, "connect_sqlite", fast_busy_connect)
    monkeypatch.setenv("FAILURE_MEMORY_HOME", str(home))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_candidate())))
    blocker.execute("BEGIN IMMEDIATE")
    try:
        exit_code = cli_module.main(["evaluate", "--input", "-"])
    finally:
        blocker.rollback()
        blocker.close()

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.out) == {
        "error": {
            "code": "busy",
            "message": "Failure-memory storage is busy; retry the operation.",
        }
    }
    assert captured.err == (
        "failure-memory: Failure-memory storage is busy; retry the operation.\n"
    )


def test_factory_internal_failure_is_sanitized_without_secret_or_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "password=" + "example-only /private/secret-data-root"

    def explode() -> object:
        raise RuntimeError(secret)

    monkeypatch.setattr(cli_module, "create_local_service", explode)

    exit_code = cli_module.main(["doctor"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["error"]["code"] == "internal_error"
    assert captured.err == "failure-memory: Internal failure-memory error.\n"
    assert secret not in captured.out + captured.err
    assert "example-only" not in captured.out + captured.err
    assert "/private/secret-data-root" not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_factory_storage_contention_is_a_retryable_busy_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def busy() -> object:
        raise StorageBusyError

    monkeypatch.setattr(cli_module, "create_local_service", busy)

    exit_code = cli_module.main(["metrics"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.out) == {
        "error": {
            "code": "busy",
            "message": "Failure-memory storage is busy; retry the operation.",
        }
    }
    assert captured.err == (
        "failure-memory: Failure-memory storage is busy; retry the operation.\n"
    )


def test_close_internal_failure_replaces_buffered_success_without_leaking(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "token=close-secret /private/close/path"

    class CloseExplodingService:
        def metrics(self) -> dict[str, int]:
            return {}

        def close(self) -> None:
            raise RuntimeError(secret)

    monkeypatch.setattr(
        cli_module,
        "create_local_service",
        lambda: CloseExplodingService(),
    )

    exit_code = cli_module.main(["metrics"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out) == {
        "error": {
            "code": "internal_error",
            "message": "Internal failure-memory error.",
        }
    }
    assert captured.err == "failure-memory: Internal failure-memory error.\n"
    assert captured.out.count("\n") == 1
    assert secret not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_non_finite_internal_result_becomes_one_strict_json_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class NonFiniteService:
        def metrics(self) -> dict[str, float]:
            return {"corrupt_metric": float("nan")}

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "create_local_service", lambda: NonFiniteService())

    exit_code = cli_module.main(["metrics"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["error"]["code"] == "internal_error"
    assert "NaN" not in captured.out
    assert captured.out.count("\n") == 1
    assert captured.err == "failure-memory: Internal failure-memory error.\n"
