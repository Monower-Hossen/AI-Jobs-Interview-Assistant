import streamlit as st
from services.support_service import (
    delete_faq,
    list_all_faqs,
    list_faq_categories,
    upsert_faq,
)

from frontend.shared.components import (
    confirm_delete,
    data_table,
    error_state,
    page_header,
    section,
    success_toast,
)

DELETE_TARGET_KEY = "_admin_faq_delete_target"
DELETE_LABEL_KEY = "_admin_faq_delete_label"


def _stamp(value: object) -> str:
    return str(value or "-")


def _option_index(options: object, value: object) -> int:
    try:
        return list(options).index(value)
    except (ValueError, TypeError):
        return 0


def _category_choices(categories: list[str], current: str = "") -> list[str]:
    choices = list(categories)
    if current and current not in choices:
        choices.append(current)
    choices.append("Custom category")
    return choices


def _render_create_form(account, categories: list[str]) -> None:
    with st.form("admin_faq_create", border=True):
        question = st.text_input(
            "Question", key="admin_faq_create_question", placeholder="What is a frequent question?"
        )
        answer = st.text_area(
            "Answer", key="admin_faq_create_answer", height=160, placeholder="Write a clear answer…"
        )
        selected_category = st.selectbox(
            "Category",
            _category_choices(categories),
            key="admin_faq_create_category",
        )
        custom_category = (
            st.text_input("New category", key="admin_faq_create_custom_category")
            if selected_category == "Custom category"
            else ""
        )
        display_order = st.number_input(
            "Display order", min_value=0, value=0, key="admin_faq_create_order"
        )
        published = st.checkbox("Publish immediately", value=True, key="admin_faq_create_published")
        created = st.form_submit_button(
            "Create FAQ", type="primary", icon=":material/add:", width="stretch"
        )
    if not created:
        return
    if not question.strip() or not answer.strip():
        error_state("Question and answer are required.")
        return
    category = (custom_category.strip() if custom_category else selected_category).strip()
    if not category:
        error_state("Enter a category name.")
        return
    try:
        upsert_faq(
            actor_admin_id=account.id,
            question=question.strip(),
            answer=answer.strip(),
            category=category,
            display_order=int(display_order),
            is_published=bool(published),
        )
    except Exception as exc:
        error_state(str(exc))
    else:
        success_toast("FAQ created.")
        st.rerun()


def _render_edit_form(account, categories: list[str], faq: dict) -> None:
    faq_id = int(faq["id"])
    category_choices = _category_choices(categories, str(faq.get("category") or ""))
    with st.form(f"admin_faq_edit_{faq_id}", border=True):
        question = st.text_input(
            "Question",
            value=str(faq.get("question") or ""),
            key=f"admin_faq_edit_question_{faq_id}",
        )
        answer = st.text_area(
            "Answer",
            value=str(faq.get("answer") or ""),
            height=160,
            key=f"admin_faq_edit_answer_{faq_id}",
        )
        selected_category = st.selectbox(
            "Category",
            category_choices,
            index=_option_index(category_choices, str(faq.get("category") or "")),
            key=f"admin_faq_edit_category_{faq_id}",
        )
        custom_category = (
            st.text_input("New category", key=f"admin_faq_edit_custom_category_{faq_id}")
            if selected_category == "Custom category"
            else ""
        )
        display_order = st.number_input(
            "Display order",
            min_value=0,
            value=int(faq.get("display_order") or 0),
            key=f"admin_faq_edit_order_{faq_id}",
        )
        published = st.checkbox(
            "Published",
            value=bool(faq.get("is_published")),
            key=f"admin_faq_edit_published_{faq_id}",
        )
        saved = st.form_submit_button("Save changes", icon=":material/save:", width="stretch")
    if not saved:
        return
    if not question.strip() or not answer.strip():
        error_state("Question and answer are required.")
        return
    category = (custom_category.strip() if custom_category else selected_category).strip()
    if not category:
        error_state("Enter a category name.")
        return
    try:
        upsert_faq(
            actor_admin_id=account.id,
            faq_id=faq_id,
            question=question.strip(),
            answer=answer.strip(),
            category=category,
            display_order=int(display_order),
            is_published=bool(published),
        )
    except Exception as exc:
        error_state(str(exc))
    else:
        success_toast("FAQ updated.")
        st.rerun()


