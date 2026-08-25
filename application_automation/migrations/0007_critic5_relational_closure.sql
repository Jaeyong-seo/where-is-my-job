CREATE TABLE one_use_challenges (
 id TEXT PRIMARY KEY,
 purpose TEXT NOT NULL CHECK(purpose IN ('assertion_confirmation','alias_review','alias_reassign','manual_completion','attended_dispatch','batch_policy_review')),
 secret_hmac TEXT NOT NULL UNIQUE, key_version INTEGER NOT NULL, candidate_profile_id TEXT REFERENCES candidate_profiles(id) ON DELETE CASCADE,
 assertion_id TEXT REFERENCES candidate_assertions(id) ON DELETE CASCADE, alias_id TEXT REFERENCES question_aliases(id) ON DELETE CASCADE,
 checkpoint_id TEXT REFERENCES checkpoints(id) ON DELETE CASCADE, field_resolution_id TEXT REFERENCES field_resolutions(id) ON DELETE CASCADE,
 run_id TEXT REFERENCES runs(id) ON DELETE CASCADE, dispatch_id TEXT REFERENCES dispatches(id) ON DELETE CASCADE,
 batch_policy_id TEXT REFERENCES batch_policies(id) ON DELETE CASCADE, web_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 browser_session_id TEXT REFERENCES browser_sessions(id) ON DELETE CASCADE, service_instance_id TEXT NOT NULL, dashboard_instance_id TEXT,
 form_signature TEXT, authority_hmac TEXT, binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
 state TEXT NOT NULL CHECK(state IN ('active','consumed','expired','revoked')), created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT,
 revision INTEGER NOT NULL CHECK(revision >= 1), CHECK((state='consumed')=(consumed_at IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX ux_active_manual_challenge ON one_use_challenges(checkpoint_id,field_resolution_id,purpose) WHERE state='active';
CREATE UNIQUE INDEX ux_active_dispatch_challenge ON one_use_challenges(dispatch_id,purpose) WHERE state='active';
CREATE UNIQUE INDEX ux_active_policy_challenge ON one_use_challenges(candidate_profile_id,purpose) WHERE purpose='batch_policy_review' AND state='active';

CREATE TABLE presence_leases (
 id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL REFERENCES one_use_challenges(id), web_session_id TEXT NOT NULL REFERENCES sessions(id),
 browser_session_id TEXT REFERENCES browser_sessions(id), run_id TEXT NOT NULL REFERENCES runs(id), dispatch_id TEXT NOT NULL REFERENCES dispatches(id),
 service_instance_id TEXT NOT NULL, dashboard_instance_id TEXT, binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
 binding_hmac TEXT NOT NULL, authority_hmac TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','consumed','expired','revoked')),
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1),
 CHECK((state='consumed')=(consumed_at IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX ux_active_presence_lease_dispatch ON presence_leases(dispatch_id) WHERE state='active';

CREATE TABLE direct_edit_inputs (
 id TEXT PRIMARY KEY, source_kind TEXT NOT NULL CHECK(source_kind IN ('jobs_json','dashboard_html')),
 capture_relative_path TEXT NOT NULL UNIQUE, raw_sha256 TEXT NOT NULL CHECK(length(raw_sha256)=64), base_projection_revision INTEGER,
 base_mirror_sha256 TEXT, parsed_json TEXT CHECK(parsed_json IS NULL OR json_valid(parsed_json)), detected_at TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('captured','resolved','ignored','retained_open')), expires_at TEXT,
 revision INTEGER NOT NULL CHECK(revision >= 1)
) STRICT;
ALTER TABLE reconciliation_conflicts ADD COLUMN direct_edit_input_id TEXT REFERENCES direct_edit_inputs(id);
ALTER TABLE reconciliation_conflicts ADD COLUMN resolution_event_id TEXT REFERENCES status_events(id);
ALTER TABLE reconciliation_conflicts ADD COLUMN resolution_payload_json TEXT CHECK(resolution_payload_json IS NULL OR json_valid(resolution_payload_json));
CREATE INDEX ix_conflicts_direct_input ON reconciliation_conflicts(direct_edit_input_id) WHERE direct_edit_input_id IS NOT NULL;

CREATE TRIGGER conflict_direct_edit_shape_insert
BEFORE INSERT ON reconciliation_conflicts
FOR EACH ROW WHEN
 (NEW.kind IN ('direct_json_status','catalog_edit','static_mirror_edit') AND NEW.direct_edit_input_id IS NULL)
 OR (NEW.kind NOT IN ('direct_json_status','catalog_edit','static_mirror_edit') AND NEW.direct_edit_input_id IS NOT NULL)
 OR (NEW.kind IN ('catalog_edit','static_mirror_edit') AND NEW.role_id IS NOT NULL)
 OR (NEW.kind NOT IN ('catalog_edit','static_mirror_edit') AND NEW.role_id IS NULL)
BEGIN SELECT RAISE(ABORT, 'invalid direct-edit conflict cardinality'); END;
CREATE TRIGGER conflict_direct_edit_shape_update
BEFORE UPDATE OF kind, role_id, direct_edit_input_id ON reconciliation_conflicts
FOR EACH ROW WHEN
 (NEW.kind IN ('direct_json_status','catalog_edit','static_mirror_edit') AND NEW.direct_edit_input_id IS NULL)
 OR (NEW.kind NOT IN ('direct_json_status','catalog_edit','static_mirror_edit') AND NEW.direct_edit_input_id IS NOT NULL)
 OR (NEW.kind IN ('catalog_edit','static_mirror_edit') AND NEW.role_id IS NOT NULL)
 OR (NEW.kind NOT IN ('catalog_edit','static_mirror_edit') AND NEW.role_id IS NULL)
BEGIN SELECT RAISE(ABORT, 'invalid direct-edit conflict cardinality'); END;

CREATE UNIQUE INDEX ux_candidate_assertion_active_semantic
ON candidate_assertions(profile_id, semantic_key) WHERE state='active';

CREATE UNIQUE INDEX ux_command_active_application
ON commands(application_id) WHERE application_id IS NOT NULL
AND state IN ('accepted','running','paused');

CREATE UNIQUE INDEX ux_batch_policy_active_candidate
ON batch_policies(candidate_profile_id) WHERE state='active';

CREATE TRIGGER candidate_assertion_provenance_insert
BEFORE INSERT ON candidate_assertions
FOR EACH ROW WHEN
  (NEW.state='staged' AND (NEW.confirmed_at IS NOT NULL OR NEW.revoked_at IS NOT NULL))
  OR (NEW.state='active' AND (NEW.confirmed_at IS NULL OR NEW.revoked_at IS NOT NULL))
  OR (NEW.state='revoked' AND (NEW.confirmed_at IS NULL OR NEW.revoked_at IS NULL))
  OR NOT EXISTS (
    SELECT 1 FROM assertion_events event
    WHERE event.id=NEW.assertion_event_id
      AND event.profile_id=NEW.profile_id
      AND event.event_kind=CASE NEW.state
        WHEN 'staged' THEN 'staged'
        ELSE 'confirmed'
      END
  )
BEGIN SELECT RAISE(ABORT, 'invalid assertion confirmation provenance'); END;

CREATE TRIGGER candidate_assertion_provenance_update
BEFORE UPDATE OF profile_id, assertion_event_id, state, confirmed_at, revoked_at ON candidate_assertions
FOR EACH ROW WHEN
  (NEW.state='staged' AND (NEW.confirmed_at IS NOT NULL OR NEW.revoked_at IS NOT NULL))
  OR (NEW.state='active' AND (NEW.confirmed_at IS NULL OR NEW.revoked_at IS NOT NULL))
  OR (NEW.state='revoked' AND (NEW.confirmed_at IS NULL OR NEW.revoked_at IS NULL))
  OR NOT EXISTS (
    SELECT 1 FROM assertion_events event
    WHERE event.id=NEW.assertion_event_id
      AND event.profile_id=NEW.profile_id
      AND event.event_kind=CASE NEW.state
        WHEN 'staged' THEN 'staged'
        ELSE 'confirmed'
      END
  )
BEGIN SELECT RAISE(ABORT, 'invalid assertion confirmation provenance'); END;

CREATE TRIGGER action_side_effect_insert
BEFORE INSERT ON actions
FOR EACH ROW WHEN
  (NEW.action_kind IN ('inspect','observe','checkpoint') AND NEW.side_effect_class <> 'none')
  OR (NEW.action_kind='fill' AND NEW.side_effect_class <> 'fill')
  OR (NEW.action_kind='submit' AND NEW.side_effect_class <> 'submit')
BEGIN SELECT RAISE(ABORT, 'action kind and side effect class disagree'); END;

CREATE TRIGGER action_side_effect_update
BEFORE UPDATE OF action_kind, side_effect_class ON actions
FOR EACH ROW WHEN
  (NEW.action_kind IN ('inspect','observe','checkpoint') AND NEW.side_effect_class <> 'none')
  OR (NEW.action_kind='fill' AND NEW.side_effect_class <> 'fill')
  OR (NEW.action_kind='submit' AND NEW.side_effect_class <> 'submit')
BEGIN SELECT RAISE(ABORT, 'action kind and side effect class disagree'); END;

CREATE TRIGGER dispatch_batch_policy_insert
BEFORE INSERT ON dispatches
FOR EACH ROW WHEN
  (NEW.transport='aside' AND NEW.batch_policy_id IS NULL)
  OR (NEW.transport <> 'aside' AND NEW.batch_policy_id IS NOT NULL)
  OR (NEW.batch_policy_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM batch_policies policy
    JOIN applications application ON application.id=NEW.application_id
    WHERE policy.id=NEW.batch_policy_id
      AND policy.candidate_profile_id=application.profile_id
      AND policy.state='active'
  ))
BEGIN SELECT RAISE(ABORT, 'dispatch must use its application active batch policy'); END;

CREATE TRIGGER dispatch_batch_policy_update
BEFORE UPDATE OF application_id, transport, batch_policy_id ON dispatches
FOR EACH ROW WHEN
  (NEW.transport='aside' AND NEW.batch_policy_id IS NULL)
  OR (NEW.transport <> 'aside' AND NEW.batch_policy_id IS NOT NULL)
  OR (NEW.batch_policy_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM batch_policies policy
    JOIN applications application ON application.id=NEW.application_id
    WHERE policy.id=NEW.batch_policy_id
      AND policy.candidate_profile_id=application.profile_id
      AND policy.state='active'
  ))
BEGIN SELECT RAISE(ABORT, 'dispatch must use its application active batch policy'); END;

CREATE TRIGGER quota_dispatch_policy_insert
BEFORE INSERT ON daily_quota_reservations
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM dispatches dispatch
  WHERE dispatch.id=NEW.dispatch_id
    AND dispatch.application_id=NEW.application_id
    AND dispatch.batch_policy_id=NEW.policy_id
    AND dispatch.transport='aside'
)
BEGIN SELECT RAISE(ABORT, 'quota reservation must match an aside dispatch policy'); END;

CREATE TRIGGER quota_dispatch_policy_update
BEFORE UPDATE OF policy_id, application_id, dispatch_id ON daily_quota_reservations
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM dispatches dispatch
  WHERE dispatch.id=NEW.dispatch_id
    AND dispatch.application_id=NEW.application_id
    AND dispatch.batch_policy_id=NEW.policy_id
    AND dispatch.transport='aside'
)
BEGIN SELECT RAISE(ABORT, 'quota reservation must match an aside dispatch policy'); END;

