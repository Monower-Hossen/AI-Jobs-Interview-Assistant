import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEARNING_CENTER_PAGE = "frontend/user/learning_center.py"
ROADMAP_PAGE = "frontend/user/learning_resources.py"
LEARNING_PATHS = {LEARNING_CENTER_PAGE, ROADMAP_PAGE}
EXPECTED_TABS = [
    "Overview",
    "Study plan",
    "Resources",
    "Knowledge library",
    "Exercises",
    "Study notes",
    "Progress",
]
EXPECTED_NON_LEARNING_PAGES = {
    "Overview": [
        {
            "path": "frontend/user/dashboard.py",
            "title": "Dashboard",
            "icon": ":material/dashboard:",
            "default": True,
        },
    ],
    "Interview Studio": [
        {
            "path": "frontend/user/job_interview.py",
            "title": "Job Interview",
            "icon": ":material/videocam:",
            "url_path": "job-interview",
        },
        {
            "path": "frontend/user/live_webcam_interview.py",
            "title": "Live Interview",
            "icon": ":material/videocam:",
            "url_path": "live-webcam-interview",
        },
        {
            "path": "frontend/user/live_interview_invitations.py",
            "title": "Invitations",
            "icon": ":material/event_available:",
            "url_path": "live-interview-invitations",
        },
    ],
    "Career Preparation": [
        {
            "path": "frontend/user/resume_analyzer.py",
            "title": "Resume Analyzer",
            "icon": ":material/description:",
        },
        {
            "path": "frontend/user/job_page.py",
            "title": "Job Preparation",
            "icon": ":material/work:",
        },
        {
            "path": "frontend/user/question_bank.py",
            "title": "Question Bank",
            "icon": ":material/library_books:",
        },
    ],
    "Performance": [
        {
            "path": "frontend/user/interview_history.py",
            "title": "Interview History",
            "icon": ":material/history:",
        },
        {
            "path": "frontend/user/analytics.py",
            "title": "Performance Analytics",
            "icon": ":material/analytics:",
        },
        {
            "path": "frontend/user/report_page.py",
            "title": "Interview Reports",
            "icon": ":material/assessment:",
        },
        {
            "path": "frontend/user/live_recording_history.py",
            "title": "Recording History",
            "icon": ":material/video_file:",
            "url_path": "live-recording-history",
        },
    ],
    "AI Career Coach": [
        {
            "path": "frontend/user/coach.py",
            "title": "AI Interview Coach",
            "icon": ":material/psychology:",
        },
        {
            "path": "frontend/user/improvement_plans.py",
            "title": "Improvement Plans",
            "icon": ":material/checklist:",
            "url_path": "improvement-plans",
        },
    ],
    "Notifications": [
        {
            "path": "frontend/user/notifications.py",
            "title": "Notifications",
            "icon": ":material/notifications:",
            "url_path": "notifications",
        },
    ],
    "My Account": [
        {
            "path": "frontend/user/profile.py",
            "title": "Profile",
            "icon": ":material/person:",
        },
        {
            "path": "frontend/user/account_settings.py",
            "title": "Account Settings",
            "icon": ":material/tune:",
        },
    ],
}


def _user_page_groups() -> dict[str, list[dict[str, object]]]:
    tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_user_pages"
    )
    return_node = next(node for node in function.body if isinstance(node, ast.Return))
    assert isinstance(return_node.value, ast.Dict)
    groups: dict[str, list[dict[str, object]]] = {}
    for key_node, value_node in zip(return_node.value.keys, return_node.value.values):
        assert isinstance(key_node, ast.Constant)
        group = str(key_node.value)
        entries: list[dict[str, object]] = []
        for node in ast.walk(value_node):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "Page"
                or not node.args
                or not isinstance(node.args[0], ast.Constant)
            ):
                continue
            entry: dict[str, object] = {"path": str(node.args[0].value)}
            for keyword in node.keywords:
                if keyword.arg and isinstance(keyword.value, ast.Constant):
                    entry[keyword.arg] = keyword.value.value
            entries.append(entry)
        groups[group] = entries
    return groups


def _navigation_pages() -> dict[str, str]:
    tree = ast.parse(
        (PROJECT_ROOT / "frontend" / "shared" / "navigation.py").read_text(encoding="utf-8")
    )
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "PAGES"
            and isinstance(node.value, ast.Dict)
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("PAGES mapping was not found")


def test_learning_center_is_the_only_visible_learning_entry() -> None:
    preparation = _user_page_groups()["Career Preparation"]
    assert "frontend/user/learning_progress.py" not in [
        str(entry.get("path")) for entry in preparation
    ]
    visible_learning_entries = [
        entry
        for entry in preparation
        if entry.get("path") in LEARNING_PATHS and entry.get("visibility", "visible") == "visible"
    ]
    assert len(visible_learning_entries) == 1
    entry = visible_learning_entries[0]
    assert entry["path"] == LEARNING_CENTER_PAGE
    assert entry["title"] == "Learning Center"
    assert entry["icon"] == ":material/school:"


