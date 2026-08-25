from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from application_automation.store import MIGRATIONS_DIR, apply_migrations, connect


NOW = "2026-07-15T00:00:00+00:00"
_MIGRATION_MANIFEST = [
    ("0001_core.sql", "80e448e2ea727e2d62333ad7be41de40ec89e588bdc1f3f4b266a8322dc27b6c"),
    ("0006_batch_policy.sql", "9708825030dbb5f8c5c0cb7908e7260d99173a746ca2e760121a063d601963fa"),
    ("0007_critic5_relational_closure.sql", "9f3c20a6d169242a39ebc5097df1d5c90c294317589353b0251bed5379daa7d2"),
    ("0008_fixture_safety_hardening.sql", "268d4b72abbcf46a67d8013963742a758ffd8c77926c43e6a4f1a8e4dd8c6ee3"),
    ("0009_fixture_dispatch_start_guards.sql", "677c6f41199838ea4549561c9ff87bf81dfc028fc5533c3d62dcf36d4f0f0793"),
    ("0010_relational_authority_closure.sql", "60d44ad1a655adb9d4abca92370571ff1beb5c264206d710dec43ecf784ec88e"),
]


def migrated(tmp_path: Path) -> sqlite3.Connection:
    database = connect(tmp_path / "automation.sqlite3")
    apply_migrations(database)
    apply_migrations(database)
    return database


def seed_profile_assertions(database: sqlite3.Connection) -> None:
    database.execute(
        "INSERT INTO candidate_profiles VALUES(?,?,?,?,?)",
        ("profile", "Candidate", "active", NOW, 1),
    )
    for index in (1, 2):
        database.execute(
            "INSERT INTO assertion_events("
            "id,profile_id,assertion_id,event_kind,payload_hmac,created_at,revision"
            ") VALUES(?,?,?,?,?,?,?)",
            (f"event-{index}", "profile", f"assertion-{index}", "confirmed", f"hmac-{index}", NOW, 1),
        )
        database.execute(
            "INSERT INTO candidate_assertions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"assertion-{index}",
                "profile",
                f"event-{index}",
                f"semantic-{index}",
                f"value-{index}",
                "active",
                NOW,
                None,
                1,
                NOW,
            ),
        )
        database.execute(
            "INSERT INTO assertion_events("
            "id,profile_id,assertion_id,event_kind,payload_hmac,created_at,revision"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                f"alias-event-{index}",
                "profile",
                f"assertion-{index}",
                "alias_confirmed",
                f"alias-hmac-{index}",
                NOW,
                1,
            ),
        )


