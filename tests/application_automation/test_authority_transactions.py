from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
import json
import sqlite3
import threading

import pytest

from application_automation.authority import (
    AliasReassignmentRequest,
    AuthorityError,
    ChallengeConsumptionRequest,
    RequestValueSecretRequest,
    _alias_event_payload,
    challenge_secret_hmac,
    consume_fresh_challenge,
    create_or_replace_request_value_secret,
    reassign_alias,
)
from application_automation.crypto import canonical_json, domain_hmac
from application_automation.store import apply_migrations, connect


NOW = datetime(2026, 7, 15, tzinfo=UTC)
HMAC_KEY = b"h" * 32
ENC_KEY = b"e" * 32
CLOCK = lambda: NOW


def db(tmp_path):
    connection = connect(tmp_path / "authority.sqlite3")
    apply_migrations(connection)
    now = NOW.isoformat()
    connection.execute("INSERT INTO candidate_profiles VALUES('profile','Fixture','active',?,1)", (now,))
    connection.execute("INSERT INTO roles VALUES('role','fixture:role','Fixture','Engineer','https://fixture.invalid','fixture',8,'materials_ready','x',1,?,?)", (now, now))
    connection.execute("INSERT INTO applications VALUES('app','profile','role','fixture:app','awaiting_user',1,?,?)", (now, now))
    connection.execute("INSERT INTO runs(id,application_id,command_id,state,revision,created_at) VALUES('run','app',NULL,'awaiting_user',1,?)", (now,))
    connection.execute("INSERT INTO checkpoints VALUES('checkpoint','app','run','unknown_question','open',1,?,NULL,1)", (now,))
    connection.execute("INSERT INTO field_resolutions VALUES('resolution','checkpoint',1,'field','unresolved',NULL,?,NULL,1)", (now,))
    connection.execute("INSERT INTO sessions VALUES('session','profile','fixture-service','dashboard','active',?,?,1)", (now, (NOW + timedelta(hours=2)).isoformat()))
    for name in ('old', 'new'):
        connection.execute("INSERT INTO assertion_events VALUES(?, 'profile', ?, 'confirmed', 'x', ?, 1, NULL)", (f'event-{name}', f'assertion-{name}', now))
        connection.execute("INSERT INTO candidate_assertions VALUES(?, 'profile', ?, ?, 'x', 'active', ?, NULL, 1, ?)", (f'assertion-{name}', f'event-{name}', name, now, now))
        if name == 'old':
            payload = _alias_event_payload(
                event_id='alias-event-old', event_kind='alias_confirmed', created_at=now,
                profile_id='profile', profile_revision=1,
                assertion_id='assertion-old', assertion_revision=1, assertion_value_hmac='x',
                alias_id='alias', alias_revision=1, provider='fixture', normalized_label='label',
                semantic_scope='scope', form_fingerprint='form',
                confirmation_event_id='alias-event-old', confirmation_event_revision=1,
                source_alias_id=None, authorization_binding=None,
            )
            connection.execute(
                "INSERT INTO assertion_events VALUES(?, 'profile', ?, 'alias_confirmed', ?, ?, 1, ?)",
                (
                    'alias-event-old', 'assertion-old',
                    domain_hmac(HMAC_KEY, 'authority.alias.event.v3', payload),
                    now,
                    canonical_json(payload).decode(),
                ),
            )
        else:
            connection.execute(
                "INSERT INTO assertion_events VALUES(?, 'profile', ?, 'alias_confirmed', 'x', ?, 1, NULL)",
                (f'alias-event-{name}', f'assertion-{name}', now),
            )
    connection.execute("INSERT INTO question_aliases VALUES('alias','profile','assertion-old','alias-event-old','fixture','label','scope','form',NULL,1,?)", (now,))
    connection.execute("INSERT INTO kill_switches VALUES('switch','global','global','closed','fixture',?,1)", (now,))
    connection.execute(
        "INSERT INTO batch_policies(id,candidate_profile_id,policy_version,state,scope_json,min_fit_score,timezone,daily_cap,provider_form_allowlist_json,assertion_snapshot_id,material_policy_json,checkpoint_classes_json,valid_from,expires_at,global_kill_switch_id,signature_hmac,key_version,candidate_confirmation_event_id,revision,created_at) VALUES('policy','profile',1,'active','{}',8,'America/Vancouver',1,'[]','assertion-old','{}','[]',?,?,'switch','x',1,'event-old',1,?)",
        (now, (NOW + timedelta(hours=1)).isoformat(), now),
    )
    connection.execute("INSERT INTO dispatches(id,application_id,run_id,transport,state,batch_policy_id,authority_hmac,form_fingerprint,started_at,finished_at,revision,created_at) VALUES('owner-dispatch','app','run','manual','intent',NULL,NULL,'fixture',NULL,NULL,1,?)", (now,))
    return connection


def challenge(connection, identifier='challenge', *, secret='token', purpose='manual_completion', alias_id=None, binding=None, field_resolution_id='resolution', expires_at=NOW + timedelta(hours=1)):
    if binding is None:
        binding = {'checkpoint_revision': 1, 'surface': 'fixture'}
    connection.execute(
        "INSERT INTO one_use_challenges(id,purpose,secret_hmac,key_version,candidate_profile_id,assertion_id,alias_id,checkpoint_id,field_resolution_id,run_id,dispatch_id,batch_policy_id,web_session_id,browser_session_id,service_instance_id,dashboard_instance_id,form_signature,authority_hmac,binding_json,state,created_at,expires_at,consumed_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (identifier, purpose, challenge_secret_hmac(HMAC_KEY, secret), 1, 'profile' if purpose == 'alias_reassign' else None, None, alias_id, 'checkpoint' if purpose == 'manual_completion' else None, field_resolution_id if purpose == 'manual_completion' else None, None, None, None, 'session', None, 'fixture-service', 'dashboard', None, None, canonical_json(binding).decode(), 'active', NOW.isoformat(), expires_at.isoformat(), None),
    )


