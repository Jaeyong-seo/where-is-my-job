ALTER TABLE breakers ADD COLUMN tenant TEXT NOT NULL DEFAULT '';
ALTER TABLE assertion_events ADD COLUMN payload_json TEXT CHECK(payload_json IS NULL OR json_valid(payload_json));

CREATE TABLE migration_0010_preflight (value INTEGER NOT NULL) STRICT;
CREATE TRIGGER migration_0010_preflight_check BEFORE INSERT ON migration_0010_preflight
FOR EACH ROW WHEN
 EXISTS (SELECT 1 FROM candidate_assertions a LEFT JOIN assertion_events e ON e.id=a.assertion_event_id WHERE e.id IS NULL OR e.profile_id IS NOT a.profile_id OR e.assertion_id IS NOT a.id OR e.event_kind IS NOT CASE a.state WHEN 'staged' THEN 'staged' ELSE 'confirmed' END)
 OR EXISTS (SELECT 1 FROM question_aliases q LEFT JOIN candidate_assertions a ON a.id=q.assertion_id LEFT JOIN assertion_events e ON e.id=q.confirmation_event_id WHERE a.id IS NULL OR e.id IS NULL OR a.profile_id IS NOT q.profile_id OR e.profile_id IS NOT q.profile_id OR e.assertion_id IS NOT q.assertion_id OR e.event_kind IS NOT 'alias_confirmed')
 OR EXISTS (SELECT 1 FROM batch_policies p LEFT JOIN candidate_assertions a ON a.id=p.assertion_snapshot_id LEFT JOIN assertion_events e ON e.id=p.candidate_confirmation_event_id LEFT JOIN kill_switches k ON k.id=p.global_kill_switch_id LEFT JOIN capabilities c ON c.id=p.fixture_capability_id WHERE a.id IS NULL OR e.id IS NULL OR k.id IS NULL OR c.id IS NULL OR a.profile_id IS NOT p.candidate_profile_id OR a.state IS NOT 'active' OR e.profile_id IS NOT p.candidate_profile_id OR e.assertion_id IS NOT p.assertion_snapshot_id OR e.event_kind IS NOT 'confirmed' OR k.scope_kind IS NOT 'global' OR k.scope_key IS NOT 'global' OR k.state IS NOT 'closed' OR p.environment IS NOT 'fixture' OR p.timezone IS NOT 'America/Vancouver' OR (p.daily_cap IS NULL OR p.daily_cap NOT BETWEEN 1 AND 20) OR c.environment IS NOT 'fixture' OR c.state IS NOT 'active' OR c.adapter_id IS NOT p.fixture_adapter_id OR c.origin IS NOT p.fixture_origin)
 OR EXISTS (SELECT 1 FROM runs r LEFT JOIN commands c ON c.id=r.command_id WHERE r.command_id IS NOT NULL AND (c.id IS NULL OR c.application_id IS NOT r.application_id))
 OR EXISTS (SELECT 1 FROM daily_quota_reservations q LEFT JOIN batch_policies p ON p.id=q.policy_id LEFT JOIN dispatches d ON d.id=q.dispatch_id LEFT JOIN applications a ON a.id=q.application_id WHERE p.id IS NULL OR d.id IS NULL OR a.id IS NULL OR q.local_date IS NULL OR q.local_date NOT GLOB '????-??-??' OR date(q.local_date) IS NOT q.local_date OR (q.state='reserved' AND q.local_date IS NOT application_automation_policy_local_date(COALESCE(p.timezone,''))) OR (p.daily_cap IS NULL OR p.daily_cap NOT BETWEEN 1 AND 20) OR d.application_id IS NOT q.application_id OR d.batch_policy_id IS NOT q.policy_id OR d.transport IS NOT 'aside')
 OR EXISTS (SELECT 1 FROM dispatches d LEFT JOIN runs r ON r.id=d.run_id LEFT JOIN batch_policies p ON p.id=d.batch_policy_id LEFT JOIN applications a ON a.id=d.application_id LEFT JOIN capabilities c ON c.id=d.fixture_capability_id WHERE d.started_at IS NOT NULL AND d.transport='aside' AND (d.state='intent' OR r.id IS NULL OR r.application_id IS NOT d.application_id OR p.id IS NULL OR a.id IS NULL OR c.id IS NULL OR d.environment IS NOT 'fixture' OR p.state IS NOT 'active' OR p.environment IS NOT 'fixture' OR p.candidate_profile_id IS NOT a.profile_id OR p.fixture_capability_id IS NOT d.fixture_capability_id OR p.fixture_adapter_id IS NOT d.fixture_adapter_id OR p.fixture_origin IS NOT d.fixture_origin OR c.environment IS NOT 'fixture' OR c.state IS NOT 'active' OR c.adapter_id IS NOT d.fixture_adapter_id OR c.origin IS NOT d.fixture_origin OR c.form_fingerprint IS NOT d.form_fingerprint OR NOT EXISTS (SELECT 1 FROM daily_quota_reservations q WHERE q.policy_id=d.batch_policy_id AND q.application_id=d.application_id AND q.dispatch_id=d.id AND q.local_date=application_automation_policy_local_date(p.timezone) AND q.state IN ('reserved','consumed')) OR NOT EXISTS (SELECT 1 FROM fixture_dispatch_outcomes o WHERE o.dispatch_id=d.id AND o.application_id=d.application_id AND o.run_id=d.run_id AND o.state IN ('prepared','possibly_started','confirmed','manual_followup'))))
 OR EXISTS (SELECT 1 FROM dispatches ds WHERE ds.transport='aside' AND ((ds.state='intent') <> (ds.started_at IS NULL)))
 OR EXISTS (SELECT 1 FROM attempts at LEFT JOIN runs r ON r.id=at.run_id WHERE r.id IS NULL OR r.application_id IS NOT at.application_id)
 OR EXISTS (SELECT 1 FROM actions ac LEFT JOIN runs r ON r.id=ac.run_id LEFT JOIN attempts at ON at.id=ac.attempt_id WHERE r.id IS NULL OR (ac.attempt_id IS NOT NULL AND (at.id IS NULL OR at.run_id IS NOT ac.run_id)))
 OR EXISTS (SELECT 1 FROM fixture_dispatch_outcomes o LEFT JOIN dispatches d ON d.id=o.dispatch_id LEFT JOIN runs r ON r.id=o.run_id LEFT JOIN applications a ON a.id=o.application_id LEFT JOIN sessions s ON s.id=o.session_id WHERE d.id IS NULL OR r.id IS NULL OR a.id IS NULL OR s.id IS NULL OR d.application_id IS NOT o.application_id OR d.run_id IS NOT o.run_id OR r.application_id IS NOT o.application_id OR s.profile_id IS NOT a.profile_id)
 OR EXISTS (SELECT 1 FROM presence_leases l LEFT JOIN one_use_challenges c ON c.id=l.challenge_id LEFT JOIN sessions s ON s.id=l.web_session_id WHERE c.id IS NULL OR s.id IS NULL OR c.web_session_id IS NOT l.web_session_id OR c.run_id IS NOT l.run_id OR c.dispatch_id IS NOT l.dispatch_id)
