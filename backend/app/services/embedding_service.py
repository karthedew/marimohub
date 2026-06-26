import asyncio
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384


def _load_model() -> "SentenceTransformer":
    # Imported lazily so the heavy sentence-transformers/torch stack is not
    # loaded at process startup, only on first embedding request.
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    return SentenceTransformer(MODEL_NAME)


def _encode(model: "SentenceTransformer", text: str) -> list[float]:
    """Encode text, normalizing the loosely typed sentence-transformers result."""
    encoded: list[float] = cast(
        "list[float]",
        model.encode(text).tolist(),  # pyright: ignore[reportUnknownMemberType]
    )
    return [float(value) for value in encoded]


class EmbeddingService:
    """Lazily loads a sentence-transformer model and embeds text with it."""

    def __init__(self) -> None:
        """Initialize the service without loading the model yet."""
        super().__init__()
        self._model: SentenceTransformer | None = None
        self._model_lock = asyncio.Lock()

    async def _get_model(self) -> "SentenceTransformer":
        if self._model is not None:
            return self._model

        async with self._model_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(_load_model)

        return self._model

    async def embed(self, text: str) -> list[float]:
        """Return the embedding vector for the given text."""
        model = await self._get_model()
        embedding = await asyncio.to_thread(_encode, model, text)

        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"Expected {EMBEDDING_DIMENSIONS} embedding dimensions, got {len(embedding)}"
            )

        return embedding


embedding_service = EmbeddingService()


async def embed(text: str) -> list[float]:
    """Embed text using the shared module-level embedding service."""
    return await embedding_service.embed(text)
