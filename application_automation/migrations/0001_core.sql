CREATE TABLE candidate_profiles (
 id TEXT PRIMARY KEY, display_name TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','revoked')),
 created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE TABLE assertion_events (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES candidate_profiles(id), assertion_id TEXT,
 event_kind TEXT NOT NULL CHECK(event_kind IN ('staged','confirmed','revoked','alias_confirmed','alias_revoked','request_replaced')),
 payload_hmac TEXT NOT NULL, created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE TABLE candidate_assertions (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES candidate_profiles(id), assertion_event_id TEXT NOT NULL REFERENCES assertion_events(id),
 semantic_key TEXT NOT NULL, value_hmac TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('staged','active','revoked')),
 confirmed_at TEXT, revoked_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL,
 UNIQUE(profile_id, semantic_key, revision), CHECK(
   (state='staged' AND confirmed_at IS NULL AND revoked_at IS NULL)
   OR (state='active' AND confirmed_at IS NOT NULL AND revoked_at IS NULL)
   OR (state='revoked' AND confirmed_at IS NOT NULL AND revoked_at IS NOT NULL)
 )
) STRICT;
CREATE TABLE question_aliases (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES candidate_profiles(id), assertion_id TEXT NOT NULL REFERENCES candidate_assertions(id),
 confirmation_event_id TEXT NOT NULL REFERENCES assertion_events(id), provider TEXT NOT NULL, normalized_label TEXT NOT NULL, semantic_scope TEXT NOT NULL,
 form_fingerprint TEXT, revoked_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX ux_question_alias_active_candidate ON question_aliases(profile_id, provider, normalized_label, semantic_scope) WHERE revoked_at IS NULL;
CREATE INDEX ix_question_alias_assertion ON question_aliases(assertion_id) WHERE revoked_at IS NULL;

CREATE TABLE roles (
 id TEXT PRIMARY KEY, canonical_key TEXT NOT NULL UNIQUE, company_name TEXT NOT NULL, title TEXT NOT NULL,
 apply_url TEXT, application_dir TEXT, score REAL, status TEXT NOT NULL CHECK(status IN ('discovered','reviewing','materials_ready','applied','closed','rejected')),
 posting_snapshot_hmac TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE role_aliases (
 id TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE, source TEXT NOT NULL, external_id TEXT,
 canonical_url TEXT, created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 UNIQUE(source, external_id), UNIQUE(source, canonical_url)
) STRICT;
CREATE TABLE applications (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES candidate_profiles(id), role_id TEXT NOT NULL REFERENCES roles(id),
 canonical_identity TEXT NOT NULL UNIQUE, state TEXT NOT NULL CHECK(state IN ('draft','queued','filling','awaiting_user','dispatching','submitted','manual_followup','abandoned')),
 revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(profile_id, role_id)
) STRICT;
CREATE TABLE commands (
 id TEXT PRIMARY KEY, application_id TEXT REFERENCES applications(id), idempotency_key TEXT NOT NULL UNIQUE,
 command_kind TEXT NOT NULL CHECK(command_kind IN ('dry_run','fill','dispatch','resume','cancel')), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 state TEXT NOT NULL CHECK(state IN ('accepted','running','paused','completed','rejected','cancelled','failed')), created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE TABLE runs (
 id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id), command_id TEXT REFERENCES commands(id),
 state TEXT NOT NULL CHECK(state IN ('queued','inspecting','filling','awaiting_user','dispatching','completed','failed','manual_followup')),
 aside_version TEXT, script_sha256 TEXT, preflight_hmac TEXT, started_at TEXT, finished_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL
) STRICT;
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id), run_id TEXT NOT NULL REFERENCES runs(id),
 sequence INTEGER NOT NULL CHECK(sequence >= 1), state TEXT NOT NULL CHECK(state IN ('planned','active','paused','completed','failed')), created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1), UNIQUE(application_id, sequence)
) STRICT;
CREATE TABLE actions (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), attempt_id TEXT REFERENCES attempts(id),
 action_kind TEXT NOT NULL CHECK(action_kind IN ('inspect','fill','submit','observe','checkpoint')), side_effect_class TEXT NOT NULL CHECK(side_effect_class IN ('none','fill','submit')),
 state TEXT NOT NULL CHECK(state IN ('planned','started','completed','failed')), payload_hmac TEXT, created_at TEXT NOT NULL, completed_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE TABLE dispatches (
 id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id), run_id TEXT NOT NULL REFERENCES runs(id),
 transport TEXT NOT NULL CHECK(transport IN ('direct','aside','manual')), state TEXT NOT NULL CHECK(state IN ('intent','dispatching','confirmed','rejected','unknown','manual_followup','abandoned')),
 batch_policy_id TEXT REFERENCES batch_policies(id), authority_hmac TEXT, form_fingerprint TEXT NOT NULL, started_at TEXT, finished_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX ux_dispatch_started_application ON dispatches(application_id) WHERE started_at IS NOT NULL;

