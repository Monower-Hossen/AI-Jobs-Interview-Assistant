from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import database.db as db_module
import pytest
from config.settings import settings
from database import User, UserSession
from services import auth_service as auth
from services import live_call_media_service as media
from services import live_call_service as calls
from services import live_interview_coordination_service as coordination
from streamlit.testing.v1 import AppTest

from frontend.shared import live_call_component as component

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stage4_db(tmp_path):
    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    media.clear_media_sessions()
    settings.database_path = str(tmp_path / "stage4.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        admin = User(
            email="admin@stage4.com",
            username="stage4-admin",
            full_name="Stage4 Admin",
            password_hash=auth.hash_password("Pass1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        candidate = User(
            email="candidate@stage4.com",
            username="stage4-candidate",
            full_name="Stage4 Candidate",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add_all([admin, candidate])
        db.flush()
        db.add_all(
            [
                UserSession(session_id="admin-token", user_id=admin.id, label="admin"),
                UserSession(session_id="candidate-token", user_id=candidate.id, label="candidate"),
            ]
        )
        ids = {
            "admin": int(admin.id),
            "candidate": int(candidate.id),
            "admin_token": "admin-token",
            "candidate_token": "candidate-token",
        }

    yield ids

    media.clear_media_sessions()
    db_module.close_database()
    db_module.engine = None
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def _create_started_room(stage4_db) -> dict[str, Any]:
    room = coordination.create_invitation(
        admin_user_id=stage4_db["admin"],
        session_token=stage4_db["admin_token"],
        candidate_user_id=stage4_db["candidate"],
        title="WebRTC interview",
    )
    coordination.accept_invitation(
        room_id=room["room_id"],
        user_id=stage4_db["candidate"],
        session_token=stage4_db["candidate_token"],
    )
    return coordination.start_invitation(
        room_id=room["room_id"],
        user_id=stage4_db["admin"],
        session_token=stage4_db["admin_token"],
    )


def _fake_lease(monkeypatch, released: list[str]) -> None:
    def fake_acquire(task: str, *, owner_id: str, **kwargs: Any):
        return SimpleNamespace(lease_id=f"{task}-lease"), None

    def fake_release(lease_id: str, **kwargs: Any) -> bool:
        released.append(lease_id)
        return True

    monkeypatch.setattr(media, "acquire_resource_lease", fake_acquire)
    monkeypatch.setattr(media, "release_resource_lease", fake_release)
    monkeypatch.setattr(media, "heartbeat_resource_lease", lambda *args, **kwargs: True)


def test_settings_and_ice_server_config_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "webrtc_stun_urls",
        ["stun:stun.example:3478", "not-a-url"],
    )
    monkeypatch.setattr(settings, "webrtc_turn_urls", ["turn:turn.example:3478", "bad-turn"])
    monkeypatch.setattr(settings, "webrtc_turn_username", "turn-user")
    monkeypatch.setattr(settings, "webrtc_turn_credential", "turn-secret")
    monkeypatch.setattr(settings, "webrtc_ice_transport_policy", "relay")

    servers = media.ice_servers()
    assert servers[0] == {"urls": "stun:stun.example:3478"}
    assert servers[1]["urls"] == "turn:turn.example:3478"
    assert "username" in servers[1]
    assert "credential" in servers[1]

    config = media.media_config(room_id="lc-test", role=calls.ROLE_ADMIN)
    assert config["roomId"] == "lc-test"
    assert config["initiator"] is True
    assert config["iceTransportPolicy"] == "relay"
    assert config["video"]["width"] >= 160
    assert 5 <= config["video"]["frameRate"] <= 60
    assert media.media_config(room_id="lc-test", role=calls.ROLE_CANDIDATE)["initiator"] is False


def test_media_session_is_bounded_and_releases(monkeypatch, stage4_db) -> None:
    room = _create_started_room(stage4_db)
    released: list[str] = []
    _fake_lease(monkeypatch, released)

    admin_state = media.attach_media_session(
        room_id=room["room_id"],
        user_id=stage4_db["admin"],
        session_token=stage4_db["admin_token"],
        role=calls.ROLE_ADMIN,
    )
    assert admin_state["active"] is True
    assert admin_state["roles"] == [calls.ROLE_ADMIN]

    candidate_state = media.attach_media_session(
        room_id=room["room_id"],
        user_id=stage4_db["candidate"],
        session_token=stage4_db["candidate_token"],
        role=calls.ROLE_CANDIDATE,
    )
    assert candidate_state["roles"] == [calls.ROLE_ADMIN, calls.ROLE_CANDIDATE]
    assert media.heartbeat_media_session(room_id=room["room_id"]) is True

    partial = media.release_media_session(
        room_id=room["room_id"],
        role=calls.ROLE_ADMIN,
    )
    assert partial["roles"] == [calls.ROLE_CANDIDATE]
    assert released == []

    final = media.release_media_session(
        room_id=room["room_id"],
        role=calls.ROLE_CANDIDATE,
    )
    assert final["active"] is False
    assert released == ["video_processing-lease"]


def test_stage4_signals_roundtrip_for_media_negotiation(stage4_db) -> None:
    room = _create_started_room(stage4_db)
    coordination.send_signal(
        room_id=room["room_id"],
        user_id=stage4_db["admin"],
        session_token=stage4_db["admin_token"],
        signal_type=calls.SIGNAL_OFFER,
        recipient_role=calls.ROLE_CANDIDATE,
        payload={"type": "offer", "sdp": "v=0\r\noffer"},
    )
    coordination.send_signal(
        room_id=room["room_id"],
        user_id=stage4_db["admin"],
        session_token=stage4_db["admin_token"],
        signal_type=calls.SIGNAL_ICE_CANDIDATE,
        recipient_role=calls.ROLE_CANDIDATE,
        payload={"candidate": "candidate:1", "sdp_mid": "0", "sdp_mline_index": 0},
    )

    messages = coordination.receive_signals(
        room_id=room["room_id"],
        user_id=stage4_db["candidate"],
        session_token=stage4_db["candidate_token"],
    )
    assert [message["signal_type"] for message in messages] == [
        calls.SIGNAL_OFFER,
        calls.SIGNAL_ICE_CANDIDATE,
    ]
    assert messages[0]["payload"]["sdp"].startswith("v=0")
    assert messages[1]["payload"]["sdp_mline_index"] == 0

    coordination.send_signal(
        room_id=room["room_id"],
        user_id=stage4_db["candidate"],
        session_token=stage4_db["candidate_token"],
        signal_type=calls.SIGNAL_ANSWER,
        recipient_role=calls.ROLE_ADMIN,
        payload={"type": "answer", "sdp": "v=0\r\nanswer"},
    )
    answers = coordination.receive_signals(
        room_id=room["room_id"],
        user_id=stage4_db["admin"],
        session_token=stage4_db["admin_token"],
    )
    assert len(answers) == 1
    assert answers[0]["sender_role"] == calls.ROLE_CANDIDATE


def test_component_source_uses_real_browser_webrtc_only() -> None:
    source = component._JS
    required = [
        "new RTCPeerConnection",
        "getUserMedia",
        "createOffer",
        "createAnswer",
        "setRemoteDescription",
        "addIceCandidate",
        "onicecandidate",
        "ontrack",
        "connectionState",
        "iceRestart",
        "setStateValue(\"outbox\"",
    ]
    for token in required:
        assert token in source
    banned = [
        "Streamlit.setComponentValue",
        "window.Streamlit",
        "window.parent.postMessage",
        "streamlit-component-lib",
        "components.declare_component",
    ]
    for token in banned:
        assert token not in source
    assert "st.components.v2.component" in Path(component.__file__).read_text(encoding="utf-8")


def _render_page(monkeypatch, page_path: str, account, token: str) -> AppTest:
    monkeypatch.syspath_prepend(str(PROJECT_ROOT))
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "backend"))
    for module_name in (
        "frontend.shared.live_call_component",
        "frontend.shared.live_invitation_ui",
        "frontend.admin.live_interview_invitations",
        "frontend.user.live_interview_invitations",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    app = AppTest.from_file(str(PROJECT_ROOT / page_path), default_timeout=30)
    app.session_state["account"] = account
    app.session_state["user_id"] = int(account.id)
    app.session_state["user_session_id"] = token
    app.run(timeout=60)
    return app


def _visible_text(app: AppTest) -> str:
    parts: list[str] = []
    for element in app.main:
        value = getattr(element, "value", None)
        if value is not None:
            parts.append(str(value))
    return "\n".join(parts)


def test_candidate_page_renders_real_call_panel(monkeypatch, stage4_db) -> None:
    room = _create_started_room(stage4_db)
    released: list[str] = []
    _fake_lease(monkeypatch, released)
    account = auth.get_account(stage4_db["candidate"])
    assert account is not None

    app = _render_page(
        monkeypatch,
        "frontend/user/live_interview_invitations.py",
        account,
        stage4_db["candidate_token"],
    )

    assert not app.exception
    rendered = _visible_text(app)
    assert room["room_id"] in rendered
    assert "Live call" in rendered
    assert "real camera and microphone access" in rendered
    assert stage4_db["candidate_token"] not in rendered
    assert "No audio or video media is relayed" not in rendered