CREATE TRIGGER challenge_purpose_insert
BEFORE INSERT ON one_use_challenges
FOR EACH ROW WHEN
  (NEW.purpose='assertion_confirmation' AND (
    NEW.candidate_profile_id IS NULL OR NEW.assertion_id IS NULL
    OR NEW.alias_id IS NOT NULL OR NEW.checkpoint_id IS NOT NULL
    OR NEW.field_resolution_id IS NOT NULL OR NEW.run_id IS NOT NULL
    OR NEW.dispatch_id IS NOT NULL OR NEW.batch_policy_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM candidate_assertions assertion
                   WHERE assertion.id=NEW.assertion_id AND assertion.profile_id=NEW.candidate_profile_id)
  ))
  OR (NEW.purpose IN ('alias_review','alias_reassign') AND (
    NEW.candidate_profile_id IS NULL OR NEW.alias_id IS NULL
    OR NEW.assertion_id IS NOT NULL OR NEW.checkpoint_id IS NOT NULL
    OR NEW.field_resolution_id IS NOT NULL OR NEW.run_id IS NOT NULL
    OR NEW.dispatch_id IS NOT NULL OR NEW.batch_policy_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM question_aliases alias
                   WHERE alias.id=NEW.alias_id AND alias.profile_id=NEW.candidate_profile_id)
  ))
  OR (NEW.purpose='manual_completion' AND (
    NEW.checkpoint_id IS NULL OR NEW.field_resolution_id IS NULL
    OR NEW.candidate_profile_id IS NOT NULL OR NEW.assertion_id IS NOT NULL OR NEW.alias_id IS NOT NULL
    OR NEW.run_id IS NOT NULL OR NEW.dispatch_id IS NOT NULL OR NEW.batch_policy_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM field_resolutions resolution
                   WHERE resolution.id=NEW.field_resolution_id AND resolution.checkpoint_id=NEW.checkpoint_id)
  ))
  OR (NEW.purpose='attended_dispatch' AND (
    NEW.run_id IS NULL OR NEW.dispatch_id IS NULL
    OR NEW.candidate_profile_id IS NOT NULL OR NEW.assertion_id IS NOT NULL
    OR NEW.alias_id IS NOT NULL OR NEW.checkpoint_id IS NOT NULL
    OR NEW.field_resolution_id IS NOT NULL OR NEW.batch_policy_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM dispatches dispatch
                   WHERE dispatch.id=NEW.dispatch_id AND dispatch.run_id=NEW.run_id)
  ))
  OR (NEW.purpose='batch_policy_review' AND (
    NEW.candidate_profile_id IS NULL OR NEW.batch_policy_id IS NULL
    OR NEW.assertion_id IS NOT NULL OR NEW.alias_id IS NOT NULL
    OR NEW.checkpoint_id IS NOT NULL OR NEW.field_resolution_id IS NOT NULL
    OR NEW.run_id IS NOT NULL OR NEW.dispatch_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM batch_policies policy
                   WHERE policy.id=NEW.batch_policy_id AND policy.candidate_profile_id=NEW.candidate_profile_id)
  ))
