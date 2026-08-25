#!/usr/bin/env python3
"""Generate tailored application packages by cloning the user's original resume template.

The DOCX layout is never rebuilt. Your one-page Word resume is copied and only the
headline, summary, and five skill lines are replaced in place. The PDF starts from
your exported PDF and replaces those same text regions at the coordinates declared
in `config/user-profile.json` (`resume_template_layout`).

Configure identity, template paths, and layout in `config/user-profile.json`
(copy from `config/user-profile.example.json`). Track content lives in
`config/tracks.json` (copy from `config/tracks.example.json`).
"""
from __future__ import annotations

import json
import zipfile
import sys
from pathlib import Path

import fitz
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from user_config import (
    ROOT,
    TRACKER_PATH,
    contact_line,
    expand,
    load_profile,
    load_tracks,
    master_resume_template,
    resume_docx_name,
    resume_pdf_name,
)

PROFILE = load_profile()
TRACKS = load_tracks()
LAYOUT = PROFILE["resume_template_layout"]

SOURCE_DOCX = expand(PROFILE["files"]["source_resume_docx"])
SOURCE_PDF = expand(PROFILE["files"]["source_resume_pdf"])
DOCX_NAME = resume_docx_name(PROFILE)
PDF_NAME = resume_pdf_name(PROFILE)

FONT_REGULAR = expand(PROFILE["fonts"]["regular"])
FONT_BOLD = expand(PROFILE["fonts"]["bold"])
PDF_GRAY = tuple(value / 255 for value in LAYOUT["pdf_headline_gray"])
MAX_TEXT_WIDTH = LAYOUT["pdf_max_text_width"]


def safe_text(value: str) -> str:
    return value.replace("—", "-").replace("–", "-")


def track_content(role: dict) -> dict:
    content = TRACKS[role["track"]]
    return {
        "headline": safe_text(content["headline"]),
        "summary": safe_text(content["summary"]),
        "skills": [(safe_text(label), safe_text(value)) for label, value in content["skills"]],
    }


def replace_xml_paragraph(paragraph, text: str) -> None:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    text_nodes = paragraph.xpath(".//w:t", namespaces=namespace)
    if not text_nodes:
        raise ValueError("Template paragraph has no text nodes")
    text_nodes[0].text = text
    for node in text_nodes[1:]:
        node.text = ""


def replace_xml_skill(paragraph, label: str, value: str) -> None:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    text_nodes = paragraph.xpath(".//w:t", namespaces=namespace)
    if len(text_nodes) < 2:
        raise ValueError("Template skill paragraph is malformed")
    text_nodes[0].text = f"{label}: "
    text_nodes[1].text = value
    for node in text_nodes[2:]:
        node.text = ""


def build_docx(path: Path, role: dict, content: dict) -> None:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(SOURCE_DOCX, "r") as source:
        document_root = etree.fromstring(source.read("word/document.xml"))
        paragraphs = document_root.xpath("./w:body/w:p", namespaces=namespace)
        if len(paragraphs) != LAYOUT["docx_paragraph_count"]:
            raise ValueError(f"Unexpected template paragraph count: {len(paragraphs)}")

        # Only seven text regions change. Paragraph and run properties, bullets,
        # tab stops, hyperlinks, margins, styles, and relationships stay intact.
        replace_xml_paragraph(paragraphs[LAYOUT["docx_headline_index"]], content["headline"])
        replace_xml_paragraph(paragraphs[LAYOUT["docx_summary_index"]], content["summary"])
        skills_start = LAYOUT["docx_skills_start_index"]
        skill_paragraphs = paragraphs[skills_start:skills_start + 5]
        for paragraph, (label, value) in zip(skill_paragraphs, content["skills"], strict=True):
            replace_xml_skill(paragraph, label, value)

        document_xml = etree.tostring(
            document_root,
            encoding="UTF-8",
            xml_declaration=True,
            standalone=True,
        )
        temporary = path.with_suffix(".docx.tmp")
        with zipfile.ZipFile(temporary, "w") as output:
            for info in source.infolist():
                payload = document_xml if info.filename == "word/document.xml" else source.read(info.filename)
                output.writestr(info, payload)
    temporary.replace(path)


def insert_skill_line(page: fitz.Page, y: float, label: str, value: str) -> None:
    bold_font = fitz.Font(fontfile=str(FONT_BOLD))
    label_text = f"{label}: "
    label_width = bold_font.text_length(label_text, fontsize=10)
    total_width = label_width + fitz.Font(fontfile=str(FONT_REGULAR)).text_length(value, fontsize=10)
    if total_width > MAX_TEXT_WIDTH:
        raise ValueError(f"Skill line too wide ({total_width:.1f}pt): {label_text}{value}")
    x = LAYOUT["pdf_headline_baseline"][0]
    page.insert_text((x, y), label_text, fontsize=10, fontname="TemplateBold", color=(0, 0, 0))
    page.insert_text((x + label_width, y), value, fontsize=10, fontname="TemplateRegular", color=(0, 0, 0))


