"""Load the user profile and track definitions that personalize every build tool.

Copy `config/user-profile.example.json` to `config/user-profile.json` and edit it
with your own identity, template paths, and screening rules. Do the same for
`config/tracks.example.json` -> `config/tracks.json`. Every tool falls back to
the example files so a fresh clone runs end to end before you configure anything.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Canonical vocabularies shared by the tracker, dashboard, and doctor.
# Order matters: it drives dashboard filter menus and the pipeline metric row.
STATUSES: list[tuple[str, str]] = [
    ("discovered", "Discovered"),
    ("materials_ready", "Ready"),
    ("applied", "Applied"),
    ("interview", "Interview"),
    ("offer", "Offer"),
    ("rejected", "Rejected"),
    ("dropped", "Dropped"),
]
TIERS: list[tuple[str, str]] = [
    ("precision", "Precision"),
    ("volume", "Volume"),
    ("remote_bonus", "Remote bonus"),
    ("relocation", "Relocation"),
]
CONFIG_DIR = ROOT / "config"
PROFILE_PATH = CONFIG_DIR / "user-profile.json"
EXAMPLE_PROFILE_PATH = CONFIG_DIR / "user-profile.example.json"
TRACKS_PATH = CONFIG_DIR / "tracks.json"
EXAMPLE_TRACKS_PATH = CONFIG_DIR / "tracks.example.json"
TRACKER_PATH = ROOT / "jobs" / "tracker.json"
MASTER_RESUME_TEMPLATE_PATH = ROOT / "profile" / "master-resume.md"
EXAMPLE_MASTER_RESUME_TEMPLATE_PATH = ROOT / "profile" / "master-resume.example.md"


_warned_fallbacks: set[Path] = set()


def _warn_fallback(path: Path, example: Path) -> None:
    if path in _warned_fallbacks:
        return
    _warned_fallbacks.add(path)
    print(
        f"[user_config] {path.relative_to(ROOT)} not found — falling back to "
        f"{example.relative_to(ROOT)} (Jane Doe placeholder). Copy the example "
        "and personalize it before building real materials, or run /initial-setup.",
        file=sys.stderr,
    )


def _load_with_fallback(path: Path, example: Path) -> dict[str, Any]:
    source = path if path.exists() else example
    if not source.exists():
        raise FileNotFoundError(
            f"Missing {path.relative_to(ROOT)} and its example fallback "
            f"{example.relative_to(ROOT)} — restore the example file from git."
        )
    if source is example:
        _warn_fallback(path, example)
    return json.loads(source.read_text(encoding="utf-8"))


def load_profile() -> dict[str, Any]:
    return _load_with_fallback(PROFILE_PATH, EXAMPLE_PROFILE_PATH)


def load_tracks() -> dict[str, Any]:
    return _load_with_fallback(TRACKS_PATH, EXAMPLE_TRACKS_PATH)["tracks"]


def master_resume_template() -> str:
    source = MASTER_RESUME_TEMPLATE_PATH
    if not source.exists():
        source = EXAMPLE_MASTER_RESUME_TEMPLATE_PATH
        _warn_fallback(MASTER_RESUME_TEMPLATE_PATH, source)
    return source.read_text(encoding="utf-8")


def expand(path: str) -> Path:
    return Path(path).expanduser()


def contact_line(profile: dict[str, Any]) -> str:
    identity = profile["identity"]
    return " | ".join(
        part
        for part in (
            identity.get("location"),
            identity.get("email"),
            identity.get("phone"),
            identity.get("linkedin"),
        )
        if part
    )


def render_markdown_resume(profile: dict[str, Any], content: dict[str, Any]) -> str:
    """Fill the master resume template with one track's headline/summary/skills."""
    skill_lines = "\n".join(f"- **{label}:** {value}" for label, value in content["skills"])
    template = master_resume_template()
    replacements = {
        "{{NAME}}": profile["identity"]["name"].upper(),
        "{{HEADLINE}}": content["headline"],
        "{{CONTACT}}": contact_line(profile),
        "{{SUMMARY}}": content["summary"],
        "{{SKILLS}}": skill_lines,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def resume_pdf_name(profile: dict[str, Any]) -> str:
    return profile["files"]["resume_basename"] + ".pdf"


def resume_docx_name(profile: dict[str, Any]) -> str:
    return profile["files"]["resume_basename"] + ".docx"