BEGIN SELECT RAISE(ABORT, 'challenge purpose has invalid identity'); END;

CREATE TRIGGER challenge_purpose_update
BEFORE UPDATE OF purpose, candidate_profile_id, assertion_id, alias_id, checkpoint_id, field_resolution_id, run_id, dispatch_id, batch_policy_id ON one_use_challenges
FOR EACH ROW WHEN
  (NEW.purpose='assertion_confirmation' AND (
    NEW.candidate_profile_id IS NULL OR NEW.assertion_id IS NULL
    OR NEW.alias_id IS NOT NULL OR NEW.checkpoint_id IS NOT NULL
    OR NEW.field_resolution_id IS NOT NULL OR NEW.run_id IS NOT NULL
    OR NEW.dispatch_id IS NOT NULL OR NEW.batch_policy_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM candidate_assertions assertion
                   WHERE assertion.id=NEW.assertion_id AND assertion.profile_id=NEW.candidate_profile_id)
  ))
  OR (NEW.purpose IN ('alias_review','alias_reassign') AND (
    NEW.candidate_profile_id IS NULL OR NEW.alias_id IS NULL
    OR NEW.assertion_id IS NOT NULL OR NEW.checkpoint_id IS NOT NULL
    OR NEW.field_resolution_id IS NOT NULL OR NEW.run_id IS NOT NULL
    OR NEW.dispatch_id IS NOT NULL OR NEW.batch_policy_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM question_aliases alias
                   WHERE alias.id=NEW.alias_id AND alias.profile_id=NEW.candidate_profile_id)
  ))
  OR (NEW.purpose='manual_completion' AND (
    NEW.checkpoint_id IS NULL OR NEW.field_resolution_id IS NULL
    OR NEW.candidate_profile_id IS NOT NULL OR NEW.assertion_id IS NOT NULL OR NEW.alias_id IS NOT NULL
    OR NEW.run_id IS NOT NULL OR NEW.dispatch_id IS NOT NULL OR NEW.batch_policy_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM field_resolutions resolution
                   WHERE resolution.id=NEW.field_resolution_id AND resolution.checkpoint_id=NEW.checkpoint_id)
  ))
  OR (NEW.purpose='attended_dispatch' AND (
    NEW.run_id IS NULL OR NEW.dispatch_id IS NULL
    OR NEW.candidate_profile_id IS NOT NULL OR NEW.assertion_id IS NOT NULL
    OR NEW.alias_id IS NOT NULL OR NEW.checkpoint_id IS NOT NULL
    OR NEW.field_resolution_id IS NOT NULL OR NEW.batch_policy_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM dispatches dispatch
                   WHERE dispatch.id=NEW.dispatch_id AND dispatch.run_id=NEW.run_id)
  ))
  OR (NEW.purpose='batch_policy_review' AND (
    NEW.candidate_profile_id IS NULL OR NEW.batch_policy_id IS NULL
    OR NEW.assertion_id IS NOT NULL OR NEW.alias_id IS NOT NULL
    OR NEW.checkpoint_id IS NOT NULL OR NEW.field_resolution_id IS NOT NULL
    OR NEW.run_id IS NOT NULL OR NEW.dispatch_id IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM batch_policies policy
                   WHERE policy.id=NEW.batch_policy_id AND policy.candidate_profile_id=NEW.candidate_profile_id)
  ))
