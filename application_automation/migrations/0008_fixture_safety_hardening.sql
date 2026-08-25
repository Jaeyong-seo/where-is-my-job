ALTER TABLE capabilities ADD COLUMN environment TEXT NOT NULL DEFAULT 'fixture' CHECK(environment='fixture');
ALTER TABLE capabilities ADD COLUMN adapter_id TEXT NOT NULL DEFAULT 'fixture-aside-v1';
ALTER TABLE capabilities ADD COLUMN origin TEXT NOT NULL DEFAULT 'fixture.local';
ALTER TABLE batch_policies ADD COLUMN environment TEXT NOT NULL DEFAULT 'fixture' CHECK(environment='fixture');
ALTER TABLE batch_policies ADD COLUMN fixture_adapter_id TEXT NOT NULL DEFAULT 'fixture-aside-v1';
ALTER TABLE batch_policies ADD COLUMN fixture_origin TEXT NOT NULL DEFAULT 'fixture.local';
ALTER TABLE batch_policies ADD COLUMN fixture_capability_id TEXT REFERENCES capabilities(id);
ALTER TABLE dispatches ADD COLUMN environment TEXT NOT NULL DEFAULT 'fixture' CHECK(environment='fixture');
ALTER TABLE dispatches ADD COLUMN fixture_adapter_id TEXT NOT NULL DEFAULT 'fixture-aside-v1';
ALTER TABLE dispatches ADD COLUMN fixture_origin TEXT NOT NULL DEFAULT 'fixture.local';
ALTER TABLE dispatches ADD COLUMN fixture_capability_id TEXT REFERENCES capabilities(id);
ALTER TABLE evidence ADD COLUMN ledger_sequence INTEGER;
ALTER TABLE evidence ADD COLUMN key_version INTEGER;
ALTER TABLE evidence ADD COLUMN receipt_digest TEXT;
ALTER TABLE evidence ADD COLUMN attestation_digest TEXT;
CREATE UNIQUE INDEX ux_evidence_ledger_sequence ON evidence(application_id,ledger_sequence) WHERE ledger_sequence IS NOT NULL;
CREATE TABLE evidence_ledger_heads (
 application_id TEXT PRIMARY KEY REFERENCES applications(id), key_version INTEGER NOT NULL CHECK(key_version>=1),
 event_count INTEGER NOT NULL CHECK(event_count>=0), event_hash TEXT, head_hmac TEXT NOT NULL
) STRICT;
CREATE TABLE fixture_dispatch_outcomes (
 dispatch_id TEXT PRIMARY KEY REFERENCES dispatches(id), application_id TEXT NOT NULL REFERENCES applications(id),
 provider TEXT NOT NULL CHECK(provider='fixture'), tenant TEXT NOT NULL CHECK(tenant='fixture'), account_hmac TEXT NOT NULL CHECK(length(account_hmac)=64 AND account_hmac NOT GLOB '*[^0-9a-f]*'), context_hmac TEXT NOT NULL CHECK(length(context_hmac)=64 AND context_hmac NOT GLOB '*[^0-9a-f]*'),
 session_id TEXT NOT NULL REFERENCES sessions(id), session_hmac TEXT NOT NULL CHECK(length(session_hmac)=64 AND session_hmac NOT GLOB '*[^0-9a-f]*'), run_id TEXT NOT NULL REFERENCES runs(id),
 intent_hmac TEXT NOT NULL CHECK(length(intent_hmac)=64 AND intent_hmac NOT GLOB '*[^0-9a-f]*'), payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
 page_fingerprint TEXT NOT NULL, form_fingerprint TEXT NOT NULL, resume_sha256 TEXT NOT NULL CHECK(length(resume_sha256)=64 AND resume_sha256 NOT GLOB '*[^0-9a-f]*'),
 state TEXT NOT NULL CHECK(state IN ('prepared','possibly_started','confirmed','manual_followup','retryable_not_started')),
 receipt_digest TEXT, attestation_digest TEXT, observed_intent_hmac TEXT,
 prepared_at TEXT NOT NULL, started_at TEXT, confirmed_at TEXT, terminal_at TEXT, revision INTEGER NOT NULL CHECK(revision>=1),
 UNIQUE(application_id, intent_hmac),
 CHECK((state='prepared' AND started_at IS NULL AND confirmed_at IS NULL AND terminal_at IS NULL)
 OR (state='possibly_started' AND started_at IS NOT NULL AND confirmed_at IS NULL AND terminal_at IS NULL)
 OR (state='retryable_not_started' AND started_at IS NULL AND confirmed_at IS NULL AND terminal_at IS NOT NULL)
 OR (state='manual_followup' AND started_at IS NOT NULL AND confirmed_at IS NULL AND terminal_at IS NOT NULL)
 OR (state='confirmed' AND started_at IS NOT NULL AND confirmed_at IS NOT NULL AND terminal_at IS NOT NULL AND receipt_digest IS NOT NULL AND attestation_digest IS NOT NULL AND observed_intent_hmac=intent_hmac))
) STRICT;
CREATE TRIGGER fixture_authority_dispatch_insert BEFORE INSERT ON dispatches FOR EACH ROW WHEN NEW.environment<>'fixture' OR (NEW.transport IN ('direct','aside') AND NOT EXISTS (SELECT 1 FROM batch_policies p JOIN applications a ON a.id=NEW.application_id JOIN capabilities c ON c.id=NEW.fixture_capability_id WHERE p.id=NEW.batch_policy_id AND p.state='active' AND p.environment='fixture' AND p.candidate_profile_id=a.profile_id AND c.environment='fixture' AND c.state='active' AND c.adapter_id=NEW.fixture_adapter_id AND c.origin=NEW.fixture_origin AND p.fixture_capability_id=c.id AND p.fixture_adapter_id=NEW.fixture_adapter_id AND p.fixture_origin=NEW.fixture_origin)) BEGIN SELECT RAISE(ABORT,'dispatch requires active fixture-only policy capability'); END;
CREATE TRIGGER fixture_authority_dispatch_update BEFORE UPDATE OF application_id,transport,batch_policy_id,environment,fixture_adapter_id,fixture_origin,fixture_capability_id ON dispatches FOR EACH ROW WHEN NEW.environment<>'fixture' OR (NEW.transport IN ('direct','aside') AND NOT EXISTS (SELECT 1 FROM batch_policies p JOIN applications a ON a.id=NEW.application_id JOIN capabilities c ON c.id=NEW.fixture_capability_id WHERE p.id=NEW.batch_policy_id AND p.state='active' AND p.environment='fixture' AND p.candidate_profile_id=a.profile_id AND c.environment='fixture' AND c.state='active' AND c.adapter_id=NEW.fixture_adapter_id AND c.origin=NEW.fixture_origin AND p.fixture_capability_id=c.id AND p.fixture_adapter_id=NEW.fixture_adapter_id AND p.fixture_origin=NEW.fixture_origin)) BEGIN SELECT RAISE(ABORT,'dispatch requires active fixture-only policy capability'); END;
CREATE TRIGGER assertion_events_append_only BEFORE UPDATE ON assertion_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'assertion events are append-only'); END;
CREATE TRIGGER assertion_events_no_delete BEFORE DELETE ON assertion_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'assertion events are append-only'); END;
CREATE TRIGGER evidence_append_only BEFORE UPDATE ON evidence FOR EACH ROW BEGIN SELECT RAISE(ABORT,'evidence rows are append-only'); END;
CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence FOR EACH ROW BEGIN SELECT RAISE(ABORT,'evidence rows are append-only'); END;
CREATE TRIGGER fixture_outcome_ownership BEFORE INSERT ON fixture_dispatch_outcomes FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM dispatches d JOIN runs r ON r.id=NEW.run_id JOIN applications a ON a.id=NEW.application_id JOIN sessions s ON s.id=NEW.session_id WHERE d.id=NEW.dispatch_id AND d.application_id=NEW.application_id AND d.run_id=NEW.run_id AND r.application_id=NEW.application_id AND s.profile_id=a.profile_id AND d.environment='fixture') BEGIN SELECT RAISE(ABORT,'fixture outcome must bind fixture dispatch identity'); END;
CREATE TRIGGER fixture_outcome_transition BEFORE UPDATE ON fixture_dispatch_outcomes FOR EACH ROW WHEN NEW.dispatch_id<>OLD.dispatch_id OR NEW.application_id<>OLD.application_id OR NEW.provider<>OLD.provider OR NEW.tenant<>OLD.tenant OR NEW.account_hmac<>OLD.account_hmac OR NEW.context_hmac<>OLD.context_hmac OR NEW.session_id<>OLD.session_id OR NEW.session_hmac<>OLD.session_hmac OR NEW.run_id<>OLD.run_id OR NEW.intent_hmac<>OLD.intent_hmac OR NEW.payload_sha256<>OLD.payload_sha256 OR NEW.page_fingerprint<>OLD.page_fingerprint OR NEW.form_fingerprint<>OLD.form_fingerprint OR NEW.resume_sha256<>OLD.resume_sha256 OR NEW.prepared_at<>OLD.prepared_at OR (OLD.state='prepared' AND NEW.state NOT IN ('prepared','possibly_started','retryable_not_started')) OR (OLD.state='possibly_started' AND NEW.state NOT IN ('possibly_started','confirmed','manual_followup')) OR (OLD.state='retryable_not_started' AND NEW.state NOT IN ('retryable_not_started','prepared')) OR (OLD.state IN ('confirmed','manual_followup') AND NEW.state<>OLD.state) BEGIN SELECT RAISE(ABORT,'fixture outcome transition or identity is invalid'); END;
CREATE TRIGGER quota_cap_insert BEFORE INSERT ON daily_quota_reservations FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM batch_policies p JOIN applications a ON a.id=NEW.application_id WHERE p.id=NEW.policy_id AND p.state='active' AND p.candidate_profile_id=a.profile_id AND NEW.local_date GLOB '????-??-??' AND date(NEW.local_date)=NEW.local_date AND NEW.local_date>=date(p.valid_from) AND NEW.local_date<=date(p.expires_at) AND (SELECT count(*) FROM daily_quota_reservations q WHERE q.policy_id=NEW.policy_id AND q.local_date=NEW.local_date AND q.state IN ('reserved','consumed'))<p.daily_cap) BEGIN SELECT RAISE(ABORT,'quota policy window or cap invalid'); END;
CREATE TRIGGER quota_terminal BEFORE UPDATE OF state,consumed_at,policy_id,local_date,application_id,dispatch_id ON daily_quota_reservations FOR EACH ROW WHEN OLD.state IN ('consumed','released') OR (NEW.state='consumed' AND NEW.consumed_at IS NULL) OR (NEW.state<>'consumed' AND NEW.consumed_at IS NOT NULL) BEGIN SELECT RAISE(ABORT,'quota terminal or timestamp immutable'); END;
CREATE TRIGGER fixture_dispatch_transport_policy_insert BEFORE INSERT ON dispatches
FOR EACH ROW WHEN (NEW.transport IN ('direct','aside') AND NEW.batch_policy_id IS NULL) OR (NEW.transport NOT IN ('direct','aside') AND NEW.batch_policy_id IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'unattended dispatch requires batch policy'); END;
CREATE TRIGGER fixture_dispatch_transport_policy_update BEFORE UPDATE OF transport,batch_policy_id ON dispatches
FOR EACH ROW WHEN (NEW.transport IN ('direct','aside') AND NEW.batch_policy_id IS NULL) OR (NEW.transport NOT IN ('direct','aside') AND NEW.batch_policy_id IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'unattended dispatch requires batch policy'); END;
CREATE TRIGGER fixture_started_at_terminal BEFORE UPDATE OF started_at ON dispatches
FOR EACH ROW WHEN OLD.started_at IS NOT NULL AND NEW.started_at IS NOT OLD.started_at
BEGIN SELECT RAISE(ABORT,'dispatch start is terminal'); END;
CREATE TRIGGER fixture_quota_dispatch_binding_insert BEFORE INSERT ON daily_quota_reservations
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM dispatches d WHERE d.id=NEW.dispatch_id AND d.application_id=NEW.application_id AND d.batch_policy_id=NEW.policy_id AND d.transport IN ('direct','aside'))
BEGIN SELECT RAISE(ABORT,'quota reservation must bind unattended dispatch'); END;
CREATE TRIGGER fixture_quota_dispatch_binding_update BEFORE UPDATE OF policy_id,application_id,dispatch_id ON daily_quota_reservations
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM dispatches d WHERE d.id=NEW.dispatch_id AND d.application_id=NEW.application_id AND d.batch_policy_id=NEW.policy_id AND d.transport IN ('direct','aside'))
BEGIN SELECT RAISE(ABORT,'quota reservation must bind unattended dispatch'); END;
CREATE TRIGGER fixture_batch_policy_ownership_insert BEFORE INSERT ON batch_policies
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM candidate_assertions a JOIN assertion_events e ON e.id=NEW.candidate_confirmation_event_id JOIN kill_switches k ON k.id=NEW.global_kill_switch_id WHERE a.id=NEW.assertion_snapshot_id AND a.profile_id=NEW.candidate_profile_id AND a.state='active' AND e.profile_id=NEW.candidate_profile_id AND e.assertion_id=NEW.assertion_snapshot_id AND e.event_kind='confirmed' AND k.scope_kind='global' AND k.scope_key='global')
BEGIN SELECT RAISE(ABORT,'batch policy authority is not profile-bound global confirmation'); END;
CREATE TRIGGER fixture_batch_policy_ownership_update BEFORE UPDATE OF candidate_profile_id,assertion_snapshot_id,candidate_confirmation_event_id,global_kill_switch_id ON batch_policies
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM candidate_assertions a JOIN assertion_events e ON e.id=NEW.candidate_confirmation_event_id JOIN kill_switches k ON k.id=NEW.global_kill_switch_id WHERE a.id=NEW.assertion_snapshot_id AND a.profile_id=NEW.candidate_profile_id AND a.state='active' AND e.profile_id=NEW.candidate_profile_id AND e.assertion_id=NEW.assertion_snapshot_id AND e.event_kind='confirmed' AND k.scope_kind='global' AND k.scope_key='global')
BEGIN SELECT RAISE(ABORT,'batch policy authority is not profile-bound global confirmation'); END;
CREATE TRIGGER fixture_assertion_event_binding_insert BEFORE INSERT ON candidate_assertions
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM assertion_events e WHERE e.id=NEW.assertion_event_id AND e.profile_id=NEW.profile_id AND e.assertion_id=NEW.id)
BEGIN SELECT RAISE(ABORT,'assertion event must identify assertion'); END;
CREATE TRIGGER fixture_assertion_event_binding_update BEFORE UPDATE OF id,profile_id,assertion_event_id ON candidate_assertions
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM assertion_events e WHERE e.id=NEW.assertion_event_id AND e.profile_id=NEW.profile_id AND e.assertion_id=NEW.id)
BEGIN SELECT RAISE(ABORT,'assertion event must identify assertion'); END;
CREATE TRIGGER fixture_alias_ownership_insert BEFORE INSERT ON question_aliases
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM candidate_assertions a JOIN assertion_events e ON e.id=NEW.confirmation_event_id WHERE a.id=NEW.assertion_id AND a.profile_id=NEW.profile_id AND e.profile_id=NEW.profile_id AND e.assertion_id=NEW.assertion_id AND e.event_kind='alias_confirmed')
BEGIN SELECT RAISE(ABORT,'alias ownership or confirmation provenance is invalid'); END;
CREATE TRIGGER fixture_alias_ownership_update BEFORE UPDATE OF profile_id,assertion_id,confirmation_event_id ON question_aliases
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM candidate_assertions a JOIN assertion_events e ON e.id=NEW.confirmation_event_id WHERE a.id=NEW.assertion_id AND a.profile_id=NEW.profile_id AND e.profile_id=NEW.profile_id AND e.assertion_id=NEW.assertion_id AND e.event_kind='alias_confirmed')
BEGIN SELECT RAISE(ABORT,'alias ownership or confirmation provenance is invalid'); END;
CREATE TRIGGER fixture_challenge_terminal BEFORE UPDATE OF state ON one_use_challenges
FOR EACH ROW WHEN OLD.state IN ('consumed','expired','revoked') AND NEW.state<>OLD.state
BEGIN SELECT RAISE(ABORT,'terminal challenge cannot reactivate'); END;
CREATE TRIGGER fixture_lease_terminal BEFORE UPDATE OF state ON presence_leases
FOR EACH ROW WHEN OLD.state IN ('consumed','expired','revoked') AND NEW.state<>OLD.state
BEGIN SELECT RAISE(ABORT,'terminal lease cannot reactivate'); END;
CREATE TRIGGER fixture_lease_active_challenge_insert BEFORE INSERT ON presence_leases
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM one_use_challenges WHERE id=NEW.challenge_id AND state='active')
BEGIN SELECT RAISE(ABORT,'lease requires active challenge'); END;
CREATE TRIGGER fixture_lease_active_challenge_update BEFORE UPDATE OF challenge_id ON presence_leases
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM one_use_challenges WHERE id=NEW.challenge_id AND state='active')
BEGIN SELECT RAISE(ABORT,'lease requires active challenge'); END;
CREATE TRIGGER fixture_lease_identity_update BEFORE UPDATE OF challenge_id,web_session_id,browser_session_id,run_id,dispatch_id,service_instance_id,dashboard_instance_id,binding_json,authority_hmac ON presence_leases
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM one_use_challenges c WHERE c.id=NEW.challenge_id AND c.purpose='attended_dispatch' AND c.web_session_id=NEW.web_session_id AND c.run_id=NEW.run_id AND c.dispatch_id=NEW.dispatch_id AND c.service_instance_id=NEW.service_instance_id AND c.binding_json=NEW.binding_json AND c.authority_hmac=NEW.authority_hmac AND c.dashboard_instance_id IS NEW.dashboard_instance_id AND c.browser_session_id IS NEW.browser_session_id)
BEGIN SELECT RAISE(ABORT,'presence lease does not match challenge identity'); END;
CREATE TRIGGER fixture_secret_terminal BEFORE UPDATE OF state,field_resolution_id ON request_value_secrets
FOR EACH ROW WHEN (OLD.state<>'active' AND NEW.state='active') OR NEW.field_resolution_id<>OLD.field_resolution_id
BEGIN SELECT RAISE(ABORT,'terminal secret cannot reactivate or rebind'); END;
CREATE TRIGGER fixture_attempt_ownership_insert BEFORE INSERT ON attempts
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM runs WHERE id=NEW.run_id AND application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'attempt run does not belong to application'); END;
CREATE TRIGGER fixture_attempt_ownership_update BEFORE UPDATE OF application_id,run_id ON attempts
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM runs WHERE id=NEW.run_id AND application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'attempt run does not belong to application'); END;
CREATE TRIGGER fixture_dispatch_ownership_insert BEFORE INSERT ON dispatches
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM runs WHERE id=NEW.run_id AND application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'dispatch run does not belong to application'); END;
CREATE TRIGGER fixture_dispatch_ownership_update BEFORE UPDATE OF application_id,run_id ON dispatches
FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM runs WHERE id=NEW.run_id AND application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'dispatch run does not belong to application'); END;
CREATE TRIGGER fixture_checkpoint_ownership_insert BEFORE INSERT ON checkpoints
FOR EACH ROW WHEN NEW.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM runs WHERE id=NEW.run_id AND application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'checkpoint run does not belong to application'); END;
CREATE TRIGGER fixture_checkpoint_ownership_update BEFORE UPDATE OF application_id,run_id ON checkpoints
FOR EACH ROW WHEN NEW.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM runs WHERE id=NEW.run_id AND application_id=NEW.application_id)
BEGIN SELECT RAISE(ABORT,'checkpoint run does not belong to application'); END;
CREATE TRIGGER fixture_action_ownership_insert BEFORE INSERT ON actions
FOR EACH ROW WHEN NEW.attempt_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM attempts WHERE id=NEW.attempt_id AND run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'action attempt does not belong to run'); END;
CREATE TRIGGER fixture_action_ownership_update BEFORE UPDATE OF run_id,attempt_id ON actions
FOR EACH ROW WHEN NEW.attempt_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM attempts WHERE id=NEW.attempt_id AND run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'action attempt does not belong to run'); END;
CREATE TRIGGER fixture_status_ownership_insert BEFORE INSERT ON status_events
FOR EACH ROW WHEN NEW.application_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM applications WHERE id=NEW.application_id AND role_id=NEW.role_id)
BEGIN SELECT RAISE(ABORT,'status application does not belong to role'); END;
CREATE TRIGGER fixture_status_ownership_update BEFORE UPDATE OF role_id,application_id ON status_events
FOR EACH ROW WHEN NEW.application_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM applications WHERE id=NEW.application_id AND role_id=NEW.role_id)
BEGIN SELECT RAISE(ABORT,'status application does not belong to role'); END;
CREATE TRIGGER fixture_evidence_ownership_insert BEFORE INSERT ON evidence
FOR EACH ROW WHEN NEW.dispatch_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dispatches WHERE id=NEW.dispatch_id AND application_id IS NEW.application_id)
BEGIN SELECT RAISE(ABORT,'evidence dispatch does not belong to application'); END;
CREATE TRIGGER fixture_evidence_ownership_update BEFORE UPDATE OF application_id,dispatch_id ON evidence
FOR EACH ROW WHEN NEW.dispatch_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dispatches WHERE id=NEW.dispatch_id AND application_id IS NEW.application_id)
BEGIN SELECT RAISE(ABORT,'evidence dispatch does not belong to application'); END;
CREATE TRIGGER fixture_capture_source_insert BEFORE INSERT ON direct_edit_inputs
FOR EACH ROW WHEN NEW.source_kind NOT IN ('jobs_json','dashboard_html')
BEGIN SELECT RAISE(ABORT,'direct edit source kind is unsupported'); END;
CREATE TRIGGER fixture_capture_source_update BEFORE UPDATE OF source_kind ON direct_edit_inputs
FOR EACH ROW WHEN NEW.source_kind NOT IN ('jobs_json','dashboard_html')
BEGIN SELECT RAISE(ABORT,'direct edit source kind is unsupported'); END;
CREATE TRIGGER fixture_conflict_source_insert BEFORE INSERT ON reconciliation_conflicts
FOR EACH ROW WHEN NEW.direct_edit_input_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM direct_edit_inputs i WHERE i.id=NEW.direct_edit_input_id AND ((i.source_kind='jobs_json' AND NEW.kind='direct_json_status') OR (i.source_kind='dashboard_html' AND NEW.kind='static_mirror_edit')))
BEGIN SELECT RAISE(ABORT,'conflict kind does not match capture source'); END;
CREATE TRIGGER fixture_conflict_source_update BEFORE UPDATE OF kind,direct_edit_input_id ON reconciliation_conflicts
FOR EACH ROW WHEN NEW.direct_edit_input_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM direct_edit_inputs i WHERE i.id=NEW.direct_edit_input_id AND ((i.source_kind='jobs_json' AND NEW.kind='direct_json_status') OR (i.source_kind='dashboard_html' AND NEW.kind='static_mirror_edit')))
BEGIN SELECT RAISE(ABORT,'conflict kind does not match capture source'); END;
CREATE TABLE fixture_data_safety_guard (value INTEGER NOT NULL) STRICT;
CREATE TRIGGER fixture_data_safety_guard_check BEFORE INSERT ON fixture_data_safety_guard
FOR EACH ROW WHEN
 EXISTS (SELECT 1 FROM dispatches d LEFT JOIN runs r ON r.id=d.run_id WHERE r.application_id<>d.application_id)
 OR EXISTS (SELECT 1 FROM attempts a LEFT JOIN runs r ON r.id=a.run_id WHERE r.application_id<>a.application_id)
 OR EXISTS (SELECT 1 FROM checkpoints c LEFT JOIN runs r ON r.id=c.run_id WHERE c.run_id IS NOT NULL AND r.application_id<>c.application_id)
 OR EXISTS (SELECT 1 FROM daily_quota_reservations q LEFT JOIN dispatches d ON d.id=q.dispatch_id WHERE d.application_id<>q.application_id OR d.batch_policy_id<>q.policy_id OR d.transport NOT IN ('direct','aside'))
 OR EXISTS (SELECT 1 FROM presence_leases l LEFT JOIN one_use_challenges c ON c.id=l.challenge_id WHERE c.state<>'active')
 OR EXISTS (SELECT 1 FROM candidate_assertions a LEFT JOIN assertion_events e ON e.id=a.assertion_event_id WHERE e.profile_id<>a.profile_id OR e.assertion_id<>a.id)
 OR EXISTS (SELECT 1 FROM question_aliases a LEFT JOIN candidate_assertions x ON x.id=a.assertion_id LEFT JOIN assertion_events e ON e.id=a.confirmation_event_id WHERE x.profile_id<>a.profile_id OR e.profile_id<>a.profile_id OR e.assertion_id<>a.assertion_id OR e.event_kind<>'alias_confirmed')
 OR EXISTS (SELECT 1 FROM reconciliation_conflicts c LEFT JOIN direct_edit_inputs i ON i.id=c.direct_edit_input_id WHERE (c.kind IN ('direct_json_status','catalog_edit','static_mirror_edit') AND i.id IS NULL) OR (c.kind NOT IN ('direct_json_status','catalog_edit','static_mirror_edit') AND i.id IS NOT NULL) OR (i.id IS NOT NULL AND NOT ((i.source_kind='jobs_json' AND c.kind='direct_json_status') OR (i.source_kind='dashboard_html' AND c.kind='static_mirror_edit'))))
 OR EXISTS (SELECT 1 FROM batch_policies p LEFT JOIN candidate_assertions a ON a.id=p.assertion_snapshot_id LEFT JOIN assertion_events e ON e.id=p.candidate_confirmation_event_id LEFT JOIN kill_switches k ON k.id=p.global_kill_switch_id WHERE a.profile_id<>p.candidate_profile_id OR a.state<>'active' OR e.profile_id<>p.candidate_profile_id OR e.assertion_id<>p.assertion_snapshot_id OR e.event_kind<>'confirmed' OR k.scope_kind<>'global' OR k.scope_key<>'global')
 OR EXISTS (SELECT 1 FROM dispatches d LEFT JOIN batch_policies p ON p.id=d.batch_policy_id LEFT JOIN applications a ON a.id=d.application_id WHERE (d.transport IN ('direct','aside') AND d.batch_policy_id IS NULL) OR (d.transport NOT IN ('direct','aside') AND d.batch_policy_id IS NOT NULL) OR (d.batch_policy_id IS NOT NULL AND (p.state<>'active' OR p.candidate_profile_id<>a.profile_id)))
BEGIN SELECT RAISE(ABORT,'legacy rows violate fixture data safety invariants'); END;
INSERT INTO fixture_data_safety_guard VALUES(1);
DROP TRIGGER fixture_data_safety_guard_check;
DROP TABLE fixture_data_safety_guard;