BEGIN SELECT RAISE(ABORT,'legacy rows violate 0010 relational authority invariants'); END;
INSERT INTO migration_0010_preflight VALUES(1);
DROP TRIGGER migration_0010_preflight_check;
DROP TABLE migration_0010_preflight;

CREATE TABLE fixture_fill_evidence (
 dispatch_binding TEXT,
 run_id TEXT NOT NULL REFERENCES runs(id),
 application_id TEXT NOT NULL REFERENCES applications(id),
 session_hmac TEXT NOT NULL CHECK(length(session_hmac)=64 AND session_hmac NOT GLOB '*[^0-9a-f]*'),
 page_fingerprint TEXT NOT NULL,
 form_fingerprint TEXT NOT NULL,
 field_digest TEXT NOT NULL CHECK(length(field_digest)=64 AND field_digest NOT GLOB '*[^0-9a-f]*'),
 resume_present INTEGER NOT NULL CHECK(resume_present IN (0,1)),
 resume_sha256 TEXT CHECK(resume_sha256 IS NULL OR (length(resume_sha256)=64 AND resume_sha256 NOT GLOB '*[^0-9a-f]*')),
 script_sha256 TEXT NOT NULL CHECK(length(script_sha256)=64 AND script_sha256 NOT GLOB '*[^0-9a-f]*'),
 executable_sha256 TEXT NOT NULL CHECK(length(executable_sha256)=64 AND executable_sha256 NOT GLOB '*[^0-9a-f]*'),
 created_at TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>=1),
 PRIMARY KEY(run_id, form_fingerprint),
 CHECK((resume_present=0)=(resume_sha256 IS NULL))
) STRICT;
CREATE TRIGGER fixture_fill_evidence_ownership_insert
BEFORE INSERT ON fixture_fill_evidence
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM runs r WHERE r.id=NEW.run_id AND r.application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'fixture fill evidence run does not belong to application'); END;
CREATE TRIGGER fixture_fill_evidence_ownership_update
BEFORE UPDATE OF run_id,application_id ON fixture_fill_evidence
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM runs r WHERE r.id=NEW.run_id AND r.application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'fixture fill evidence run does not belong to application'); END;
CREATE TRIGGER fixture_fill_evidence_binding_claim_immutable
BEFORE UPDATE OF dispatch_binding ON fixture_fill_evidence
FOR EACH ROW WHEN OLD.dispatch_binding IS NOT NULL AND NEW.dispatch_binding IS NOT OLD.dispatch_binding
BEGIN SELECT RAISE(ABORT,'fixture fill evidence dispatch binding is immutable after claim'); END;

