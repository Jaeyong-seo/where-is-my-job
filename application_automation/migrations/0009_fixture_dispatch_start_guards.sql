CREATE TRIGGER fixture_quota_reservation_identity_immutable
BEFORE UPDATE OF policy_id,local_date,application_id,dispatch_id ON daily_quota_reservations
FOR EACH ROW WHEN NEW.policy_id IS NOT OLD.policy_id
 OR NEW.local_date IS NOT OLD.local_date
 OR NEW.application_id IS NOT OLD.application_id
 OR NEW.dispatch_id IS NOT OLD.dispatch_id
BEGIN SELECT RAISE(ABORT,'quota reservation identity is immutable'); END;

CREATE TRIGGER fixture_unattended_dispatch_started_insert
BEFORE INSERT ON dispatches
FOR EACH ROW WHEN NEW.environment='fixture'
 AND NEW.transport IN ('direct','aside')
 AND NEW.started_at IS NOT NULL
BEGIN SELECT RAISE(ABORT,'unattended fixture dispatch must start from an intent'); END;

CREATE TRIGGER fixture_unattended_dispatch_start_authority
BEFORE UPDATE OF started_at ON dispatches
FOR EACH ROW WHEN NEW.environment='fixture'
 AND NEW.transport IN ('direct','aside')
 AND OLD.started_at IS NULL
 AND NEW.started_at IS NOT NULL
 AND NOT EXISTS (
   SELECT 1
   FROM daily_quota_reservations q
   JOIN batch_policies p ON p.id=q.policy_id
   JOIN applications a ON a.id=NEW.application_id
   JOIN kill_switches k ON k.id=p.global_kill_switch_id
   JOIN capabilities c ON c.id=NEW.fixture_capability_id
   WHERE q.policy_id=NEW.batch_policy_id
     AND q.application_id=NEW.application_id
     AND q.dispatch_id=NEW.id
     AND q.state IN ('reserved','consumed')
     AND p.id=NEW.batch_policy_id
     AND p.state='active'
     AND p.environment='fixture'
     AND p.candidate_profile_id=a.profile_id
     AND julianday(p.valid_from)<=julianday('now')
     AND julianday(p.expires_at)>julianday('now')
     AND p.fixture_capability_id=c.id
     AND p.fixture_adapter_id=NEW.fixture_adapter_id
     AND p.fixture_origin=NEW.fixture_origin
     AND k.scope_kind='global'
     AND k.scope_key='global'
     AND k.state='closed'
     AND c.environment='fixture'
     AND c.state='active'
     AND c.adapter_id=NEW.fixture_adapter_id
     AND c.origin=NEW.fixture_origin
     AND c.form_fingerprint=NEW.form_fingerprint
     AND julianday(c.expires_at)>julianday('now')
 )
BEGIN SELECT RAISE(ABORT,'unattended fixture dispatch requires fresh matching authority'); END;

CREATE TRIGGER fixture_run_application_identity_immutable
BEFORE UPDATE OF application_id ON runs
FOR EACH ROW WHEN NEW.application_id IS NOT OLD.application_id
 AND (
   EXISTS (SELECT 1 FROM attempts WHERE run_id=OLD.id)
   OR EXISTS (SELECT 1 FROM actions WHERE run_id=OLD.id)
   OR EXISTS (SELECT 1 FROM dispatches WHERE run_id=OLD.id)
   OR EXISTS (SELECT 1 FROM checkpoints WHERE run_id=OLD.id)
 )
BEGIN SELECT RAISE(ABORT,'run application is immutable after child binding'); END;