def build_pdf(path: Path, role: dict, content: dict) -> None:
    doc = fitz.open(SOURCE_PDF)
    if doc.page_count != 1:
        raise ValueError(f"Unexpected source PDF page count: {doc.page_count}")
    page = doc[0]

    for rect in LAYOUT["pdf_redact_rects"]:
        page.add_redact_annot(fitz.Rect(*rect), fill=(1, 1, 1))
    page.apply_redactions()

    page.insert_font(fontname="TemplateRegular", fontfile=str(FONT_REGULAR))
    page.insert_font(fontname="TemplateBold", fontfile=str(FONT_BOLD))

    headline_width = fitz.Font(fontfile=str(FONT_REGULAR)).text_length(content["headline"], fontsize=11)
    if headline_width > MAX_TEXT_WIDTH:
        raise ValueError(f"Headline too wide ({headline_width:.1f}pt): {content['headline']}")
    page.insert_text(
        tuple(LAYOUT["pdf_headline_baseline"]),
        content["headline"],
        fontsize=11,
        fontname="TemplateRegular",
        color=PDF_GRAY,
    )

    remainder = page.insert_textbox(
        fitz.Rect(*LAYOUT["pdf_summary_box"]),
        content["summary"],
        fontsize=10,
        lineheight=1.15,
        fontname="TemplateRegular",
        color=(0, 0, 0),
        align=fitz.TEXT_ALIGN_LEFT,
    )
    if remainder < 0:
        raise ValueError(f"Summary does not fit template for {role['id']}: overflow {-remainder:.2f}pt")

    for y, (label, value) in zip(LAYOUT["pdf_skill_baselines"], content["skills"], strict=True):
        insert_skill_line(page, y, label, value)

    name = PROFILE["identity"]["name"]
    doc.set_metadata({
        **doc.metadata,
        "title": f"{name} Resume - {role['company']} {role['title']}",
        "author": name,
        "subject": f"Tailored resume for {role['title']} at {role['company']}",
        "keywords": ", ".join(role["keywords"]),
    })
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def markdown_resume(content: dict) -> str:
    skill_lines = "\n".join(f"- **{label}:** {value}" for label, value in content["skills"])
    template = master_resume_template()
    replacements = {
        "{{NAME}}": PROFILE["identity"]["name"].upper(),
        "{{HEADLINE}}": content["headline"],
        "{{CONTACT}}": contact_line(PROFILE),
        "{{SUMMARY}}": content["summary"],
        "{{SKILLS}}": skill_lines,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def job_markdown(role: dict) -> str:
    keywords = " · ".join(role["keywords"])
    authorization = PROFILE["screening"]["work_authorization_statement"]
    return safe_text(f"""# {role['company']} - {role['title']}

- Status: `{role['status']}`
- Fit score: **{role['score']} / 10** ({role['tier']})
- Location: {role['location']}
- Work model: {role['work_model']}
- Posted: {role['posted']}
- Salary: {role['salary']}
- Source: {role['source_url']}
- Apply: {role['apply_url']}
- Application materials: `{role['application_dir']}`

## Requirements

{role['requirements']}

## ATS keywords

{keywords}

## Match rationale

{role['match']}

## Submission checklist

- [ ] Confirm posting is still active
- [ ] Upload `{PDF_NAME}` (or DOCX when requested)
- [ ] Confirm location / work-model answer
- [ ] Work authorization answer: {authorization}
- [ ] Review autofilled employment dates before submitting
- [ ] Record submission date and confirmation in `jobs/tracker.json`
""")


def validate_sources() -> None:
    for source in (SOURCE_DOCX, SOURCE_PDF, FONT_REGULAR, FONT_BOLD):
        if not source.exists():
            raise FileNotFoundError(
                f"Required source template missing: {source} — "
                "check the files/fonts paths in config/user-profile.json"
            )


def build_role(role: dict) -> Path:
    output = ROOT / role["application_dir"]
    output.mkdir(parents=True, exist_ok=True)
    content = track_content(role)
    (output / "resume.md").write_text(markdown_resume(content), encoding="utf-8")
    (output / "job.md").write_text(job_markdown(role), encoding="utf-8")
    build_docx(output / DOCX_NAME, role, content)
    build_pdf(output / PDF_NAME, role, content)
    return output


def main() -> None:
    validate_sources()
    data = json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
    wanted = set(sys.argv[1:])
    roles = [role for role in data["roles"] if not wanted or role["id"] in wanted]
    missing = wanted - {role["id"] for role in roles}
    if missing:
        raise SystemExit(f"Unknown role ids: {', '.join(sorted(missing))}")
    for role in roles:
        output = build_role(role)
        print(f"{role['id']}: {output.relative_to(ROOT)}")
    print(f"Generated {len(roles)} source-template application packages")


if __name__ == "__main__":
    main()
