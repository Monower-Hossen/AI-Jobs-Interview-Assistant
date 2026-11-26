"""Knowledge base management, semantic search and grounded question answering."""

from pathlib import Path

import streamlit as st
from config.settings import runtime_path, settings
from services.knowledge_base import get_knowledge_base
from services.resource_monitor import ResourceBusyError
from utils.file_utils import save_uploaded_file

from frontend.shared.components import (
    confirm_delete,
    data_table,
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
    success_toast,
)

SUPPORTED = {".pdf", ".docx", ".txt"}


def _resource_busy_warning(exc: ResourceBusyError) -> None:
    st.warning(
        "This action is temporarily unavailable because system memory is low. "
        f"{exc} Document indexing stays local; cloud interviews continue with Groq/Gemini."
    )


def show_knowledge_page(embedded: bool = False) -> None:
    if not embedded:
        page_header(
            "RAG knowledge base",
            "Upload documents to ground interviews and answers in your own material",
            ":material/library_books:",
        )
    try:
        kb = get_knowledge_base(st.session_state.user_session_id, st.session_state.get("user_id"))
        stats = kb.stats()
    except ResourceBusyError as exc:
        st.warning(
            "The knowledge library is temporarily unavailable because system memory is low. "
            f"{exc} Document indexing stays local; cloud interviews continue with Groq/Gemini."
        )
        if st.button("Retry", icon=":material/refresh:", key="kb_resource_retry"):
            st.rerun()
        return

    kpi_row(
        [
            ("Documents", stats["documents"], None),
            ("Indexed chunks", stats["chunks"], None),
            ("Vector store", "FAISS" if stats["indexed"] else "Empty", None),
        ]
    )

    upload_tab, search_tab, questions_tab = st.tabs(
        ["Documents", "Semantic search", "Question generator"]
    )

    with upload_tab:
        _render_upload(kb)
        section("Indexed documents", ":material/folder_open:")
        documents = kb.list_documents()
        data_table(documents, "No documents uploaded yet. Add a PDF, DOCX or TXT file above.")
        if documents:
            options = {f"{doc['file_name']} (id {doc['id']})": doc["id"] for doc in documents}
            choice = st.selectbox("Document to manage", list(options), key="kb_manage_document")
            col1, col2 = st.columns(2)
            if col1.button("Rebuild entire index", icon=":material/refresh:", width="stretch"):
                try:
                    with st.spinner("Rebuilding the index for every document…"):
                        count = kb.rebuild_index()
                except ResourceBusyError as exc:
                    _resource_busy_warning(exc)
                else:
                    success_toast(f"Entire index rebuilt with {count} chunks.")
            if col2.button("Delete document", icon=":material/delete:", width="stretch"):
                _start_delete(options[choice], choice)
                st.rerun()

    with search_tab:
        section("Ask your knowledge base", ":material/search:")
        query = st.text_input(
            "Question", key="kb_query", placeholder="What does the document say about…"
        )
        if st.button(
            "Search and answer", type="primary", icon=":material/send:", disabled=not query.strip()
        ):
            try:
                with st.spinner("Retrieving relevant passages…"):
                    result = kb.answer_query(query)
            except ResourceBusyError as exc:
                _resource_busy_warning(exc)
            except Exception as exc:
                error_state("Question answering failed.", str(exc))
            else:
                with st.container(border=True):
                    st.markdown("**Answer**")
                    st.write(result["answer"])
                    if result["sources"]:
                        st.caption("Sources: " + ", ".join(result["sources"]))
                    st.caption(
                        "Grounded in retrieved passages only."
                        if result["grounded"]
                        else "No relevant context found."
                    )
        st.markdown("#### Raw retrieval")
        raw_query = st.text_input("Search query", key="kb_raw_query")
        if raw_query.strip():
            try:
                with st.spinner("Searching indexed passages…"):
                    results = kb.search(raw_query)
            except ResourceBusyError as exc:
                _resource_busy_warning(exc)
            except Exception as exc:
                error_state("Search failed.", str(exc))
            else:
                if not results:
                    empty_state("No matching passages.")
                for result in results:
                    with st.expander(f"{result['source']} · similarity {result['score']}"):
                        st.write(result["text"])

    with questions_tab:
        section("Generate interview questions from documents", ":material/quiz:")
        topic = st.text_input(
            "Topic or role", key="kb_topic", placeholder="e.g. Transformer architecture"
        )
        count = st.slider("Number of questions", 3, 10, 5)
        if st.button(
            "Generate questions",
            type="primary",
            icon=":material/auto_awesome:",
            disabled=not topic.strip(),
        ):
            try:
                with st.spinner("Generating grounded questions…"):
                    questions = kb.generate_questions(topic, count)
            except ResourceBusyError as exc:
                _resource_busy_warning(exc)
            except Exception as exc:
                error_state("Question generation failed.", str(exc))
            else:
                if not questions:
                    empty_state("Upload relevant documents first.")
                for index, question in enumerate(questions, 1):
                    st.markdown(f"{index}. {question}")

    _handle_pending_delete(kb)


def _start_delete(target_id: int, label: str) -> None:
    key = f"kb_delete_{target_id}"
    st.session_state["kb_delete_target"] = target_id
    st.session_state["kb_delete_label"] = label
    st.session_state[key] = False
    st.session_state[f"{key}_ack"] = False
    st.session_state[f"{key}_resolved"] = False


def _handle_pending_delete(kb) -> None:
    target_id = st.session_state.get("kb_delete_target")
    if target_id is None:
        return
    key = f"kb_delete_{target_id}"
    if st.session_state.get(f"{key}_resolved"):
        st.session_state.pop(f"{key}_resolved", None)
        if st.session_state.get(key):
            kb.delete_document(target_id)
            success_toast("Document removed.")
        st.session_state.pop("kb_delete_target", None)
        st.session_state.pop("kb_delete_label", None)
        st.rerun()
        return
    confirm_delete(st.session_state.get("kb_delete_label", str(target_id)), key=key)


def _render_upload(kb) -> None:
    st.markdown("**Add documents**")
    uploads = st.file_uploader(
        "PDF, DOCX or TXT",
        type=sorted(ext.lstrip(".") for ext in SUPPORTED),
        accept_multiple_files=True,
        key="kb_uploader",
    )
    if not uploads:
        st.caption(f"Maximum file size: {settings.max_file_size_mb} MB per document.")
        return
    if st.button(
        "Process documents", type="primary", icon=":material/upload_file:", width="stretch"
    ):
        limit = settings.max_file_size_mb * 1024 * 1024
        indexed = 0
        for upload in uploads:
            if Path(upload.name).suffix.lower() not in SUPPORTED:
                st.error(f"Unsupported file type: {upload.name}")
                continue
            if len(upload.getbuffer()) > limit:
                st.error(f"{upload.name} exceeds the {settings.max_file_size_mb} MB limit.")
                continue
            with st.spinner(f"Indexing {upload.name}…"):
                try:
                    path, safe_name = save_uploaded_file(upload, runtime_path("data/uploads/knowledge"))
                    result = kb.add_document(path, safe_name)
                except ResourceBusyError as exc:
                    _resource_busy_warning(exc)
                    break
                except Exception as exc:
                    error_state(f"Could not index {upload.name}.", str(exc))
                else:
                    if result.get("duplicate"):
                        st.caption(f"{upload.name} was already indexed (skipped duplicate).")
                    else:
                        indexed += 1
                        success_toast(f"{upload.name}: {result['chunk_count']} chunks indexed.")
        if indexed:
            st.rerun()
