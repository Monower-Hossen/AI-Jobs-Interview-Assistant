import streamlit as st
from services import live_call_media_service as media
from services import live_call_service as calls
from services.auth_service import list_users
from services.live_interview_coordination_service import (
    bulk_delete_invitations,
    create_invitation,
    list_invitations,
)

from frontend.shared.components import empty_state, error_state, page_header, section
from frontend.shared.live_invitation_ui import render_invitation_refresh, render_room

_SELECTED_KEY = "_live_bulk_delete_selected"
_SELECT_ALL_KEY = "_live_bulk_delete_select_all"
_CONFIRM_KEY = "_live_bulk_delete_confirm"
_ANSWER_KEY = "_live_bulk_delete_answer"
_ANSWERED_KEY = "_live_bulk_delete_answered"
_REFRESH_SUPPRESS_KEY = "_live_invitation_refresh_suppress_once"
_TARGET_KEY = "_live_bulk_delete_target"
_TARGET_IDS_KEY = "_live_bulk_delete_target_ids"
_FEEDBACK_KEY = "_live_bulk_delete_feedback"


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


def _is_bulk_deletable(room: dict) -> bool:
    room_id = str(room["room_id"])
    if str(room["status"]) not in calls.DELETABLE_CALL_STATES:
        return False
    try:
        return not bool(media.media_session_state(room_id).get("active"))
    except Exception:
        return True


def _room_checkbox_key(room_id: str) -> str:
    return f"_live_bulk_select_{room_id}"


def _selected_ids() -> set[str]:
    return {str(item) for item in st.session_state.get(_SELECTED_KEY) or ()}


def _store_selected_ids(room_ids: set[str]) -> None:
    st.session_state[_SELECTED_KEY] = sorted(room_ids)


def _on_room_selected(room_id: str, eligible_ids: tuple[str, ...]) -> None:
    selected = _selected_ids()
    if bool(st.session_state.get(_room_checkbox_key(room_id))):
        selected.add(room_id)
    else:
        selected.discard(room_id)
    _store_selected_ids(selected)
    st.session_state[_SELECT_ALL_KEY] = bool(eligible_ids) and selected >= set(eligible_ids)


def _on_select_all(eligible_ids: tuple[str, ...]) -> None:
    select_all = bool(st.session_state.get(_SELECT_ALL_KEY))
    selected = set(eligible_ids) if select_all else set()
    _store_selected_ids(selected)
    for room_id in eligible_ids:
        st.session_state[_room_checkbox_key(room_id)] = select_all


def _sync_selection(eligible_ids: list[str]) -> set[str]:
    eligible = set(eligible_ids)
    selected = _selected_ids() & eligible
    _store_selected_ids(selected)
    for room_id in eligible_ids:
        st.session_state[_room_checkbox_key(room_id)] = room_id in selected
    st.session_state[_SELECT_ALL_KEY] = bool(eligible_ids) and selected == eligible
    return selected


def _store_feedback(message: str, tone: str) -> None:
    st.session_state[_FEEDBACK_KEY] = {"message": message, "tone": tone}


def _feedback_details(skipped: list[dict], category: str) -> str:
    matching = [item for item in skipped if str(item.get("category") or "") == category]
    return "; ".join(
        f"{item.get('room_id')}: {item.get('reason')}" for item in matching[:5]
    )


def _run_bulk_delete(
    room_ids: list[str], admin_user_id: int, session_token: str
) -> dict:
    try:
        result = bulk_delete_invitations(
            room_ids=room_ids,
            user_id=admin_user_id,
            session_token=session_token,
            confirmation=True,
        )
    except calls.LiveCallError as exc:
        _store_feedback(str(exc), "error")
        return {}
    except Exception as exc:
        _store_feedback(f"The invitations could not be deleted. {str(exc)[:200]}", "error")
        return {}

    _store_selected_ids(set())
    counts = dict(result.get("counts") or {})
    deleted_count = int(result.get("deleted_count") or counts.get("deleted") or 0)
    active_count = int(counts.get("active") or 0)
    missing_count = int(counts.get("missing") or 0)
    failed_count = int(counts.get("failed") or 0)
    skipped = list(result.get("skipped") or [])
    parts: list[str] = []
    if deleted_count:
        parts.append(f"Deleted {deleted_count} invitation(s).")
    if active_count:
        parts.append(f"Skipped {active_count} invitation(s) because active or ongoing.")
    if missing_count:
        parts.append(f"Already missing: {missing_count} invitation(s).")
    if failed_count:
        parts.append(f"Failed: {failed_count} invitation(s).")
    for label, category in (("Active", "active"), ("Missing", "missing"), ("Failed", "failed")):
        details = _feedback_details(skipped, category)
        if details:
            parts.append(f"{label} details: {details}")
    if parts:
        tone = "success" if not (active_count or missing_count or failed_count) else "warning"
        _store_feedback(" ".join(parts), tone)
    return result


