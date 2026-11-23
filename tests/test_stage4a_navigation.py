import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AI_AVATAR_PAGE = "frontend/user/ai_avatar_interview.py"
LEGACY_PAGE = "frontend/user/mock_interview.py"


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


def test_user_sidebar_groups_are_consolidated_groups() -> None:
    groups = _user_page_groups()
    assert list(groups) == [
        "Overview",
        "Interview Studio",
        "Career Preparation",
        "Performance",
        "AI Career Coach",
        "Notifications",
        "My Account",
    ]
    assert all(groups.values())


def test_mock_interview_alias_and_legacy_routes_are_registered() -> None:
    groups = _user_page_groups()
    practice = groups["Interview Studio"]

    canonical = [entry for entry in practice if entry.get("title") == "Mock Interview"]
    assert len(canonical) == 1
    assert canonical[0]["path"] == AI_AVATAR_PAGE
    assert canonical[0]["url_path"] == "mock_interview"
    assert "visibility" not in canonical[0]

    alias = [entry for entry in practice if entry.get("title") == "AI Avatar Interview"]
    assert len(alias) == 1
    assert alias[0]["path"] == AI_AVATAR_PAGE
    assert alias[0]["url_path"] == "ai-avatar-interview"
    assert alias[0]["visibility"] == "hidden"

    legacy = [entry for entry in practice if entry.get("title") == "Legacy Mock Interview"]
    assert len(legacy) == 1
    assert legacy[0]["path"] == LEGACY_PAGE
    assert legacy[0]["url_path"] == "legacy-mock-interview"
    assert legacy[0]["visibility"] == "hidden"

    live_webcam = [entry for entry in practice if entry.get("title") == "Live Interview"]
    assert len(live_webcam) == 1
    assert live_webcam[0]["path"] == "frontend/user/live_webcam_interview.py"
    assert live_webcam[0]["url_path"] == "live-webcam-interview"
    assert not [
        entry for entry in groups["Career Preparation"] if entry.get("title") == "Live Interview"
    ]


def test_navigation_aliases_resolve_to_registered_pages() -> None:
    pages = _navigation_pages()
    for alias in ("mock_interview", "interview_setup", "interview", "ai_avatar", "ai_avatar_interview"):
        assert pages[alias] == AI_AVATAR_PAGE
    assert pages["legacy_mock_interview"] == LEGACY_PAGE
    for target in pages.values():
        assert (PROJECT_ROOT / str(target)).exists(), target


def test_go_to_keeps_live_webcam_teardown_exception() -> None:
    source = (PROJECT_ROOT / "frontend" / "shared" / "navigation.py").read_text(
        encoding="utf-8"
    )
    assert 'target != PAGES["live_webcam"]' in source
    assert "teardown_for_navigation()" in source
    assert "switch_page(target)" in source