CREATE TRIGGER fixture_aside_intent_insert
BEFORE INSERT ON dispatches
FOR EACH ROW WHEN NEW.transport='aside' AND (NEW.state IS NOT 'intent' OR NEW.started_at IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'aside dispatch must be inserted as an unstarted intent'); END;

CREATE TRIGGER fixture_aside_intent_start_transition
BEFORE UPDATE OF state,started_at ON dispatches
FOR EACH ROW WHEN NEW.transport='aside' AND OLD.started_at IS NULL AND NEW.started_at IS NOT NULL
 AND (OLD.state IS NOT 'intent' OR NEW.state IS NOT 'dispatching')
BEGIN SELECT RAISE(ABORT,'aside dispatch must start by transitioning intent to dispatching'); END;

CREATE TRIGGER fixture_current_policy_quota_insert
BEFORE INSERT ON daily_quota_reservations
FOR EACH ROW WHEN NOT EXISTS (
 SELECT 1 FROM batch_policies p
 WHERE p.id=NEW.policy_id AND p.timezone='America/Vancouver'
   AND NEW.local_date=application_automation_policy_local_date(p.timezone)
   AND p.daily_cap BETWEEN 1 AND 20
   AND (SELECT count(*) FROM daily_quota_reservations q WHERE q.policy_id=NEW.policy_id AND q.local_date=NEW.local_date AND q.state IN ('reserved','consumed')) < p.daily_cap
)
BEGIN SELECT RAISE(ABORT,'quota reservation requires current policy-local date and cap'); END;

CREATE TRIGGER fixture_current_policy_quota_start
BEFORE UPDATE OF started_at ON dispatches
FOR EACH ROW WHEN NEW.transport='aside' AND OLD.started_at IS NULL AND NEW.started_at IS NOT NULL
 AND NOT EXISTS (
 SELECT 1 FROM daily_quota_reservations q JOIN batch_policies p ON p.id=q.policy_id
 WHERE q.dispatch_id=NEW.id AND q.application_id=NEW.application_id AND q.policy_id=NEW.batch_policy_id
   AND q.state IN ('reserved','consumed') AND q.local_date=application_automation_policy_local_date(p.timezone)
   AND p.daily_cap BETWEEN 1 AND 20
)
BEGIN SELECT RAISE(ABORT,'aside dispatch start requires current policy-local quota'); END;

CREATE TRIGGER fixture_aside_start_outcome_proof
BEFORE UPDATE OF started_at ON dispatches
FOR EACH ROW WHEN NEW.transport='aside' AND OLD.started_at IS NULL AND NEW.started_at IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM fixture_dispatch_outcomes o WHERE o.dispatch_id=NEW.id AND o.application_id=NEW.application_id AND o.run_id=NEW.run_id AND o.state='prepared')
BEGIN SELECT RAISE(ABORT,'aside dispatch start requires prepared outcome proof'); END;