def _render_feedback() -> None:
    feedback = st.session_state.pop(_FEEDBACK_KEY, None)
    if not feedback:
        return
    message = str(feedback.get("message") or "")
    tone = str(feedback.get("tone") or "success")
    if tone == "error":
        st.error(message, icon=":material/error:")
    elif tone == "warning":
        st.warning(message, icon=":material/warning:")
    else:
        st.success(message, icon=":material/check_circle:")


def _consume_bulk_delete_answer(admin_user_id: int, session_token: str) -> None:
    if not (st.session_state.get(_TARGET_KEY) and st.session_state.get(_ANSWERED_KEY)):
        return
    target_ids = [str(item) for item in st.session_state.get(_TARGET_IDS_KEY) or ()]
    confirmed = bool(st.session_state.get(_ANSWER_KEY))
    st.session_state.pop(_ANSWERED_KEY, None)
    st.session_state.pop(_ANSWER_KEY, None)
    st.session_state.pop(_TARGET_KEY, None)
    st.session_state.pop(_TARGET_IDS_KEY, None)
    st.session_state[_CONFIRM_KEY] = False
    if confirmed and target_ids:
        _run_bulk_delete(target_ids, admin_user_id, session_token)
        st.session_state[_REFRESH_SUPPRESS_KEY] = True


def _on_bulk_dialog_dismiss() -> None:
    st.session_state[_ANSWER_KEY] = False
    st.session_state[_ANSWERED_KEY] = True


@st.dialog("Confirm bulk deletion", width="small", on_dismiss=_on_bulk_dialog_dismiss)
def _bulk_delete_dialog(count: int, has_invited: bool) -> None:
    st.warning(
        f"You are about to delete {count} selected invitations. "
        "This action cannot be undone. Active or ongoing invitations will not be deleted."
    )
    confirmation_label = (
        "I confirm the pending invitations are no longer needed and understand "
        "this action is permanent"
        if has_invited
        else "I understand this action is permanent"
    )
    confirmed = st.checkbox(confirmation_label, key=_CONFIRM_KEY)
    left, right = st.columns(2)
    if left.button("Cancel", width="stretch", key="_live_bulk_delete_cancel"):
        st.session_state[_ANSWER_KEY] = False
        st.session_state[_ANSWERED_KEY] = True
    if right.button(
        "Delete",
        type="primary",
        disabled=not confirmed,
        width="stretch",
        key="_live_bulk_delete_confirm_button",
    ):
        st.session_state[_ANSWER_KEY] = True
        st.session_state[_ANSWERED_KEY] = True


def _render_bulk_controls(rooms: list[dict], eligible_ids: list[str]) -> None:
    selected = _sync_selection(eligible_ids)
    with st.container(border=True):
        st.checkbox(
            "Select all eligible invitations",
            key=_SELECT_ALL_KEY,
            on_change=_on_select_all,
            args=(tuple(eligible_ids),),
            disabled=not eligible_ids,
        )
        st.caption(
            "Active and ongoing interviews, and rooms with an active WebRTC session, "
            "are not eligible for deletion."
        )
        st.write(f"Selected: {len(selected)} invitations")
        pending = bool(st.session_state.get(_TARGET_KEY))
        if st.button(
            "Delete selected",
            icon=":material/delete:",
            width="stretch",
            type="primary",
            disabled=not selected or pending,
            key="_live_bulk_delete_start",
        ):
            st.session_state[_TARGET_KEY] = True
            st.session_state[_TARGET_IDS_KEY] = sorted(selected)
            st.session_state[_ANSWER_KEY] = False
            st.session_state[_ANSWERED_KEY] = False
            st.session_state[_CONFIRM_KEY] = False

        if st.session_state.get(_TARGET_KEY):
            target_ids = [str(item) for item in st.session_state.get(_TARGET_IDS_KEY) or ()]
            target_set = set(target_ids)
            has_invited = any(
                str(room["room_id"]) in target_set and str(room["status"]) == calls.INVITED
                for room in rooms
            )
            _bulk_delete_dialog(len(target_ids), has_invited)


def show_admin_live_interviews() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()
    admin_user_id = int(account.id)
    session_token = str(st.session_state.get("user_session_id") or "")
    _consume_bulk_delete_answer(admin_user_id, session_token)
    _render_feedback()
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

    eligible_ids = [str(room["room_id"]) for room in rooms if _is_bulk_deletable(room)]
    _render_bulk_controls(rooms, eligible_ids)

    changed = False
    for room in rooms:
        room_id = str(room["room_id"])
        selection_checkbox = None
        if room_id in set(eligible_ids):
            eligible = tuple(eligible_ids)

            def _render_checkbox(
                room_id: str = room_id, eligible: tuple[str, ...] = eligible
            ) -> None:
                st.checkbox(
                    "Select for bulk deletion",
                    key=_room_checkbox_key(room_id),
                    on_change=_on_room_selected,
                    args=(room_id, eligible),
                )

            selection_checkbox = _render_checkbox
        if render_room(room, allow_delete=True, selection_checkbox=selection_checkbox):
            changed = True
    if changed:
        st.rerun()


show_admin_live_interviews()