def _render_delete_confirmation(account) -> None:
    target_id = st.session_state.get(DELETE_TARGET_KEY)
    if target_id is None:
        return
    key = f"_admin_faq_delete_{int(target_id)}"
    if st.session_state.get(f"{key}_resolved"):
        st.session_state.pop(f"{key}_resolved", None)
        if st.session_state.get(key):
            try:
                delete_faq(int(target_id), actor_admin_id=account.id)
            except Exception as exc:
                error_state(str(exc))
                st.session_state.pop(DELETE_TARGET_KEY, None)
                st.session_state.pop(DELETE_LABEL_KEY, None)
                st.session_state.pop(key, None)
                st.session_state.pop(f"{key}_ack", None)
                st.rerun()
            success_toast("FAQ deleted.")
        st.session_state.pop(DELETE_TARGET_KEY, None)
        st.session_state.pop(DELETE_LABEL_KEY, None)
        st.rerun()
    confirm_delete(st.session_state.get(DELETE_LABEL_KEY, str(target_id)), key=key)


def show_admin_faq_management(*, render_header: bool = True) -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()

    from services.auth_service import AuthError, require_admin

    try:
        require_admin(account.id)
    except AuthError as exc:
        st.error(str(exc), icon=":material/error:")
        st.stop()

    if render_header:
        page_header(
            "FAQ management",
            "Create, edit, publish, and organize help center answers",
            ":material/quiz:",
        )

    try:
        categories = list_faq_categories(actor_admin_id=account.id)
    except Exception as exc:
        error_state(str(exc))
        categories = []

    section("Create an FAQ", ":material/add:")
    _render_create_form(account, categories)

    section("Manage FAQs", ":material/edit:")
    search = st.text_input(
        "Search FAQs",
        type="search",
        placeholder="Question, answer, or category",
        key="admin_faq_search",
    )
    filter_column, publish_column = st.columns(2)
    category_filter = filter_column.selectbox(
        "Category", ["All", *categories], key="admin_faq_category_filter"
    )
    published_filter = publish_column.selectbox(
        "Visibility", ["All", "Published", "Draft"], key="admin_faq_published_filter"
    )
    published_value = {
        "All": None,
        "Published": "published",
        "Draft": "draft",
    }[published_filter]

    try:
        faqs = list_all_faqs(
            actor_admin_id=account.id,
            search=search,
            category=category_filter if category_filter != "All" else None,
            published=published_value,
        )
    except Exception as exc:
        error_state(str(exc))
        return

    data_table(
        [
            {
                "ID": item.get("faq_id", ""),
                "Category": item.get("category", ""),
                "Question": item.get("question", ""),
                "Published": "Yes" if item.get("is_published") else "No",
                "Order": item.get("display_order", 0),
                "Updated": _stamp(item.get("updated_at")),
            }
            for item in faqs
        ],
        "No FAQs match the current filters.",
        "FAQ library",
    )
    if not faqs:
        return

    options = {
        f"{item.get('faq_id', '')} · {item.get('question', '')}": int(item["id"]) for item in faqs
    }
    choice = st.selectbox("Select an FAQ to edit", list(options), key="admin_faq_choice")
    selected_faq = next((item for item in faqs if int(item["id"]) == options[choice]), None)
    if not selected_faq:
        return

    with st.expander("Preview", expanded=False):
        st.markdown(f"### {selected_faq.get('question', '')}")
        st.write(str(selected_faq.get("answer") or ""))
        st.caption(f"Category: {selected_faq.get('category', '')}")

    action_column, delete_column = st.columns([2, 1])
    if action_column.button(
        "Toggle publish",
        icon=":material/visibility:",
        width="stretch",
        key=f"admin_faq_toggle_{int(selected_faq['id'])}",
    ):
        try:
            upsert_faq(
                actor_admin_id=account.id,
                faq_id=int(selected_faq["id"]),
                question=str(selected_faq.get("question") or ""),
                answer=str(selected_faq.get("answer") or ""),
                category=str(selected_faq.get("category") or ""),
                display_order=int(selected_faq.get("display_order") or 0),
                is_published=not bool(selected_faq.get("is_published")),
            )
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast(
                "FAQ unpublished." if selected_faq.get("is_published") else "FAQ published."
            )
            st.rerun()
    if delete_column.button(
        "Delete FAQ",
        icon=":material/delete:",
        width="stretch",
        key=f"admin_faq_delete_start_{int(selected_faq['id'])}",
    ):
        st.session_state[DELETE_TARGET_KEY] = int(selected_faq["id"])
        st.session_state[DELETE_LABEL_KEY] = str(selected_faq.get("question") or "FAQ")
        key = f"_admin_faq_delete_{int(selected_faq['id'])}"
        st.session_state[key] = False
        st.session_state[f"{key}_ack"] = False
        st.session_state[f"{key}_resolved"] = False

    _render_edit_form(account, categories, selected_faq)
    _render_delete_confirmation(account)