def test_legacy_learning_resources_page_is_registered_and_hidden() -> None:
    preparation = _user_page_groups()["Career Preparation"]
    roadmap_entries = [entry for entry in preparation if entry.get("path") == ROADMAP_PAGE]
    assert len(roadmap_entries) == 1
    entry = roadmap_entries[0]
    assert entry["title"] == "Learning roadmap"
    assert entry["icon"] == ":material/route:"
    assert entry["url_path"] == "learning_resources"
    assert entry["visibility"] == "hidden"

    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    assert "st.navigation(_user_pages(account), position=\"sidebar\", expanded=True).run()" in source


def test_legacy_roadmap_page_calls_original_renderer() -> None:
    tree = ast.parse((PROJECT_ROOT / ROADMAP_PAGE).read_text(encoding="utf-8"))
    calls = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "show_roadmap_page"
    ]
    assert len(calls) == 1
    assert not calls[0].args
    assert not calls[0].keywords
    imported_modules = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert "frontend.user.roadmap_page" in imported_modules
    source = (PROJECT_ROOT / ROADMAP_PAGE).read_text(encoding="utf-8")
    assert "show_learning_center" not in source


def test_learning_center_tabs_are_preserved() -> None:
    tree = ast.parse((PROJECT_ROOT / LEARNING_CENTER_PAGE).read_text(encoding="utf-8"))
    tab_lists = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "tabs"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            tab_lists.append(ast.literal_eval(node.args[0]))
    assert tab_lists == [EXPECTED_TABS]


def test_roadmap_aliases_and_go_to_behavior_are_unchanged() -> None:
    pages = _navigation_pages()
    expected = {
        "technical": LEARNING_CENTER_PAGE,
        "technical_assessment": LEARNING_CENTER_PAGE,
        "knowledge": LEARNING_CENTER_PAGE,
        "knowledge_base": LEARNING_CENTER_PAGE,
        "roadmap": ROADMAP_PAGE,
        "learning_roadmap": ROADMAP_PAGE,
        "learning_center": LEARNING_CENTER_PAGE,
    }
    for alias, target in expected.items():
        assert pages[alias] == target
    for target in set(expected.values()):
        assert (PROJECT_ROOT / target).exists()

    source = (PROJECT_ROOT / "frontend" / "shared" / "navigation.py").read_text(
        encoding="utf-8"
    )
    assert 'target != PAGES["live_webcam"]' in source
    assert "teardown_for_navigation()" in source
    assert "switch_page(target)" in source


def test_no_unrelated_user_page_declarations_changed() -> None:
    groups = _user_page_groups()
    assert list(groups) == list(EXPECTED_NON_LEARNING_PAGES)
    unrelated = {
        group: [entry for entry in entries if entry.get("path") not in LEARNING_PATHS]
        for group, entries in groups.items()
    }
    assert unrelated == EXPECTED_NON_LEARNING_PAGES


def test_learning_center_tabs_only_render_selected_tab() -> None:
    source = (PROJECT_ROOT / LEARNING_CENTER_PAGE).read_text(encoding="utf-8")
    assert 'on_change="rerun"' in source
    assert 'key="learning_center_tabs"' in source
    for tab in (
        "overview_tab",
        "roadmap_tab",
        "resources_tab",
        "library_tab",
        "exercises_tab",
        "notes_tab",
        "progress_tab",
    ):
        assert f"if {tab}.open:" in source
        assert f"with {tab}:" in source


def test_knowledge_page_handles_low_memory_guard(monkeypatch) -> None:
    from services.resource_monitor import ResourceBusyError
    from streamlit.testing.v1 import AppTest

    def _denied(*args, **kwargs):
        raise ResourceBusyError("Available system memory is critically low.")

    monkeypatch.syspath_prepend(str(PROJECT_ROOT))
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "backend"))
    monkeypatch.setattr("frontend.user.knowledge_page.get_knowledge_base", _denied)
    app = AppTest.from_string(
        "from frontend.user.knowledge_page import show_knowledge_page\n"
        "show_knowledge_page()",
        default_timeout=30,
    )
    app.session_state["user_session_id"] = "resource-token"
    app.session_state["user_id"] = 7
    app.run()

    assert not app.exception, [str(error.value) for error in app.exception]
    warnings = "\n".join(str(item.value) for item in app.warning)
    assert "knowledge library is temporarily unavailable" in warnings.lower()