CREATE TRIGGER fixture_aside_start_provider_authority
BEFORE UPDATE OF started_at ON dispatches
FOR EACH ROW WHEN NEW.transport='aside' AND OLD.started_at IS NULL AND NEW.started_at IS NOT NULL
 AND NOT EXISTS (
 SELECT 1 FROM capabilities c
 JOIN kill_switches pk ON pk.scope_kind='provider' AND pk.scope_key=c.provider AND pk.state='closed'
 JOIN breakers b ON b.provider=c.provider AND b.tenant=c.tenant AND b.state<>'open'
 WHERE c.id=NEW.fixture_capability_id
)
BEGIN SELECT RAISE(ABORT,'aside dispatch start requires closed provider kill switch and non-open breaker'); END;

CREATE TRIGGER fixture_aside_dispatch_identity_frozen
BEFORE UPDATE OF transport,environment,batch_policy_id,fixture_capability_id,fixture_adapter_id,fixture_origin,form_fingerprint ON dispatches
FOR EACH ROW WHEN (OLD.transport='aside' OR NEW.transport='aside')
 AND (OLD.started_at IS NOT NULL OR EXISTS (SELECT 1 FROM fixture_dispatch_outcomes o WHERE o.dispatch_id=OLD.id))
 AND (NEW.transport IS NOT OLD.transport OR NEW.environment IS NOT OLD.environment OR NEW.batch_policy_id IS NOT OLD.batch_policy_id
      OR NEW.fixture_capability_id IS NOT OLD.fixture_capability_id OR NEW.fixture_adapter_id IS NOT OLD.fixture_adapter_id
      OR NEW.fixture_origin IS NOT OLD.fixture_origin OR NEW.form_fingerprint IS NOT OLD.form_fingerprint)
BEGIN SELECT RAISE(ABORT,'aside dispatch identity is frozen after evidence binding or start'); END;

CREATE TRIGGER fixture_dispatch_owner_immutable
BEFORE UPDATE OF application_id,run_id ON dispatches
FOR EACH ROW WHEN NEW.application_id IS NOT OLD.application_id OR NEW.run_id IS NOT OLD.run_id
BEGIN SELECT RAISE(ABORT,'dispatch owner identity is immutable'); END;

CREATE TRIGGER fixture_attempt_owner_immutable
BEFORE UPDATE OF application_id,run_id ON attempts
FOR EACH ROW WHEN NEW.application_id IS NOT OLD.application_id OR NEW.run_id IS NOT OLD.run_id
BEGIN SELECT RAISE(ABORT,'attempt owner identity is immutable'); END;

CREATE TRIGGER fixture_action_owner_immutable
BEFORE UPDATE OF run_id,attempt_id ON actions
FOR EACH ROW WHEN NEW.run_id IS NOT OLD.run_id OR NEW.attempt_id IS NOT OLD.attempt_id
BEGIN SELECT RAISE(ABORT,'action owner identity is immutable'); END;

CREATE TRIGGER fixture_run_command_owner_insert
BEFORE INSERT ON runs
FOR EACH ROW WHEN NEW.command_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM commands c WHERE c.id=NEW.command_id AND c.application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'run command must belong to application'); END;

CREATE TRIGGER fixture_run_command_owner_update
BEFORE UPDATE OF application_id,command_id ON runs
FOR EACH ROW WHEN (NEW.application_id IS NOT OLD.application_id OR NEW.command_id IS NOT OLD.command_id)
 AND (EXISTS (SELECT 1 FROM attempts a WHERE a.run_id=OLD.id) OR EXISTS (SELECT 1 FROM actions a WHERE a.run_id=OLD.id) OR EXISTS (SELECT 1 FROM dispatches d WHERE d.run_id=OLD.id) OR EXISTS (SELECT 1 FROM checkpoints c WHERE c.run_id=OLD.id))
BEGIN SELECT RAISE(ABORT,'run owner and command are immutable after child binding'); END;

CREATE TRIGGER fixture_run_command_application_update
BEFORE UPDATE OF application_id,command_id ON runs
FOR EACH ROW WHEN NEW.command_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM commands c WHERE c.id=NEW.command_id AND c.application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'run command must belong to application'); END;

CREATE TRIGGER fixture_command_application_immutable
BEFORE UPDATE OF application_id ON commands
FOR EACH ROW WHEN NEW.application_id IS NOT OLD.application_id AND EXISTS (SELECT 1 FROM runs r WHERE r.command_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'command application is immutable after run binding'); END;