BEGIN SELECT RAISE(ABORT, 'challenge purpose has invalid identity'); END;

CREATE TRIGGER presence_lease_identity_insert
BEFORE INSERT ON presence_leases
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM one_use_challenges challenge
  WHERE challenge.id=NEW.challenge_id
    AND challenge.purpose='attended_dispatch'
    AND challenge.web_session_id=NEW.web_session_id
    AND challenge.run_id=NEW.run_id
    AND challenge.dispatch_id=NEW.dispatch_id
    AND challenge.service_instance_id=NEW.service_instance_id
    AND challenge.binding_json=NEW.binding_json
    AND challenge.authority_hmac=NEW.authority_hmac
    AND challenge.dashboard_instance_id IS NEW.dashboard_instance_id
    AND challenge.browser_session_id IS NEW.browser_session_id
)
BEGIN SELECT RAISE(ABORT, 'presence lease does not match challenge identity'); END;

CREATE TRIGGER presence_lease_identity_update
BEFORE UPDATE OF challenge_id, web_session_id, browser_session_id, run_id, dispatch_id, service_instance_id, binding_json, authority_hmac ON presence_leases
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM one_use_challenges challenge
  WHERE challenge.id=NEW.challenge_id
    AND challenge.purpose='attended_dispatch'
    AND challenge.web_session_id=NEW.web_session_id
    AND challenge.run_id=NEW.run_id
    AND challenge.dispatch_id=NEW.dispatch_id
    AND challenge.service_instance_id=NEW.service_instance_id
    AND challenge.binding_json=NEW.binding_json
    AND challenge.authority_hmac=NEW.authority_hmac
    AND challenge.dashboard_instance_id IS NEW.dashboard_instance_id
    AND challenge.browser_session_id IS NEW.browser_session_id
)
BEGIN SELECT RAISE(ABORT, 'presence lease does not match challenge identity'); END;

