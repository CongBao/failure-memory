from __future__ import annotations

import importlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from failure_memory.adapters.dependency_runtime.manager import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_REVISION,
)
from failure_memory.domain.retrieval import EmbeddingSpec
from failure_memory.ports.retrieval import EmbeddingProviderPort


class FastEmbedProvider(EmbeddingProviderPort):
    def __init__(
        self,
        cache_dir: Path,
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        revision: str = DEFAULT_EMBEDDING_REVISION,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> None:
        module = importlib.import_module("fastembed")
        embedding_class = module.TextEmbedding
        self._model: Any = embedding_class(model_name=model, cache_dir=str(cache_dir))
        self._spec = EmbeddingSpec(
            provider="fastembed",
            model=model,
            revision=revision,
            dimensions=dimensions,
        )

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            tuple(float(value) for value in vector) for vector in self._model.embed(list(texts))
        )

    def embed_query(self, text: str) -> tuple[float, ...]:
        vectors = self.embed_documents([text])
        if len(vectors) != 1:
            raise RuntimeError("embedding provider returned an unexpected vector count")
        return vectors[0]