CREATE TRIGGER fixture_outcome_owner_immutable
BEFORE UPDATE OF dispatch_id,application_id,session_id,run_id ON fixture_dispatch_outcomes
FOR EACH ROW WHEN NEW.dispatch_id IS NOT OLD.dispatch_id OR NEW.application_id IS NOT OLD.application_id OR NEW.session_id IS NOT OLD.session_id OR NEW.run_id IS NOT OLD.run_id
BEGIN SELECT RAISE(ABORT,'fixture outcome owner identity is immutable'); END;

CREATE TRIGGER fixture_outcome_confirmed_proof_shape_insert
BEFORE INSERT ON fixture_dispatch_outcomes
FOR EACH ROW WHEN NEW.state='confirmed' AND (
  NEW.receipt_digest IS NULL OR length(NEW.receipt_digest)<>64 OR NEW.receipt_digest GLOB '*[^0-9a-f]*'
  OR NEW.attestation_digest IS NULL OR length(NEW.attestation_digest)<>64 OR NEW.attestation_digest GLOB '*[^0-9a-f]*'
  OR NEW.observed_intent_hmac IS NULL OR NEW.observed_intent_hmac IS NOT NEW.intent_hmac
)
BEGIN SELECT RAISE(ABORT,'confirmed fixture outcome requires well-formed receipt, attestation, and observed intent proof'); END;

CREATE TRIGGER fixture_outcome_confirmed_proof_shape_update
BEFORE UPDATE OF state,receipt_digest,attestation_digest,observed_intent_hmac,intent_hmac ON fixture_dispatch_outcomes
FOR EACH ROW WHEN NEW.state='confirmed' AND (
  NEW.receipt_digest IS NULL OR length(NEW.receipt_digest)<>64 OR NEW.receipt_digest GLOB '*[^0-9a-f]*'
  OR NEW.attestation_digest IS NULL OR length(NEW.attestation_digest)<>64 OR NEW.attestation_digest GLOB '*[^0-9a-f]*'
  OR NEW.observed_intent_hmac IS NULL OR NEW.observed_intent_hmac IS NOT NEW.intent_hmac
)
BEGIN SELECT RAISE(ABORT,'confirmed fixture outcome requires well-formed receipt, attestation, and observed intent proof'); END;

CREATE TRIGGER fixture_outcome_terminal_proof_frozen
BEFORE UPDATE OF receipt_digest,attestation_digest,observed_intent_hmac,started_at,confirmed_at,terminal_at ON fixture_dispatch_outcomes
FOR EACH ROW WHEN OLD.terminal_at IS NOT NULL AND (
  NEW.receipt_digest IS NOT OLD.receipt_digest OR NEW.attestation_digest IS NOT OLD.attestation_digest
  OR NEW.observed_intent_hmac IS NOT OLD.observed_intent_hmac OR NEW.started_at IS NOT OLD.started_at
  OR NEW.confirmed_at IS NOT OLD.confirmed_at OR NEW.terminal_at IS NOT OLD.terminal_at
)
BEGIN SELECT RAISE(ABORT,'fixture outcome terminal proof fields are frozen'); END;

CREATE TRIGGER fixture_dispatch_no_delete BEFORE DELETE ON dispatches
FOR EACH ROW BEGIN SELECT RAISE(ABORT,'dispatches are append-only'); END;
CREATE TRIGGER fixture_outcome_no_delete BEFORE DELETE ON fixture_dispatch_outcomes
FOR EACH ROW BEGIN SELECT RAISE(ABORT,'fixture outcomes are append-only'); END;
CREATE TRIGGER fixture_quota_no_delete BEFORE DELETE ON daily_quota_reservations
FOR EACH ROW BEGIN SELECT RAISE(ABORT,'quota history is append-only'); END;

