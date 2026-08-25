"""Load the user profile and track definitions that personalize every build tool.

Copy `config/user-profile.example.json` to `config/user-profile.json` and edit it
with your own identity, template paths, and screening rules. Do the same for
`config/tracks.example.json` -> `config/tracks.json`. Every tool falls back to
the example files so a fresh clone runs end to end before you configure anything.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
PROFILE_PATH = CONFIG_DIR / "user-profile.json"
EXAMPLE_PROFILE_PATH = CONFIG_DIR / "user-profile.example.json"
TRACKS_PATH = CONFIG_DIR / "tracks.json"
EXAMPLE_TRACKS_PATH = CONFIG_DIR / "tracks.example.json"
TRACKER_PATH = ROOT / "jobs" / "tracker.json"
MASTER_RESUME_TEMPLATE_PATH = ROOT / "profile" / "master-resume.md"
EXAMPLE_MASTER_RESUME_TEMPLATE_PATH = ROOT / "profile" / "master-resume.example.md"


def _load_with_fallback(path: Path, example: Path) -> dict[str, Any]:
    source = path if path.exists() else example
    if not source.exists():
        raise FileNotFoundError(
            f"Missing {path.relative_to(ROOT)} and its example fallback "
            f"{example.relative_to(ROOT)} — restore the example file from git."
        )
    return json.loads(source.read_text(encoding="utf-8"))


def load_profile() -> dict[str, Any]:
    return _load_with_fallback(PROFILE_PATH, EXAMPLE_PROFILE_PATH)


def load_tracks() -> dict[str, Any]:
    return _load_with_fallback(TRACKS_PATH, EXAMPLE_TRACKS_PATH)["tracks"]


def master_resume_template() -> str:
    source = (
        MASTER_RESUME_TEMPLATE_PATH
        if MASTER_RESUME_TEMPLATE_PATH.exists()
        else EXAMPLE_MASTER_RESUME_TEMPLATE_PATH
    )
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


def resume_pdf_name(profile: dict[str, Any]) -> str:
    return profile["files"]["resume_basename"] + ".pdf"


def resume_docx_name(profile: dict[str, Any]) -> str:
    return profile["files"]["resume_basename"] + ".docx"