CREATE TABLE migration_relational_guard (value INTEGER NOT NULL) STRICT;
CREATE TRIGGER migration_relational_guard_check
BEFORE INSERT ON migration_relational_guard
FOR EACH ROW WHEN
  EXISTS (
    SELECT 1 FROM candidate_assertions assertion
    LEFT JOIN assertion_events event ON event.id=assertion.assertion_event_id
    WHERE (assertion.state='staged' AND (assertion.confirmed_at IS NOT NULL OR assertion.revoked_at IS NOT NULL))
       OR (assertion.state='active' AND (assertion.confirmed_at IS NULL OR assertion.revoked_at IS NOT NULL))
       OR (assertion.state='revoked' AND (assertion.confirmed_at IS NULL OR assertion.revoked_at IS NULL))
       OR event.id IS NULL OR event.profile_id<>assertion.profile_id
       OR event.event_kind<>CASE assertion.state
         WHEN 'staged' THEN 'staged'
         ELSE 'confirmed'
       END
  )
  OR EXISTS (
    SELECT 1 FROM one_use_challenges challenge
    WHERE
      (challenge.purpose='assertion_confirmation' AND (
        challenge.candidate_profile_id IS NULL OR challenge.assertion_id IS NULL
        OR challenge.alias_id IS NOT NULL OR challenge.checkpoint_id IS NOT NULL
        OR challenge.field_resolution_id IS NOT NULL OR challenge.run_id IS NOT NULL
        OR challenge.dispatch_id IS NOT NULL OR challenge.batch_policy_id IS NOT NULL
        OR NOT EXISTS (SELECT 1 FROM candidate_assertions assertion
                       WHERE assertion.id=challenge.assertion_id
                         AND assertion.profile_id=challenge.candidate_profile_id)
      ))
      OR (challenge.purpose IN ('alias_review','alias_reassign') AND (
        challenge.candidate_profile_id IS NULL OR challenge.alias_id IS NULL
        OR challenge.assertion_id IS NOT NULL OR challenge.checkpoint_id IS NOT NULL
        OR challenge.field_resolution_id IS NOT NULL OR challenge.run_id IS NOT NULL
        OR challenge.dispatch_id IS NOT NULL OR challenge.batch_policy_id IS NOT NULL
        OR NOT EXISTS (SELECT 1 FROM question_aliases alias
                       WHERE alias.id=challenge.alias_id
                         AND alias.profile_id=challenge.candidate_profile_id)
      ))
      OR (challenge.purpose='manual_completion' AND (
        challenge.checkpoint_id IS NULL OR challenge.field_resolution_id IS NULL
        OR challenge.candidate_profile_id IS NOT NULL OR challenge.assertion_id IS NOT NULL OR challenge.alias_id IS NOT NULL
        OR challenge.run_id IS NOT NULL OR challenge.dispatch_id IS NOT NULL OR challenge.batch_policy_id IS NOT NULL
        OR NOT EXISTS (SELECT 1 FROM field_resolutions resolution
                       WHERE resolution.id=challenge.field_resolution_id AND resolution.checkpoint_id=challenge.checkpoint_id)
      ))
      OR (challenge.purpose='attended_dispatch' AND (
        challenge.run_id IS NULL OR challenge.dispatch_id IS NULL
        OR challenge.candidate_profile_id IS NOT NULL OR challenge.assertion_id IS NOT NULL
        OR challenge.alias_id IS NOT NULL OR challenge.checkpoint_id IS NOT NULL
        OR challenge.field_resolution_id IS NOT NULL OR challenge.batch_policy_id IS NOT NULL
        OR NOT EXISTS (SELECT 1 FROM dispatches dispatch
                       WHERE dispatch.id=challenge.dispatch_id
                         AND dispatch.run_id=challenge.run_id)
      ))
      OR (challenge.purpose='batch_policy_review' AND (
        challenge.candidate_profile_id IS NULL OR challenge.batch_policy_id IS NULL
        OR challenge.assertion_id IS NOT NULL OR challenge.alias_id IS NOT NULL
        OR challenge.checkpoint_id IS NOT NULL OR challenge.field_resolution_id IS NOT NULL
        OR challenge.run_id IS NOT NULL OR challenge.dispatch_id IS NOT NULL
        OR NOT EXISTS (SELECT 1 FROM batch_policies policy
                       WHERE policy.id=challenge.batch_policy_id
                         AND policy.candidate_profile_id=challenge.candidate_profile_id)
      ))
  )
  OR EXISTS (
    SELECT 1 FROM actions
    WHERE (action_kind IN ('inspect','observe','checkpoint') AND side_effect_class<>'none')
       OR (action_kind='fill' AND side_effect_class<>'fill')
       OR (action_kind='submit' AND side_effect_class<>'submit')
  )
  OR EXISTS (
    SELECT 1 FROM batch_policies
    WHERE valid_from NOT GLOB '????-??-??T??:??:??*'
       OR expires_at NOT GLOB '????-??-??T??:??:??*'
       OR julianday(valid_from) IS NULL OR julianday(expires_at) IS NULL
       OR julianday(valid_from)>=julianday(expires_at)
       OR julianday(expires_at)>julianday(valid_from)+1
       OR min_fit_score<5
  )
  OR EXISTS (
    SELECT 1 FROM dispatches dispatch
    WHERE (dispatch.transport='aside' AND dispatch.batch_policy_id IS NULL)
       OR (dispatch.transport<>'aside' AND dispatch.batch_policy_id IS NOT NULL)
       OR (dispatch.batch_policy_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM batch_policies policy
         JOIN applications application ON application.id=dispatch.application_id
         WHERE policy.id=dispatch.batch_policy_id
           AND policy.candidate_profile_id=application.profile_id
           AND policy.state='active'
       ))
  )
  OR EXISTS (
    SELECT 1 FROM daily_quota_reservations reservation
    WHERE NOT EXISTS (
      SELECT 1 FROM dispatches dispatch
      WHERE dispatch.id=reservation.dispatch_id
        AND dispatch.application_id=reservation.application_id
        AND dispatch.batch_policy_id=reservation.policy_id
        AND dispatch.transport='aside'
    )
  )
