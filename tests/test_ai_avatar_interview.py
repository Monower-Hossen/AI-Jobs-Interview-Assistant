from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from services import ai_avatar_interview_service as service
from services.ai_avatar_session import (
    ERROR,
    LISTENING,
    SPEAKING,
    WAITING,
    AvatarSessionError,
    begin_listening,
    default_state,
    initialize_ai_avatar_state,
    mark_question_spoken,
    mark_started,
    mark_submitted,
    owner_fingerprint,
    pause,
    register_audio,
    require_avatar_owner,
    set_error,
    set_transcript,
)
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = PROJECT_ROOT / "frontend" / "user" / "ai_avatar_interview.py"
USER_ID = 7
TOKEN = "avatar-token"


def _lease_factory(events: list[tuple[str, str, str | None]]):
    def factory(task: str, *, snapshot: Any = None, owner_id: str | None = None):
        return _lease(events, task, owner_id)

    return factory


@contextmanager
def _lease(events: list[tuple[str, str, str | None]], task: str, owner_id: str | None):
    events.append(("enter", task, owner_id))
    try:
        yield
    finally:
        events.append(("exit", task, owner_id))


def _started_state() -> tuple[dict[str, Any], dict[str, Any]]:
    container: dict[str, Any] = {}
    initialize_ai_avatar_state(container, USER_ID, TOKEN)
    record = require_avatar_owner(container, USER_ID, TOKEN)
    mark_started(
        record,
        interview_id=11,
        job_title="Backend Engineer",
        company="Acme",
        question_id=1,
        question_number=1,
        question_text="Explain API design.",
        question_category="Technical",
        question_difficulty="medium",
    )
    return container, record


def _next_question(number: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        question_number=number,
        question_text="Explain secure endpoints.",
        category="Technical",
        difficulty="medium",
    )


def _active_job_state() -> SimpleNamespace:
    return SimpleNamespace(
        is_complete=False,
        current_question=_next_question(1),
        job_title="Backend Engineer",
        company="Acme",
        consent_given=True,
        answered_count=0,
        config=SimpleNamespace(mode="avatar", target_questions=5),
    )


def test_avatar_state_machine_enforces_transitions_and_duplicates() -> None:
    container, record = _started_state()
    assert record["status"] == SPEAKING
    with pytest.raises(AvatarSessionError):
        require_avatar_owner(container, USER_ID, "different-token")
    mark_question_spoken(record)
    begin_listening(record)
    register_audio(record, b"audio")
    set_transcript(record, "Edited answer")
    assert record["status"] == WAITING
    mark_submitted(record, _next_question())
    assert record["status"] == SPEAKING
    assert record["answered_ids"] == [1]
    assert record["question_id"] == 2
    pause(record)
    assert record["status"] == "paused"
    record["status"] = WAITING
    record["question_id"] = 1
    with pytest.raises(AvatarSessionError):
        mark_submitted(record, None)
    set_error(record, "Simulated provider failure")
    assert record["status"] == ERROR


def test_start_avatar_interview_uses_avatar_mode_and_releases_lease(monkeypatch) -> None:
    container: dict[str, Any] = {}
    events: list[tuple[str, str, str | None]] = []
    captured: dict[str, Any] = {}
    job_state = SimpleNamespace(
        interview_id=11,
        job_title="Backend Engineer",
        company="Acme",
        consent_given=True,
        current_question=_next_question(1),
    )

    monkeypatch.setattr(service, "acquire_heavy_task", _lease_factory(events))

    def fake_start(**kwargs):
        captured.update(kwargs)
        return job_state

    monkeypatch.setattr(service, "start_job_interview", fake_start)
    result = service.start_avatar_interview(
        container,
        user_id=USER_ID,
        session_token=TOKEN,
        job_id=3,
        difficulty="medium",
        duration_minutes=20,
        consent_given=True,
    )
    assert result is job_state
    assert captured["mode"] == "avatar"
    assert captured["actor_user_id"] == USER_ID
    assert ("enter", "llm_inference", "ai_avatar:7") in events
    assert ("exit", "llm_inference", "ai_avatar:7") in events
    assert container["ai_avatar_state"]["status"] == SPEAKING


def test_transcribe_avatar_answer_clears_audio_without_storing_bytes(monkeypatch) -> None:
    container, record = _started_state()
    mark_question_spoken(record)
    begin_listening(record)
    monkeypatch.setattr(
        service,
        "transcribe_audio_bytes",
        lambda data, transcriber=None: "I designed a REST API.",
    )
    text = service.transcribe_avatar_answer(
        container,
        audio_bytes=b"wav-bytes",
        user_id=USER_ID,
        session_token=TOKEN,
    )
    assert text == "I designed a REST API."
    assert record["status"] == WAITING
    assert record["audio_present"] is False
    assert record["audio_size"] == 0
    assert all(not isinstance(value, bytes) for value in container.values())
    assert all(not isinstance(value, bytes) for value in record.values())