def manual_binding(connection, plaintext, ttl=timedelta(minutes=30), *, resolution_id='resolution', run_ceiling=NOW + timedelta(hours=1), policy_ceiling=NOW + timedelta(hours=1), challenge_ceiling=None):
    checkpoint = connection.execute("SELECT * FROM checkpoints WHERE id='checkpoint'").fetchone()
    resolution = connection.execute("SELECT * FROM field_resolutions WHERE id=?", (resolution_id,)).fetchone()
    run = connection.execute("SELECT * FROM runs WHERE id='run'").fetchone()
    policy = connection.execute("SELECT * FROM batch_policies WHERE id='policy'").fetchone()
    application = connection.execute("SELECT * FROM applications WHERE id=?", (checkpoint['application_id'],)).fetchone()
    profile = connection.execute("SELECT * FROM candidate_profiles WHERE id=?", (application['profile_id'],)).fetchone()
    session = connection.execute("SELECT * FROM sessions WHERE id='session'").fetchone()
    if challenge_ceiling is None:
        challenge_ceiling = run_ceiling
    return {
        'purpose': 'manual_completion',
        'checkpoint_id': checkpoint['id'],
        'checkpoint_revision': checkpoint['revision'],
        'checkpoint_generation': checkpoint['generation'],
        'field_resolution_id': resolution['id'],
        'resolution_revision': resolution['revision'],
        'resolution_generation': resolution['generation'],
        'field_key': resolution['field_key'],
        'run_id': run['id'],
        'run_revision': run['revision'],
        'run_state': run['state'],
        'batch_policy_id': policy['id'],
        'batch_policy_revision': policy['revision'],
        'batch_policy_state': policy['state'],
        'batch_policy_expires_at': policy['expires_at'],
        'candidate_profile_id': profile['id'],
        'candidate_profile_revision': profile['revision'],
        'candidate_profile_state': profile['state'],
        'application_profile_id': application['profile_id'],
        'application_role_id': application['role_id'],
        'application_revision': application['revision'],
        'application_state': application['state'],
        'web_session_id': session['id'],
        'web_session_profile_id': session['profile_id'],
        'web_session_revision': session['revision'],
        'web_session_state': session['state'],
        'web_session_expires_at': session['expires_at'],
        'service_instance_id': session['service_instance_id'],
        'dashboard_instance_id': session['dashboard_instance_id'],
        'application_id': application['id'],
        'value_digest': domain_hmac(HMAC_KEY, 'authority.manual.value_digest.v1', {'value': plaintext.hex()}),
        'ttl_seconds': int(ttl.total_seconds()),
        'expires_at_ceilings': {
            'challenge': challenge_ceiling.isoformat(),
            'policy': policy_ceiling.isoformat(),
            'run': run_ceiling.isoformat(),
            'persisted_policy': policy['expires_at'],
            'session': session['expires_at'],
        },
    }


def secret_request(binding, plaintext, *, resolution_id='resolution', field_key='field', challenge_id='challenge', secret='token', checkpoint_revision=1, resolution_revision=1, challenge_revision=1, ttl=None, run_ceiling=NOW + timedelta(hours=1), policy_ceiling=NOW + timedelta(hours=1)):
    return RequestValueSecretRequest('checkpoint', resolution_id, field_key, plaintext, checkpoint_revision, resolution_revision, 1, challenge_id, secret, binding, challenge_revision, ttl, run_ceiling, policy_ceiling)


def alias_binding(connection, *, alias_id='alias', old_assertion_id='assertion-old', new_assertion_id='assertion-new'):
    profile = connection.execute("SELECT * FROM candidate_profiles WHERE id='profile'").fetchone()
    alias = connection.execute("SELECT * FROM question_aliases WHERE id=?", (alias_id,)).fetchone()
    old_assertion = connection.execute("SELECT * FROM candidate_assertions WHERE id=?", (old_assertion_id,)).fetchone()
    new_assertion = connection.execute("SELECT * FROM candidate_assertions WHERE id=?", (new_assertion_id,)).fetchone()
    session = connection.execute("SELECT * FROM sessions WHERE id='session'").fetchone()
    return {
        'purpose': 'alias_reassign',
        'profile_id': profile['id'],
        'profile_revision': profile['revision'],
        'alias_id': alias['id'],
        'alias_revision': alias['revision'],
        'provider': alias['provider'],
        'normalized_label': alias['normalized_label'],
        'semantic_scope': alias['semantic_scope'],
        'form_fingerprint': alias['form_fingerprint'],
        'old_assertion_id': old_assertion['id'],
        'old_assertion_revision': old_assertion['revision'],
        'old_value_hmac': old_assertion['value_hmac'],
        'new_assertion_id': new_assertion['id'],
        'new_assertion_revision': new_assertion['revision'],
        'new_value_hmac': new_assertion['value_hmac'],
        'candidate_profile_id': profile['id'],
        'web_session_id': session['id'],
        'web_session_profile_id': session['profile_id'],
        'web_session_revision': session['revision'],
        'web_session_state': session['state'],
        'web_session_expires_at': session['expires_at'],
        'service_instance_id': session['service_instance_id'],
        'dashboard_instance_id': session['dashboard_instance_id'],
    }


def authority_state(connection):
    tables = {
        'profile': "SELECT id,state,revision FROM candidate_profiles ORDER BY id",
        'aliases': "SELECT id,assertion_id,revoked_at,revision FROM question_aliases ORDER BY id",
        'assertions': "SELECT id,state,confirmed_at,revoked_at,revision FROM candidate_assertions ORDER BY id",
        'events': "SELECT id,assertion_id,event_kind,payload_hmac,created_at,revision FROM assertion_events ORDER BY id",
        'challenges': "SELECT id,purpose,secret_hmac,candidate_profile_id,assertion_id,alias_id,checkpoint_id,field_resolution_id,run_id,dispatch_id,batch_policy_id,web_session_id,browser_session_id,service_instance_id,dashboard_instance_id,binding_json,state,expires_at,consumed_at,revision FROM one_use_challenges ORDER BY id",
        'checkpoints': "SELECT id,generation,state,revision FROM checkpoints ORDER BY id",
        'resolutions': "SELECT id,generation,state,revision FROM field_resolutions ORDER BY id",
        'secrets': "SELECT id,field_resolution_id,ciphertext,nonce,value_hmac,state,destroyed_at,tombstone_hmac,revision FROM request_value_secrets ORDER BY id",
        'dispatches': "SELECT id,state,authority_hmac,started_at,revision FROM dispatches ORDER BY id",
        'sessions': "SELECT id,profile_id,service_instance_id,dashboard_instance_id,state,created_at,expires_at,revision FROM sessions ORDER BY id",
        'applications': "SELECT id,profile_id,role_id,state,revision FROM applications ORDER BY id",
        'runs': "SELECT id,application_id,state,preflight_hmac,revision FROM runs ORDER BY id",
        'batch_policies': "SELECT id,candidate_profile_id,state,revision FROM batch_policies ORDER BY id",
    }
    return {name: [tuple(row) for row in connection.execute(query)] for name, query in tables.items()}


