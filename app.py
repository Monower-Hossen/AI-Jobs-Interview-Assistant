import asyncio
import atexit
import logging
import os
import sys
import uuid
import warnings
from pathlib import Path

import streamlit as st

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*torchvision.*")

if sys.platform == "win32":

    def _suppress_win32_connection_reset(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) and exc.winerror == 10054:
            return
        loop.default_exception_handler(context)

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
backend_root = project_root / "backend"
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from database.db import init_database
from services.auth_service import get_account, is_admin_role
from services.llm_service import (
    check_provider_health,
    get_provider_registry,
    get_provider_status,
)

from frontend.shared.auth_ui import show_auth_page, sign_out
from frontend.shared.components import render_resource_status_sidebar


def _teardown_live_capture(reason: str) -> None:
    try:
        from frontend.shared.live_session_cleanup import teardown_for_auth

        teardown_for_auth(reason=reason)
    except Exception:
        logging.getLogger(__name__).exception("live_capture_teardown_failed")


def _shutdown_live_capture() -> None:
    try:
        from services.live_webcam_video import shutdown_all_video_recorders

        shutdown_all_video_recorders()
    except Exception:
        logging.getLogger(__name__).exception("live_capture_shutdown_failed")


atexit.register(_shutdown_live_capture)


def main() -> None:
    st.set_page_config(
        page_title="AI Interview Intelligence Platform",
        page_icon=":material/psychology:",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    if "db_initialized" not in st.session_state:
        try:
            init_database()
            st.session_state.db_initialized = True
        except Exception:
            logging.getLogger(__name__).exception("db.init_failed")
            st.error("Database initialization failed. Please check logs and try again.", icon=":material/error:")
            st.stop()

    if "user_session_id" not in st.session_state:
        st.session_state.user_session_id = str(uuid.uuid4())[:8]

    try:
        account = _current_account()
    except Exception:
        logging.getLogger(__name__).exception("auth.resolve_account_failed")
        _teardown_live_capture("Session validation failed during live capture.")
        st.session_state.pop("account", None)
        st.session_state.pop("user_id", None)
        st.session_state.pop("must_change_password", None)
        account = None
        st.warning("Session validation failed. Please sign in again.", icon=":material/warning:")

    if st.session_state.pop("auth_notice", None):
        st.warning(
            "Your account is no longer active. Contact the administrator.",
            icon=":material/warning:",
        )

    if account is None:
        login_page = st.Page(show_auth_page, title="Sign in", icon=":material/login:")
        st.navigation([login_page], position="hidden").run()
        return

    # If user must change password, allow only the password change page
    if st.session_state.get("must_change_password") and st.session_state.get("user_id"):
        st.navigation(
            [
                st.Page(
                    "frontend/shared/change_password.py",
                    title="Set New Password",
                    icon=":material/security:",
                )
            ],
            position="hidden",
        ).run()
        return

    if is_admin_role(account):
        _render_sidebar(account)
        st.navigation(_admin_pages(), position="sidebar", expanded=True).run()
    else:
        _render_user_sidebar(account)
        st.navigation(_user_pages(account), position="sidebar", expanded=True).run()


def _current_account():
    """Resolve the signed-in account from the database on every script run.

    Session state alone must never grant access: an admin can deactivate or
    suspend an account while it is open in another browser.
    """
    user_id = st.session_state.get("user_id")
    if not user_id:
        st.session_state.pop("account", None)
        st.session_state.pop("must_change_password", None)
        return None

    account = get_account(int(user_id))
    if account is None or not getattr(account, "is_active", False):
        _teardown_live_capture("The account became inactive during live capture.")
        st.session_state.pop("account", None)
        st.session_state.pop("user_id", None)
        st.session_state.pop("must_change_password", None)
        st.session_state.auth_notice = "inactive"
        return None

    st.session_state.account = account
    st.session_state.must_change_password = bool(getattr(account, "must_change_password", False))
    return account


def _render_provider_sidebar(button_key: str) -> None:
    registry = get_provider_registry()
    try:
        status = get_provider_status()
        active_provider = str(status.get("active") or "none").upper()
        providers = status.get("providers", {})
        ready = [name for name in registry if providers.get(name, {}).get("configured")]
    except Exception:
        active_provider = "UNAVAILABLE"
        ready = []

    st.sidebar.caption(
        f"Provider: {active_provider} · Ready: {', '.join(ready) if ready else 'none'}"
    )
    if not ready:
        st.sidebar.warning(
            "No provider configured. Ask an administrator to add a key.",
            icon=":material/key:",
        )

    if st.sidebar.button(
        "Check provider health",
        icon=":material/cloud:",
        width="stretch",
        key=button_key,
    ):
        try:
            with st.spinner("Checking providers…"):
                health = check_provider_health()
        except Exception:
            st.sidebar.warning("Provider check unavailable.", icon=":material/warning:")
            return
        for name, info in health.items():
            message = f"{name}: {info.get('status', 'Unavailable')}"
            if info.get("error"):
                message += f" · {str(info['error'])[:80]}"
            if info.get("healthy"):
                st.sidebar.success(message, icon=":material/check_circle:")
            else:
                st.sidebar.warning(message, icon=":material/warning:")


def _render_sidebar(account) -> None:
    st.sidebar.markdown("### AI Interview Intelligence")
    st.sidebar.caption(f"Workspace • {str(st.session_state.user_session_id)[:8]}")
    st.sidebar.divider()

    with st.sidebar.container(border=True):
        st.markdown(f"**{account.full_name}**")
        st.caption(f"{account.email} • {account.role.title()} account")
        if st.button(
            "Sign out",
            icon=":material/logout:",
            width="stretch",
            key="sidebar_sign_out",
        ):
            sign_out()

    _render_provider_sidebar("sidebar_provider_health")
    render_resource_status_sidebar("sidebar_resource_status")


def _render_user_sidebar(account) -> None:
    st.sidebar.markdown("### AI Interview Assistant")
    st.sidebar.caption(f"Private workspace · {str(st.session_state.user_session_id)[:8]}")

    with st.sidebar.container(border=True):
        st.markdown(f"**{account.full_name}**")
        st.caption(account.email)
        st.caption("User account")
        if st.button(
            "Sign out",
            icon=":material/logout:",
            width="stretch",
            key="user_sidebar_sign_out",
        ):
            sign_out()

    st.sidebar.caption("Workspace navigation")
    _render_provider_sidebar("user_sidebar_provider_health")
    render_resource_status_sidebar("user_sidebar_resource_status")


def _admin_pages() -> dict:
    return {
        "Platform": [
            st.Page(
                "frontend/admin/dashboard.py",
                title="Dashboard",
                icon=":material/admin_panel_settings:",
            ),
            st.Page("frontend/admin/users.py", title="User Management", icon=":material/group:"),
        ],
        "Interview": [
            st.Page(
                "frontend/admin/jobs.py",
                title="Job postings",
                icon=":material/work:",
                url_path="admin-jobs",
            ),
            st.Page(
                "frontend/admin/interviews.py",
                title="Interview Management",
                icon=":material/forum:",
            ),
            st.Page(
                "frontend/admin/live_interview_invitations.py",
                title="Live interview invitations",
                icon=":material/videocam:",
                url_path="admin-live-interviews",
            ),
            st.Page(
                "frontend/admin/live_media.py",
                title="Live recording management",
                icon=":material/video_file:",
                url_path="admin-live-media",
            ),
        ],
        "Learning": [
            st.Page(
                "frontend/admin/learning.py",
                title="Learning management",
                icon=":material/school:",
                url_path="admin-learning",
            ),
        ],
        "AI Performance": [
            st.Page(
                "frontend/admin/settings.py",
                title="AI Performance Monitoring",
                icon=":material/tune:",
                url_path="admin-ai-settings",
            ),
        ],
        "Reports": [
            st.Page(
                "frontend/admin/reports.py",
                title="Reports",
                icon=":material/summarize:",
                url_path="admin-reports",
            ),
        ],
        "System": [
            st.Page(
                "frontend/user/settings.py",
                title="System Settings",
                icon=":material/settings:",
                url_path="system-settings",
            ),
        ],
    }


def _user_pages(account) -> dict:
    return {
        "Overview": [
            st.Page(
                "frontend/user/dashboard.py",
                title="Dashboard",
                icon=":material/dashboard:",
                default=True,
            ),
        ],
        "Interview Studio": [
            st.Page(
                "frontend/user/job_interview.py",
                title="Job Interview",
                icon=":material/videocam:",
                url_path="job-interview",
            ),
            st.Page(
                "frontend/user/ai_avatar_interview.py",
                title="Mock Interview",
                icon=":material/smart_toy:",
                url_path="mock_interview",
            ),
            st.Page(
                "frontend/user/ai_avatar_interview.py",
                title="AI Avatar Interview",
                icon=":material/smart_toy:",
                url_path="ai-avatar-interview",
                visibility="hidden",
            ),
            st.Page(
                "frontend/user/live_webcam_interview.py",
                title="Live Interview",
                icon=":material/videocam:",
                url_path="live-webcam-interview",
            ),
            st.Page(
                "frontend/user/live_interview_invitations.py",
                title="Invitations",
                icon=":material/event_available:",
                url_path="live-interview-invitations",
            ),
            st.Page(
                "frontend/user/mock_interview.py",
                title="Legacy Mock Interview",
                icon=":material/forum:",
                url_path="legacy-mock-interview",
                visibility="hidden",
            ),
        ],
        "Career Preparation": [
            st.Page(
                "frontend/user/resume_analyzer.py",
                title="Resume Analyzer",
                icon=":material/description:",
            ),
            st.Page("frontend/user/job_page.py", title="Job Preparation", icon=":material/work:"),
            st.Page(
                "frontend/user/question_bank.py",
                title="Question Bank",
                icon=":material/library_books:",
            ),
            st.Page(
                "frontend/user/learning_center.py",
                title="Learning Center",
                icon=":material/school:",
            ),
            st.Page(
                "frontend/user/learning_resources.py",
                title="Learning roadmap",
                icon=":material/route:",
                url_path="learning_resources",
                visibility="hidden",
            ),
        ],
        "Performance": [
            st.Page(
                "frontend/user/interview_history.py",
                title="Interview History",
                icon=":material/history:",
            ),
            st.Page(
                "frontend/user/analytics.py",
                title="Performance Analytics",
                icon=":material/analytics:",
            ),
            st.Page(
                "frontend/user/report_page.py",
                title="Interview Reports",
                icon=":material/assessment:",
            ),
            st.Page(
                "frontend/user/live_recording_history.py",
                title="Recording History",
                icon=":material/video_file:",
                url_path="live-recording-history",
            ),
            st.Page(
                "frontend/user/learning_progress.py",
                title="Learning Progress",
                icon=":material/trending_up:",
                url_path="learning-progress",
            ),
        ],
        "AI Career Coach": [
            st.Page(
                "frontend/user/coach.py", title="AI Interview Coach", icon=":material/psychology:"
            ),
            st.Page(
                "frontend/user/improvement_plans.py",
                title="Improvement Plans",
                icon=":material/checklist:",
                url_path="improvement-plans",
            ),
        ],
        "Notifications": [
            st.Page(
                "frontend/user/notifications.py",
                title="Notifications",
                icon=":material/notifications:",
                url_path="notifications",
            ),
        ],
        "My Account": [
            st.Page("frontend/user/profile.py", title="Profile", icon=":material/person:"),
            st.Page(
                "frontend/user/account_settings.py",
                title="Account Settings",
                icon=":material/tune:",
            ),
        ],
    }


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            loop = asyncio.get_event_loop()
            loop.set_exception_handler(_suppress_win32_connection_reset)
        except RuntimeError:
            pass
    main()
