import streamlit as st
from services.support_service import (
    BUG_SEVERITIES,
    create_bug_report,
    list_my_bug_reports,
)

from frontend.shared.components import (
    empty_state,
    error_state,
    page_header,
    section,
    success_toast,
)

ATTACHMENT_TYPES = ["pdf", "docx", "txt", "png", "jpg", "jpeg"]


def _stamp(value) -> str:
    return str(value or "-")


def show_report_bug(*, render_header: bool = True) -> None:
    if render_header:
        page_header(
            "Report a bug",
            "Describe the problem so the team can reproduce and fix it",
            ":material/bug_report:",
        )
    actor_user_id = st.session_state.get("user_id")
    if not actor_user_id:
        empty_state("Sign in to report a bug.")
        return

    section("Bug details", ":material/edit_note:")
    with st.form("bug_report_form", border=True):
        title = st.text_input(
            "Title", max_chars=150, placeholder="Short description of the bug", key="bug_title"
        )
        feature = st.text_input(
            "Affected feature", placeholder="e.g. resume analyzer", key="bug_feature"
        )
        description = st.text_area("Description", height=110, key="bug_description")
        steps = st.text_area(
            "Steps to reproduce", height=120, placeholder="1. Open… 2. Click…", key="bug_steps"
        )
        expected = st.text_area("Expected result", height=80, key="bug_expected")
        actual = st.text_area("Actual result", height=80, key="bug_actual")
        severity = st.selectbox("Severity", list(BUG_SEVERITIES), index=1, key="bug_severity")
        uploads = st.file_uploader(
            "Screenshots or logs (optional)",
            type=ATTACHMENT_TYPES,
            accept_multiple_files=True,
            key="bug_attachments",
        )
        submitted = st.form_submit_button(
            "Submit report", type="primary", icon=":material/send:", width="stretch"
        )

    if submitted:
        required = {
            "Title": title,
            "Affected feature": feature,
            "Description": description,
            "Steps to reproduce": steps,
            "Expected result": expected,
            "Actual result": actual,
        }
        missing = [label for label, value in required.items() if not value.strip()]
        if missing:
            error_state(f"Complete these fields: {', '.join(missing)}.")
        else:
            try:
                create_bug_report(
                    int(actor_user_id),
                    title=title.strip(),
                    feature=feature.strip(),
                    description=description.strip(),
                    steps=steps.strip(),
                    expected=expected.strip(),
                    actual=actual.strip(),
                    severity=severity,
                    attachments=uploads or None,
                )
            except Exception as exc:
                error_state("Bug report could not be submitted.", str(exc))
            else:
                success_toast("Thanks for reporting the bug.")
                st.rerun()

    section("Your reports", ":material/history:")
    try:
        rows = list_my_bug_reports(int(actor_user_id))
    except Exception as exc:
        error_state("Bug reports could not be loaded.", str(exc))
        return
    if not rows:
        empty_state(
            "You have not reported a bug yet.", "Reports and their resolutions appear here."
        )
        return
    for item in rows:
        with st.container(border=True):
            st.markdown(f"**{item.get('report_id', '')}** · {item.get('title', '')}")
            st.caption(
                f"{item.get('feature', '')} · {item.get('severity', '')} · "
                f"{item.get('status', '')} · {_stamp(item.get('created_at'))}"
            )
            if item.get("admin_notes"):
                st.markdown("**Admin notes**")
                st.write(item["admin_notes"])
            if item.get("resolution_details"):
                st.markdown("**Resolution**")
                st.write(item["resolution_details"])
