from pathlib import Path

import streamlit as st
from streamlit import switch_page

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

PAGES: dict[str, str] = {
    "dashboard": "frontend/user/dashboard.py",
    "overview": "frontend/user/dashboard.py",
    "resume": "frontend/user/resume_analyzer.py",
    "job": "frontend/user/job_page.py",
    "job_description": "frontend/user/job_page.py",
    "job_matcher": "frontend/user/job_page.py",
    "interview_setup": "frontend/user/job_interview.py",
    "interview": "frontend/user/job_interview.py",
    "job_interview": "frontend/user/job_interview.py",
    "live_recording_history": "frontend/user/live_recording_history.py",
    "live_interview_invitations": "frontend/user/live_interview_invitations.py",
    "report": "frontend/user/report_page.py",
    "reports": "frontend/user/report_page.py",
    "progress": "frontend/user/analytics.py",
    "analytics": "frontend/user/analytics.py",
    "history": "frontend/user/interview_history.py",
    "settings": "frontend/user/account_settings.py",
    "account_settings": "frontend/user/account_settings.py",
    "coach": "frontend/user/coach.py",
    "behavioral": "frontend/user/coach.py",
    "evaluation": "frontend/user/coach.py",
    "technical": "frontend/user/learning_center.py",
    "technical_assessment": "frontend/user/learning_center.py",
    "knowledge": "frontend/user/learning_center.py",
    "knowledge_base": "frontend/user/learning_center.py",
    "question": "frontend/user/question_bank.py",
    "question_bank": "frontend/user/question_bank.py",
    "roadmap": "frontend/user/learning_resources.py",
    "learning_roadmap": "frontend/user/learning_resources.py",
    "learning_center": "frontend/user/learning_center.py",
    "improvement_plans": "frontend/user/improvement_plans.py",
    "notifications": "frontend/user/notifications.py",
    "support_center": "frontend/user/support_center.py",
    "my_support_tickets": "frontend/user/support_center.py",
    "submit_ticket": "frontend/user/support_center.py",
    "my_feedback": "frontend/user/support_center.py",
    "report_bug": "frontend/user/support_center.py",
    "feature_requests": "frontend/user/support_center.py",
    "help_center": "frontend/user/support_center.py",
    "contact_support": "frontend/user/support_center.py",
    "profile": "frontend/user/profile.py",
    "admin": "frontend/admin/dashboard.py",
    "admin_user_activity": "frontend/admin/user_activity.py",
    "admin_jobs": "frontend/admin/jobs.py",
    "admin_live_media": "frontend/admin/live_media.py",
    "admin_live_interviews": "frontend/admin/live_interview_invitations.py",
    "admin_learning": "frontend/admin/learning.py",
    "admin_data_management": "frontend/admin/data_management.py",
    "admin_support_management": "frontend/admin/support_management.py",
    "support_dashboard": "frontend/admin/support_management.py",
    "admin_support_tickets": "frontend/admin/support_management.py",
    "admin_user_feedback": "frontend/admin/support_management.py",
    "admin_bug_reports": "frontend/admin/support_management.py",
    "admin_feature_requests": "frontend/admin/support_management.py",
    "admin_contact_messages": "frontend/admin/support_management.py",
    "admin_faq_management": "frontend/admin/support_management.py",
    "admin_support_analytics": "frontend/admin/support_management.py",
}


def go_to(page: str) -> None:
    target = PAGES.get(page)
    if target is None:
        if not page.startswith(("frontend/", "app_pages/")):
            st.error(f"Unknown page: {page}")
            return
        target = page
    if not (_PROJECT_ROOT / target).exists():
        st.error(f"Page is not available: {target}")
        return
    st.session_state["current_page"] = page
    switch_page(target)
