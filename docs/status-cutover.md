# Status authority and cutover

## Current authority

The current release is fixture-only. SQLite can hold local workflow records and status history, but it is not evidence of an employer submission and has zero real-provider authority. The legacy static `dashboard.html` is browser-local scratch state; it makes no API calls and cannot override local durable records.

The canonical layer-by-layer command, application, status-event/CLI, and dashboard matrix is in [Local application automation](application-automation.md#canonical-status-matrix). In particular, `status ROLE_ID` returns the latest status-event kind or `null`, not a command state, and dashboard `manual_follow_up` is only the presentation spelling of persisted `manualfollowup`.

Fixture command completion, fixture `applied`, a static export, and a dashboard display are not employer-submission evidence.

## Unavailable operations and projection boundary

No supported CLI or end-to-end wiring performs status import/reconciliation, projection publication, or cutover. Do not infer command syntax from this document or treat a static export as importable service authority.

`ProjectionStore` is an implemented lower-level library: it can stage hash-verified immutable JSON/HTML releases and manifests and update an atomic current pointer. It is not integrated or authorized as a supported status-import/cutover surface in this release. Its artifacts are noncanonical and must not be represented as released, current, or employer-submission authority.

For a human-only review, preserve the original static export and its hash, compare it with the local SQLite history, record conflicts explicitly, and do not bulk overwrite either source.

## Future cutover boundary

The detailed future-release checklist is canonical in [Local application automation](application-automation.md#future-real-provider-release-checklist). Until it is satisfied, real-provider dispatch, status import, projection publication, and cutover remain unavailable.

A possible or ambiguous dispatch is recorded as `manualfollowup` and requires human resolution. Never infer a dispatch from a dashboard projection or command completion, and never automatically retry a possible submission.