def reopen(connection, tmp_path):
    connection.close()
    reopened = connect(tmp_path / 'authority.sqlite3')
    apply_migrations(reopened)
    return reopened
CANONICAL_MANUAL_CLAIMS = {
    'purpose': 'manual_completion',
    'checkpoint_id': 'checkpoint',
    'checkpoint_revision': 1,
    'checkpoint_generation': 1,
    'field_resolution_id': 'resolution',
    'resolution_revision': 1,
    'resolution_generation': 1,
    'field_key': 'field',
    'run_id': 'run',
    'run_revision': 1,
    'run_state': 'awaiting_user',
    'batch_policy_id': 'policy',
    'batch_policy_revision': 1,
    'batch_policy_state': 'active',
    'batch_policy_expires_at': (NOW + timedelta(hours=1)).isoformat(),
    'candidate_profile_id': 'profile',
    'candidate_profile_revision': 1,
    'candidate_profile_state': 'active',
    'application_profile_id': 'profile',
    'application_role_id': 'role',
    'application_revision': 1,
    'application_state': 'awaiting_user',
    'web_session_id': 'session',
    'web_session_profile_id': 'profile',
    'web_session_revision': 1,
    'web_session_state': 'active',
    'web_session_expires_at': (NOW + timedelta(hours=2)).isoformat(),
    'service_instance_id': 'fixture-service',
    'dashboard_instance_id': 'dashboard',
    'application_id': 'app',
    'value_digest': domain_hmac(HMAC_KEY, 'authority.manual.value_digest.v1', {'value': '6f6e65'}),
    'ttl_seconds': 1800,
    'expires_at_ceilings': {
        'challenge': (NOW + timedelta(hours=1)).isoformat(),
        'policy': (NOW + timedelta(hours=1)).isoformat(),
        'run': (NOW + timedelta(hours=1)).isoformat(),
        'persisted_policy': (NOW + timedelta(hours=1)).isoformat(),
        'session': (NOW + timedelta(hours=2)).isoformat(),
    },
}

CANONICAL_ALIAS_CLAIMS = {
    'purpose': 'alias_reassign',
    'profile_id': 'profile',
    'profile_revision': 1,
    'alias_id': 'alias',
    'alias_revision': 1,
    'provider': 'fixture',
    'normalized_label': 'label',
    'semantic_scope': 'scope',
    'form_fingerprint': 'form',
    'old_assertion_id': 'assertion-old',
    'old_assertion_revision': 1,
    'old_value_hmac': 'x',
    'new_assertion_id': 'assertion-new',
    'new_assertion_revision': 1,
    'new_value_hmac': 'x',
    'candidate_profile_id': 'profile',
    'web_session_id': 'session',
    'web_session_profile_id': 'profile',
    'web_session_revision': 1,
    'web_session_state': 'active',
    'web_session_expires_at': (NOW + timedelta(hours=2)).isoformat(),
    'service_instance_id': 'fixture-service',
    'dashboard_instance_id': 'dashboard',
}


def altered_claims(claims, claim):
    changed = json.loads(canonical_json(claims))
    value = changed[claim]
    if isinstance(value, int):
        changed[claim] = value + 1
    elif isinstance(value, dict):
        changed[claim] = {**value, 'run': (NOW + timedelta(minutes=59)).isoformat()}
    else:
        changed[claim] = f'wrong-{value}'
    return changed


def test_fixture_authority_bindings_match_independent_literal_claim_sets(tmp_path):
    connection = db(tmp_path)
    assert manual_binding(connection, b'one') == CANONICAL_MANUAL_CLAIMS
    assert alias_binding(connection) == CANONICAL_ALIAS_CLAIMS


@pytest.mark.parametrize('claim', tuple(CANONICAL_MANUAL_CLAIMS))
def test_manual_binding_rejects_each_independently_mismatched_claim_without_mutation(tmp_path, claim):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'one')
    challenge(connection, binding=binding)
    before = authority_state(connection)

    with pytest.raises(AuthorityError):
        create_or_replace_request_value_secret(
            connection,
            secret_request(altered_claims(binding, claim), b'one'),
            encryption_key=ENC_KEY,
            hmac_key=HMAC_KEY,
            trusted_clock=CLOCK,
        )

    assert authority_state(connection) == before


@pytest.mark.parametrize('claim', tuple(CANONICAL_ALIAS_CLAIMS))
def test_alias_binding_rejects_each_independently_mismatched_claim_without_mutation(tmp_path, claim):
    connection = db(tmp_path)
    binding = alias_binding(connection)
    challenge(connection, purpose='alias_reassign', alias_id='alias', binding=binding)
    before = authority_state(connection)

    with pytest.raises(AuthorityError):
        reassign_alias(
            connection,
            AliasReassignmentRequest(
                'challenge', 'token', altered_claims(binding, claim), 1, 'profile', 'alias', 1,
                'assertion-old', 1, 'assertion-new', 1,
            ),
            hmac_key=HMAC_KEY,
            trusted_clock=CLOCK,
        )

    assert authority_state(connection) == before