CREATE TABLE sessions (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES candidate_profiles(id), service_instance_id TEXT NOT NULL, dashboard_instance_id TEXT,
 state TEXT NOT NULL CHECK(state IN ('active','expired','revoked')), created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE TABLE browser_sessions (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES candidate_profiles(id), aside_context_hmac TEXT NOT NULL, account_hmac TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','expired','revoked','challenged')), aside_version TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE TABLE checkpoints (
 id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id), run_id TEXT REFERENCES runs(id),
 kind TEXT NOT NULL CHECK(kind IN ('captcha','mfa','login','security','security_challenge','rate_limit','provider_challenge','account_creation','new_question','unknown_question','sensitive_question','legal_question','required_demographics','form_drift','posting_drift','address','street_address','salary','salary_unverified','salary_exact_number','attestation','unexpected_redirect','daily_cap','policy_expired','policy_revoked','kill_switch','breaker_open','manual_completion')),
 state TEXT NOT NULL CHECK(state IN ('open','resolved','expired','cancelled')), generation INTEGER NOT NULL DEFAULT 1 CHECK(generation >= 1),
 created_at TEXT NOT NULL, resolved_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE TABLE field_resolutions (
 id TEXT PRIMARY KEY, checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id), generation INTEGER NOT NULL CHECK(generation >= 1), field_key TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('unresolved','resolved','expired_request','cancelled')), assertion_id TEXT REFERENCES candidate_assertions(id),
 created_at TEXT NOT NULL, resolved_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1), UNIQUE(checkpoint_id, generation, field_key)
) STRICT;
CREATE TABLE request_value_secrets (
 id TEXT PRIMARY KEY, field_resolution_id TEXT NOT NULL REFERENCES field_resolutions(id), ciphertext BLOB, nonce BLOB, value_hmac TEXT,
 key_version INTEGER NOT NULL CHECK(key_version >= 1), state TEXT NOT NULL CHECK(state IN ('active','expired','destroyed','tombstoned')), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL, destroyed_at TEXT,
 tombstone_hmac TEXT, revision INTEGER NOT NULL CHECK(revision >= 1),
 CHECK(julianday(created_at) IS NOT NULL AND julianday(expires_at) IS NOT NULL AND julianday(expires_at) > julianday(created_at) AND julianday(expires_at) <= julianday(created_at) + 1),
 CHECK(
   (state='active' AND ciphertext IS NOT NULL AND nonce IS NOT NULL AND value_hmac IS NOT NULL AND destroyed_at IS NULL AND tombstone_hmac IS NULL)
   OR
   (state IN ('destroyed','tombstoned','expired') AND ciphertext IS NULL AND nonce IS NULL AND value_hmac IS NULL AND destroyed_at IS NOT NULL AND tombstone_hmac IS NOT NULL)
 )
) STRICT;
CREATE UNIQUE INDEX ux_request_value_secret_active ON request_value_secrets(field_resolution_id) WHERE state='active';

CREATE TABLE capabilities (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, tenant TEXT NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('inspect','fill','submit','observe')),
 transport TEXT NOT NULL CHECK(transport IN ('direct','aside')), form_fingerprint TEXT, state TEXT NOT NULL CHECK(state IN ('draft','active','revoked','expired')),
 expires_at TEXT, capability_json TEXT NOT NULL CHECK(json_valid(capability_json)), revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL,
 UNIQUE(provider,tenant,operation,transport,revision)
) STRICT;
CREATE TABLE breakers (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('closed','open','half_open')), reason TEXT NOT NULL,
 opened_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1), UNIQUE(provider)
) STRICT;
CREATE TABLE kill_switches (
 id TEXT PRIMARY KEY, scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global','provider','application')), scope_key TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','closed')), reason TEXT NOT NULL, created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1), UNIQUE(scope_kind,scope_key)
) STRICT;

CREATE TABLE status_events (
 id TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES roles(id), application_id TEXT REFERENCES applications(id), event_kind TEXT NOT NULL CHECK(event_kind IN ('queued','awaiting_user','applied','rejected','cancelled','closed','manual_followup','direct_edit')),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE TABLE projection_releases (
 id TEXT PRIMARY KEY, projection_name TEXT NOT NULL, payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64), state TEXT NOT NULL CHECK(state IN ('current','superseded','tombstoned')),
 created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE UNIQUE INDEX ux_projection_current ON projection_releases(projection_name) WHERE state='current';
CREATE TABLE projection_outbox (
 id TEXT PRIMARY KEY, aggregate_kind TEXT NOT NULL, aggregate_id TEXT NOT NULL, event_kind TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 state TEXT NOT NULL CHECK(state IN ('pending','processing','published','failed')), created_at TEXT NOT NULL, published_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE INDEX ix_projection_outbox_pending ON projection_outbox(state, created_at) WHERE state='pending';
CREATE TABLE evidence (
 id TEXT PRIMARY KEY, application_id TEXT REFERENCES applications(id), dispatch_id TEXT REFERENCES dispatches(id), kind TEXT NOT NULL,
 metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)), content_sha256 TEXT CHECK(content_sha256 IS NULL OR length(content_sha256)=64), created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;

CREATE TABLE reconciliation_conflicts (
 id TEXT PRIMARY KEY, role_id TEXT REFERENCES roles(id), kind TEXT NOT NULL CHECK(kind IN ('legacy_vs_ledger','direct_json_status','projection_cas','identity_collision','catalog_edit','static_mirror_edit','projection_metadata_edit')),
 state TEXT NOT NULL CHECK(state IN ('open','resolved','ignored')), detail_json TEXT NOT NULL CHECK(json_valid(detail_json)), created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
CREATE INDEX ix_conflicts_open ON reconciliation_conflicts(state, created_at) WHERE state='open';
CREATE TABLE gate_records (
 id TEXT PRIMARY KEY, gate_kind TEXT NOT NULL CHECK(gate_kind IN ('G0','G1','G2','G3','G4')), state TEXT NOT NULL CHECK(state IN ('denied','approved','expired','revoked')),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), expires_at TEXT, created_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
