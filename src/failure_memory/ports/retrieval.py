from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from failure_memory.domain.learning import SimilarityPair
from failure_memory.domain.retrieval import (
    EmbeddingSpec,
    RecallQuery,
    RetrievalDocument,
    RetrievalIndexStatus,
    RetrievalMatch,
    RetrievalProfile,
)


class EmbeddingProviderPort(Protocol):
    @property
    def spec(self) -> EmbeddingSpec: ...

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...


class RetrievalIndexPort(Protocol):
    @property
    def profile_name(self) -> str: ...

    @property
    def profile(self) -> RetrievalProfile: ...

    def status(self) -> RetrievalIndexStatus: ...

    def sync(self, documents: Sequence[RetrievalDocument]) -> int: ...

    def search_lexical(
        self,
        query: RecallQuery,
        *,
        limit: int,
    ) -> Sequence[RetrievalMatch]: ...

    def search_semantic(
        self,
        query: RecallQuery,
        *,
        limit: int,
    ) -> Sequence[RetrievalMatch]: ...

    def similar_pairs(
        self,
        documents: Sequence[RetrievalDocument],
        *,
        distance_threshold: float,
    ) -> Sequence[SimilarityPair]: ...

    def close(self) -> None: ...
