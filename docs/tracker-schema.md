# jobs/tracker.json schema

`jobs/tracker.json` is the canonical source of postings, scores, and statuses.
Everything else — the dashboard, the tailored-materials builder, the fixture
automation catalog — is derived from it. It round-trips cleanly with
`json.dumps(data, ensure_ascii=False, indent=2) + "\n"`.

## Top level

| Field | Type | Notes |
|-------|------|-------|
| `generated_at` | string | ISO-8601 timestamp of the last regeneration; shown in the dashboard footer and used as the automation catalog revision. |
| `candidate` | object | Your identity snapshot: `name`, `location`, `email`, `phone`, `linkedin`, `work_authorization`. Kept in sync with `config/user-profile.json`. |
| `roles` | array | One entry per posting; see below. |

## Role entry

Required by the builders and the dashboard:

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Canonical slug (`[a-z0-9-]`), unique across the file, e.g. `example-corp-full-stack-engineer`. |
| `company` | string | Display name. |
| `domain` | string | Company domain; part of the automation canonical identity. |
| `title` | string | Exact posting title. |
| `location` | string | e.g. `Vancouver, BC`. |
| `work_model` | string | Free text: onsite/hybrid/remote expectations. |
| `channel` | string | Where it was found (LinkedIn, Company site, referral…). |
| `source_url` / `apply_url` | string | Canonical posting URL / direct application URL (https). |
| `posted` | string | `YYYY-MM-DD`. |
| `salary` | string | Posted band, or `Not disclosed`. |
| `score` | number | Honest fit score 0–10. |
| `tier` | string | One of the tiers in `tools/user_config.py` (`precision` ≥ 8, `volume` below, `remote_bonus`, `relocation`). |
| `status` | string | One of the statuses in `tools/user_config.py`: `discovered`, `materials_ready`, `applied`, `interview`, `offer`, `rejected`, `dropped`. |
| `track` | string | Key into `config/tracks.json`; selects the resume positioning. |
| `keywords` | string[] | ATS keywords; embedded in the tailored PDF metadata. |
| `requirements` | string | Condensed JD requirements. |
| `match` | string | Rationale: gates met, narrative mapping, explicit gaps, channel. |
| `application_dir` | string | Repo-relative directory of the materials, e.g. `applications/example-corp/full-stack-engineer`. |

Required additionally by the fixture automation catalog (`tools/apply_service.py`
rejects the whole catalog when any role violates these):

| Field | Type | Notes |
|-------|------|-------|
| `posting_active` | bool | Must be an explicit boolean. |
| `remote` / `remote_country` | bool / string\|null | Remote flags used by location policy. |
| `material_manifest` | object | `{"path": "<file name inside application_dir>", "sha256": "<64-hex digest of that file>"}`. The path must be relative, must exist as a regular file (no symlinks), and must be named `resume.pdf` / `resume.docx` / `application.pdf` / `application.docx` or match `<Name> Resume.pdf|docx`. The digest must match the file exactly. |

The canonical vocabularies (statuses, tiers) are defined once in
`tools/user_config.py`; the dashboard menus are generated from them and
`tools/doctor.py` validates every role against them.