def test_transcribe_failure_preserves_error_state(monkeypatch) -> None:
    container, record = _started_state()
    mark_question_spoken(record)
    begin_listening(record)

    def fail(*args, **kwargs):
        raise RuntimeError("Whisper unavailable")

    monkeypatch.setattr(service, "transcribe_audio_bytes", fail)
    with pytest.raises(service.AvatarInterviewError):
        service.transcribe_avatar_answer(
            container,
            audio_bytes=b"wav-bytes",
            user_id=USER_ID,
            session_token=TOKEN,
        )
    assert record["status"] == ERROR
    assert "Whisper unavailable" in record["last_error"]


def test_submit_prevents_duplicate_and_releases_llm_lease(monkeypatch) -> None:
    container, record = _started_state()
    record["status"] = WAITING
    record["answered_ids"] = [1]
    events: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(service, "acquire_heavy_task", _lease_factory(events))
    monkeypatch.setattr(
        service,
        "submit_job_answer",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not submit")),
    )
    with pytest.raises(service.AvatarInterviewError, match="already been answered"):
        service.submit_avatar_answer(
            container, answer="An answer", user_id=USER_ID, session_token=TOKEN
        )
    assert events == []

    record["answered_ids"] = []
    returned = SimpleNamespace(current_question=None)
    monkeypatch.setattr(service, "submit_job_answer", lambda **kwargs: returned)
    service.submit_avatar_answer(
        container, answer="An answer", user_id=USER_ID, session_token=TOKEN
    )
    assert record["can_complete"] is True
    assert ("exit", "llm_inference", "ai_avatar:7:11") in events


def test_provider_failure_recovery_keeps_transcript_and_releases_lease(monkeypatch) -> None:
    container, record = _started_state()
    record["status"] = WAITING
    record["transcript"] = "Editable answer"
    events: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(service, "acquire_heavy_task", _lease_factory(events))

    def fail(**kwargs):
        raise RuntimeError("All AI providers failed")

    monkeypatch.setattr(service, "submit_job_answer", fail)
    with pytest.raises(service.AvatarInterviewError):
        service.submit_avatar_answer(
            container, answer="Editable answer", user_id=USER_ID, session_token=TOKEN
        )
    assert record["status"] == ERROR
    assert record["transcript"] == "Editable answer"
    assert ("exit", "llm_inference", "ai_avatar:7:11") in events


def test_completion_and_cancel_update_state(monkeypatch) -> None:
    container, record = _started_state()
    record["status"] = WAITING
    record["can_complete"] = True
    events: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(service, "acquire_heavy_task", _lease_factory(events))
    monkeypatch.setattr(service, "complete_job_interview", lambda **kwargs: {"overall_score": 8})
    report = service.complete_avatar_interview(
        container, user_id=USER_ID, session_token=TOKEN
    )
    assert report["overall_score"] == 8
    assert record["status"] == "completed"
    assert ("exit", "llm_inference", "ai_avatar:7:11") in events

    cancel_container, cancel_record = _started_state()
    canceled: list[int] = []
    monkeypatch.setattr(
        service,
        "abandon_job_interview",
        lambda **kwargs: canceled.append(int(kwargs["interview_id"])),
    )
    service.cancel_avatar_interview(cancel_container, user_id=USER_ID, session_token=TOKEN)
    assert canceled == [11]
    assert cancel_record["status"] == "cancelled"


def test_invalid_workspace_token_is_wrapped_for_ui(monkeypatch) -> None:
    container: dict[str, Any] = {}
    initialize_ai_avatar_state(container, USER_ID, TOKEN)
    monkeypatch.setattr(service, "acquire_heavy_task", _lease_factory([]))

    def fail(**kwargs):
        raise ValueError("This workspace does not belong to the signed-in account.")

    monkeypatch.setattr(service, "start_job_interview", fail)
    with pytest.raises(service.AvatarInterviewError, match="workspace"):
        service.start_avatar_interview(
            container,
            user_id=USER_ID,
            session_token="other-token",
            job_id=3,
            difficulty="medium",
            duration_minutes=20,
            consent_given=True,
        )


def test_retry_after_transcription_error_returns_to_listening() -> None:
    _, record = _started_state()
    record["status"] = ERROR
    record["error_from"] = "processing"
    service.retry_avatar_step(
        {"ai_avatar_state": record, "user_id": USER_ID, "user_session_id": TOKEN},
        user_id=USER_ID,
        session_token=TOKEN,
    )
    assert record["status"] == LISTENING


def test_sign_out_cleanup_includes_avatar_transient_prefixes() -> None:
    from frontend.shared.auth_ui import _TRANSIENT_PREFIXES

    assert "ai_avatar_" in _TRANSIENT_PREFIXES
    assert "_ai_avatar_" in _TRANSIENT_PREFIXES
    assert "_avatar_" in _TRANSIENT_PREFIXES
    assert "_spoken_text_" in _TRANSIENT_PREFIXES
    assert "_speech_stopped_" in _TRANSIENT_PREFIXES


def _prepare_app_test(monkeypatch) -> AppTest:
    monkeypatch.syspath_prepend(str(PROJECT_ROOT))
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "backend"))
    return AppTest.from_file(str(PAGE_PATH), default_timeout=30)