BEGIN SELECT RAISE(ABORT, 'legacy rows violate relational migration invariants'); END;
INSERT INTO migration_relational_guard VALUES(1);
DROP TRIGGER migration_relational_guard_check;
DROP TABLE migration_relational_guard;
DROP INDEX ux_request_value_secret_active;
ALTER TABLE request_value_secrets RENAME TO request_value_secrets_legacy;
CREATE TABLE request_value_secrets (
 id TEXT PRIMARY KEY, field_resolution_id TEXT NOT NULL REFERENCES field_resolutions(id), ciphertext BLOB, nonce BLOB, value_hmac TEXT,
 key_version INTEGER NOT NULL CHECK(key_version >= 1), state TEXT NOT NULL CHECK(state IN ('active','expired','destroyed','tombstoned')), created_at TEXT NOT NULL, expires_at TEXT NOT NULL, destroyed_at TEXT,
 tombstone_hmac TEXT, revision INTEGER NOT NULL CHECK(revision >= 1),
 CHECK(julianday(expires_at) IS NOT NULL AND julianday(created_at) IS NOT NULL AND julianday(expires_at) > julianday(created_at) AND julianday(expires_at) <= julianday(created_at) + 1.0),
 CHECK((state='active' AND ciphertext IS NOT NULL AND nonce IS NOT NULL AND value_hmac IS NOT NULL AND destroyed_at IS NULL AND tombstone_hmac IS NULL)
       OR (state<>'active' AND ciphertext IS NULL AND nonce IS NULL AND value_hmac IS NULL AND destroyed_at IS NOT NULL AND tombstone_hmac IS NOT NULL))
) STRICT;
INSERT INTO request_value_secrets SELECT * FROM request_value_secrets_legacy;
DROP TABLE request_value_secrets_legacy;
CREATE UNIQUE INDEX ux_request_value_secret_active ON request_value_secrets(field_resolution_id) WHERE state='active';
CREATE TRIGGER request_value_secret_shape_insert
BEFORE INSERT ON request_value_secrets
FOR EACH ROW WHEN
  NEW.key_version < 1
  OR julianday(NEW.expires_at) IS NULL
  OR julianday(NEW.created_at) IS NULL
  OR julianday(NEW.expires_at) <= julianday(NEW.created_at)
  OR julianday(NEW.expires_at) > julianday(NEW.created_at) + 1.0
  OR (NEW.state='active' AND (NEW.ciphertext IS NULL OR NEW.nonce IS NULL OR NEW.value_hmac IS NULL OR NEW.destroyed_at IS NOT NULL OR NEW.tombstone_hmac IS NOT NULL))
  OR (NEW.state<>'active' AND (NEW.ciphertext IS NOT NULL OR NEW.nonce IS NOT NULL OR NEW.value_hmac IS NOT NULL OR NEW.destroyed_at IS NULL OR NEW.tombstone_hmac IS NULL))
BEGIN SELECT RAISE(ABORT, 'invalid request-value secret shape'); END;
CREATE TRIGGER request_value_secret_shape_update
BEFORE UPDATE OF ciphertext, nonce, value_hmac, key_version, state, created_at, expires_at, destroyed_at, tombstone_hmac ON request_value_secrets
FOR EACH ROW WHEN
  NEW.key_version < 1
  OR julianday(NEW.expires_at) IS NULL
  OR julianday(NEW.created_at) IS NULL
  OR julianday(NEW.expires_at) <= julianday(NEW.created_at)
  OR julianday(NEW.expires_at) > julianday(NEW.created_at) + 1.0
  OR (NEW.state='active' AND (NEW.ciphertext IS NULL OR NEW.nonce IS NULL OR NEW.value_hmac IS NULL OR NEW.destroyed_at IS NOT NULL OR NEW.tombstone_hmac IS NOT NULL))
  OR (NEW.state<>'active' AND (NEW.ciphertext IS NOT NULL OR NEW.nonce IS NOT NULL OR NEW.value_hmac IS NOT NULL OR NEW.destroyed_at IS NULL OR NEW.tombstone_hmac IS NULL))
BEGIN SELECT RAISE(ABORT, 'invalid request-value secret shape'); END;