@pytest.mark.parametrize(
    ('axis', 'request_kwargs', 'challenge_column', 'challenge_value'),
    [
        ('secret', {'secret': 'wrong-token'}, None, None),
        ('purpose', {}, 'purpose', 'alias_reassign'),
        ('candidate', {}, 'candidate_profile_id', 'profile'),
        ('checkpoint', {}, 'checkpoint_id', 'wrong-checkpoint'),
        ('resolution', {}, 'field_resolution_id', 'wrong-resolution'),
        ('field', {'field_key': 'wrong-field'}, None, None),
        ('generation', {}, 'checkpoint_id', None),
        ('checkpoint_revision', {'checkpoint_revision': 2}, None, None),
        ('resolution_revision', {'resolution_revision': 2}, None, None),
        ('challenge_revision', {'challenge_revision': 2}, None, None),
        ('session', {}, None, None),
        ('session_revision', {}, None, None),
        ('session_expiry', {}, None, None),
        ('service', {}, None, None),
        ('dashboard', {}, None, None),
        ('ttl', {'ttl': timedelta(minutes=5)}, None, None),
        ('expiry', {}, None, None),
    ],
)
def test_manual_authority_axes_reject_without_mutation(tmp_path, axis, request_kwargs, challenge_column, challenge_value):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'one')
    challenge(connection, binding=binding)
    if axis == 'generation':
        connection.execute("UPDATE checkpoints SET generation=2 WHERE id='checkpoint'")
    elif axis == 'session':
        connection.execute("UPDATE sessions SET state='revoked' WHERE id='session'")
    elif axis == 'session_revision':
        connection.execute("UPDATE sessions SET revision=2 WHERE id='session'")
    elif axis == 'session_expiry':
        connection.execute("UPDATE sessions SET expires_at=? WHERE id='session'", ((NOW + timedelta(minutes=45)).isoformat(),))
    elif axis in {'service', 'dashboard'}:
        connection.execute(f"UPDATE sessions SET {axis}_instance_id=? WHERE id='session'", (f'wrong-{axis}',))
    elif axis == 'expiry':
        connection.execute("UPDATE one_use_challenges SET expires_at=? WHERE id='challenge'", (NOW.isoformat(),))
    elif challenge_column is not None:
        before = authority_state(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE one_use_challenges SET {challenge_column}=? WHERE id='challenge'", (challenge_value,))
        assert authority_state(connection) == before
        return
    before = authority_state(connection)

    with pytest.raises(AuthorityError):
        create_or_replace_request_value_secret(
            connection,
            secret_request(binding, b'one', **request_kwargs),
            encryption_key=ENC_KEY,
            hmac_key=HMAC_KEY,
            trusted_clock=CLOCK,
        )

    assert authority_state(connection) == before


@pytest.mark.parametrize(
    ('axis', 'request_values', 'challenge_column', 'challenge_value'),
    [
        ('secret', {'secret': 'wrong-token'}, None, None),
        ('purpose', {}, 'purpose', 'manual_completion'),
        ('candidate', {'profile_id': 'wrong-profile'}, None, None),
        ('alias', {'old_alias_id': 'wrong-alias'}, None, None),
        ('old_assertion', {'old_assertion_id': 'assertion-new'}, None, None),
        ('new_assertion', {'new_assertion_id': 'assertion-old'}, None, None),
        ('profile_revision', {}, None, None),
        ('alias_revision', {'old_alias_revision': 2}, None, None),
        ('old_assertion_revision', {'old_assertion_revision': 2}, None, None),
        ('new_assertion_revision', {'new_assertion_revision': 2}, None, None),
        ('challenge_revision', {'challenge_revision': 2}, None, None),
        ('session', {}, None, None),
        ('session_revision', {}, None, None),
        ('session_expiry', {}, None, None),
        ('service', {}, None, None),
        ('dashboard', {}, None, None),
        ('expiry', {}, None, None),
    ],
)
def test_alias_authority_axes_reject_without_mutation(tmp_path, axis, request_values, challenge_column, challenge_value):
    connection = db(tmp_path)
    binding = alias_binding(connection)
    challenge(connection, purpose='alias_reassign', alias_id='alias', binding=binding)
    if axis == 'profile_revision':
        connection.execute("UPDATE candidate_profiles SET revision=2 WHERE id='profile'")
    elif axis == 'session':
        connection.execute("UPDATE sessions SET state='revoked' WHERE id='session'")
    elif axis == 'session_revision':
        connection.execute("UPDATE sessions SET revision=2 WHERE id='session'")
    elif axis == 'session_expiry':
        connection.execute("UPDATE sessions SET expires_at=? WHERE id='session'", ((NOW + timedelta(minutes=45)).isoformat(),))
    elif axis in {'service', 'dashboard'}:
        connection.execute(f"UPDATE sessions SET {axis}_instance_id=? WHERE id='session'", (f'wrong-{axis}',))
    elif axis == 'expiry':
        connection.execute("UPDATE one_use_challenges SET expires_at=? WHERE id='challenge'", (NOW.isoformat(),))
    elif challenge_column is not None:
        before = authority_state(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE one_use_challenges SET {challenge_column}=? WHERE id='challenge'", (challenge_value,))
        assert authority_state(connection) == before
        return
    before = authority_state(connection)
    values = {
        'secret': 'token',
        'profile_id': 'profile',
        'old_alias_id': 'alias',
        'old_alias_revision': 1,
        'old_assertion_id': 'assertion-old',
        'old_assertion_revision': 1,
        'new_assertion_id': 'assertion-new',
        'new_assertion_revision': 1,
        'challenge_revision': 1,
    }
    values.update(request_values)

    with pytest.raises(AuthorityError):
        reassign_alias(
            connection,
            AliasReassignmentRequest(
                'challenge', values['secret'], binding, values['challenge_revision'],
                values['profile_id'], values['old_alias_id'], values['old_alias_revision'],
                values['old_assertion_id'], values['old_assertion_revision'],
                values['new_assertion_id'], values['new_assertion_revision'],
            ),
            hmac_key=HMAC_KEY,
            trusted_clock=CLOCK,
        )

    assert authority_state(connection) == before


def test_challenge_race_is_bounded_durable_and_replay_safe(tmp_path):
    connection = db(tmp_path)
    challenge(connection)
    barrier = threading.Barrier(2, timeout=2)
    outcomes, setup_errors = [], []
    lock = threading.Lock()

    def consume():
        writer = None
        try:
            writer = connect(tmp_path / 'authority.sqlite3')
            barrier.wait()
            result = consume_fresh_challenge(writer, ChallengeConsumptionRequest('challenge', 'token', {'checkpoint_revision': 1, 'surface': 'fixture'}, 1), hmac_key=HMAC_KEY, trusted_clock=CLOCK)
            outcome = ('consumed', result.revision, result.consumed_at)
        except (sqlite3.Error, threading.BrokenBarrierError) as exc:
            with lock:
                setup_errors.append(exc)
            return
        except AuthorityError as exc:
            outcome = ('rejected', str(exc))
        finally:
            if writer is not None:
                writer.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=consume, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert not any(thread.is_alive() for thread in threads)
    assert not setup_errors
    assert sorted(item[0] for item in outcomes) == ['consumed', 'rejected']
    assert next(item for item in outcomes if item[0] == 'consumed') == ('consumed', 2, NOW)
    assert next(item for item in outcomes if item[0] == 'rejected')[1] in {'challenge is stale or unavailable', 'challenge consumption raced'}
    connection = reopen(connection, tmp_path)
    before = authority_state(connection)
    assert connection.execute("SELECT state,consumed_at,revision FROM one_use_challenges WHERE id='challenge'").fetchone()[:] == ('consumed', NOW.isoformat(), 2)
    with pytest.raises(AuthorityError, match='challenge is stale or unavailable'):
        consume_fresh_challenge(connection, ChallengeConsumptionRequest('challenge', 'token', {'checkpoint_revision': 1, 'surface': 'fixture'}, 1), hmac_key=HMAC_KEY, trusted_clock=CLOCK)
    assert authority_state(connection) == before


@pytest.mark.parametrize('axis', ['alias', 'old_assertion', 'new_assertion'])
def test_alias_reassignment_rejects_each_stale_revision_without_mutation(tmp_path, axis):
    connection = db(tmp_path)
    binding = alias_binding(connection)
    challenge(connection, purpose='alias_reassign', alias_id='alias', binding=binding)
    before = authority_state(connection)
    revisions = {'alias': 1, 'old_assertion': 1, 'new_assertion': 1}
    revisions[axis] = 2
    request = AliasReassignmentRequest('challenge', 'token', binding, 1, 'profile', 'alias', revisions['alias'], 'assertion-old', revisions['old_assertion'], 'assertion-new', revisions['new_assertion'])
    with pytest.raises(AuthorityError, match='alias reassignment is stale'):
        reassign_alias(connection, request, hmac_key=HMAC_KEY, trusted_clock=CLOCK)
    assert authority_state(connection) == before


def test_alias_reassignment_rotates_complete_event_and_snapshot_authority(tmp_path):
    connection = db(tmp_path)
    binding = alias_binding(connection)
    challenge(connection, purpose='alias_reassign', alias_id='alias', binding=binding)
    result = reassign_alias(connection, AliasReassignmentRequest('challenge', 'token', binding, 1, 'profile', 'alias', 1, 'assertion-old', 1, 'assertion-new', 1), hmac_key=HMAC_KEY, trusted_clock=CLOCK)
    events = connection.execute("SELECT id,assertion_id,event_kind,payload_hmac,created_at,revision FROM assertion_events WHERE id IN (?,?) ORDER BY event_kind", (result.confirm_event_id, result.revoke_event_id)).fetchall()
    assert [(row['assertion_id'], row['event_kind'], row['created_at'], row['revision']) for row in events] == [('assertion-new', 'alias_confirmed', NOW.isoformat(), 1), ('assertion-old', 'alias_revoked', NOW.isoformat(), 1)]
    assert all(row['payload_hmac'] and len(row['payload_hmac']) == 64 for row in events)
    assert result.assertion_snapshot_id == 'profile' and result.assertion_snapshot_revision == 2
    assert connection.execute("SELECT revision FROM candidate_profiles WHERE id='profile'").fetchone()[0] == 2
    assert connection.execute("SELECT state,consumed_at,revision FROM one_use_challenges WHERE id='challenge'").fetchone()[:] == ('consumed', NOW.isoformat(), 2)



def test_alias_reassignment_verifies_two_hops_after_reopen(tmp_path):
    connection = db(tmp_path)
    binding = alias_binding(connection)
    challenge(connection, purpose='alias_reassign', alias_id='alias', binding=binding)
    first = reassign_alias(
        connection,
        AliasReassignmentRequest('challenge', 'token', binding, 1, 'profile', 'alias', 1, 'assertion-old', 1, 'assertion-new', 1),
        hmac_key=HMAC_KEY, trusted_clock=CLOCK,
    )
    connection = reopen(connection, tmp_path)
    now = NOW.isoformat()
    connection.execute("INSERT INTO assertion_events VALUES('event-third', 'profile', 'assertion-third', 'confirmed', 'x', ?, 1, NULL)", (now,))
    connection.execute("INSERT INTO candidate_assertions VALUES('assertion-third', 'profile', 'event-third', 'third', 'x', 'active', ?, NULL, 1, ?)", (now, now))
    second_binding = alias_binding(connection, alias_id=first.alias_id, old_assertion_id='assertion-new', new_assertion_id='assertion-third')
    challenge(connection, 'challenge-2', purpose='alias_reassign', alias_id=first.alias_id, binding=second_binding)
    second = reassign_alias(
        connection,
        AliasReassignmentRequest('challenge-2', 'token', second_binding, 1, 'profile', first.alias_id, 1, 'assertion-new', 2, 'assertion-third', 1),
        hmac_key=HMAC_KEY, trusted_clock=CLOCK,
    )
    connection = reopen(connection, tmp_path)
    assert connection.execute("SELECT revoked_at FROM question_aliases WHERE id=?", (first.alias_id,)).fetchone()[0] is not None
    surviving = connection.execute("SELECT id,assertion_id,revoked_at,revision FROM question_aliases WHERE id=?", (second.alias_id,)).fetchone()
    assert tuple(surviving) == (second.alias_id, 'assertion-third', None, 1)
    assert connection.execute("SELECT revision FROM candidate_profiles WHERE id='profile'").fetchone()[0] == 3


@pytest.mark.parametrize('axis', ['checkpoint', 'resolution', 'challenge'])
def test_secret_request_rejects_each_stale_revision_with_complete_snapshot(tmp_path, axis):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'one')
    challenge(connection, binding=binding)
    before = authority_state(connection)
    revisions = {'checkpoint': 1, 'resolution': 1, 'challenge': 1}
    revisions[axis] = 2
    with pytest.raises(AuthorityError, match='stale|unavailable'):
        create_or_replace_request_value_secret(connection, secret_request(binding, b'one', checkpoint_revision=revisions['checkpoint'], resolution_revision=revisions['resolution'], challenge_revision=revisions['challenge']), encryption_key=ENC_KEY, hmac_key=HMAC_KEY, trusted_clock=CLOCK)
    assert authority_state(connection) == before


def test_secret_replacement_authenticates_metadata_and_invalidates_only_unstarted_dispatches(tmp_path):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'one')
    challenge(connection, binding=binding)
    first = create_or_replace_request_value_secret(connection, secret_request(binding, b'one'), encryption_key=ENC_KEY, hmac_key=HMAC_KEY, trusted_clock=CLOCK)
    now = NOW.isoformat()
    connection.execute("INSERT INTO runs(id,application_id,command_id,state,revision,created_at) VALUES('run-unstarted','app',NULL,'queued',1,?)", (now,))
    connection.execute("INSERT INTO dispatches(id,application_id,run_id,transport,state,batch_policy_id,authority_hmac,form_fingerprint,started_at,finished_at,revision,created_at) VALUES('unstarted-a','app','run-unstarted','manual','intent',NULL,'old-authority-a','fixture',NULL,NULL,1,?)", (now,))
    connection.execute("INSERT INTO dispatches(id,application_id,run_id,transport,state,batch_policy_id,authority_hmac,form_fingerprint,started_at,finished_at,revision,created_at) VALUES('unstarted-b','app','run-unstarted','manual','intent',NULL,'old-authority-b','fixture',NULL,NULL,1,?)", (now,))
    replacement_binding = manual_binding(connection, b'two', timedelta(minutes=5))
    challenge(connection, 'replacement', secret='replacement-token', binding=replacement_binding)
    second = create_or_replace_request_value_secret(connection, secret_request(replacement_binding, b'two', challenge_id='replacement', secret='replacement-token', ttl=timedelta(minutes=5)), encryption_key=ENC_KEY, hmac_key=HMAC_KEY, trusted_clock=CLOCK)
    old = connection.execute("SELECT ciphertext,nonce,value_hmac,state,destroyed_at,tombstone_hmac,revision FROM request_value_secrets WHERE id=?", (first.secret_id,)).fetchone()
    assert tuple(old[:4]) == (None, None, None, 'tombstoned')
    assert old['destroyed_at'] == NOW.isoformat() and old['tombstone_hmac'] and len(old['tombstone_hmac']) == 64 and old['revision'] == 2
    new = connection.execute("SELECT field_resolution_id,ciphertext,nonce,value_hmac,key_version,state,expires_at,destroyed_at,tombstone_hmac,revision FROM request_value_secrets WHERE id=?", (second.secret_id,)).fetchone()
    envelope = json.loads(new['ciphertext'])
    metadata = envelope['metadata']
    assert tuple(new[0:2]) == (second.field_resolution_id, new['ciphertext'])
    assert new['nonce'] and new['value_hmac'] and len(new['value_hmac']) == 64 and new['key_version'] == 1
    assert new['state'] == 'active' and new['destroyed_at'] is None and new['tombstone_hmac'] is None and new['revision'] == 1
    expected_metadata = {
        'secret_id': second.secret_id,
        'field_resolution_id': second.field_resolution_id,
        'generation': 2,
        'checkpoint_id': 'checkpoint',
        'checkpoint_revision': 2,
        'resolution_revision': 1,
        'source_checkpoint_revision': 1,
        'source_resolution_revision': 1,
        'challenge_id': 'replacement',
        'challenge_revision': 1,
        'run_id': 'run',
        'run_revision': 1,
        'run_state': 'awaiting_user',
        'batch_policy_id': 'policy',
        'batch_policy_revision': 1,
        'batch_policy_state': 'active',
        'batch_policy_expires_at': (NOW + timedelta(hours=1)).isoformat(),
        'key_version': 1,
        'expires_at': (NOW + timedelta(minutes=5)).isoformat(),
        'run_expires_at': (NOW + timedelta(hours=1)).isoformat(),
        'policy_expires_at': (NOW + timedelta(hours=1)).isoformat(),
        'value_digest': domain_hmac(HMAC_KEY, 'authority.manual.value_digest.v1', {'value': b'two'.hex()}),
    }
    assert metadata == expected_metadata
    assert new['value_hmac'] == domain_hmac(HMAC_KEY, 'authority.request.metadata.v2', expected_metadata)
    ciphertext_bytes = bytes(new['ciphertext'])
    assert b'two' not in ciphertext_bytes and b'dHdv' not in ciphertext_bytes and b'74776f' not in ciphertext_bytes
    assert [tuple(row) for row in connection.execute("SELECT id,state,authority_hmac,revision FROM dispatches WHERE id IN ('unstarted-a','unstarted-b') ORDER BY id")] == [('unstarted-a', 'rejected', None, 2), ('unstarted-b', 'rejected', None, 2)]
    assert connection.execute("SELECT COUNT(*) FROM dispatches WHERE application_id='app' AND started_at IS NULL AND authority_hmac IS NOT NULL").fetchone()[0] == 0
    connection = reopen(connection, tmp_path)
    assert connection.execute("SELECT state,tombstone_hmac FROM request_value_secrets WHERE id=?", (first.secret_id,)).fetchone()[0] == 'tombstoned'
    durable = connection.execute("SELECT ciphertext,nonce,value_hmac,state FROM request_value_secrets WHERE id=?", (second.secret_id,)).fetchone()
    assert all(durable[index] for index in range(3)) and durable[3] == 'active'


