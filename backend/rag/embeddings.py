import os
from functools import lru_cache
from typing import Any

import numpy as np
from config.settings import settings
from sentence_transformers import SentenceTransformer
from services.resource_monitor import acquire_heavy_task


class EmbeddingModel:
    _instance: Any = None
    _model: Any = None
    _dimension: int = 0

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._model is None:
            self._load_model()

    def _load_model(self):
        device = settings.embedding_device
        if not bool(getattr(settings, "allow_cuda_inference", False)):
            device = "cpu"
        model_name = settings.embedding_model

        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        self._model = SentenceTransformer(model_name, device=device)
        self._dimension = self._model.get_embedding_dimension()

    def encode(
        self, texts: list[str], batch_size: int = 32, show_progress_bar: bool = False
    ) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            return np.array([])

        with acquire_heavy_task("embedding_generation"):
            embeddings = self._model.encode(
                texts,
                batch_size=batch_size,
                show_progress_bar=show_progress_bar,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
        return embeddings

    def encode_single(self, text: str) -> np.ndarray:
        return self.encode([text])[0]

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return settings.embedding_model


@lru_cache(maxsize=1)
def get_embedding_model() -> EmbeddingModel:
    return EmbeddingModel()


def reset_embedding_model() -> None:
    EmbeddingModel._instance = None
    EmbeddingModel._model = None
    EmbeddingModel._dimension = 0
    cache_clear = getattr(get_embedding_model, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


def compute_embeddings(texts: list[str]) -> list[list[float]]:
    model = get_embedding_model()
    embeddings = model.encode(texts)
    return embeddings.tolist()


def compute_single_embedding(text: str) -> list[float]:
    model = get_embedding_model()
    embedding = model.encode_single(text)
    return embedding.tolist()