def test_ai_avatar_page_requires_auth() -> None:
    at = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    at.run()
    assert any("Sign in" in str(error.value) for error in at.error)


def test_ai_avatar_page_renders_setup(monkeypatch) -> None:
    job = SimpleNamespace(
        id=3,
        title="Backend Engineer",
        company="Acme",
        experience_level="Mid",
        description="Build Python APIs",
        responsibilities=["Design APIs"],
    )
    monkeypatch.setattr(
        "services.job_interview_service.list_job_interviews", lambda **kwargs: []
    )
    monkeypatch.setattr("services.job_post_service.list_jobs", lambda: [job])
    monkeypatch.setattr(
        "services.job_post_service.get_job_post", lambda *args, **kwargs: job
    )
    at = _prepare_app_test(monkeypatch)
    at.session_state["user_id"] = USER_ID
    at.session_state["user_session_id"] = TOKEN
    at.run()
    assert not list(at.exception)
    assert len(at.selectbox) >= 1
    assert any("Start AI avatar interview" in str(button.label) for button in at.button)


def test_ai_avatar_page_renders_active_speaking_state(monkeypatch) -> None:
    record = default_state()
    record.update(
        {
            "owner_fingerprint": owner_fingerprint(USER_ID, TOKEN),
            "status": SPEAKING,
            "interview_id": 11,
            "job_title": "Backend Engineer",
            "company": "Acme",
            "question_id": 1,
            "question_number": 1,
            "question_text": "Explain API design.",
            "question_category": "Technical",
            "question_difficulty": "medium",
            "tts_key": 1,
            "consent_given": True,
        }
    )
    job_state = SimpleNamespace(
        is_complete=False,
        current_question=_next_question(1),
        job_title="Backend Engineer",
        company="Acme",
        consent_given=True,
        answered_count=0,
        config=SimpleNamespace(mode="avatar", target_questions=5),
    )
    monkeypatch.setattr(
        "services.ai_avatar_interview_service.get_avatar_interview_state",
        lambda *args, **kwargs: job_state,
    )
    at = _prepare_app_test(monkeypatch)
    at.session_state["user_id"] = USER_ID
    at.session_state["user_session_id"] = TOKEN
    at.session_state["ai_avatar_state"] = record
    at.run()
    assert not list(at.exception)
    assert any("Explain API design." in str(item.value) for item in at.markdown)
    assert any("I heard the question" in str(button.label) for button in at.button)


def test_ai_avatar_page_does_not_stop_speech_on_speaking_rerun(monkeypatch) -> None:
    _, record = _started_state()
    record["tts_key"] = 1
    monkeypatch.setattr(
        "services.ai_avatar_interview_service.get_avatar_interview_state",
        lambda *args, **kwargs: _active_job_state(),
    )
    stops: list[str] = []
    speaks: list[str] = []
    monkeypatch.setattr(
        "frontend.shared.components.stop_speech", lambda key="default": stops.append(key)
    )
    monkeypatch.setattr(
        "frontend.shared.components.speak_text",
        lambda text, key="default": speaks.append(key),
    )
    at = _prepare_app_test(monkeypatch)
    at.session_state["user_id"] = USER_ID
    at.session_state["user_session_id"] = TOKEN
    at.session_state["ai_avatar_state"] = record
    at.run()
    at.run()
    assert len(speaks) == 2
    assert set(speaks) == {"avatar_11_1_1"}
    assert stops == []
    heard = next(button for button in at.button if button.label == "I heard the question")
    heard.click().run()
    assert stops == ["ai_avatar"]


def test_ai_avatar_transcript_does_not_reset_widget_default(monkeypatch, caplog) -> None:
    _, record = _started_state()
    record.update(
        {
            "status": WAITING,
            "transcript": "Edited transcript",
            "transcript_key": 0,
            "audio_present": False,
        }
    )
    monkeypatch.setattr(
        "services.ai_avatar_interview_service.get_avatar_interview_state",
        lambda *args, **kwargs: _active_job_state(),
    )
    at = _prepare_app_test(monkeypatch)
    at.session_state["user_id"] = USER_ID
    at.session_state["user_session_id"] = TOKEN
    at.session_state["ai_avatar_state"] = record
    with caplog.at_level(logging.WARNING):
        at.run()
        at.run()
    assert not any("created with a default value" in item.message for item in caplog.records)
    assert at.text_area[0].value == "Edited transcript"


def test_sign_out_stops_avatar_speech(monkeypatch) -> None:
    from frontend.shared import auth_ui

    stopped: list[str] = []
    monkeypatch.setattr(auth_ui, "stop_speech", lambda key="default": stopped.append(key))
    monkeypatch.setattr(auth_ui, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "frontend.shared.live_session_cleanup.teardown_for_auth",
        lambda **kwargs: {"stopped": False},
    )
    code = """
import streamlit as st
from frontend.shared.auth_ui import sign_out
if not st.session_state.get("probe_signed_out"):
    st.session_state["probe_signed_out"] = True
    st.session_state["user_id"] = 7
    sign_out()
"""
    at = AppTest.from_string(code).run()
    assert not list(at.exception)
    assert stopped == ["ai_avatar"]