def test_started_dispatch_blocks_secret_replacement_without_mutation(tmp_path):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'one')
    challenge(connection, binding=binding)
    create_or_replace_request_value_secret(
        connection,
        secret_request(binding, b'one'),
        encryption_key=ENC_KEY,
        hmac_key=HMAC_KEY,
        trusted_clock=CLOCK,
    )
    now = NOW.isoformat()
    connection.execute(
        "INSERT INTO runs(id,application_id,command_id,state,revision,created_at) "
        "VALUES('run-started','app',NULL,'dispatching',1,?)",
        (now,),
    )
    connection.execute(
        "INSERT INTO dispatches(id,application_id,run_id,transport,state,batch_policy_id,"
        "authority_hmac,form_fingerprint,started_at,finished_at,revision,created_at) "
        "VALUES('started','app','run-started','manual','dispatching',NULL,"
        "'started-authority','fixture',?,NULL,1,?)",
        (now, now),
    )
    replacement_binding = manual_binding(connection, b'two', timedelta(minutes=5))
    challenge(
        connection,
        'replacement',
        secret='replacement-token',
        binding=replacement_binding,
    )
    before = authority_state(connection)

    with pytest.raises(AuthorityError, match='started dispatch requires manual follow-up'):
        create_or_replace_request_value_secret(
            connection,
            secret_request(
                replacement_binding,
                b'two',
                challenge_id='replacement',
                secret='replacement-token',
                ttl=timedelta(minutes=5),
            ),
            encryption_key=ENC_KEY,
            hmac_key=HMAC_KEY,
            trusted_clock=CLOCK,
        )

    assert authority_state(connection) == before
    assert connection.execute(
        "SELECT state,authority_hmac,revision FROM dispatches WHERE id='started'"
    ).fetchone()[:] == ('dispatching', 'started-authority', 1)


