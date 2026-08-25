---
name: initial-setup
description: Interactive onboarding for this repo — interview the user about who they are, what roles they want, where they search, and their screening rules; then generate config/user-profile.json, config/tracks.json, and profile/master-resume.md, calibrate the PDF layout, and verify everything with the doctor. Run this once on a fresh clone before the first /apply-cycle.
argument-hint: "(no arguments — just run it)"
---

# Initial Setup

Turn a fresh clone into a personalized ops room through a short interview. End state: `python3 tools/doctor.py` reports 0 failures and the dashboard shows the user's name.

Conduct the interview in the user's language; write all generated files in English unless the user asks otherwise. Ask in small batches (use AskUserQuestion where available, otherwise plain questions), and never invent an answer the user didn't give — leave optional fields out rather than guessing.

## Step 1 — Interview: identity

Ask for: full name, email, phone (optional), city + region ("Vancouver, BC"), LinkedIn slug or URL. Derive `initials` from the name and confirm.

## Step 2 — Interview: target search

Ask for:
- Target positions (e.g. "frontend, full-stack, mobile") and the keywords that describe their stack.
- Target city and acceptable work models — this fills `search.target_city_label` and `search.target_locations` (include remote-in-country if acceptable) and `search.timezone`.
- Work authorization: which country, any expiry, sponsorship needed or not → `screening.work_authorization_statement`, `screening.sponsorship_answer` (the answer they want to give on "will you now or in the future require sponsorship?" forms).
- Salary expectations → `screening.salary_answer_anchor` (e.g. "mid-to-low end of the posted band").
- Hard drop rules: what postings should be discarded on sight (wrong-country remote, seniority far above/below, staffing agencies, specific industries) → `screening.drop_rules`.

## Step 3 — Interview: resume sources

Ask for:
- The path to their one-page resume as DOCX and as PDF (exported from the same document). Explain these are cloned, never edited: only headline / summary / five skill lines get replaced.
- Their preferred output file naming → `files.resume_basename` ("<Name> Resume") and `files.cover_letter_basename`.
- The resume body font. Default to the platform's Arial paths (macOS: `/System/Library/Fonts/Supplemental/Arial.ttf` and `Arial Bold.ttf`); on Linux ask for TTF paths (e.g. DejaVu or Liberation fonts).

If they don't have a one-page DOCX/PDF template yet, tell them the builders will stay blocked until they do, finish the rest of the setup anyway, and note it in the final report.

## Step 4 — Generate configuration

1. Write `config/user-profile.json` from the answers, starting from `config/user-profile.example.json` as the schema reference. Keep the example's `resume_template_layout` values as the starting point — they get calibrated in Step 6. Keep `dashboard.storage_key` unique-ish (e.g. `<slug>-job-status-v1`).
2. Build 2–5 tracks in `config/tracks.json`, one per target position family from Step 2. For each track write a headline, a 2–3 sentence summary that ends with the location / work-authorization line, and exactly five `[label, value]` skill lines using their keywords. Show the drafts and iterate until the user approves the wording.
3. Create `profile/master-resume.md`: copy `profile/master-resume.example.md` and fill the EXPERIENCE / EDUCATION / LANGUAGES sections from whatever the user provides (pasted resume text, or the PDF read directly). Keep the `{{NAME}}` / `{{HEADLINE}}` / `{{CONTACT}}` / `{{SUMMARY}}` / `{{SKILLS}}` placeholders intact.
4. Update the `candidate` block in `jobs/tracker.json` to match the new identity.

## Step 5 — Verify

Run `python3 tools/doctor.py` (use `.venv/bin/python` if the repo has a venv; `uv sync --group dev --group builders` first if not). Fix every ✗ before continuing. Warnings about sample roles are fine at this stage.

## Step 6 — Calibrate the PDF layout

Only when the source PDF exists:

1. Run `python3 tools/calibrate_layout.py` and show the user `calibration-preview.png` (send the file).
2. Ask whether the red redact boxes cover exactly the headline, summary, and skills text of THEIR template, the green line sits on the headline baseline, the blue box bounds the summary, and the five purple lines sit on the skill-line baselines.
3. Adjust `resume_template_layout` in `config/user-profile.json` and re-render until the user confirms. Also verify `docx_paragraph_count` and the three `docx_*` indices via the doctor's DOCX check; if the paragraph count differs, inspect `word/document.xml` to find the headline/summary/skills paragraph indices.
4. Smoke-test: run `python3 tools/build_job_applications.py <sample-role-id>` against one sample role, open the generated PDF, and confirm it looks identical to their template with replaced text. Delete the generated sample output afterwards.

## Step 7 — Finish

1. Regenerate the baseline resume (`python3 tools/build_master_resume.py`, uses `default_track`) and rebuild the dashboard (`python3 tools/build_job_dashboard.py`); confirm the header shows their name/initials/city.
2. Remind them: sample roles in `jobs/tracker.json` are still in place — the first `/apply-cycle <posting-url>` replaces them with a real one, and they should delete the samples (`applications/example-corp/`, `applications/acme/`, plus the two tracker entries) whenever they like.
3. Commit on a branch (`setup/<name>`), ff-merge to main per repo convention. Note: `config/user-profile.json`, `config/tracks.json`, and `profile/master-resume.md` are gitignored by default — if the repo is a private fork and the user wants them versioned, remove those lines from `.gitignore` with their confirmation.
4. Final report: what was configured, what remains (e.g. missing DOCX template), and the suggested next command (`/apply-cycle <first-posting-url>`).
