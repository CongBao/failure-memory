from __future__ import annotations

STORAGE_BUSY_MESSAGE = "Failure-memory storage is busy; retry the operation."
SEMANTIC_SETUP_MESSAGE = (
    "Semantic adapter setup is required; run failure-memory adapters install."
)
ADAPTER_SETUP_FAILED_MESSAGE = (
    "Optional semantic adapter setup failed; verify network trust and retry."
)


class StorageBusyError(RuntimeError):
    """The local event store remained locked after its bounded write retries."""


class SemanticSetupRequiredError(RuntimeError):
    """Semantic retrieval was requested before its optional adapter was installed."""


class AdapterSetupError(RuntimeError):
    """The explicitly requested optional adapter setup did not validate."""