def seed_fixture_dispatch_intent(database: sqlite3.Connection) -> None:
    seed_profile_assertions(database)
    database.execute(
        "INSERT INTO roles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("role", "fixture:role", "Fixture", "Engineer", None, None, 8.0, "reviewing", "hmac", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
        ("application", "profile", "role", "identity", "draft", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO roles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("other-role", "fixture:other-role", "Fixture", "Other Engineer", None, None, 8.0, "reviewing", "other-hmac", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
        ("other-application", "profile", "other-role", "other-identity", "draft", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run", "application", None, "queued", None, None, None, None, None, 1, NOW),
    )
    database.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("other-run", "other-application", None, "queued", None, None, None, None, None, 1, NOW),
    )
    database.execute(
        "INSERT INTO kill_switches VALUES(?,?,?,?,?,?,?)",
        ("kill", "global", "global", "closed", "test", NOW, 1),
    )
    database.execute(
        "INSERT INTO kill_switches VALUES(?,?,?,?,?,?,?)",
        ("provider-kill", "provider", "fixture", "closed", "test", NOW, 1),
    )
    breaker_columns = {
        row["name"] for row in database.execute("PRAGMA table_info(breakers)")
    }
    if "tenant" in breaker_columns:
        database.execute(
            "INSERT INTO breakers(id,provider,tenant,state,reason,opened_at,revision) "
            "VALUES(?,?,?,?,?,?,?)",
            ("breaker", "fixture", "tenant", "closed", "test", None, 1),
        )
    else:
        database.execute(
            "INSERT INTO breakers(id,provider,state,reason,opened_at,revision) "
            "VALUES(?,?,?,?,?,?)",
            ("breaker", "fixture", "closed", "test", None, 1),
        )
    database.execute(
        """
        INSERT INTO capabilities(
            id, provider, tenant, operation, transport, form_fingerprint, state,
            expires_at, capability_json, revision, created_at, environment,
            adapter_id, origin
        ) VALUES(
            'capability', 'fixture', 'tenant', 'submit', 'aside', 'form', 'active',
            strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+1 hour'), '{}', 1, ?, 'fixture',
            'fixture-aside-v1', 'fixture.local'
        )
        """,
        (NOW,),
    )
    database.execute(
        """
        INSERT INTO batch_policies(
            id, candidate_profile_id, policy_version, state, scope_json, min_fit_score,
            timezone, daily_cap, provider_form_allowlist_json, assertion_snapshot_id,
            material_policy_json, checkpoint_classes_json, valid_from, expires_at,
            global_kill_switch_id, signature_hmac, key_version,
            candidate_confirmation_event_id, revision, created_at, environment,
            fixture_adapter_id, fixture_origin, fixture_capability_id
        ) VALUES(
            'policy', 'profile', 1, 'active', '{}', 8.0, 'America/Vancouver', 2,
            '[]', 'assertion-1', '{}', '[]',
            strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 hour'),
            strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+1 hour'),
            'kill', 'policy-hmac', 1, 'event-1', 1, ?,
            'fixture', 'fixture-aside-v1', 'fixture.local', 'capability'
        )
        """,
        (NOW,),
    )
    database.execute(
        """
        INSERT INTO dispatches(
            id, application_id, run_id, transport, state, batch_policy_id,
            authority_hmac, form_fingerprint, started_at, finished_at, revision,
            created_at, environment, fixture_adapter_id, fixture_origin,
            fixture_capability_id
        ) VALUES(
            'dispatch', 'application', 'run', 'aside', 'intent', 'policy',
            'intent-hmac', 'form', NULL, NULL, 1, ?, 'fixture',
            'fixture-aside-v1', 'fixture.local', 'capability'
        )
        """,
        (NOW,),
    )


def reserve_fixture_dispatch_quota(database: sqlite3.Connection) -> None:
    database.execute(
        """
        INSERT INTO daily_quota_reservations(
            id, policy_id, local_date, application_id, dispatch_id, state,
            created_at, consumed_at, revision
        ) VALUES(
            'quota', 'policy', application_automation_policy_local_date('America/Vancouver'), 'application', 'dispatch', 'reserved',
            ?, NULL, 1
        )
        """,
        (NOW,),
    )

def seed_prepared_fixture_outcome(database: sqlite3.Connection) -> None:
    database.execute(
        "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
        ("session", "profile", "service", None, "active", NOW, "2099-01-01T00:00:00+00:00", 1),
    )
    database.execute(
        """
        INSERT INTO fixture_dispatch_outcomes(
            dispatch_id, application_id, provider, tenant, account_hmac, context_hmac,
            session_id, session_hmac, run_id, intent_hmac, payload_sha256,
            page_fingerprint, form_fingerprint, resume_sha256, state, receipt_digest,
            attestation_digest, observed_intent_hmac, prepared_at, started_at,
            confirmed_at, terminal_at, revision
        ) VALUES(
            'dispatch', 'application', 'fixture', 'fixture', ?, ?, 'session', ?,
            'run', ?, ?, 'page', 'form', ?, 'prepared', NULL, NULL, NULL, ?,
            NULL, NULL, NULL, 1
        )
        """,
        ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64, NOW),
    )



def test_migrations_are_repeatable_and_relationally_clean(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    tables = {
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required = {
        "applications",
        "dispatches",
        "batch_policies",
        "daily_quota_reservations",
        "one_use_challenges",
        "presence_leases",
        "direct_edit_inputs",
        "reconciliation_conflicts",
        "field_resolutions",
        "request_value_secrets",
    }
    assert required <= tables
    assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    assert database.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
        _MIGRATION_MANIFEST
    )
    assert [tuple(row) for row in database.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    )] == _MIGRATION_MANIFEST


def test_upgrade_from_0007_matches_fresh_schema_and_preserves_history(tmp_path: Path) -> None:
    fresh = migrated(tmp_path / "fresh")
    upgraded = connect(tmp_path / "upgraded.sqlite3")
    for version, _ in _MIGRATION_MANIFEST[:3]:
        upgraded.executescript((MIGRATIONS_DIR / version).read_text(encoding="utf-8"))
    upgraded.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, checksum TEXT) STRICT"
    )
    upgraded.executemany(
        "INSERT INTO schema_migrations(version,checksum) VALUES(?,?)",
        _MIGRATION_MANIFEST[:3],
    )
    apply_migrations(upgraded)
    objects = "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','index','trigger') AND name NOT LIKE 'sqlite_%' AND name<>'schema_migrations' ORDER BY type,name"
    assert [tuple(row) for row in upgraded.execute(objects)] == [tuple(row) for row in fresh.execute(objects)]
    assert [tuple(row) for row in upgraded.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    )] == _MIGRATION_MANIFEST
    assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []


def test_active_alias_is_unique_for_candidate_provider_and_semantics(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_profile_assertions(database)
    database.execute(
        "INSERT INTO question_aliases VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "alias-1",
            "profile",
            "assertion-1",
            "alias-event-1",
            "fixture",
            "will you require sponsorship",
            "work.sponsorship_now_or_future",
            "form-1",
            None,
            1,
            NOW,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO question_aliases VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "alias-2",
                "profile",
                "assertion-2",
                "alias-event-2",
                "fixture",
                "will you require sponsorship",
                "work.sponsorship_now_or_future",
                "form-2",
                None,
                1,
                NOW,
            ),
        )


