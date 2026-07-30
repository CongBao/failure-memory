from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SourceStore:
    database: Path
    source_store_id: str
    fingerprint_domain: str
    schema_version: int
    content_fingerprint: str
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class StoreImportPlan:
    source: SourceStore
    target_store_id: str
    already_imported: bool
    importable_counts: dict[str, int]
    skipped_counts: dict[str, int]
    conflicts: tuple[str, ...]

    @property
    def can_apply(self) -> bool:
        return not self.already_imported and not self.conflicts


@dataclass(frozen=True, slots=True)
class StoreImportResult:
    import_id: str
    source_store_id: str
    content_fingerprint: str
    imported_counts: dict[str, int]
    skipped_counts: dict[str, int]
    already_imported: bool