@pytest.mark.parametrize('ceiling_name, ceiling', [('run', NOW + timedelta(minutes=10)), ('policy', NOW + timedelta(minutes=15)), ('challenge', NOW + timedelta(minutes=20))])
def test_request_secret_uses_minimum_trusted_authority_ceiling(tmp_path, ceiling_name, ceiling):
    connection = db(tmp_path)
    ceilings = {'run': NOW + timedelta(minutes=30), 'policy': NOW + timedelta(minutes=30), 'challenge': NOW + timedelta(minutes=30)}
    ceilings[ceiling_name] = ceiling
    binding = manual_binding(connection, b'value', timedelta(hours=1), run_ceiling=ceilings['run'], policy_ceiling=ceilings['policy'], challenge_ceiling=ceilings['challenge'])
    challenge(connection, binding=binding, expires_at=ceilings['challenge'])
    result = create_or_replace_request_value_secret(connection, secret_request(binding, b'value', ttl=timedelta(hours=1), run_ceiling=ceilings['run'], policy_ceiling=ceilings['policy']), encryption_key=ENC_KEY, hmac_key=HMAC_KEY, trusted_clock=CLOCK)
    assert result.expires_at == ceiling


@pytest.mark.parametrize('clock, reason', [(lambda: datetime(2026, 7, 15), 'timestamps must be timezone-aware'), (lambda: NOW + timedelta(hours=2), 'request value authority has expired')])
def test_naive_or_stale_trusted_clock_cannot_create_or_revive_authority(tmp_path, clock, reason):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'value')
    challenge(connection, binding=binding)
    before = authority_state(connection)
    with pytest.raises(AuthorityError, match=reason):
        create_or_replace_request_value_secret(connection, secret_request(binding, b'value'), encryption_key=ENC_KEY, hmac_key=HMAC_KEY, trusted_clock=clock)
    assert authority_state(connection) == before