def test_checkpoint_generation_preserves_replacement_history(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_profile_assertions(database)
    database.execute(
        "INSERT INTO roles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "role",
            "fixture:role",
            "Fixture",
            "Engineer",
            "https://fixture.invalid",
            "applications/fixture/role",
            8.0,
            "materials_ready",
            "posting-hmac",
            1,
            NOW,
            NOW,
        ),
    )
    database.execute(
        "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
        ("application", "profile", "role", "fixture:role", "awaiting_user", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
        ("checkpoint", "application", None, "unknown_question", "open", 1, NOW, None, 1),
    )
    database.execute(
        "INSERT INTO field_resolutions VALUES(?,?,?,?,?,?,?,?,?)",
        ("resolution-1", "checkpoint", 1, "field", "expired_request", None, NOW, NOW, 1),
    )
    database.execute(
        "INSERT INTO field_resolutions VALUES(?,?,?,?,?,?,?,?,?)",
        ("resolution-2", "checkpoint", 2, "field", "unresolved", None, NOW, None, 1),
    )
    expected_history = [
        (1, "field", "expired_request", NOW),
        (2, "field", "unresolved", NOW),
    ]
    assert [tuple(row) for row in database.execute(
        "SELECT generation,field_key,state,created_at FROM field_resolutions "
        "WHERE checkpoint_id='checkpoint' ORDER BY generation"
    )] == expected_history
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO field_resolutions VALUES(?,?,?,?,?,?,?,?,?)",
            ("duplicate", "checkpoint", 2, "field", "unresolved", None, NOW, None, 1),
        )
    assert [tuple(row) for row in database.execute(
        "SELECT generation,field_key,state,created_at FROM field_resolutions "
        "WHERE checkpoint_id='checkpoint' ORDER BY generation"
    )] == expected_history
    database.close()
    reopened = connect(tmp_path / "automation.sqlite3")
    assert [tuple(row) for row in reopened.execute(
        "SELECT generation,field_key,state,created_at FROM field_resolutions "
        "WHERE checkpoint_id='checkpoint' ORDER BY generation"
    )] == expected_history
    reopened.close()


