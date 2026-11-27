import streamlit as st
from services.support_service import FAQ_CATEGORIES, list_published_faqs

from frontend.shared.components import empty_state, error_state, page_header, section
from frontend.shared.navigation import go_to


def _stamp(value) -> str:
    return str(value or "-")


def show_help_center(*, render_header: bool = True) -> None:
    if render_header:
        page_header(
            "Help center",
            "Browse common questions and reach the support team when you need more help",
            ":material/help:",
        )
    try:
        faqs = list_published_faqs()
    except Exception as exc:
        error_state("Help articles could not be loaded.", str(exc))
        return

    search = st.text_input(
        "Search FAQs",
        type="search",
        placeholder="Search questions and answers",
        key="help_faq_search",
    )
    categories = st.multiselect("Categories", list(FAQ_CATEGORIES), key="help_faq_categories")
    needle = search.strip().lower()

    def _matches(item: dict) -> bool:
        if categories and item.get("category") not in categories:
            return False
        haystack = f"{item.get('question', '')} {item.get('answer', '')}".lower()
        return not needle or needle in haystack

    filtered = [item for item in faqs if _matches(item)]
    section("Frequently asked questions", ":material/quiz:")
    if not filtered:
        empty_state(
            "No FAQs found." if faqs else "No help articles are published yet.",
            "Try another search or contact support for a personal answer.",
        )
    else:
        group_names = sorted({str(item.get("category", "General")) for item in filtered})
        for category in group_names:
            with st.expander(category):
                group = [
                    item for item in filtered if str(item.get("category", "General")) == category
                ]
                group.sort(
                    key=lambda item: (item.get("display_order") or 0, str(item.get("question", "")))
                )
                for item in group:
                    st.markdown(f"**{item.get('question', '')}**")
                    st.write(item.get("answer", ""))
                    st.caption(f"Updated {_stamp(item.get('updated_at'))}")

    section("Still need help?", ":material/contact_support:")
    st.caption("Send a message to the support team and we will get back to you.")
    if st.button(
        "Contact support",
        type="primary",
        icon=":material/mail:",
        width="stretch",
        key="help_contact_support",
    ):
        go_to("contact_support")
