import math

import pytest

from app.services.embedding_service import EMBEDDING_DIMENSIONS, EmbeddingService


_MODEL_UNAVAILABLE_RUNTIME_MARKERS = (
    "connection",
    "download",
    "huggingface",
    "model",
    "offline",
    "sentence-transformers",
    "torch",
)


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    dot_product = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot_product / (left_norm * right_norm)


async def _embed_or_skip(service: EmbeddingService, text: str) -> list[float]:
    try:
        return await service.embed(text)
    except (ImportError, OSError, ConnectionError, TimeoutError) as exc:
        pytest.skip(f"sentence-transformers model unavailable: {exc}")
    except RuntimeError as exc:
        if any(marker in str(exc).lower() for marker in _MODEL_UNAVAILABLE_RUNTIME_MARKERS):
            pytest.skip(f"sentence-transformers model unavailable: {exc}")
        raise


@pytest.mark.asyncio
async def test_embedding_has_expected_dimensions() -> None:
    service = EmbeddingService()

    embedding = await _embed_or_skip(service, "a marimo notebook about climate data")

    assert len(embedding) == EMBEDDING_DIMENSIONS
    assert all(isinstance(value, float) for value in embedding)


@pytest.mark.asyncio
async def test_similar_texts_rank_above_dissimilar_texts() -> None:
    service = EmbeddingService()

    query = await _embed_or_skip(service, "visualize weather and climate data in a notebook")
    similar = await _embed_or_skip(service, "analyze temperature and rainfall datasets with charts")
    dissimilar = await _embed_or_skip(service, "bake sourdough bread with a cast iron dutch oven")

    assert _cosine_similarity(query, similar) > _cosine_similarity(query, dissimilar)
