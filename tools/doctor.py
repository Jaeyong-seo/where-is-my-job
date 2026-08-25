#!/usr/bin/env python3
"""Onboarding health check: validate configuration before building materials.

Usage: python3 tools/doctor.py

Checks the user profile, tracks, resume source templates, fonts, the master
resume markdown template, and the job tracker. Exits non-zero when anything
that would break a build is wrong; warnings don't fail the run.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from user_config import (
    EXAMPLE_MASTER_RESUME_TEMPLATE_PATH,
    MASTER_RESUME_TEMPLATE_PATH,
    PROFILE_PATH,
    ROOT,
    STATUSES,
    TIERS,
    TRACKER_PATH,
    TRACKS_PATH,
    expand,
    load_profile,
    load_tracks,
    master_resume_template,
)

OK, WARN, FAIL = "ok", "warn", "fail"
RESULTS: list[tuple[str, str]] = []

REQUIRED_PROFILE_KEYS = {
    "identity": ["name", "initials", "headline_role", "email", "location", "linkedin", "linkedin_url"],
    "files": ["resume_basename", "cover_letter_basename", "source_resume_docx", "source_resume_pdf"],
    "fonts": ["regular", "bold"],
    "resume_template_layout": [
        "docx_paragraph_count", "docx_headline_index", "docx_summary_index",
        "docx_skills_start_index", "pdf_redact_rects", "pdf_headline_baseline",
        "pdf_summary_box", "pdf_skill_baselines", "pdf_max_text_width", "pdf_headline_gray",
    ],
    "search": ["target_city_label", "target_locations", "timezone"],
    "screening": ["drop_rules", "sponsorship_answer", "salary_answer_anchor", "work_authorization_statement"],
    "dashboard": ["storage_key"],
}
PLACEHOLDERS = ("{{NAME}}", "{{HEADLINE}}", "{{CONTACT}}", "{{SUMMARY}}", "{{SKILLS}}")


def report(level: str, message: str) -> None:
    RESULTS.append((level, message))
    prefix = {OK: "  ✓", WARN: "  !", FAIL: "  ✗"}[level]
    print(f"{prefix} {message}")


def section(title: str) -> None:
    print(f"\n{title}")


def check_profile() -> dict:
    section("Profile (config/user-profile.json)")
    if PROFILE_PATH.exists():
        report(OK, "personal profile present")
    else:
        report(WARN, "using the Jane Doe example profile — copy user-profile.example.json or run /initial-setup")
    profile = load_profile()
    for block, keys in REQUIRED_PROFILE_KEYS.items():
        if block not in profile:
            report(FAIL, f"missing block: {block}")
            continue
        missing = [key for key in keys if key not in profile[block]]
        if missing:
            report(FAIL, f"{block}: missing keys {', '.join(missing)}")
        else:
            report(OK, f"{block}: complete")
    layout = profile.get("resume_template_layout", {})
    if len(layout.get("pdf_skill_baselines", [])) != 5:
        report(FAIL, "resume_template_layout.pdf_skill_baselines must list exactly 5 y-coordinates")
    return profile


def check_tracks(profile: dict) -> dict:
    section("Tracks (config/tracks.json)")
    if TRACKS_PATH.exists():
        report(OK, "personal tracks present")
    else:
        report(WARN, "using example tracks — copy tracks.example.json and write your own")
    tracks = load_tracks()
    if not tracks:
        report(FAIL, "no tracks defined")
    for name, track in tracks.items():
        problems = []
        if not track.get("headline"):
            problems.append("headline missing")
        if not track.get("summary"):
            problems.append("summary missing")
        skills = track.get("skills", [])
        if len(skills) != 5 or any(len(pair) != 2 for pair in skills):
            problems.append("skills must be exactly 5 [label, value] pairs")
        if problems:
            report(FAIL, f"track '{name}': {'; '.join(problems)}")
        else:
            report(OK, f"track '{name}': valid")
    default_track = profile.get("default_track")
    if not default_track:
        report(WARN, "no default_track in the profile — build_master_resume.py will need an explicit track")
    elif default_track in tracks:
        report(OK, f"default_track '{default_track}' exists")
    else:
        report(FAIL, f"default_track '{default_track}' is not a defined track")
    return tracks


def check_sources(profile: dict) -> None:
    section("Resume sources and fonts")
    paths = {
        "source DOCX": expand(profile["files"]["source_resume_docx"]),
        "source PDF": expand(profile["files"]["source_resume_pdf"]),
        "regular font": expand(profile["fonts"]["regular"]),
        "bold font": expand(profile["fonts"]["bold"]),
    }
    for label, path in paths.items():
        if path.exists():
            report(OK, f"{label}: {path}")
        else:
            report(FAIL, f"{label} missing: {path}")

    layout = profile["resume_template_layout"]
    docx = paths["source DOCX"]
    if docx.exists():
        try:
            from lxml import etree

            namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            with zipfile.ZipFile(docx) as archive:
                root = etree.fromstring(archive.read("word/document.xml"))
            count = len(root.xpath("./w:body/w:p", namespaces=namespace))
            expected = layout["docx_paragraph_count"]
            if count == expected:
                report(OK, f"DOCX paragraph count matches layout ({count})")
            else:
                report(FAIL, f"DOCX has {count} paragraphs but layout expects {expected} — recalibrate docx_* indices")
        except ImportError:
            report(WARN, "lxml not installed — skipping DOCX structure check (uv sync --group builders)")
        except Exception as error:  # noqa: BLE001 - surface any template problem
            report(FAIL, f"cannot inspect DOCX: {error}")

    pdf = paths["source PDF"]
    if pdf.exists():
        try:
            import fitz

            with fitz.open(pdf) as doc:
                if doc.page_count == 1:
                    report(OK, "source PDF is a single page")
                else:
                    report(FAIL, f"source PDF has {doc.page_count} pages; the builder requires exactly 1")
        except ImportError:
            report(WARN, "PyMuPDF not installed — skipping PDF check (uv sync --group builders)")


def check_master_template() -> None:
    section("Master resume template (profile/master-resume.md)")
    if MASTER_RESUME_TEMPLATE_PATH.exists():
        report(OK, "personal template present")
    elif EXAMPLE_MASTER_RESUME_TEMPLATE_PATH.exists():
        report(WARN, "using master-resume.example.md — copy it and fill in your experience")
    else:
        report(FAIL, "no master resume template found")
        return
    template = master_resume_template()
    missing = [placeholder for placeholder in PLACEHOLDERS if placeholder not in template]
    if missing:
        report(FAIL, f"template is missing placeholders: {', '.join(missing)}")
    else:
        report(OK, "all substitution placeholders present")


def check_tracker(tracks: dict) -> None:
    section("Tracker (jobs/tracker.json)")
    if not TRACKER_PATH.exists():
        report(FAIL, "jobs/tracker.json missing")
        return
    try:
        data = json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        report(FAIL, f"tracker is not valid JSON: {error}")
        return
    roles = data.get("roles")
    if not isinstance(roles, list):
        report(FAIL, "tracker must contain a 'roles' list")
        return
    report(OK, f"{len(roles)} role(s) registered")
    required = ("id", "company", "title", "location", "status", "track", "application_dir", "keywords")
    valid_statuses = {value for value, _ in STATUSES}
    valid_tiers = {value for value, _ in TIERS}
    for role in roles:
        role_id = role.get("id", "<missing id>")
        missing = [field for field in required if field not in role]
        if missing:
            report(FAIL, f"role '{role_id}': missing fields {', '.join(missing)}")
            continue
        if role["track"] not in tracks:
            report(FAIL, f"role '{role_id}': unknown track '{role['track']}'")
        if role["status"] not in valid_statuses:
            report(FAIL, f"role '{role_id}': unknown status '{role['status']}'")
        if "tier" in role and role["tier"] not in valid_tiers:
            report(FAIL, f"role '{role_id}': unknown tier '{role['tier']}'")
    if any(role.get("company") in {"Example Corp", "Acme Inc"} for role in roles):
        report(WARN, "sample roles still present — replace them with real postings")


def main() -> int:
    print(f"where-is-my-job doctor · {ROOT}")
    profile = check_profile()
    tracks = check_tracks(profile)
    check_sources(profile)
    check_master_template()
    check_tracker(tracks)

    failures = sum(1 for level, _ in RESULTS if level == FAIL)
    warnings = sum(1 for level, _ in RESULTS if level == WARN)
    print(f"\nSummary: {failures} failure(s), {warnings} warning(s)")
    if failures:
        print("Fix the ✗ items above, then re-run: python3 tools/doctor.py")
    elif warnings:
        print("Builds will work, but personalize the ! items before applying anywhere real.")
    else:
        print("All checks passed — you are ready to run /apply-cycle.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
