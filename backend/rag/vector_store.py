import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from config.settings import settings
from rag.text_splitter import TextChunk

logger = logging.getLogger(__name__)


@dataclass
class VectorDocument:
    text: str
    metadata: dict
    score: float = 0.0
    id: int = 0


class FAISSVectorStore:
    """Inner-product vector store with on-disk persistence and source deletion."""

    INDEX_FILE = "index.faiss"
    META_FILE = "documents.json"

    def __init__(
        self,
        embedding_model: Any | None = None,
        dimension: int | None = None,
    ):
        from rag.embeddings import get_embedding_model

        self.embedding_model = embedding_model or get_embedding_model()
        self.dimension = dimension or self.embedding_model.dimension
        self.index: Any = None
        try:
            import faiss

            self.index = faiss.IndexFlatIP(self.dimension)
            self._faiss_available = True
        except ImportError:
            self._faiss_available = False
        self.documents: list[VectorDocument] = []
        self._embeddings: list[np.ndarray] = []

    def add_documents(self, chunks: list[TextChunk]) -> list[str]:
        if not chunks:
            return []
        texts = [chunk.text for chunk in chunks]
        encoded = np.asarray(self.embedding_model.encode(texts), dtype="float32")

        if self._faiss_available and self.index is not None:
            self.index.add(encoded)
        self._embeddings.extend(np.asarray(vector, dtype="float32") for vector in encoded)

        ids: list[str] = []
        for chunk in chunks:
            document = VectorDocument(
                text=chunk.text,
                metadata=dict(chunk.metadata),
                id=len(self.documents),
            )
            self.documents.append(document)
            ids.append(str(document.id))
        return ids

    def search(self, query: str, top_k: int | None = None) -> list[tuple[VectorDocument, float]]:
        top_k = top_k or settings.top_k_retrieval
        encoded = np.asarray(self.embedding_model.encode([query]), dtype="float32")
        return self._search_by_embeddings(encoded, top_k)

    def search_by_vector(
        self, vector: list[float], top_k: int | None = None
    ) -> list[tuple[VectorDocument, float]]:
        top_k = top_k or settings.top_k_retrieval
        query_vec = np.asarray(vector, dtype="float32").reshape(1, -1)
        return self._search_by_embeddings(query_vec, top_k)

    def _search_by_embeddings(
        self, query_embedding: np.ndarray, top_k: int
    ) -> list[tuple[VectorDocument, float]]:
        if self.count() == 0:
            return []
        top_k = max(1, min(top_k, self.count()))

        if self._faiss_available and self.index is not None:
            scores, indices = self.index.search(query_embedding, top_k)
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if 0 <= idx < len(self.documents):
                    document = self.documents[int(idx)]
                    document.score = float(score)
                    results.append((document, float(score)))
            return results

        all_embeddings = np.vstack(self._embeddings)
        query_vec = query_embedding.reshape(1, -1)
        similarities = (all_embeddings @ query_vec.T).flatten()
        top_indices = np.argsort(similarities)[::-1][:top_k]
        results = []
        for idx in top_indices:
            document = self.documents[int(idx)]
            document.score = float(similarities[idx])
            results.append((document, float(similarities[idx])))
        return results

    def delete_by_source(self, source: str) -> int:
        """Remove every chunk whose metadata ``source`` matches, then rebuild the index."""
        keep = [
            i
            for i, document in enumerate(self.documents)
            if str(document.metadata.get("source", "")) != source
        ]
        removed = len(self.documents) - len(keep)
        if removed == 0:
            return 0

        self.documents = [self.documents[i] for i in keep]
        self._embeddings = [self._embeddings[i] for i in keep]
        for new_id, document in enumerate(self.documents):
            document.id = new_id
        self._rebuild_index()
        return removed

    def _rebuild_index(self) -> None:
        if not self._faiss_available:
            return
        import faiss

        self.index = faiss.IndexFlatIP(self.dimension)
        if self._embeddings:
            self.index.add(np.vstack(self._embeddings).astype("float32"))

    def clear(self) -> None:
        self.documents = []
        self._embeddings = []
        self._rebuild_index()

    def count(self) -> int:
        return len(self.documents)

    def save(self, directory: str | Path) -> None:
        """Persist the index and document metadata to ``directory``."""
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        if self._faiss_available and self.index is not None:
            import faiss

            faiss.write_index(self.index, str(path / self.INDEX_FILE))

        payload = {
            "dimension": self.dimension,
            "embedding_model": getattr(self.embedding_model, "model_name", ""),
            "documents": [
                {"text": doc.text, "metadata": doc.metadata, "id": doc.id} for doc in self.documents
            ],
            "embeddings": [vector.tolist() for vector in self._embeddings],
            "faiss_available": self._faiss_available,
        }
        index_tmp = path / f"{self.INDEX_FILE}.tmp"
        meta_tmp = path / f"{self.META_FILE}.tmp"
        if self._faiss_available and self.index is not None:
            import faiss

            faiss.write_index(self.index, str(index_tmp))
        meta_tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        if self._faiss_available and self.index is not None:
            os.replace(index_tmp, path / self.INDEX_FILE)
        os.replace(meta_tmp, path / self.META_FILE)

    def load(self, directory: str | Path) -> bool:
        """Load a previously persisted index. Returns False when none exists."""
        path = Path(directory)
        metadata_path = path / self.META_FILE
        if not metadata_path.exists():
            return False

        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        saved_model = str(payload.get("embedding_model") or "")
        current_model = str(getattr(self.embedding_model, "model_name", "") or "")
        if saved_model and current_model and saved_model != current_model:
            logger.warning(
                "Index built with %s does not match current model %s; rebuild required.",
                saved_model,
                current_model,
            )
            return False
        self.documents = [
            VectorDocument(
                text=item["text"], metadata=item.get("metadata", {}), id=item.get("id", i)
            )
            for i, item in enumerate(payload.get("documents", []))
        ]
        self._embeddings = [
            np.asarray(vector, dtype="float32") for vector in payload.get("embeddings", [])
        ]
        saved_dimension = int(payload.get("dimension", self.dimension))
        if saved_dimension != self.dimension:
            logger.warning(
                "Index dimension %s does not match current dimension %s; rebuild required.",
                saved_dimension,
                self.dimension,
            )
            return False
        self.dimension = saved_dimension

        if self._faiss_available and (path / self.INDEX_FILE).exists():
            import faiss

            self.index = faiss.read_index(str(path / self.INDEX_FILE))
        else:
            self._rebuild_index()
        return True


def create_vector_store(store_type: str | None = None, **kwargs: Any) -> FAISSVectorStore:
    if store_type and store_type.lower() != "faiss":
        raise ValueError(f"Unsupported vector store type: {store_type}. Only 'faiss' is supported.")
    return FAISSVectorStore(**kwargs)
