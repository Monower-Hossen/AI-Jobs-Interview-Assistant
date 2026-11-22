from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"

SECRET_NAME_RE = re.compile(
    r"^(?P<name>"
    r"[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PRIVATE_KEY|PASSWORD)"
    r"[A-Z0-9_]*"
    r")\s*=\s*(?P<value>.*)$"
)
# Non-credential config knobs that contain secret-ish words.
NON_CREDENTIAL_NAMES = {
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_MAX_LENGTH",
    "TOKEN_EXPIRY_SECONDS",
    "TOKEN_TTL_SECONDS",
}
SAFE_PLACEHOLDER_RE = re.compile(
    r"^(?:"
    r"your-[a-z0-9][a-z0-9-]*"
    r"|replace-with-[a-z0-9][a-z0-9-]*"
    r"|example[a-z0-9-]*"
    r"|changeme[a-z0-9-]*"
    r"|dummy[a-z0-9-]*"
    r"|placeholder[a-z0-9-]*"
    r"|<[^>]+>"
    r"|"
    r")$",
    re.IGNORECASE,
)
# Common real-key prefixes/shapes. Keep generic; do not include actual secrets.
REAL_KEY_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{16,}\b"),
    re.compile(r"\bAQ\.[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
]


def _secret_assignments(text: str) -> list[tuple[int, str, str]]:
    assignments: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = SECRET_NAME_RE.match(stripped)
        if not match:
            continue
        name = match.group("name")
        if name in NON_CREDENTIAL_NAMES:
            continue
        value = match.group("value").strip().strip('"').strip("'")
        assignments.append((lineno, name, value))
    return assignments


def test_env_example_contains_only_safe_secret_placeholders() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    bad: list[str] = []
    for lineno, name, value in _secret_assignments(text):
        if not SAFE_PLACEHOLDER_RE.match(value):
            bad.append(f"{name}@{lineno}")
        for pattern in REAL_KEY_PATTERNS:
            if pattern.search(value):
                bad.append(f"{name}@{lineno}:real-key-shape")
    assert not bad, f"Unsafe .env.example secret values: {bad}"


def test_gitignore_keeps_env_private_but_tracks_env_example() -> None:
    text = GITIGNORE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "\n.env\n" in text or text.startswith(".env\n")
    assert "\n.env.local\n" in text
    assert "\n.env.*.local\n" in text
    assert "\n!.env.example\n" in text
    # Ensure .env.example is not ignored as a plain entry.
    plain_ignored = re.search(r"(?m)^[ \t]*\.env\.example[ \t]*$", text)
    assert plain_ignored is None, ".env.example should not be ignored"


def test_env_example_declares_required_provider_keys() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for name in [
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MISTRAL_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
    ]:
        assert re.search(rf"^{re.escape(name)}=", text, re.MULTILINE), name