def test_missing_or_expired_run_policy_ceilings_fail_without_mutation(tmp_path):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'value')
    challenge(connection, binding=binding)
    before = authority_state(connection)
    for kwargs, reason in [
        ({'run_ceiling': None}, 'run and policy expiry ceilings are required'),
        ({'policy_ceiling': None}, 'run and policy expiry ceilings are required'),
        ({'run_ceiling': datetime(2026, 7, 15)}, 'timestamps must be timezone-aware'),
        ({'policy_ceiling': datetime(2026, 7, 15)}, 'timestamps must be timezone-aware'),
        ({'run_ceiling': NOW}, 'request value expiry ceilings do not match persisted authority'),
        ({'policy_ceiling': NOW}, 'request value expiry ceilings do not match persisted authority'),
    ]:
        with pytest.raises(AuthorityError, match=reason):
            create_or_replace_request_value_secret(connection, secret_request(binding, b'value', **kwargs), encryption_key=ENC_KEY, hmac_key=HMAC_KEY, trusted_clock=CLOCK)
        assert authority_state(connection) == before


@pytest.mark.parametrize('axis', ['run_id', 'dispatch_id', 'web_session_id', 'dashboard_instance_id', 'service_instance_id'])
def test_presence_lease_rejects_each_identity_axis_independently(tmp_path, axis):
    connection = db(tmp_path)
    now, expires = NOW.isoformat(), (NOW + timedelta(minutes=5)).isoformat()
    connection.execute("INSERT INTO runs(id,application_id,command_id,state,revision,created_at) VALUES('other-run','app',NULL,'queued',1,?)", (now,))
    for identifier, run_id in [('dispatch', 'run'), ('other-dispatch', 'other-run')]:
        connection.execute("INSERT INTO dispatches(id,application_id,run_id,transport,state,batch_policy_id,authority_hmac,form_fingerprint,started_at,finished_at,revision,created_at) VALUES(?,?,?,'manual','intent',NULL,NULL,'fixture',NULL,NULL,1,?)", (identifier, 'app', run_id, now))
    connection.execute("INSERT INTO sessions VALUES('other-session','profile','other-service','other-dashboard','active',?,?,1)", (now, expires))
    binding = canonical_json({'surface': 'fixture'}).decode()
    connection.execute("INSERT INTO one_use_challenges(id,purpose,secret_hmac,key_version,run_id,dispatch_id,web_session_id,service_instance_id,dashboard_instance_id,binding_json,authority_hmac,state,created_at,expires_at,revision) VALUES('attended','attended_dispatch',?,1,'run','dispatch','session','fixture-service','dashboard',?,'authority','active',?,?,1)", (challenge_secret_hmac(HMAC_KEY, 'attended-token'), binding, now, expires))
    identity = {'run_id': 'run', 'dispatch_id': 'dispatch', 'web_session_id': 'session', 'service_instance_id': 'fixture-service', 'dashboard_instance_id': 'dashboard'}
    identity[axis] = {'run_id': 'other-run', 'dispatch_id': 'other-dispatch', 'web_session_id': 'other-session', 'service_instance_id': 'other-service', 'dashboard_instance_id': 'other-dashboard'}[axis]
    with pytest.raises(sqlite3.IntegrityError, match='does not match'):
        connection.execute("INSERT INTO presence_leases(id,challenge_id,web_session_id,run_id,dispatch_id,service_instance_id,dashboard_instance_id,binding_json,binding_hmac,authority_hmac,state,created_at,expires_at,revision) VALUES(?,?,?,?,?,?,?,?,?,'authority','active',?,?,1)", ('lease', 'attended', identity['web_session_id'], identity['run_id'], identity['dispatch_id'], identity['service_instance_id'], identity['dashboard_instance_id'], binding, 'binding', now, expires))
    assert connection.execute("SELECT COUNT(*) FROM presence_leases").fetchone()[0] == 0
