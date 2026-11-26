"""Persistent, per-workspace RAG knowledge base built on the existing FAISS pipeline."""

import hashlib
import logging
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from config.settings import settings
from database import DocumentChunk, KnowledgeDocument, get_db_context
from pydantic import BaseModel, Field
from rag.document_loader import load_document
from rag.text_splitter import create_text_splitter
from rag.vector_store import FAISSVectorStore
from services.auth_service import require_owned_session_token
from services.llm_service import get_llm_service
from utils.file_utils import compute_file_hash

logger = logging.getLogger(__name__)


class QuestionList(BaseModel):
    questions: list[str] = Field(default_factory=list)


def _safe_token(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", token)[:80] or "default"


def _text_hash(source_type: str, text: str) -> str:
    return hashlib.sha256(f"{source_type}\0{text}".encode()).hexdigest()


@lru_cache(maxsize=16)
def _shared_store(session_token: str, model_name: str) -> FAISSVectorStore:
    store = FAISSVectorStore(model_name=model_name)
    if not store.load(Path(settings.vector_store_path) / _safe_token(session_token)):
        store = FAISSVectorStore(model_name=model_name)
    return store


class KnowledgeBaseService:
    def __init__(self, session_token: str, expected_user_id: int):
        self.session_token = session_token
        self.session_id = require_owned_session_token(session_token, expected_user_id)
        self.store = _shared_store(session_token, settings.embedding_model)

    def _db_session_id(self, db) -> int:
        return self.session_id

    def _persist(self) -> None:
        self.store.save(Path(settings.vector_store_path) / _safe_token(self.session_token))

    def _index_chunks(self, db, session_id: int, record, chunks) -> dict:
        source = f"doc:{record.id}"
        for chunk in chunks:
            chunk.metadata = {**chunk.metadata, "source": source, "file_name": record.file_name}
        self.store.add_documents(chunks)

        for chunk in chunks:
            db.add(
                DocumentChunk(
                    session_id=session_id,
                    source_type=record.source_type,
                    source_id=record.id,
                    chunk_text=chunk.text,
                    chunk_index=chunk.chunk_index,
                )
            )

        record.chunk_count = len(chunks)
        record.status = "ready" if chunks else "failed"
        record.processed_at = datetime.now()
        return {
            "id": record.id,
            "file_name": record.file_name,
            "status": record.status,
            "chunk_count": record.chunk_count,
            "word_count": record.word_count,
            "duplicate": False,
        }

    def add_document(self, file_path: str, display_name: str | None = None) -> dict:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Document not found: {file_path}")
        if path.suffix.lower() not in {".pdf", ".docx", ".txt"}:
            raise ValueError("Only PDF, DOCX and TXT documents are supported.")

        file_hash = compute_file_hash(str(path))
        with get_db_context() as db:
            session_id = self._db_session_id(db)
            duplicate = (
                db.query(KnowledgeDocument)
                .filter(
                    KnowledgeDocument.session_id == session_id,
                    KnowledgeDocument.file_hash == file_hash,
                )
                .first()
            )
            if duplicate:
                return {
                    "id": duplicate.id,
                    "file_name": duplicate.file_name,
                    "status": duplicate.status,
                    "chunk_count": duplicate.chunk_count,
                    "duplicate": True,
                }

            documents = load_document(str(path))
            if not documents or not any(doc.content.strip() for doc in documents):
                raise ValueError("No readable text was found in this document.")

            record = KnowledgeDocument(
                session_id=session_id,
                file_name=display_name or path.name,
                file_path=str(path),
                file_hash=file_hash,
                source_type="document",
                status="processing",
                word_count=sum(len(doc.content.split()) for doc in documents),
            )
            db.add(record)
            db.flush()
            chunks = create_text_splitter().split_documents(documents)
            result = self._index_chunks(db, session_id, record, chunks)
        self._persist()
        return result

    def add_text(self, text: str, display_name: str, source_type: str = "document") -> dict:
        if not text or not text.strip():
            raise ValueError("No readable text was found in this document.")
        clean_type = re.sub(r"[^a-z0-9_-]", "_", (source_type or "document").lower())[:30]
        clean_type = clean_type or "document"
        content_hash = _text_hash(clean_type, text)
        with get_db_context() as db:
            session_id = self._db_session_id(db)
            duplicate = (
                db.query(KnowledgeDocument)
                .filter(
                    KnowledgeDocument.session_id == session_id,
                    KnowledgeDocument.file_hash == content_hash,
                )
                .first()
            )
            if duplicate:
                return {
                    "id": duplicate.id,
                    "file_name": duplicate.file_name,
                    "status": duplicate.status,
                    "chunk_count": duplicate.chunk_count,
                    "duplicate": True,
                }

            chunks = create_text_splitter().split_text(
                text, metadata={"type": clean_type}, source=display_name
            )
            if not chunks:
                raise ValueError("No readable text was found in this document.")
            record = KnowledgeDocument(
                session_id=session_id,
                file_name=display_name[:255],
                file_path=f"text://{clean_type}/{content_hash[:12]}",
                file_hash=content_hash,
                source_type=clean_type,
                status="processing",
                word_count=len(text.split()),
            )
            db.add(record)
            db.flush()
            result = self._index_chunks(db, session_id, record, chunks)
        self._persist()
        return result

    def list_documents(self) -> list[dict]:
        with get_db_context() as db:
            session_id = self._db_session_id(db)
            rows = (
                db.query(KnowledgeDocument)
                .filter(KnowledgeDocument.session_id == session_id)
                .order_by(KnowledgeDocument.created_at.desc())
                .all()
            )
            return [
                {
                    "id": row.id,
                    "file_name": row.file_name,
                    "status": row.status,
                    "chunk_count": row.chunk_count,
                    "word_count": row.word_count,
                    "error": row.error_message,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                }
                for row in rows
            ]

    def delete_document(self, document_id: int) -> bool:
        with get_db_context() as db:
            session_id = self._db_session_id(db)
            record = (
                db.query(KnowledgeDocument)
                .filter(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.session_id == session_id,
                )
                .first()
            )
            if not record:
                return False
            self.store.delete_by_source(f"doc:{record.id}")
            db.query(DocumentChunk).filter(DocumentChunk.source_id == record.id).delete()
            db.delete(record)
            self._persist()
        return True

    def rebuild_index(self) -> int:
        """Rebuild the FAISS index from stored chunks when the index file is lost."""
        with get_db_context() as db:
            session_id = self._db_session_id(db)
            rows = (
                db.query(DocumentChunk)
                .filter(DocumentChunk.session_id == session_id)
                .order_by(DocumentChunk.source_id, DocumentChunk.chunk_index)
                .all()
            )
            self.store.clear()
            from rag.text_splitter import TextChunk

            for row in rows:
                self.store.add_documents(
                    [
                        TextChunk(
                            text=row.chunk_text,
                            metadata={"source": f"doc:{row.source_id}"},
                            chunk_index=row.chunk_index,
                            source=f"doc:{row.source_id}",
                        )
                    ]
                )
            self._persist()
        return self.store.count()

    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        if self.store.count() == 0:
            return []
        results = self.store.search(query, top_k or settings.top_k_retrieval)
        return [
            {
                "text": document.text,
                "source": document.metadata.get("file_name", str(document.metadata.get("source"))),
                "score": round(score, 4),
                "chunk_index": document.metadata.get("page", document.id),
            }
            for document, score in results
        ]

    def build_context(self, query: str, max_chars: int | None = None) -> str:
        results = self.search(query, settings.max_rag_chunks)
        if not results:
            return ""
        budget = max_chars or settings.max_context_chars
        parts: list[str] = []
        used = 0
        for result in results:
            block = f"[Source: {result['source']}]\n{result['text']}"
            if used + len(block) > budget and parts:
                break
            parts.append(block)
            used += len(block)
        return "\n\n---\n\n".join(parts)

    def answer_query(self, query: str) -> dict:
        context = self.build_context(query)
        if not context:
            return {
                "answer": "I could not find relevant material in your knowledge base. Upload documents first.",
                "sources": [],
                "grounded": False,
            }
        prompt = f"""Answer the question using only the provided context. Cite sources inline as [Source: name].
If the context does not contain the answer, say so explicitly. Do not invent facts.

CONTEXT:
{context}

QUESTION:
{query}
"""
        answer = get_llm_service().generate(prompt).content
        return {
            "answer": answer,
            "sources": sorted({result["source"] for result in self.search(query)}),
            "grounded": True,
        }

    def generate_questions(self, topic: str, count: int = 5) -> list[str]:
        context = self.build_context(topic)
        if not context:
            return []
        prompt = f"""Generate {count} specific interview questions based only on the material below.
Ask questions that test understanding, application, and trade-offs.

TOPIC: {topic}
MATERIAL:
{context}

Return JSON with a "questions" array of strings."""
        result = get_llm_service().generate_structured(prompt, QuestionList)
        return [question.strip() for question in result.questions if question.strip()][:count]

    def stats(self) -> dict:
        return {
            "documents": len(self.list_documents()),
            "chunks": self.store.count(),
            "indexed": self.store.count() > 0,
        }


def get_knowledge_base(session_token: str, expected_user_id: int) -> KnowledgeBaseService:
    return KnowledgeBaseService(session_token, expected_user_id)


def reset_knowledge_base() -> None:
    from rag.embeddings import reset_embedding_model

    _shared_store.cache_clear()
    reset_embedding_model()