CREATE TRIGGER fixture_challenge_owner_immutable
BEFORE UPDATE OF purpose,candidate_profile_id,assertion_id,alias_id,checkpoint_id,field_resolution_id,run_id,dispatch_id,batch_policy_id,web_session_id,browser_session_id,service_instance_id,dashboard_instance_id,binding_json,authority_hmac ON one_use_challenges
FOR EACH ROW WHEN EXISTS (SELECT 1 FROM presence_leases l WHERE l.challenge_id=OLD.id)
 AND (NEW.purpose IS NOT OLD.purpose OR NEW.candidate_profile_id IS NOT OLD.candidate_profile_id OR NEW.assertion_id IS NOT OLD.assertion_id OR NEW.alias_id IS NOT OLD.alias_id OR NEW.checkpoint_id IS NOT OLD.checkpoint_id OR NEW.field_resolution_id IS NOT OLD.field_resolution_id OR NEW.run_id IS NOT OLD.run_id OR NEW.dispatch_id IS NOT OLD.dispatch_id OR NEW.batch_policy_id IS NOT OLD.batch_policy_id OR NEW.web_session_id IS NOT OLD.web_session_id OR NEW.browser_session_id IS NOT OLD.browser_session_id OR NEW.service_instance_id IS NOT OLD.service_instance_id OR NEW.dashboard_instance_id IS NOT OLD.dashboard_instance_id OR NEW.binding_json IS NOT OLD.binding_json OR NEW.authority_hmac IS NOT OLD.authority_hmac)
BEGIN SELECT RAISE(ABORT,'challenge owner identity is immutable after lease binding'); END;

CREATE TRIGGER fixture_lease_owner_immutable
BEFORE UPDATE OF challenge_id,web_session_id,browser_session_id,run_id,dispatch_id,service_instance_id,dashboard_instance_id,binding_json,authority_hmac ON presence_leases
FOR EACH ROW WHEN NEW.challenge_id IS NOT OLD.challenge_id OR NEW.web_session_id IS NOT OLD.web_session_id OR NEW.browser_session_id IS NOT OLD.browser_session_id OR NEW.run_id IS NOT OLD.run_id OR NEW.dispatch_id IS NOT OLD.dispatch_id OR NEW.service_instance_id IS NOT OLD.service_instance_id OR NEW.dashboard_instance_id IS NOT OLD.dashboard_instance_id OR NEW.binding_json IS NOT OLD.binding_json OR NEW.authority_hmac IS NOT OLD.authority_hmac
BEGIN SELECT RAISE(ABORT,'presence lease owner identity is immutable'); END;

CREATE TRIGGER fixture_lease_session_active_insert
BEFORE INSERT ON presence_leases
FOR EACH ROW WHEN NOT EXISTS (
 SELECT 1 FROM sessions s JOIN one_use_challenges c ON c.id=NEW.challenge_id
 WHERE s.id=NEW.web_session_id AND s.id=c.web_session_id AND s.state='active' AND julianday(s.expires_at)>julianday('now')
)
BEGIN SELECT RAISE(ABORT,'presence lease requires an active unexpired profile-owned session'); END;

CREATE TRIGGER fixture_lease_consume_requires_live_session
BEFORE UPDATE OF state,consumed_at ON presence_leases
FOR EACH ROW WHEN NEW.state='consumed' AND NOT EXISTS (
 SELECT 1 FROM sessions s WHERE s.id=NEW.web_session_id AND s.state='active' AND julianday(s.expires_at)>julianday('now')
)
BEGIN SELECT RAISE(ABORT,'presence lease cannot be consumed without a live bound session'); END;

CREATE TRIGGER fixture_lease_cascade_challenge_terminal
AFTER UPDATE OF state ON one_use_challenges
FOR EACH ROW WHEN NEW.state IN ('revoked','expired') AND OLD.state IS NOT NEW.state
BEGIN
 UPDATE presence_leases SET state=NEW.state WHERE challenge_id=NEW.id AND state='active';
END;

CREATE TRIGGER fixture_challenge_cascade_session_terminal
AFTER UPDATE OF state ON sessions
FOR EACH ROW WHEN NEW.state IS NOT 'active' AND OLD.state IS 'active'
BEGIN
 UPDATE one_use_challenges SET state='revoked' WHERE web_session_id=NEW.id AND state='active';
 UPDATE presence_leases SET state='revoked' WHERE web_session_id=NEW.id AND state='active';
END;

CREATE TRIGGER fixture_assertion_confirmed_identity_immutable
BEFORE UPDATE OF semantic_key,value_hmac ON candidate_assertions
FOR EACH ROW WHEN OLD.state='active' AND (NEW.semantic_key IS NOT OLD.semantic_key OR NEW.value_hmac IS NOT OLD.value_hmac)
BEGIN SELECT RAISE(ABORT,'confirmed assertion value and semantic identity are immutable'); END;

