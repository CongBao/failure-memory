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
        self._cache_dir = cache_dir
        self._model_name = model
        self._model: Any | None = None
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
        model = self._load_model()
        return tuple(
            tuple(float(value) for value in vector) for vector in model.embed(list(texts))
        )

    def embed_query(self, text: str) -> tuple[float, ...]:
        vectors = self.embed_documents([text])
        if len(vectors) != 1:
            raise RuntimeError("embedding provider returned an unexpected vector count")
        return vectors[0]

    def _load_model(self) -> Any:
        if self._model is None:
            module = importlib.import_module("fastembed")
            embedding_class = module.TextEmbedding
            self._model = embedding_class(
                model_name=self._model_name,
                cache_dir=str(self._cache_dir),
            )
        return self._model
