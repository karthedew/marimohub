import asyncio
from typing import Any


MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384


def _load_model() -> Any:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(MODEL_NAME)


class EmbeddingService:
    def __init__(self) -> None:
        self._model: Any | None = None
        self._model_lock = asyncio.Lock()

    async def _get_model(self) -> Any:
        if self._model is not None:
            return self._model

        async with self._model_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(_load_model)

        return self._model

    async def embed(self, text: str) -> list[float]:
        model = await self._get_model()
        vector = await asyncio.to_thread(model.encode, text)
        embedding = [float(value) for value in vector.tolist()]

        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise RuntimeError(f"Expected {EMBEDDING_DIMENSIONS} embedding dimensions, got {len(embedding)}")

        return embedding


embedding_service = EmbeddingService()


async def embed(text: str) -> list[float]:
    return await embedding_service.embed(text)