def test_direct_edit_conflicts_enforce_cardinality_state_and_resolution(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    database.execute(
        "INSERT INTO roles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("role", "fixture:role", "Fixture", "Engineer", None, None, 8.0, "reviewing", "hmac", 1, NOW, NOW),
    )
    with pytest.raises(sqlite3.IntegrityError, match="cardinality"):
        database.execute(
            "INSERT INTO reconciliation_conflicts(id,role_id,kind,state,detail_json,created_at,revision) "
            "VALUES('missing-capture',NULL,'static_mirror_edit','open','{}',?,1)",
            (NOW,),
        )
    database.execute(
        "INSERT INTO direct_edit_inputs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "capture",
            "jobs_json",
            "captures/dashboard.html",
            "a" * 64,
            None,
            None,
            None,
            NOW,
            "retained_open",
            None,
            1,
        ),
    )
    database.execute(
        "INSERT INTO direct_edit_inputs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "dashboard-capture",
            "dashboard_html",
            "captures/dashboard-2.html",
            "b" * 64,
            None,
            None,
            None,
            NOW,
            "retained_open",
            None,
            1,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="cardinality"):
        database.execute(
            "INSERT INTO reconciliation_conflicts VALUES(?,?,'static_mirror_edit','open','{}',?,1,?,NULL,NULL)",
            ("static-with-role", "role", NOW, "dashboard-capture"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cardinality"):
        database.execute(
            "INSERT INTO reconciliation_conflicts VALUES(?,NULL,'direct_json_status','open','{}',?,1,?,NULL,NULL)",
            ("direct-without-role", NOW, "capture"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="conflict kind does not match capture source"):
        database.execute(
            "INSERT INTO reconciliation_conflicts VALUES(?,NULL,'static_mirror_edit','open','{}',?,1,?,NULL,NULL)",
            ("source-kind-mismatch", NOW, "capture"),
        )
    database.execute(
        "INSERT INTO reconciliation_conflicts VALUES(?,?,'direct_json_status','open','{}',?,1,?,NULL,NULL)",
        ("conflict", "role", NOW, "capture"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute("UPDATE reconciliation_conflicts SET state='pending' WHERE id='conflict'")
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "UPDATE reconciliation_conflicts SET resolution_event_id='missing' WHERE id='conflict'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "UPDATE reconciliation_conflicts SET resolution_payload_json='not-json' WHERE id='conflict'"
        )
    database.execute(
        "INSERT INTO status_events VALUES(?,?,?,?,?,?,?)",
        ("resolution", "role", None, "direct_edit", '{"source":"capture"}', NOW, 1),
    )
    database.execute(
        "UPDATE reconciliation_conflicts SET state='resolved', resolution_event_id=?, "
        "resolution_payload_json=? WHERE id='conflict'",
        ("resolution", '{"outcome":"applied"}'),
    )
    assert tuple(database.execute(
        "SELECT id, source_kind, capture_relative_path, raw_sha256, state, revision "
        "FROM direct_edit_inputs WHERE id='capture'"
    ).fetchone()) == (
        "capture",
        "jobs_json",
        "captures/dashboard.html",
        "a" * 64,
        "retained_open",
        1,
    )
    assert tuple(database.execute(
        "SELECT id, role_id, kind, state, detail_json, direct_edit_input_id, resolution_event_id, "
        "resolution_payload_json, revision FROM reconciliation_conflicts WHERE id='conflict'"
    ).fetchone()) == (
        "conflict",
        "role",
        "direct_json_status",
        "resolved",
        "{}",
        "capture",
        "resolution",
        '{"outcome":"applied"}',
        1,
    )


def test_explicit_legacy_0007_checksum_upgrades_only_to_pinned_target(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    legacy = "9a79f204a93750225264e556c209d07abdd8099bf9a51ad05019514e96e98e3e"
    database.execute(
        "UPDATE schema_migrations SET checksum=? "
        "WHERE version='0007_critic5_relational_closure.sql'",
        (legacy,),
    )

    apply_migrations(database)

    assert database.execute(
        "SELECT checksum FROM schema_migrations "
        "WHERE version='0007_critic5_relational_closure.sql'"
    ).fetchone()[0] == _MIGRATION_MANIFEST[2][1]
    assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []

@pytest.mark.parametrize(
    ("version", "tampered_checksum"),
    [
        *[(version, "0" * 64) for version, _ in _MIGRATION_MANIFEST],
        ("0007_critic5_relational_closure.sql", "f" * 64),
    ],
)
def test_existing_database_rejects_historical_migration_checksum_change(
    tmp_path: Path,
    version: str,
    tampered_checksum: str,
) -> None:
    database = migrated(tmp_path)
    database.execute(
        "UPDATE schema_migrations SET checksum=? WHERE version=?",
        (tampered_checksum, version),
    )
    history_before = [
        tuple(row)
        for row in database.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
        )
    ]
    schema_before = [
        tuple(row)
        for row in database.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
    ]

    with pytest.raises(RuntimeError, match=f"migration checksum mismatch: {version}"):
        apply_migrations(database)

    assert [
        tuple(row)
        for row in database.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
        )
    ] == history_before
    assert [
        tuple(row)
        for row in database.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
    ] == schema_before

def test_migrations_serialize_concurrent_rechecks(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    initial = connect(path)
    apply_migrations(initial)
    initial.close()
    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def migrate() -> None:
        database = None
        try:
            database = connect(path)
            barrier.wait(timeout=2)
            apply_migrations(database)
            result: object = None
        except BaseException as error:
            result = error
        finally:
            if database is not None:
                database.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=migrate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=7)
    assert not any(thread.is_alive() for thread in threads)
    assert outcomes == [None, None]
    database = connect(path)
    apply_migrations(database)
    assert [tuple(row) for row in database.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    )] == _MIGRATION_MANIFEST
    assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "column, value",
    [
        ("local_date", "date('now', '+1 day')"),
        ("application_id", "'other-application'"),
    ],
)
def test_fixture_quota_reservation_identity_and_date_cannot_be_rebound(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    database = migrated(tmp_path)
    seed_fixture_dispatch_intent(database)
    reserve_fixture_dispatch_quota(database)
    reservation_before = tuple(
        database.execute(
            "SELECT policy_id, local_date, application_id, dispatch_id "
            "FROM daily_quota_reservations WHERE id='quota'"
        ).fetchone()
    )

    with pytest.raises(sqlite3.IntegrityError, match="quota reservation identity is immutable"):
        database.execute(
            f"UPDATE daily_quota_reservations SET {column}={value} WHERE id='quota'"
        )

    assert tuple(
        database.execute(
            "SELECT policy_id, local_date, application_id, dispatch_id "
            "FROM daily_quota_reservations WHERE id='quota'"
        ).fetchone()
    ) == reservation_before


def test_fixture_dispatch_start_requires_intent_quota_and_current_authority(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_fixture_dispatch_intent(database)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="aside dispatch must be inserted as an unstarted intent",
    ):
        database.execute(
            """
            INSERT INTO dispatches(
                id, application_id, run_id, transport, state, batch_policy_id,
                authority_hmac, form_fingerprint, started_at, finished_at, revision,
                created_at, environment, fixture_adapter_id, fixture_origin,
                fixture_capability_id
            ) VALUES(
                'started-dispatch', 'application', 'run', 'aside', 'dispatching',
                'policy', 'intent-hmac', 'form', ?, NULL, 1, ?, 'fixture',
                'fixture-aside-v1', 'fixture.local', 'capability'
            )
            """,
            (NOW, NOW),
        )
    assert database.execute(
        "SELECT COUNT(*) FROM dispatches WHERE id='started-dispatch'"
    ).fetchone()[0] == 0
    reserve_fixture_dispatch_quota(database)
    seed_prepared_fixture_outcome(database)


    with pytest.raises(
        sqlite3.IntegrityError,
        match="aside dispatch must start by transitioning intent to dispatching",
    ):
        database.execute(
            "UPDATE dispatches SET state='confirmed', started_at=? WHERE id='dispatch'",
            (NOW,),
        )
    assert database.execute(
        "SELECT started_at FROM dispatches WHERE id='dispatch'"
    ).fetchone()[0] is None

    database.execute("UPDATE capabilities SET state='revoked' WHERE id='capability'")
    with pytest.raises(
        sqlite3.IntegrityError,
        match="unattended fixture dispatch requires fresh matching authority",
    ):
        database.execute("UPDATE dispatches SET state='dispatching', started_at=? WHERE id='dispatch'", (NOW,))
    assert database.execute(
        "SELECT started_at FROM dispatches WHERE id='dispatch'"
    ).fetchone()[0] is None

    database.execute("UPDATE capabilities SET state='active' WHERE id='capability'")
    database.execute(
        "UPDATE dispatches SET state='dispatching', started_at=? WHERE id='dispatch'",
        (NOW,),
    )
    assert tuple(
        database.execute(
            "SELECT started_at, state FROM dispatches WHERE id='dispatch'"
        ).fetchone()
    ) == (NOW, "dispatching")
    assert tuple(
        database.execute(
            "SELECT state, consumed_at FROM daily_quota_reservations WHERE id='quota'"
        ).fetchone()
    ) == ("reserved", None)


def test_run_application_cannot_change_after_dispatch_child_exists(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_fixture_dispatch_intent(database)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="run owner and command are immutable after child binding",
    ):
        database.execute(
            "UPDATE runs SET application_id='other-application' WHERE id='run'"
        )

    assert database.execute(
        "SELECT application_id FROM runs WHERE id='run'"
    ).fetchone()[0] == "application"

def test_foreign_key_and_relational_invariants_fail_closed(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO candidate_assertions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "orphan",
                "missing-profile",
                "missing-event",
                "semantic",
                "value",
                "staged",
                None,
                None,
                1,
                NOW,
            ),
        )

    seed_profile_assertions(database)
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO candidate_assertions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "duplicate-active",
                "profile",
                "event-1",
                "semantic-1",
                "other-value",
                "active",
                NOW,
                None,
                2,
                NOW,
            ),
        )
    database.execute(
        "INSERT INTO roles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("role", "fixture:role", "Fixture", "Engineer", None, None, 8.0, "reviewing", "hmac", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
        ("application", "profile", "role", "identity", "draft", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run", "application", None, "queued", None, None, None, None, None, 1, NOW),
    )
    with pytest.raises(sqlite3.IntegrityError, match="side effect"):
        database.execute(
            "INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("action", "run", None, "submit", "fill", "planned", None, NOW, None, 1),
        )
def test_staged_assertion_uses_staged_event_then_confirmation_provenance(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    database.execute(
        "INSERT INTO candidate_profiles VALUES(?,?,?,?,?)",
        ("profile", "Candidate", "active", NOW, 1),
    )
    database.execute(
        "INSERT INTO assertion_events("
        "id,profile_id,assertion_id,event_kind,payload_hmac,created_at,revision"
        ") VALUES(?,?,?,?,?,?,?)",
        ("staged-event", "profile", "assertion", "staged", "staged-hmac", NOW, 1),
    )
    database.execute(
        "INSERT INTO candidate_assertions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "assertion",
            "profile",
            "staged-event",
            "semantic",
            "value",
            "staged",
            None,
            None,
            1,
            NOW,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="confirmation provenance"):
        database.execute(
            "UPDATE candidate_assertions SET state='active', confirmed_at=? WHERE id='assertion'",
            (NOW,),
        )
    assert tuple(database.execute(
        "SELECT assertion_event_id, state, confirmed_at, revoked_at, revision "
        "FROM candidate_assertions WHERE id='assertion'"
    ).fetchone()) == ("staged-event", "staged", None, None, 1)
    database.execute(
        "INSERT INTO assertion_events("
        "id,profile_id,assertion_id,event_kind,payload_hmac,created_at,revision"
        ") VALUES(?,?,?,?,?,?,?)",
        ("confirmed-event", "profile", "assertion", "confirmed", "confirmed-hmac", NOW, 1),
    )
    database.execute(
        "UPDATE candidate_assertions SET assertion_event_id=?, state='active', confirmed_at=? WHERE id='assertion'",
        ("confirmed-event", NOW),
    )
    assert tuple(database.execute(
        "SELECT assertion_event_id, state, confirmed_at, revoked_at, revision "
        "FROM candidate_assertions WHERE id='assertion'"
    ).fetchone()) == ("confirmed-event", "active", NOW, None, 1)


def test_one_active_command_per_application(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    database.execute(
        "INSERT INTO candidate_profiles VALUES(?,?,?,?,?)",
        ("profile", "Candidate", "active", NOW, 1),
    )
    database.execute(
        "INSERT INTO roles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("role", "fixture:role", "Fixture", "Engineer", None, None, 8.0, "reviewing", "hmac", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
        ("application", "profile", "role", "identity", "draft", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?)",
        ("command-1", "application", "key-1", "fill", "{}", "paused", NOW, 1),
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?)",
            ("command-2", "application", "key-2", "resume", "{}", "accepted", NOW, 1),
        )
    database.execute("UPDATE commands SET state='failed' WHERE id='command-1'")
    database.execute(
        "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?)",
        ("command-2", "application", "key-2", "resume", "{}", "accepted", NOW, 1),
    )


def test_legacy_invalid_rows_block_relational_closure(tmp_path: Path) -> None:
    database = connect(tmp_path / "legacy.sqlite3")
    first = [MIGRATIONS_DIR / version for version, _ in _MIGRATION_MANIFEST[:2]]
    for migration in first:
        database.executescript(migration.read_text(encoding="utf-8"))
    database.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP) STRICT"
    )
    database.executemany(
        "INSERT INTO schema_migrations(version) VALUES(?)",
        [(migration.name,) for migration in first],
    )
    database.execute(
        "INSERT INTO candidate_profiles VALUES(?,?,?,?,?)",
        ("profile", "Candidate", "active", NOW, 1),
    )
    database.execute(
        "INSERT INTO assertion_events("
        "id,profile_id,assertion_id,event_kind,payload_hmac,created_at,revision"
        ") VALUES(?,?,?,?,?,?,?)",
        ("event", "profile", None, "confirmed", "hmac", NOW, 1),
    )
    database.execute(
        "INSERT INTO candidate_assertions VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("assertion", "profile", "event", "semantic", "value", "active", NOW, None, 1, NOW),
    )
    database.execute(
        "INSERT INTO roles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("role", "fixture:role", "Fixture", "Engineer", None, None, 8.0, "reviewing", "hmac", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
        ("application", "profile", "role", "identity", "draft", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run", "application", None, "queued", None, None, None, None, None, 1, NOW),
    )
    database.execute(
        "INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("action", "run", None, "submit", "fill", "planned", None, NOW, None, 1),
    )
    with pytest.raises(RuntimeError, match=f"migration checksum is missing: {first[0].name}"):
        apply_migrations(database)
    database.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")
    assert database.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE checksum IS NULL"
    ).fetchone()[0] == len(first)
    database.executemany(
        "UPDATE schema_migrations SET checksum=? WHERE version=?",
        [(checksum, version) for version, checksum in _MIGRATION_MANIFEST[:2]],
    )
    with pytest.raises(sqlite3.IntegrityError, match="legacy rows violate"):
        apply_migrations(database)
    assert database.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version LIKE '0007_%'"
    ).fetchone()[0] == 0
    assert [
        tuple(row)
        for row in database.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        )
    ] == _MIGRATION_MANIFEST[:2]


