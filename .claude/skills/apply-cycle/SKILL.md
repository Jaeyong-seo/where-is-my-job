---
name: apply-cycle
description: Run one full job-application cycle in this repo — verify a posting live, register it in the tracker, build a tailored resume + cover letter, strip AI tells (im-not-ai pass), render and verify PDFs, update tracker/dashboard, and draft (never send) a cold mail to a relevant manager. Trigger with a job posting URL or ATS id.
argument-hint: "[job posting URL, or company + role hint]"
---

# Apply Cycle

One cycle = posting → tailored materials → outreach draft. Input: `$ARGUMENTS` (a job URL, ATS id, or "company + role" hint).

The user's identity, target market, and screening rules live in `config/user-profile.json` (fallback: `config/user-profile.example.json`). Read it at the start of every cycle and apply its `screening` block instead of hardcoded rules.

## Hard rules (never skip)

- **Never send email, never submit application forms, never automate login/CAPTCHA/MFA.** Cold mail stops at a draft; the human sends.
- Screening: apply every `screening.drop_rules` entry from the profile. Sponsorship questions are answered with `screening.sponsorship_answer`. Salary answers anchor to `screening.salary_answer_anchor`.
- Concurrent sessions may run in this repo: before committing shared files (`jobs/jobs.md`, `jobs/tracker.json`, `dashboard.html`), diff against HEAD and stage only your own changes.
- A PreToolUse hook may block edits on main — work on a `job/<slug>` branch, then ff-merge back (`git fetch . job/<slug>:main`).

## Phase 0 — Discovery sweep (when no posting is given)

When the input is a hint ("find new postings") rather than a URL, run a multi-site sweep for the profile's `search.target_locations` market: the major boards for that region (e.g. Indeed, LinkedIn, Glassdoor, Wellfound, YC Work at a Startup, the national job bank), plus direct ATS board sweeps (Greenhouse/Ashby/Lever APIs) for companies already in the tracker. If a browser session is needed, use the user's own logged-in browser read-only at human pace; if a login page ever appears, stop and tell the user — never log in yourself. Screen results against `jobs/tracker.json` (dedupe by company+title), apply the profile's screening rules, and register only the candidates worth materials.

## Phase 1 — Verify the posting live

1. Prefer the ATS API when the URL reveals one:
   - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/<org>/jobs/<id>` (`?content=true` on the board list). Full board sweep: `/v1/boards/<org>/jobs`.
   - Ashby: `https://api.ashbyhq.com/posting-api/job-board/<org>?includeCompensation=true`; form fields via `POST https://jobs.ashbyhq.com/api/non-user-graphql` (op `ApiJobPosting`).
   - Lever: `https://api.lever.co/v0/postings/<org>/<id>`.
2. For pages without an API, browse with whatever browser tooling this session has, read-only.
3. Record: exact title, canonical URL, full JD text, salary band, location, work model, posted/updated date. A dead posting (404/board-missing) → mark any existing tracker entry `dropped` and stop.
4. While at the ATS, check sibling postings — the best-fit role is sometimes not the one the user linked.

## Phase 2 — Register in the tracker

Append the role to `jobs/tracker.json` (round-trips cleanly with `json.dumps(..., ensure_ascii=False, indent=2) + "\n"`). Fields follow existing entries; `match` is a rationale in the user's working language covering: gates met, narrative mapping, explicit gaps, channel. Score honestly; `tier`: precision ≥8 / volume below. Status `materials_ready` once Phase 6 completes.

## Phase 3 — Tailored resume

1. Pick a track in `config/tracks.json` (fallback: `config/tracks.example.json`) that matches the JD's emphasis; add a new track only when no existing one covers it. A track = headline + summary + exactly 5 skill lines.
2. PDF template overflow guard: a too-long summary fails the build with an `overflow …pt` error → cut roughly one line (~30 chars).
3. `python3 tools/build_job_applications.py <role-id>` → resume.md, job.md, DOCX, PDF in `applications/<company>/<role>/`.

## Phase 4 — Cover letter

Write `applications/<company>/<role>/cover-letter.md`: first line `# <Company> - <Role>`, blank-line-separated paragraphs, trailing contact block (the renderer drops it). Structure that has worked:

1. Hook: quote the single JD line that matches the profile best, and claim it concretely.
2. Evidence paragraphs mapped to JD asks — draw from the standing arsenal in `profile/analysis/positioning.md` (numbers, shipped products, scope). Keep claims verifiable.
3. Honest gap declaration — name the JD requirement not met, plainly ("I'd rather flag it now than have it surface in a screen").
4. Practical notes: location/work-model fit and the profile's `screening.work_authorization_statement`.
5. Short close offering a concrete demo/walkthrough.

## Phase 5 — im-not-ai pass (AI-tell removal)

Apply language-agnostic AI-tell removal by hand (or a humanizer pipeline if one is installed for the letter's language), subtract-only, content anchors (numbers, names, claims) preserved verbatim:

- em-dash asides ≤ 2 per letter
- "not X, but Y" / "rather than" antithesis ≤ 1
- paragraph-final punchlines ≤ 2 — don't end every paragraph on a mic-drop
- vary or break triadic enumerations
- no colon scaffolding ("my concrete version is this:")
- no manufactured hooks; prefer plain word order

Target change rate 10–30%; if a rewrite would exceed ~50%, stop and reconsider.

## Phase 6 — Render + verify

1. `python3 tools/build_cover_letter.py <cover-letter.md> <application-dir>` → 1-page PDF/DOCX (sign-off is rendered in the header; the md's trailing contact line is dropped by design).
2. Verify both PDFs: page count == 1 (PyMuPDF), and render page 1 to an image for a visual check when a track or template changed.

## Phase 7 — Tracking + dashboard

1. `jobs/active/<role-id>.md` in the user's working language: status line, fit-score breakdown (full-credit items / gaps), selling points (for interviews), follow-up checklist, full JD text.
2. `jobs/jobs.md`: dated note at top + row in the summary table.
3. `python3 tools/build_job_dashboard.py`.

## Phase 8 — Outreach draft (cold mail / referral)

1. Find a relevant contact: a referral contact the user names, the ATS recruiter field, or the company team page. LinkedIn browsing only through the user's own logged-in browser and only when the user asks.
2. Write `applications/<company>/<role>/outreach.md`: short subject (`[Referral] <Name> — <Role> (<Location>)` for referrals), 3–6 sentence body naming the exact role, one line of fit, links/attachments list. Run the Phase 5 pass on it. Strip tracking params (`gh_src`, `utm_*`) from links.
3. Deliver the draft to the user. Creating an email draft is allowed only on explicit request; **sending is always the human's action**.

## Phase 9 — Commit + deliver

1. Stage only this cycle's files; commit on `job/<slug>`; ff-merge to main.
2. Send the resume + cover letter PDFs to the user (and outreach.md when written).
3. Final report: fit score with reasons, gaps declared, what's left for the human (share-settings check on any links, send the mail, submit the form).
