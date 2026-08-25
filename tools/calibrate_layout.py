#!/usr/bin/env python3
"""Visualize the configured resume-template layout on top of your source PDF.

Usage: python3 tools/calibrate_layout.py [output.png]

Renders page 1 of `files.source_resume_pdf` with the regions from
`resume_template_layout` drawn on top:

  red boxes     pdf_redact_rects       (areas whited out before re-inserting text)
  green line    pdf_headline_baseline  (baseline the new headline is drawn on)
  blue box      pdf_summary_box        (text box the summary must fit inside)
  purple lines  pdf_skill_baselines    (baselines of the five skill lines)

Adjust the numbers in config/user-profile.json and re-run until every region
sits exactly over the corresponding text in your template. Default output:
calibration-preview.png in the repo root (gitignored).
"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from user_config import ROOT, expand, load_profile

RED = (0.86, 0.15, 0.15)
GREEN = (0.05, 0.55, 0.30)
BLUE = (0.12, 0.35, 0.85)
PURPLE = (0.55, 0.20, 0.80)


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "calibration-preview.png"
    profile = load_profile()
    layout = profile["resume_template_layout"]
    source = expand(profile["files"]["source_resume_pdf"])
    if not source.exists():
        raise SystemExit(
            f"Source PDF missing: {source} — set files.source_resume_pdf in config/user-profile.json"
        )

    doc = fitz.open(source)
    page = doc[0]
    width = page.rect.width

    for rect in layout["pdf_redact_rects"]:
        page.draw_rect(fitz.Rect(*rect), color=RED, width=0.8)

    x, y = layout["pdf_headline_baseline"]
    page.draw_line((x, y), (x + layout["pdf_max_text_width"], y), color=GREEN, width=0.8)
    page.draw_circle((x, y), 2, color=GREEN, fill=GREEN)

    page.draw_rect(fitz.Rect(*layout["pdf_summary_box"]), color=BLUE, width=0.8)

    for baseline in layout["pdf_skill_baselines"]:
        page.draw_line((x, baseline), (min(x + layout["pdf_max_text_width"], width - 20), baseline),
                       color=PURPLE, width=0.8)

    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    pixmap.save(output)
    doc.close()
    print(f"Calibration preview written to {output}")
    print("red = redact rects · green = headline baseline · blue = summary box · purple = skill baselines")


if __name__ == "__main__":
    main()
