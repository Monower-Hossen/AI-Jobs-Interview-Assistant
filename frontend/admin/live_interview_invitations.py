import streamlit as st
from services import live_call_service as calls
from services.auth_service import list_users
from services.live_interview_coordination_service import create_invitation, list_invitations

from frontend.shared.components import empty_state, error_state, page_header, section
from frontend.shared.live_invitation_ui import render_invitation_refresh, render_room


def _candidate_options(admin_user_id: int) -> dict[str, int]:
    accounts = list_users(actor_admin_id=admin_user_id)
    options: dict[str, int] = {}
    for account in accounts:
        if str(account.role) != "user" or not account.is_active:
            continue
        label = f"{account.full_name} ({account.email})"
        options[label] = int(account.id)
    return options


def _render_create_form(admin_user_id: int, session_token: str) -> None:
    section("Invite a candidate", ":material/person_add:")
    try:
        options = _candidate_options(admin_user_id)
    except Exception as exc:
        error_state("Candidate accounts could not be loaded.", str(exc)[:200])
        return
    if not options:
        empty_state("No active candidate accounts are available.", "Create a user account first.")
        return

    with st.container(border=True):
        labels = list(options)
        selected = st.selectbox("Candidate", options=labels, key="_live_invite_candidate")
        title = st.text_input(
            "Invitation title",
            placeholder="e.g. Backend engineer interview",
            key="_live_invite_title",
        )
        ttl = st.number_input(
            "Invitation lifetime (seconds)",
            min_value=30,
            max_value=3600,
            value=120,
            step=30,
            key="_live_invite_ttl",
        )
        if st.button(
            "Create invitation",
            icon=":material/add:",
            width="stretch",
            type="primary",
            key="_live_invite_create",
        ):
            try:
                create_invitation(
                    admin_user_id=admin_user_id,
                    session_token=session_token,
                    candidate_user_id=options[str(selected)],
                    title=title,
                    invite_ttl_seconds=int(ttl),
                )
            except calls.LiveCallError as exc:
                st.error(str(exc), icon=":material/error:")
            except Exception as exc:
                error_state("The invitation could not be created.", str(exc)[:200])
            else:
                st.toast("Invitation created.", icon=":material/check_circle:")
                st.rerun()


def show_admin_live_interviews() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()
    admin_user_id = int(account.id)
    session_token = str(st.session_state.get("user_session_id") or "")
    page_header(
        "Live Interview Invitations",
        "Invite candidates and run authenticated real-time WebRTC interviews.",
        ":material/videocam:",
    )
    _render_create_form(admin_user_id, session_token)
    render_invitation_refresh(admin_user_id, session_token)

    section("Existing invitations", ":material/list:")
    try:
        rooms = list_invitations(
            user_id=admin_user_id,
            session_token=session_token,
            limit=50,
        )
    except calls.LiveCallError as exc:
        error_state("Invitations could not be loaded.", str(exc))
        return
    except Exception as exc:
        error_state("Invitations could not be loaded.", str(exc)[:200])
        return
    if not rooms:
        empty_state("No live interview invitations yet.")
        return
    changed = False
    for room in rooms:
        if render_room(room, allow_delete=True):
            changed = True
    if changed:
        st.rerun()


show_admin_live_interviews()
