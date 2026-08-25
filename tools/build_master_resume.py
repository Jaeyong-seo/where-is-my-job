#!/usr/bin/env python3
"""Regenerate applications/_master/resume.md from the master resume template.

Usage: python3 tools/build_master_resume.py [track]

The master resume is the untailored baseline the dashboard links to. It is the
same substitution as a tailored resume, using the track named by the argument,
or `default_track` from config/user-profile.json when omitted.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from user_config import ROOT, load_profile, load_tracks, render_markdown_resume

OUTPUT = ROOT / "applications" / "_master" / "resume.md"


def main() -> None:
    profile = load_profile()
    tracks = load_tracks()
    track_name = sys.argv[1] if len(sys.argv) > 1 else profile.get("default_track")
    if not track_name:
        raise SystemExit("No track given and no default_track in config/user-profile.json")
    if track_name not in tracks:
        raise SystemExit(f"Unknown track '{track_name}' — available: {', '.join(sorted(tracks))}")
    content = tracks[track_name]
    content = {
        "headline": content["headline"],
        "summary": content["summary"],
        "skills": [tuple(pair) for pair in content["skills"]],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_markdown_resume(profile, content), encoding="utf-8")
    print(f"{track_name} -> {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