def test_0010_preflight_rolls_back_unbound_run_command(tmp_path: Path) -> None:
    database = connect(tmp_path / "legacy-0009.sqlite3")
    migrations = _MIGRATION_MANIFEST[:5]
    for version, _ in migrations:
        database.executescript((MIGRATIONS_DIR / version).read_text(encoding="utf-8"))
    database.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, checksum TEXT) STRICT"
    )
    database.executemany(
        "INSERT INTO schema_migrations(version, checksum) VALUES(?, ?)", migrations
    )
    seed_fixture_dispatch_intent(database)
    database.execute(
        "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?)",
        ("wrong-command", "other-application", "wrong-key", "dispatch", "{}", "completed", NOW, 1),
    )
    database.execute("UPDATE runs SET command_id='wrong-command' WHERE id='run'")

    with pytest.raises(sqlite3.IntegrityError, match="0010 relational authority"):
        apply_migrations(database)

    assert database.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version='0010_relational_authority_closure.sql'"
    ).fetchone()[0] == 0
    assert database.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='fixture_run_command_owner_insert'"
    ).fetchone()[0] == 0


def test_fixture_fill_evidence_binding_claim_is_immutable(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_fixture_dispatch_intent(database)
    database.execute(
        """
        INSERT INTO fixture_fill_evidence(
            dispatch_binding, run_id, application_id, session_hmac, page_fingerprint,
            form_fingerprint, field_digest, resume_present, resume_sha256, script_sha256,
            executable_sha256, created_at, revision
        ) VALUES(NULL, 'run', 'application', ?, 'page', 'form', ?, 0, NULL, ?, ?, ?, 1)
        """,
        ("c" * 64, "d" * 64, "e" * 64, "f" * 64, NOW),
    )
    with pytest.raises(sqlite3.IntegrityError, match="run does not belong to application"):
        database.execute(
            "UPDATE fixture_fill_evidence SET application_id='other-application' "
            "WHERE run_id='run' AND form_fingerprint='form'"
        )
    database.execute(
        "UPDATE fixture_fill_evidence SET dispatch_binding='dispatch' "
        "WHERE run_id='run' AND form_fingerprint='form'"
    )
    with pytest.raises(sqlite3.IntegrityError, match="dispatch binding is immutable after claim"):
        database.execute(
            "UPDATE fixture_fill_evidence SET dispatch_binding='other-dispatch' "
            "WHERE run_id='run' AND form_fingerprint='form'"
        )
    assert tuple(database.execute(
        "SELECT dispatch_binding, resume_present, resume_sha256 FROM fixture_fill_evidence "
        "WHERE run_id='run' AND form_fingerprint='form'"
    ).fetchone()) == ("dispatch", 0, None)


def test_confirmed_fixture_outcome_requires_well_formed_terminal_proof(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_fixture_dispatch_intent(database)
    reserve_fixture_dispatch_quota(database)
    seed_prepared_fixture_outcome(database)
    database.execute(
        "UPDATE dispatches SET state='dispatching', started_at=? WHERE id='dispatch'", (NOW,)
    )
    database.execute(
        "UPDATE fixture_dispatch_outcomes SET state='possibly_started', started_at=? "
        "WHERE dispatch_id='dispatch'",
        (NOW,),
    )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="confirmed fixture outcome requires well-formed receipt",
    ):
        database.execute(
            "UPDATE fixture_dispatch_outcomes SET state='confirmed', confirmed_at=?, "
            "terminal_at=?, receipt_digest='not-hex', attestation_digest=?, "
            "observed_intent_hmac=intent_hmac WHERE dispatch_id='dispatch'",
            (NOW, NOW, "2" * 64),
        )
    database.execute(
        "UPDATE fixture_dispatch_outcomes SET state='confirmed', confirmed_at=?, "
        "terminal_at=?, receipt_digest=?, attestation_digest=?, "
        "observed_intent_hmac=intent_hmac WHERE dispatch_id='dispatch'",
        (NOW, NOW, "1" * 64, "2" * 64),
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="terminal proof fields are frozen"
    ):
        database.execute(
            "UPDATE fixture_dispatch_outcomes SET receipt_digest=? WHERE dispatch_id='dispatch'",
            ("3" * 64,),
        )


def test_fixture_dispatch_outcome_and_quota_rows_are_append_only(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_fixture_dispatch_intent(database)
    reserve_fixture_dispatch_quota(database)
    seed_prepared_fixture_outcome(database)
    with pytest.raises(sqlite3.IntegrityError, match="dispatches are append-only"):
        database.execute("DELETE FROM dispatches WHERE id='dispatch'")
    with pytest.raises(sqlite3.IntegrityError, match="fixture outcomes are append-only"):
        database.execute("DELETE FROM fixture_dispatch_outcomes WHERE dispatch_id='dispatch'")
    with pytest.raises(sqlite3.IntegrityError, match="quota history is append-only"):
        database.execute("DELETE FROM daily_quota_reservations WHERE id='quota'")


def test_aside_dispatch_start_requires_closed_provider_switch_and_non_open_breaker(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_fixture_dispatch_intent(database)
    reserve_fixture_dispatch_quota(database)
    seed_prepared_fixture_outcome(database)

    database.execute("UPDATE breakers SET state='open' WHERE id='breaker'")
    with pytest.raises(
        sqlite3.IntegrityError,
        match="requires closed provider kill switch and non-open breaker",
    ):
        database.execute(
            "UPDATE dispatches SET state='dispatching', started_at=? WHERE id='dispatch'", (NOW,)
        )
    database.execute("UPDATE breakers SET state='closed' WHERE id='breaker'")

    database.execute("UPDATE kill_switches SET state='open' WHERE id='provider-kill'")
    with pytest.raises(
        sqlite3.IntegrityError,
        match="requires closed provider kill switch and non-open breaker",
    ):
        database.execute(
            "UPDATE dispatches SET state='dispatching', started_at=? WHERE id='dispatch'", (NOW,)
        )
    database.execute("UPDATE kill_switches SET state='closed' WHERE id='provider-kill'")

    database.execute(
        "UPDATE dispatches SET state='dispatching', started_at=? WHERE id='dispatch'", (NOW,)
    )
    assert database.execute(
        "SELECT state FROM dispatches WHERE id='dispatch'"
    ).fetchone()[0] == "dispatching"


def test_aside_dispatch_identity_frozen_blocks_same_statement_reclassification(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_fixture_dispatch_intent(database)
    reserve_fixture_dispatch_quota(database)
    seed_prepared_fixture_outcome(database)
    database.execute(
        "UPDATE dispatches SET state='dispatching', started_at=? WHERE id='dispatch'", (NOW,)
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="identity is frozen after evidence binding or start"
    ):
        database.execute("UPDATE dispatches SET transport='direct' WHERE id='dispatch'")
    assert database.execute(
        "SELECT transport FROM dispatches WHERE id='dispatch'"
    ).fetchone()[0] == "aside"


def test_confirmed_assertion_alias_and_batch_policy_authority_are_immutable(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_fixture_dispatch_intent(database)
    with pytest.raises(
        sqlite3.IntegrityError, match="value and semantic identity are immutable"
    ):
        database.execute(
            "UPDATE candidate_assertions SET semantic_key='changed' WHERE id='assertion-1'"
        )
    database.execute(
        "UPDATE candidate_assertions SET state='revoked', revoked_at=? WHERE id='assertion-1'",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="revoked assertion cannot be reactivated"):
        database.execute("UPDATE candidate_assertions SET state='active' WHERE id='assertion-1'")

    database.execute(
        "INSERT INTO question_aliases VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "alias-1",
            "profile",
            "assertion-2",
            "alias-event-2",
            "fixture",
            "label",
            "scope",
            "form-1",
            None,
            1,
            NOW,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="scope and provenance are immutable"):
        database.execute("UPDATE question_aliases SET semantic_scope='other' WHERE id='alias-1'")
    database.execute("UPDATE question_aliases SET revoked_at=? WHERE id='alias-1'", (NOW,))
    with pytest.raises(sqlite3.IntegrityError, match="revoked alias cannot change revocation"):
        database.execute("UPDATE question_aliases SET revoked_at=NULL WHERE id='alias-1'")

    with pytest.raises(
        sqlite3.IntegrityError, match="signed batch policy authority fields are immutable"
    ):
        database.execute("UPDATE batch_policies SET daily_cap=5 WHERE id='policy'")
    with pytest.raises(
        sqlite3.IntegrityError, match="batch policy replacement requires a new revision"
    ):
        database.execute("UPDATE batch_policies SET state='revoked' WHERE id='policy'")
    database.execute("UPDATE batch_policies SET state='revoked', revision=2 WHERE id='policy'")
    with pytest.raises(
        sqlite3.IntegrityError, match="terminal batch policy state cannot change"
    ):
        database.execute(
            "UPDATE batch_policies SET state='active', revision=3 WHERE id='policy'"
        )


def test_prior_day_consumed_quota_reservation_survives_relational_preflight(
    tmp_path: Path,
) -> None:
    database = connect(tmp_path / "legacy-consumed-quota.sqlite3")
    prefix = _MIGRATION_MANIFEST[:5]
    for version, _ in prefix:
        database.executescript((MIGRATIONS_DIR / version).read_text(encoding="utf-8"))
    database.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP, checksum TEXT) STRICT"
    )
    database.executemany(
        "INSERT INTO schema_migrations(version, checksum) VALUES(?, ?)", prefix
    )
    database.execute(
        "INSERT INTO candidate_profiles VALUES(?,?,?,?,?)",
        ("profile", "Candidate", "active", NOW, 1),
    )
    database.execute(
        "INSERT INTO assertion_events VALUES(?,?,?,?,?,?,?)",
        ("event-1", "profile", "assertion-1", "confirmed", "hmac-1", NOW, 1),
    )
    database.execute(
        "INSERT INTO candidate_assertions VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("assertion-1", "profile", "event-1", "semantic-1", "value-1", "active", NOW, None, 1, NOW),
    )
    database.execute(
        "INSERT INTO roles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("role", "fixture:role", "Fixture", "Engineer", None, None, 8.0, "reviewing", "hmac", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
        ("application", "profile", "role", "identity", "draft", 1, NOW, NOW),
    )
    database.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run", "application", None, "queued", None, None, None, None, None, 1, NOW),
    )
    database.execute(
        "INSERT INTO kill_switches VALUES(?,?,?,?,?,?,?)",
        ("kill", "global", "global", "closed", "test", NOW, 1),
    )
    database.execute(
        """
        INSERT INTO capabilities(
            id, provider, tenant, operation, transport, form_fingerprint, state,
            expires_at, capability_json, revision, created_at, environment,
            adapter_id, origin
        ) VALUES('historical-capability', 'fixture', 'tenant', 'submit', 'aside', 'form', 'active',
            '2020-01-01T23:00:00Z', '{}', 1, ?, 'fixture', 'fixture-aside-v1', 'fixture.local')
        """,
        (NOW,),
    )
    database.execute(
        """
        INSERT INTO batch_policies(
            id, candidate_profile_id, policy_version, state, scope_json, min_fit_score,
            timezone, daily_cap, provider_form_allowlist_json, assertion_snapshot_id,
            material_policy_json, checkpoint_classes_json, valid_from, expires_at,
            global_kill_switch_id, signature_hmac, key_version,
            candidate_confirmation_event_id, revision, created_at, environment,
            fixture_adapter_id, fixture_origin, fixture_capability_id
        ) VALUES(
            'historical-policy', 'profile', 1, 'active', '{}', 8.0, 'America/Vancouver', 2,
            '[]', 'assertion-1', '{}', '[]',
            '2020-01-01T00:00:00Z', '2020-01-01T23:00:00Z',
            'kill', 'policy-hmac', 1, 'event-1', 1, ?, 'fixture',
            'fixture-aside-v1', 'fixture.local', 'historical-capability'
        )
        """,
        (NOW,),
    )
    database.execute(
        """
        INSERT INTO dispatches(
            id, application_id, run_id, transport, state, batch_policy_id,
            authority_hmac, form_fingerprint, started_at, finished_at, revision,
            created_at, environment, fixture_adapter_id, fixture_origin,
            fixture_capability_id
        ) VALUES('dispatch', 'application', 'run', 'aside', 'intent', 'historical-policy',
            'intent-hmac', 'form', NULL, NULL, 1, ?, 'fixture', 'fixture-aside-v1',
            'fixture.local', 'historical-capability')
        """,
        (NOW,),
    )
    database.execute(
        """
        INSERT INTO daily_quota_reservations(
            id, policy_id, local_date, application_id, dispatch_id, state,
            created_at, consumed_at, revision
        ) VALUES('quota', 'historical-policy', '2020-01-01', 'application', 'dispatch', 'consumed', ?, ?, 1)
        """,
        (NOW, NOW),
    )

    apply_migrations(database)

    assert tuple(database.execute(
        "SELECT state, local_date FROM daily_quota_reservations WHERE id='quota'"
    ).fetchone()) == ("consumed", "2020-01-01")
    assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def test_apply_migrations_rejects_non_prefix_migration_history(tmp_path: Path) -> None:
    database = connect(tmp_path / "nonprefix.sqlite3")
    database.executescript((MIGRATIONS_DIR / _MIGRATION_MANIFEST[0][0]).read_text(encoding="utf-8"))
    database.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP, checksum TEXT) STRICT"
    )
    database.execute(
        "INSERT INTO schema_migrations(version, checksum) VALUES(?, ?)", _MIGRATION_MANIFEST[0]
    )
    database.execute(
        "INSERT INTO schema_migrations(version, checksum) VALUES(?, ?)", _MIGRATION_MANIFEST[2]
    )
    with pytest.raises(RuntimeError, match="not an exact prefix of the manifest"):
        apply_migrations(database)