CREATE TRIGGER fixture_assertion_revocation_terminal
BEFORE UPDATE OF state ON candidate_assertions
FOR EACH ROW WHEN OLD.state='revoked' AND NEW.state IS NOT 'revoked'
BEGIN SELECT RAISE(ABORT,'revoked assertion cannot be reactivated'); END;

CREATE TRIGGER fixture_alias_scope_provenance_immutable
BEFORE UPDATE OF profile_id,assertion_id,confirmation_event_id,provider,normalized_label,semantic_scope ON question_aliases
FOR EACH ROW WHEN NEW.profile_id IS NOT OLD.profile_id OR NEW.assertion_id IS NOT OLD.assertion_id OR NEW.confirmation_event_id IS NOT OLD.confirmation_event_id OR NEW.provider IS NOT OLD.provider OR NEW.normalized_label IS NOT OLD.normalized_label OR NEW.semantic_scope IS NOT OLD.semantic_scope
BEGIN SELECT RAISE(ABORT,'alias scope and provenance are immutable'); END;

CREATE TRIGGER fixture_alias_revocation_terminal
BEFORE UPDATE OF revoked_at ON question_aliases
FOR EACH ROW WHEN OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at
BEGIN SELECT RAISE(ABORT,'revoked alias cannot change revocation'); END;

CREATE TRIGGER fixture_batch_policy_authority_immutable
BEFORE UPDATE OF scope_json,min_fit_score,daily_cap,provider_form_allowlist_json,assertion_snapshot_id,material_policy_json,checkpoint_classes_json,valid_from,expires_at,global_kill_switch_id,signature_hmac,key_version,candidate_confirmation_event_id,candidate_profile_id,policy_version,timezone,environment,fixture_adapter_id,fixture_origin,fixture_capability_id ON batch_policies
FOR EACH ROW WHEN OLD.state IS NOT 'draft' AND (
  NEW.scope_json IS NOT OLD.scope_json OR NEW.min_fit_score IS NOT OLD.min_fit_score OR NEW.daily_cap IS NOT OLD.daily_cap
  OR NEW.provider_form_allowlist_json IS NOT OLD.provider_form_allowlist_json OR NEW.assertion_snapshot_id IS NOT OLD.assertion_snapshot_id
  OR NEW.material_policy_json IS NOT OLD.material_policy_json OR NEW.checkpoint_classes_json IS NOT OLD.checkpoint_classes_json
  OR NEW.valid_from IS NOT OLD.valid_from OR NEW.expires_at IS NOT OLD.expires_at OR NEW.global_kill_switch_id IS NOT OLD.global_kill_switch_id
  OR NEW.signature_hmac IS NOT OLD.signature_hmac OR NEW.key_version IS NOT OLD.key_version OR NEW.candidate_confirmation_event_id IS NOT OLD.candidate_confirmation_event_id
  OR NEW.candidate_profile_id IS NOT OLD.candidate_profile_id OR NEW.policy_version IS NOT OLD.policy_version OR NEW.timezone IS NOT OLD.timezone
  OR NEW.environment IS NOT OLD.environment OR NEW.fixture_adapter_id IS NOT OLD.fixture_adapter_id OR NEW.fixture_origin IS NOT OLD.fixture_origin OR NEW.fixture_capability_id IS NOT OLD.fixture_capability_id
)
BEGIN SELECT RAISE(ABORT,'signed batch policy authority fields are immutable once active'); END;

CREATE TRIGGER fixture_batch_policy_revocation_terminal
BEFORE UPDATE OF state ON batch_policies
FOR EACH ROW WHEN OLD.state IN ('revoked','expired','exhausted') AND NEW.state IS NOT OLD.state
BEGIN SELECT RAISE(ABORT,'terminal batch policy state cannot change'); END;

CREATE TRIGGER fixture_batch_policy_state_revision_monotonic
BEFORE UPDATE OF state ON batch_policies
FOR EACH ROW WHEN NEW.state IS NOT OLD.state AND NEW.revision<=OLD.revision
BEGIN SELECT RAISE(ABORT,'batch policy replacement requires a new revision'); END;

DROP TRIGGER fixture_run_application_identity_immutable;
