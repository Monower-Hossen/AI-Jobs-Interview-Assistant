from __future__ import annotations

import importlib
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "frontend.user.live_webcam_interview"


def _clear_live_webcam_modules() -> None:
    for name in list(sys.modules):
        if name == MODULE_NAME or name.startswith("frontend.user.live_webcam_"):
            sys.modules.pop(name, None)
        if name == "services.live_webcam_capture":
            sys.modules.pop(name, None)


def _render_page(monkeypatch, user_id: int | None = 1, token: str | None = "test-token"):
    monkeypatch.syspath_prepend(str(PROJECT_ROOT))
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "backend"))
    _clear_live_webcam_modules()
    at = AppTest.from_file(str(PROJECT_ROOT / "frontend" / "user" / "live_webcam_interview.py"))
    if user_id is not None:
        at.session_state["user_id"] = user_id
    if token is not None:
        at.session_state["user_session_id"] = token
    at.run(timeout=60)
    return at


def _error_details(at: AppTest) -> str:
    details: list[str] = []
    for err in list(getattr(at, "error", [])):
        value = getattr(err, "value", err)
        details.append(str(value))
        body = getattr(err, "body", None)
        if body:
            details.append(str(body))
    for cap in list(getattr(at, "caption", [])):
        details.append(str(getattr(cap, "value", cap)))
    return "\n".join(details)


def test_missing_streamlit_webrtc_shows_actionable_diagnostic(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "streamlit_webrtc":
            raise ModuleNotFoundError("No module named 'streamlit_webrtc'", name="streamlit_webrtc")
        if name == "services.live_webcam_capture":
            raise ModuleNotFoundError("No module named 'streamlit_webrtc'", name="streamlit_webrtc")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    at = _render_page(monkeypatch)

    assert not at.exception
    details = _error_details(at)
    assert "Live webcam support is unavailable." in details
    assert r".\venv\Scripts\python.exe -m streamlit run app.py" in details
    assert "different interpreter" in details.lower() or "virtual environment" in details.lower()
    assert "No module named 'streamlit_webrtc'" not in details


def test_unrelated_import_error_reports_actual_exception(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "streamlit_webrtc" or name == "services.live_webcam_capture":
            raise ImportError("DLL load failed: native dependency missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    at = _render_page(monkeypatch)

    assert not at.exception
    details = _error_details(at)
    assert "Live webcam support is unavailable." in details
    assert "ImportError" in details
    assert "DLL load failed: native dependency missing" in details
    assert "No module named 'streamlit_webrtc'" not in details
    assert r".\venv\Scripts\python.exe -m streamlit run app.py" in details


def test_unexpected_import_exception_is_handled_safely(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "streamlit_webrtc" or name == "services.live_webcam_capture":
            raise RuntimeError("unexpected init failure")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    at = _render_page(monkeypatch)

    assert not at.exception
    details = _error_details(at)
    assert "Live webcam support is unavailable." in details
    assert "RuntimeError" in details
    assert "unexpected init failure" not in details  # keep unexpected traceback/details in logs, not UI
    assert r".\venv\Scripts\python.exe -m streamlit run app.py" in details


def test_successful_import_path_renders_page(monkeypatch):
    at = _render_page(monkeypatch)
    assert not at.exception
    assert at.title[0].value == "AI live webcam interview"
    # should not show unavailable error when imports succeed
    details = _error_details(at)
    assert "Live webcam support is unavailable." not in details


def test_runtime_diagnostic_variables_set_on_missing_dependency(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "streamlit_webrtc":
            raise ModuleNotFoundError("No module named 'streamlit_webrtc'", name="streamlit_webrtc")
        if name == "services.live_webcam_capture":
            raise ModuleNotFoundError("No module named 'streamlit_webrtc'", name="streamlit_webrtc")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.syspath_prepend(str(PROJECT_ROOT))
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "backend"))
    _clear_live_webcam_modules()
    module = importlib.import_module(MODULE_NAME)

    assert module.WEBRTC_IMPORT_ERROR
    assert module.WEBRTC_IMPORT_ERROR_TYPE == "ModuleNotFoundError"
    assert module.WEBRTC_IMPORT_DIAGNOSTIC
    assert r".\venv\Scripts\python.exe -m streamlit run app.py" in module.WEBRTC_IMPORT_DIAGNOSTIC