class PseudoNaiveTimezone(tzinfo):
    def utcoffset(self, value):
        return None

    def dst(self, value):
        return None


@pytest.mark.parametrize(
    ('column', 'value'),
    [('normalized_label', 'tampered-label'), ('semantic_scope', 'tampered-scope')],
)
def test_tampered_durable_alias_is_rejected_without_mutation(tmp_path, column, value):
    connection = db(tmp_path)
    binding = alias_binding(connection)
    challenge(connection, purpose='alias_reassign', alias_id='alias', binding=binding)
    connection.execute(f"UPDATE question_aliases SET {column}=? WHERE id='alias'", (value,))
    before = authority_state(connection)

    with pytest.raises(AuthorityError, match='durable alias confirmation does not match'):
        reassign_alias(
            connection,
            AliasReassignmentRequest('challenge', 'token', binding, 1, 'profile', 'alias', 1,
                                     'assertion-old', 1, 'assertion-new', 1),
            hmac_key=HMAC_KEY,
            trusted_clock=CLOCK,
        )

    assert authority_state(connection) == before


@pytest.mark.parametrize('mutation', [
    "UPDATE runs SET revision=revision+1 WHERE id='run'",
    "UPDATE batch_policies SET state='revoked', revision=revision+1 WHERE id='policy'",
    "UPDATE candidate_profiles SET revision=revision+1 WHERE id='profile'",
    "UPDATE candidate_profiles SET state='revoked', revision=revision+1 WHERE id='profile'",
    "UPDATE applications SET revision=revision+1 WHERE id='app'",
    "UPDATE applications SET state='manual_followup', revision=revision+1 WHERE id='app'",
])
def test_manual_secret_rejects_rotated_or_revoked_owner_without_mutation(tmp_path, mutation):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'value')
    challenge(connection, binding=binding)
    connection.execute(mutation)
    before = authority_state(connection)

    with pytest.raises(AuthorityError):
        create_or_replace_request_value_secret(
            connection, secret_request(binding, b'value'), encryption_key=ENC_KEY,
            hmac_key=HMAC_KEY, trusted_clock=CLOCK,
        )

    assert authority_state(connection) == before


def test_fractional_ttl_and_pseudo_naive_clock_fail_closed_without_mutation(tmp_path):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'value')
    challenge(connection, binding=binding)
    before = authority_state(connection)

    with pytest.raises(AuthorityError, match='whole seconds'):
        create_or_replace_request_value_secret(
            connection, secret_request(binding, b'value', ttl=timedelta(seconds=1, microseconds=1)),
            encryption_key=ENC_KEY, hmac_key=HMAC_KEY, trusted_clock=CLOCK,
        )
    with pytest.raises(AuthorityError, match='timezone-aware'):
        consume_fresh_challenge(
            connection, ChallengeConsumptionRequest('challenge', 'token', binding, 1),
            hmac_key=HMAC_KEY,
            trusted_clock=lambda: datetime(2026, 7, 15, tzinfo=PseudoNaiveTimezone()),
        )

    assert authority_state(connection) == before

def test_alias_reassignment_late_abort_at_final_profile_update_rolls_back_completely(tmp_path):
    connection = db(tmp_path)
    connection.execute(
        "CREATE TRIGGER late_abort_alias_profile_update BEFORE UPDATE OF revision ON candidate_profiles "
        "FOR EACH ROW WHEN NEW.id='profile' AND NEW.revision=OLD.revision+1 "
        "BEGIN SELECT RAISE(ABORT,'late abort after alias/challenge update'); END;"
    )
    binding = alias_binding(connection)
    challenge(connection, purpose='alias_reassign', alias_id='alias', binding=binding)
    before = authority_state(connection)

    with pytest.raises(sqlite3.IntegrityError, match='late abort after alias/challenge update'):
        reassign_alias(
            connection,
            AliasReassignmentRequest('challenge', 'token', binding, 1, 'profile', 'alias', 1, 'assertion-old', 1, 'assertion-new', 1),
            hmac_key=HMAC_KEY, trusted_clock=CLOCK,
        )

    assert authority_state(connection) == before
    connection = reopen(connection, tmp_path)
    assert authority_state(connection) == before


def test_secret_replacement_late_abort_after_tombstone_rolls_back_completely(tmp_path):
    connection = db(tmp_path)
    binding = manual_binding(connection, b'one')
    challenge(connection, binding=binding)
    create_or_replace_request_value_secret(
        connection, secret_request(binding, b'one'), encryption_key=ENC_KEY, hmac_key=HMAC_KEY, trusted_clock=CLOCK,
    )
    replacement_binding = manual_binding(connection, b'two', timedelta(minutes=5))
    challenge(connection, 'replacement', secret='replacement-token', binding=replacement_binding)
    connection.execute(
        "CREATE TRIGGER late_abort_after_tombstone BEFORE UPDATE OF state ON field_resolutions "
        "FOR EACH ROW WHEN NEW.state='expired_request' "
        "BEGIN SELECT RAISE(ABORT,'late abort after tombstone'); END;"
    )
    before = authority_state(connection)

    with pytest.raises(sqlite3.IntegrityError, match='late abort after tombstone'):
        create_or_replace_request_value_secret(
            connection,
            secret_request(replacement_binding, b'two', challenge_id='replacement', secret='replacement-token', ttl=timedelta(minutes=5)),
            encryption_key=ENC_KEY, hmac_key=HMAC_KEY, trusted_clock=CLOCK,
        )

    assert authority_state(connection) == before
    connection = reopen(connection, tmp_path)
    assert authority_state(connection) == before