def test_apply_migrations_rejects_marker_only_migration_history(tmp_path: Path) -> None:
    database = connect(tmp_path / "marker-only.sqlite3")
    database.executescript((MIGRATIONS_DIR / _MIGRATION_MANIFEST[0][0]).read_text(encoding="utf-8"))
    database.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP, checksum TEXT) STRICT"
    )
    database.execute(
        "INSERT INTO schema_migrations(version, checksum) VALUES(?, ?)", _MIGRATION_MANIFEST[0]
    )
    database.execute(
        "INSERT INTO schema_migrations(version, checksum) VALUES(?, ?)", _MIGRATION_MANIFEST[1]
    )
    with pytest.raises(RuntimeError, match="no matching schema objects"):
        apply_migrations(database)


def test_legacy_0007_transition_is_schema_verified_before_relabeling(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    legacy = "9a79f204a93750225264e556c209d07abdd8099bf9a51ad05019514e96e98e3e"
    database.execute(
        "UPDATE schema_migrations SET checksum=? "
        "WHERE version='0007_critic5_relational_closure.sql'",
        (legacy,),
    )
    database.execute("DROP TRIGGER candidate_assertion_provenance_insert")

    with pytest.raises(RuntimeError, match="missing expected schema objects"):
        apply_migrations(database)
    assert database.execute(
        "SELECT checksum FROM schema_migrations WHERE version='0007_critic5_relational_closure.sql'"
    ).fetchone()[0] == legacy
