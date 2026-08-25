# Local application automation

## Current capability

This release is a local, deterministic **fixture-only** runtime. It has zero authority to contact or submit to a real provider. `doctor`, `aside-doctor`, `queue`, `worker --once`, `recover`, `status`, and the fixture service validate only local behavior; none is a release gate or evidence of an employer submission.

The static `file://` `dashboard.html` remains browser-local scratch state and makes no API calls. Its status is not service authority.

## Install and fixture checks

Use [uv](https://docs.astral.sh/uv/) from the repository root. Use explicit local paths for an isolated fixture check:

```bash
uv sync --frozen
umask 077
runtime_parent=${TMPDIR:-/tmp}
if [ ! -d "$runtime_parent" ] || [ -L "$runtime_parent" ]; then
  printf '%s\n' 'refusing an unsafe runtime parent' >&2
  exit 1
fi
AUTOMATION_RUNTIME_DIR=$(mktemp -d "$runtime_parent/application-automation.XXXXXXXX") || exit 1
if [ -L "$AUTOMATION_RUNTIME_DIR" ] || [ "$(stat -f '%u' "$AUTOMATION_RUNTIME_DIR")" != "$(id -u)" ]; then
  rm -rf -- "$AUTOMATION_RUNTIME_DIR"
  printf '%s\n' 'refusing an unsafe runtime directory' >&2
  exit 1
fi
export AUTOMATION_RUNTIME_DIR
trap 'rm -rf -- "$AUTOMATION_RUNTIME_DIR"' EXIT
trap 'exit 1' HUP INT TERM
# The EXIT trap removes only this mktemp-created directory when this shell exits.
export AUTOMATION_DB="$AUTOMATION_RUNTIME_DIR/fixture.sqlite3"
export AUTOMATION_CATALOG="$PWD/jobs/tracker.json"
uv run python tools/apply_service.py --db "$AUTOMATION_DB" --catalog "$AUTOMATION_CATALOG" --fixture doctor
uv run python tools/apply_service.py --db "$AUTOMATION_DB" --catalog "$AUTOMATION_CATALOG" --fixture aside-doctor
```
The trap cleans up only the validated, `mktemp`-created directory at shell exit. Do not reuse a prior runtime directory or replace this cleanup with recursive deletion of a path you did not create and validate.

`doctor` reports local database/catalog setup. Fixture `aside-doctor` reports the deterministic fixture adapter. Neither checks a real provider, dispatches an application, or grants authority.

The CLI exposes `serve`, `doctor`, `aside-doctor`, `queue ROLE_ID MODE IDEMPOTENCY_KEY`, `worker --once`, `recover`, and `status ROLE_ID`. Real-provider dispatch, status import, projection publication, and cutover commands are **unavailable** in this release; do not infer syntax for them.

## Fixture service bootstrap

`serve` requires either an existing mode-`0600` bootstrap-token file or a new file created with mode `0600`. It refuses to start without one. Never put a token in argv, a URL, shell history, source, logs, screenshots, or repository files.

```bash
umask 077
export BOOTSTRAP_TOKEN_FILE="$AUTOMATION_RUNTIME_DIR/bootstrap-token"
# Create the token file through an approved local secret-management path.
uv run python tools/apply_service.py --db "$AUTOMATION_DB" --catalog "$AUTOMATION_CATALOG" --fixture serve --bootstrap-token-file "$BOOTSTRAP_TOKEN_FILE" --port 8765
```

To have the service create a new token file instead, use `--generate-bootstrap-token-file "$BOOTSTRAP_TOKEN_FILE"`; it refuses an existing path and creates the file with mode `0600`. Open `http://127.0.0.1:8765/app/v1/`, paste the one-use token into the local bootstrap form, submit it, then remove the consumed token file through the same local secret-management path. The token is sent in a same-origin POST body, consumed once, and never placed in the URL. Keep the runtime directory outside the repository.

The connected dashboard's source-data, master-resume, and tailored-material links are authenticated, catalog-bound same-origin routes with `no-store` and `nosniff` responses. Material delivery is restricted to the catalog-bound application directory, approved filenames and formats, the expected SHA-256, a 10 MiB ceiling, and paths with no symbolic-link components. The disconnected `file://` dashboard keeps its original local relative links and never gains API authority.

## Fixture queue, recovery, and status

Fixture queue and worker commands are intentionally explicit:

```bash
export ROLE_ID='an-eligible-catalog-role'
uv run python tools/apply_service.py --db "$AUTOMATION_DB" --catalog "$AUTOMATION_CATALOG" --fixture queue "$ROLE_ID" dry_run fixture-dry-run-001
uv run python tools/apply_service.py --db "$AUTOMATION_DB" --catalog "$AUTOMATION_CATALOG" --fixture worker --once
uv run python tools/apply_service.py --db "$AUTOMATION_DB" --catalog "$AUTOMATION_CATALOG" --fixture status "$ROLE_ID"
```

When `serve` runs with `--fixture`, it starts one serial background worker and the connected dashboard polls durable status every three seconds. A queue click therefore proceeds automatically to fixture event `applied`, `awaiting_user`, or `manualfollowup`; no second CLI command is required. `worker --once` remains available for headless fixture verification. Use `batch` only for deterministic fixtures.

Service creation runs stale-command recovery. `recover` also invokes it explicitly. For stale running work before a dispatch has started, recovery opens a manual checkpoint, changes the command to `paused`, and changes the run (when present) and application to `awaiting_user`. After a started dispatch, recovery preserves a confirmed dispatch that already has an `applied` event: it completes the run and command and sets the application to `submitted`. Any other started dispatch is ambiguous: recovery opens a manual checkpoint, keeps a confirmed dispatch confirmed when present, changes the run and application to `manualfollowup`, changes a nonconfirmed dispatch to `manualfollowup`, and completes the command. Neither branch requeues, auto-resumes, or retries work.

## Canonical status matrix

This is the canonical status vocabulary for the current release. Values are layer-specific; they are not interchangeable and do not prove an employer submission.

| Layer | Stored or returned values | Meaning |
| --- | --- | --- |
| Command | `accepted`, `running`, `paused`, `completed`, `rejected`, `cancelled`, `failed` | Command lifecycle only. `completed` is not a submission. |
| Application | `draft`, `queued`, `filling`, `awaiting_user`, `dispatching`, `submitted`, `manualfollowup`, `abandoned` | Application-record lifecycle. Recovery uses `awaiting_user` before dispatch and `manualfollowup` after a started dispatch. |
| Status event / `status ROLE_ID` | `queued`, `awaiting_user`, `applied`, `rejected`, `cancelled`, `closed`, `manualfollowup`, `direct_edit`; `null` when no event exists | `status ROLE_ID` returns the latest event kind, or `null`; it does not return a command state. `applied` is fixture evidence only in this release. |
| Connected dashboard | `queued`, `running`, `paused`, `awaiting_user`, `applied`, `manual_follow_up` | Presentation boundary only. It renders persisted event spelling `manualfollowup` as `manual_follow_up`. |

A manual checkpoint requires human review. Fixture `applied`, command `completed`, a dashboard display, and an Aside probe are never evidence that an employer received an application. [Status authority and cutover](status-cutover.md) links to this matrix rather than defining another vocabulary.

## Future real-provider release checklist

This checklist is the canonical detailed release boundary. A future release may receive real-provider authority only after a release-approved definition and verification of its required relational contracts, a candidate-confirmed assertion snapshot, exact form/provider/script/version/executable-SHA pins, a dedicated Aside context, sandbox gates, and an explicitly approved pilot capped at one real dispatch. Until then, all real-provider operations remain denied.

CAPTCHA, MFA, login/session challenges, account creation, rate limits, security challenges, unknown questions, redirects, and form drift are human checkpoints. The system must not solve, bypass, spoof, guess, or blindly retry them.

## Local data

Without `--db` (or `APPLICATION_AUTOMATION_DB`), the runtime database is `~/.local/share/application_automation/application-automation.sqlite3` (or `$XDG_DATA_HOME/application_automation/application-automation.sqlite3`). Keep local backups access-controlled and encrypted. Approved candidate resumes and materials may be in this repository; credentials, cookies, session secrets, bootstrap tokens, raw assertion values, account identifiers, and provider payloads must not be.