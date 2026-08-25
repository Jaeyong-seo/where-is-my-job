CREATE TABLE batch_policies (
 id TEXT PRIMARY KEY, candidate_profile_id TEXT NOT NULL REFERENCES candidate_profiles(id), policy_version INTEGER NOT NULL CHECK(policy_version >= 1),
 state TEXT NOT NULL CHECK(state IN ('draft','active','revoked','expired','exhausted')), scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
 min_fit_score REAL NOT NULL CHECK(min_fit_score >= 5 AND min_fit_score <= 10), timezone TEXT NOT NULL CHECK(timezone='America/Vancouver'),
 daily_cap INTEGER NOT NULL CHECK(daily_cap BETWEEN 1 AND 20), provider_form_allowlist_json TEXT NOT NULL CHECK(json_valid(provider_form_allowlist_json)),
 assertion_snapshot_id TEXT NOT NULL REFERENCES candidate_assertions(id), material_policy_json TEXT NOT NULL CHECK(json_valid(material_policy_json)),
 checkpoint_classes_json TEXT NOT NULL CHECK(json_valid(checkpoint_classes_json)), valid_from TEXT NOT NULL, expires_at TEXT NOT NULL,
 global_kill_switch_id TEXT NOT NULL REFERENCES kill_switches(id), signature_hmac TEXT NOT NULL, key_version INTEGER NOT NULL,
 candidate_confirmation_event_id TEXT NOT NULL REFERENCES assertion_events(id), revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL,
 UNIQUE(candidate_profile_id, policy_version),
 CHECK(
   valid_from GLOB '????-??-??T??:??:??*'
   AND expires_at GLOB '????-??-??T??:??:??*'
   AND julianday(valid_from) IS NOT NULL
   AND julianday(expires_at) IS NOT NULL
   AND julianday(valid_from) < julianday(expires_at)
   AND julianday(expires_at) <= julianday(valid_from) + 1
 )
) STRICT;
CREATE INDEX ix_batch_policies_active ON batch_policies(candidate_profile_id, expires_at) WHERE state='active';
CREATE TABLE daily_quota_reservations (
 id TEXT PRIMARY KEY, policy_id TEXT NOT NULL REFERENCES batch_policies(id), local_date TEXT NOT NULL,
 application_id TEXT NOT NULL REFERENCES applications(id), dispatch_id TEXT NOT NULL REFERENCES dispatches(id),
 state TEXT NOT NULL CHECK(state IN ('reserved','consumed','released')), created_at TEXT NOT NULL, consumed_at TEXT,
 revision INTEGER NOT NULL CHECK(revision >= 1), UNIQUE(policy_id, application_id), UNIQUE(dispatch_id),
 CHECK((state='consumed') = (consumed_at IS NOT NULL))
) STRICT;
CREATE INDEX ix_quota_active ON daily_quota_reservations(policy_id, local_date, state) WHERE state IN ('reserved','consumed');
