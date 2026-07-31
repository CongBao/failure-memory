from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from failure_memory.adapters.embedding import fastembed as fastembed_module
from failure_memory.adapters.embedding.fastembed import FastEmbedProvider


def test_provider_defers_import_and_model_loading_until_first_embedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imports: list[str] = []
    constructions: list[tuple[str, str]] = []

    class Model:
        def __init__(self, *, model_name: str, cache_dir: str) -> None:
            constructions.append((model_name, cache_dir))

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(text)), 1.0] for text in texts]

    def import_module(name: str) -> object:
        imports.append(name)
        return SimpleNamespace(TextEmbedding=Model)

    monkeypatch.setattr(fastembed_module.importlib, "import_module", import_module)

    provider = FastEmbedProvider(tmp_path)

    assert imports == []
    assert constructions == []
    assert provider.embed_query("lesson") == (6.0, 1.0)
    assert provider.embed_query("again") == (5.0, 1.0)
    assert imports == ["fastembed"]
    assert len(constructions) == 1
