from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import socket
import threading
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3

import pytest

from application_automation.adapters.mcp import canonical_submit_payload_sha256
from application_automation.aside import DispatchIntent, PauseReason, StatusObservation, SubmitOutcome
import application_automation.orchestrator as orchestrator_module
from application_automation.orchestrator import ApplicationOrchestrator, OrchestrationError
from application_automation.status import append_event
from application_automation.store import apply_migrations, connect


def _catalog(root: Path, count: int = 1) -> dict[str, dict[str, object]]:
    catalog: dict[str, dict[str, object]] = {}
    for index in range(count):
        role_id = f"role-{index:02d}"
        directory = root / "applications" / role_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "job.md").write_text("# Fixture role", encoding="utf-8")
        (directory / "resume.md").write_text(f"fixture resume {index}", encoding="utf-8")
        (directory / "resume.docx").write_bytes(b"fixture docx")
        resume = directory / "resume.pdf"
        resume.write_bytes(b"%PDF-fixture")
        catalog[role_id] = {
            "score": 8,
            "location": "Vancouver, BC",
            "posting_active": True,
            "remote": False,
            "remote_country": None,
            "automation_status": "materials_ready",
            "canonical_identity": f"fixture:{role_id}",
            "application_dir": str(directory),
            "material_path": str(resume),
            "material_sha256": hashlib.sha256(resume.read_bytes()).hexdigest(),
        }
    return catalog


def _orchestrator(root: Path, *, count: int = 1, fixture_mode: bool = True) -> tuple[ApplicationOrchestrator, object, dict[str, dict[str, object]]]:
    database = connect(root / "service.sqlite")
    apply_migrations(database)
    catalog = _catalog(root, count)
    orchestrator = ApplicationOrchestrator(database, fixture_mode=fixture_mode, catalog=catalog)
    orchestrator.sync_catalog()
    return orchestrator, database, catalog


def test_dry_run_fill_only_and_batch_have_distinct_persisted_boundaries(tmp_path: Path) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path)
    role_id = "role-00"

    dry_run = orchestrator.queue(role_id, "dry_run", "dry-run")
    assert orchestrator.run(dry_run["id"])["state"] == "completed"
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM daily_quota_reservations").fetchone()[0] == 0
    assert [row[0] for row in database.execute("SELECT kind FROM evidence ORDER BY rowid")] == ["inspect"]

    fill_only = orchestrator.queue(role_id, "fill_only", "fill-only")
    assert orchestrator.run(fill_only["id"])["state"] == "completed"
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM daily_quota_reservations").fetchone()[0] == 0
    assert [row[0] for row in database.execute("SELECT kind FROM evidence ORDER BY rowid")] == ["inspect", "inspect", "fill"]

    batch = orchestrator.queue(role_id, "batch", "batch")
    assert orchestrator.run(batch["id"])["state"] == "completed"
    assert database.execute("SELECT state FROM applications").fetchone()[0] == "submitted"
    assert database.execute("SELECT state FROM dispatches").fetchone()[0] == "confirmed"
    assert database.execute("SELECT state FROM daily_quota_reservations").fetchone()[0] == "consumed"
    assert [row[0] for row in database.execute("SELECT kind FROM evidence ORDER BY rowid")] == [
        "inspect", "inspect", "fill", "inspect", "fill", "submit", "observe"
    ]
    outcome = database.execute(
        "SELECT dispatch_id,state,receipt_digest,attestation_digest,observed_intent_hmac,payload_sha256 "
        "FROM fixture_dispatch_outcomes"
    ).fetchone()
    dispatch = database.execute(
        "SELECT environment,fixture_adapter_id,fixture_origin,fixture_capability_id FROM dispatches"
    ).fetchone()
    expected_receipt = hashlib.sha256(b"fixture-receipt-v1").hexdigest()
    expected_intent = hashlib.sha256(
        f"intent:{outcome['dispatch_id']}:{outcome['payload_sha256']}".encode()
    ).hexdigest()
    expected_attestation = hashlib.sha256(
        f"attestation:{expected_intent}:{expected_receipt}".encode()
    ).hexdigest()
    assert outcome["state"] == "confirmed"
    assert outcome["receipt_digest"] == expected_receipt
    assert outcome["observed_intent_hmac"] == expected_intent
    assert outcome["attestation_digest"] == expected_attestation
    assert dispatch["environment"] == "fixture"
    assert dispatch["fixture_adapter_id"] == "fixture-aside-v1"
    assert dispatch["fixture_origin"] == "fixture.local"
    assert dispatch["fixture_capability_id"] is not None
    database.close()


@pytest.mark.parametrize("scenario", [reason.value for reason in PauseReason])
def test_every_challenge_preserves_reason_checkpoint_and_is_not_rerunnable(tmp_path: Path, scenario: str) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path)
    command = orchestrator.queue("role-00", "batch", f"pause-{scenario}")
    paused = orchestrator.run(command["id"], scenario=scenario)

    assert paused["state"] == "paused"
    checkpoint = database.execute(
        "SELECT kind, state FROM checkpoints WHERE application_id=?", (command["application_id"],)
    ).fetchone()
    evidence = database.execute(
        "SELECT metadata_json FROM evidence WHERE application_id=? AND kind='pause'", (command["application_id"],)
    ).fetchone()
    assert checkpoint["kind"] == scenario
    assert checkpoint["state"] == "open"
    assert json.loads(evidence["metadata_json"])["reason_code"] == scenario
    with pytest.raises(OrchestrationError):
        orchestrator.run(command["id"])
    assert database.execute("SELECT COUNT(*) FROM runs WHERE command_id=?", (command["id"],)).fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    database.close()


def test_ambiguous_post_start_requires_manual_followup_consumes_quota_and_cannot_retry(tmp_path: Path) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path)
    command = orchestrator.queue("role-00", "batch", "ambiguous")
    completed = orchestrator.run(command["id"], scenario="ambiguous")

    assert completed["state"] == "completed"
    assert database.execute("SELECT state FROM applications").fetchone()[0] == "manual_followup"
    assert database.execute("SELECT state FROM dispatches").fetchone()[0] == "manual_followup"
    assert database.execute("SELECT state FROM daily_quota_reservations").fetchone()[0] == "consumed"
    assert database.execute("SELECT kind FROM checkpoints").fetchone()[0] == "manual_completion"
    assert database.execute("SELECT event_kind FROM status_events ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "manual_followup"
    with pytest.raises(OrchestrationError):
        orchestrator.run(command["id"])
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 1
    database.close()


def test_real_mode_has_no_adapter_or_network_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path, fixture_mode=False)
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("real mode attempted an external call")

    monkeypatch.setattr("application_automation.orchestrator.AsideFixtureAdapter", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(OrchestrationError, match="^real execution has no active authority$"):
        orchestrator.queue("role-00", "batch", "real-mode")
    assert database.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    database.close()
def test_nonfixture_snapshot_has_zero_authority_and_quota(tmp_path: Path) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path, fixture_mode=False)
    profile_id = "nonfixture-profile"
    database.execute(
        "INSERT INTO candidate_profiles(id,display_name,state,created_at,revision) "
        "VALUES(?,?, 'active','2026-01-01T00:00:00+00:00',1)",
        (profile_id, "Candidate"),
    )

    assert orchestrator._effective_policy(profile_id) is None
    snapshot = orchestrator.snapshot()["automation"]
    assert snapshot["kill_switch_active"] is True
    assert snapshot["daily_quota"] == {"used": 0, "limit": 0}
    database.close()


@pytest.mark.parametrize(
    ("stage", "expected_dispatches", "expected_quota", "expected_application"),
    [
        ("inspect", 0, 0, "awaiting_user"),
        ("fill", 0, 0, "awaiting_user"),
        ("submit", 1, 1, "manual_followup"),
        ("observe", 1, 1, "manual_followup"),
    ],
)
def test_unexpected_adapter_failure_is_sanitized_failed_closed_at_each_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected_dispatches: int,
    expected_quota: int,
    expected_application: str,
) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path)
    command = orchestrator.queue("role-00", "batch", f"unexpected-{stage}")

    class BrokenAdapter:
        def inspect(self, *_args: object) -> object:
            if stage == "inspect":
                raise RuntimeError("credential=super-secret filesystem path")
            return self._adapter.inspect(*_args)

        def fill(self, *_args: object) -> object:
            if stage == "fill":
                raise RuntimeError("credential=super-secret filesystem path")
            return self._adapter.fill(*_args)

        def submit(self, *_args: object) -> object:
            if stage == "submit":
                raise RuntimeError("credential=super-secret filesystem path")
            return self._adapter.submit(*_args)

        def observe(self, *_args: object) -> object:
            if stage == "observe":
                raise RuntimeError("credential=super-secret filesystem path")
            return self._adapter.observe(*_args)

        def __init__(self) -> None:
            from application_automation.adapters.aside_fixture import AsideFixtureAdapter

            self._adapter = AsideFixtureAdapter()

    monkeypatch.setattr("application_automation.orchestrator.AsideFixtureAdapter", BrokenAdapter)
    with pytest.raises(RuntimeError, match="credential=super-secret"):
        orchestrator.run(command["id"])

    assert orchestrator.command(command["id"])["state"] == "failed"
    assert database.execute("SELECT state FROM applications").fetchone()[0] == expected_application
    assert database.execute("SELECT state FROM runs").fetchone()[0] == (
        "manual_followup" if expected_dispatches else "failed"
    )
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == expected_dispatches
    assert database.execute("SELECT COUNT(*) FROM daily_quota_reservations").fetchone()[0] == expected_quota
    assert database.execute("SELECT kind FROM checkpoints").fetchone()[0] == "manual_completion"
    assert database.execute("SELECT state FROM actions ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "failed"
    evidence = database.execute(
        "SELECT metadata_json FROM evidence WHERE application_id=? AND kind='pause'",
        (command["application_id"],),
    ).fetchone()
    assert json.loads(evidence[0])["reason_code"] == "internal_failure"
    snapshot = json.dumps(orchestrator.snapshot())
    assert "internal_failure" in snapshot
    assert "credential=super-secret" not in snapshot
    with pytest.raises(OrchestrationError):
        orchestrator.run(command["id"])
    with pytest.raises(OrchestrationError):
        orchestrator.queue("role-00", "batch", f"retry-{stage}")
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == expected_dispatches
    database.close()


def test_attended_mode_is_not_an_advertised_queue_mode(tmp_path: Path) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path)
    with pytest.raises(OrchestrationError, match="^invalid command$"):
        orchestrator.queue("role-00", "attended", "attended")
    database.close()




def test_stale_running_recovery_pauses_before_dispatch_and_manualizes_after_dispatch(tmp_path: Path) -> None:
    before, before_db, _ = _orchestrator(tmp_path / "before")
    before_command = before.queue("role-00", "batch", "before")
    before_db.execute("UPDATE commands SET state='running' WHERE id=?", (before_command["id"],))
    before_db.execute("UPDATE applications SET state='filling' WHERE id=?", (before_command["application_id"],))
    assert before.recover_stale_commands() == 1
    assert before_db.execute("SELECT state FROM commands WHERE id=?", (before_command["id"],)).fetchone()[0] == "paused"
    assert before_db.execute("SELECT state FROM applications WHERE id=?", (before_command["application_id"],)).fetchone()[0] == "awaiting_user"
    assert before_db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    assert before_db.execute(
        "SELECT COUNT(*) FROM fixture_dispatch_outcomes WHERE application_id=?",
        (before_command["application_id"],),
    ).fetchone()[0] == 0
    assert before_db.execute(
        "SELECT COUNT(*) FROM daily_quota_reservations WHERE application_id=?",
        (before_command["application_id"],),
    ).fetchone()[0] == 0
    before_recovery = before_db.execute(
        "SELECT metadata_json FROM evidence WHERE application_id=? AND kind='pause'",
        (before_command["application_id"],),
    ).fetchone()
    assert json.loads(before_recovery["metadata_json"])["reason_code"] == "stale_running_no_dispatch"
    assert before_db.execute("SELECT COUNT(*) FROM status_events WHERE event_kind='applied'").fetchone()[0] == 0
    with pytest.raises(OrchestrationError):
        before.run(before_command["id"])

    after, after_db, _ = _orchestrator(tmp_path / "after")
    after_command = after.queue("role-00", "batch", "after")
    profile_id = after._profile_id()
    assert after._effective_policy(profile_id) is not None
    after_db.execute("UPDATE commands SET state='running' WHERE id=?", (after_command["id"],))
    after_db.execute("UPDATE applications SET state='filling' WHERE id=?", (after_command["application_id"],))
    after_db.execute(
        "INSERT INTO runs(id,application_id,command_id,state,revision,created_at) "
        "VALUES('crash-run',?,?,'filling',1,'2026-01-01T00:00:00+00:00')",
        (after_command["application_id"], after_command["id"]),
    )
    app = after_db.execute(
        "SELECT * FROM applications WHERE id=?", (after_command["application_id"],)
    ).fetchone()
    role = after_db.execute("SELECT * FROM roles WHERE id='role-00'").fetchone()
    after._start_batch_dispatch(app, role, "crash-run", "a" * 64)
    assert after_db.execute(
        "SELECT COUNT(*) FROM status_events WHERE application_id=? AND event_kind='applied'",
        (after_command["application_id"],),
    ).fetchone()[0] == 0
    assert after_db.execute(
        "SELECT state FROM fixture_dispatch_outcomes WHERE application_id=?",
        (after_command["application_id"],),
    ).fetchone()[0] == "prepared"
    assert after_db.execute(
        "SELECT state FROM dispatches WHERE application_id=?",
        (after_command["application_id"],),
    ).fetchone()[0] == "dispatching"
    assert after_db.execute(
        "SELECT state FROM daily_quota_reservations WHERE application_id=?",
        (after_command["application_id"],),
    ).fetchone()[0] == "consumed"
    assert after.recover_stale_commands() == 1
    assert after_db.execute("SELECT state FROM commands WHERE id=?", (after_command["id"],)).fetchone()[0] == "completed"
    assert after_db.execute("SELECT state FROM applications WHERE id=?", (after_command["application_id"],)).fetchone()[0] == "manual_followup"
    assert after_db.execute("SELECT state FROM dispatches").fetchone()[0] == "manual_followup"
    assert after_db.execute("SELECT COUNT(*) FROM checkpoints WHERE application_id=?", (after_command["application_id"],)).fetchone()[0] == 1
    assert after_db.execute(
        "SELECT state FROM fixture_dispatch_outcomes WHERE application_id=?",
        (after_command["application_id"],),
    ).fetchone()[0] == "manual_followup"
    assert after_db.execute(
        "SELECT state FROM daily_quota_reservations WHERE application_id=?",
        (after_command["application_id"],),
    ).fetchone()[0] == "consumed"
    with pytest.raises(OrchestrationError):
        after.run(after_command["id"])
    assert after_db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 1
    before_db.close()
    after_db.close()


def test_daily_cap_and_concurrent_claims_have_one_persisted_owner(tmp_path: Path) -> None:
    orchestrator, database, catalog = _orchestrator(tmp_path, count=21)
    commands = [orchestrator.queue(f"role-{index:02d}", "batch", f"quota-{index}") for index in range(20)]
    for command in commands:
        assert orchestrator.run(command["id"])["state"] == "completed"
    assert database.execute("SELECT COUNT(*) FROM daily_quota_reservations WHERE state='consumed'").fetchone()[0] == 20

    second = ApplicationOrchestrator(connect(tmp_path / "service.sqlite"), fixture_mode=True, catalog=catalog)
    second.sync_catalog()
    blocked = second.queue("role-20", "batch", "quota-20")
    assert second.run(blocked["id"])["state"] == "paused"
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 20
    assert database.execute("SELECT state FROM commands WHERE id=?", (blocked["id"],)).fetchone()[0] == "paused"
    assert database.execute("SELECT kind FROM checkpoints WHERE application_id=?", (blocked["application_id"],)).fetchone()[0] == "daily_cap"

    _, concurrent_db, concurrent_catalog = _orchestrator(tmp_path / "concurrent")
    database_path = tmp_path / "concurrent" / "service.sqlite"
    queue_workers = [
        ApplicationOrchestrator(connect(database_path), fixture_mode=True, catalog=concurrent_catalog)
        for _ in range(2)
    ]
    for worker in queue_workers:
        worker.sync_catalog()
    barrier = threading.Barrier(2)
    queued: list[dict[str, object]] = []
    queue_reasons: list[str] = []

    def queue_claim(worker: ApplicationOrchestrator, key: str) -> None:
        barrier.wait()
        try:
            queued.append(worker.queue("role-00", "batch", key))
        except OrchestrationError as error:
            queue_reasons.append(str(error))

    queue_threads = [
        threading.Thread(target=queue_claim, args=(worker, f"concurrent-{index}"))
        for index, worker in enumerate(queue_workers)
    ]
    for thread in queue_threads:
        thread.start()
    for thread in queue_threads:
        thread.join()
    assert len(queued) == 1
    assert queue_reasons == ["application already has an active command"]
    assert concurrent_db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
    command = queued[0]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    def claim(worker: ApplicationOrchestrator) -> None:
        barrier.wait()
        result = worker.run_next()
        if result is not None:
            outcomes.append(result["state"])

    threads = [threading.Thread(target=claim, args=(worker,)) for worker in queue_workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == ["completed"]
    assert concurrent_db.execute("SELECT COUNT(*) FROM runs WHERE command_id=?", (command["id"],)).fetchone()[0] == 1
    assert concurrent_db.execute("SELECT COUNT(*) FROM dispatches WHERE application_id=?", (command["application_id"],)).fetchone()[0] == 1
    second.connection.close()
    for worker in queue_workers:
        worker.connection.close()
    database.close()
    concurrent_db.close()
def test_slot_twenty_race_has_one_completed_owner_and_one_clean_loser(tmp_path: Path) -> None:
    orchestrator, database, catalog = _orchestrator(tmp_path, count=21)
    for index in range(19):
        command = orchestrator.queue(f"role-{index:02d}", "batch", f"seed-{index}")
        assert orchestrator.run(command["id"])["state"] == "completed"

    database_path = tmp_path / "service.sqlite"
    workers = [
        ApplicationOrchestrator(connect(database_path), fixture_mode=True, catalog=catalog)
        for _ in range(2)
    ]
    for worker in workers:
        worker.sync_catalog()
    commands = [
        worker.queue(f"role-{19 + index:02d}", "batch", f"slot-20-{index}")
        for index, worker in enumerate(workers)
    ]
    barrier = threading.Barrier(2)
    results: list[tuple[dict[str, object], dict[str, object]]] = []
    errors: list[BaseException] = []

    def claim(worker: ApplicationOrchestrator, command: dict[str, object]) -> None:
        try:
            barrier.wait(timeout=1)
            results.append((command, worker.run(str(command["id"]))))
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=claim, args=(worker, command))
        for worker, command in zip(workers, commands, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(result["state"] for _, result in results) == ["completed", "paused"]

    winner, loser = sorted(
        results,
        key=lambda pair: pair[1]["state"] == "paused",
    )
    assert database.execute(
        "SELECT state FROM daily_quota_reservations WHERE application_id=?",
        (winner[0]["application_id"],),
    ).fetchone()[0] == "consumed"
    assert database.execute(
        "SELECT COUNT(*) FROM daily_quota_reservations WHERE application_id=?",
        (loser[0]["application_id"],),
    ).fetchone()[0] == 0
    assert database.execute(
        "SELECT kind FROM checkpoints WHERE application_id=?",
        (loser[0]["application_id"],),
    ).fetchone()[0] == "daily_cap"
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 20
    for worker in workers:
        worker.connection.close()
    database.close()
@pytest.mark.parametrize(
    ("mutation_sql", "reason"),
    [
        ("UPDATE batch_policies SET state='revoked'", "policy_revoked"),
        ("UPDATE capabilities SET state='revoked'", "policy_revoked"),
        ("UPDATE capabilities SET expires_at='2000-01-01T00:00:00+00:00'", "policy_expired"),
        ("UPDATE kill_switches SET state='open' WHERE scope_kind='global' AND scope_key='global'", "kill_switch"),
        ("UPDATE kill_switches SET state='open' WHERE scope_kind='provider' AND scope_key='fixture'", "kill_switch"),
        ("UPDATE breakers SET state='open' WHERE provider='fixture' AND tenant='fixture'", "breaker_open"),
        ("DELETE FROM kill_switches WHERE scope_kind='provider' AND scope_key='fixture'", "kill_switch"),
        ("DELETE FROM breakers WHERE provider='fixture' AND tenant='fixture'", "breaker_open"),
    ],
)
def test_revoked_fixture_authority_never_reprovisions_or_dispatches(
    tmp_path: Path, mutation_sql: str, reason: str
) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path, count=2)
    first = orchestrator.queue("role-00", "batch", "authority-first")
    assert orchestrator.run(first["id"])["state"] == "completed"
    database.execute(mutation_sql)

    second = orchestrator.queue("role-01", "batch", f"authority-{reason}")
    paused = orchestrator.run(second["id"])
    assert paused["state"] == "paused"
    assert database.execute("SELECT COUNT(*) FROM batch_policies").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM daily_quota_reservations").fetchone()[0] == 1
    assert database.execute(
        "SELECT kind FROM checkpoints WHERE application_id=?", (second["application_id"],)
    ).fetchone()[0] == reason
    assert json.loads(
        database.execute(
            "SELECT metadata_json FROM evidence WHERE application_id=? AND kind='pause'",
            (second["application_id"],),
        ).fetchone()[0]
    )["reason_code"] == reason
    database.close()


def test_deleted_global_kill_switch_row_is_rejected_by_foreign_key_with_zero_mutation(tmp_path: Path) -> None:
    """The global kill switch row is FK-referenced by the active policy; a missing/deleted live row
    must be rejected at the write itself, never reaching a state where dispatch could reprovision."""
    orchestrator, database, _ = _orchestrator(tmp_path, count=1)
    first = orchestrator.queue("role-00", "batch", "authority-first")
    assert orchestrator.run(first["id"])["state"] == "completed"
    policies_before = database.execute("SELECT COUNT(*) FROM batch_policies").fetchone()[0]
    dispatches_before = database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    quota_before = database.execute("SELECT COUNT(*) FROM daily_quota_reservations").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        database.execute("DELETE FROM kill_switches WHERE scope_kind='global' AND scope_key='global'")

    assert database.execute("SELECT COUNT(*) FROM batch_policies").fetchone()[0] == policies_before
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == dispatches_before
    assert database.execute("SELECT COUNT(*) FROM daily_quota_reservations").fetchone()[0] == quota_before
    assert database.execute("SELECT COUNT(*) FROM kill_switches WHERE scope_kind='global'").fetchone()[0] == 1
    database.close()


def test_canonical_submit_payload_digest_matches_independent_fixture_vector() -> None:
    # Audited SHA-256 vector, independently computed over the canonical payload's own
    # sorted-key JSON serialization for a fixed fixture DispatchIntent.
    intent = DispatchIntent(
        "dispatch-pin", "application-pin", "session-pin", "run-pin", "i" * 64, "p" * 64,
        "page-pin", "form-pin", "r" * 64,
    )
    payload = {
        "dispatch_id": "dispatch-pin", "application_id": "application-pin", "session_id": "session-pin",
        "run_id": "run-pin", "page_fingerprint": "page-pin", "form_fingerprint": "form-pin",
        "resume_sha256": "r" * 64, "field_digest": None,
    }
    expected = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert expected == "ba5ff90a72234ffab4c64c1ace0d31e4f5d4c36a6c0b56f72e72aefa9197840b"
    assert canonical_submit_payload_sha256(intent) == expected


@pytest.mark.parametrize("field", ["dispatch_id", "application_id", "payload_sha256", "intent_hmac"])
def test_each_preconfirmation_outcome_identity_field_tampering_is_rejected_with_no_second_dispatch(
    tmp_path: Path, field: str,
) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path)
    command = orchestrator.queue("role-00", "batch", f"tampered-outcome-{field}")
    profile_id = orchestrator._profile_id()
    assert orchestrator._effective_policy(profile_id) is not None
    database.execute("UPDATE commands SET state='running' WHERE id=?", (command["id"],))
    database.execute("UPDATE applications SET state='filling' WHERE id=?", (command["application_id"],))
    database.execute(
        "INSERT INTO runs(id,application_id,command_id,state,revision,created_at) "
        "VALUES('tamper-run',?,?,'filling',1,'2026-01-01T00:00:00+00:00')",
        (command["application_id"], command["id"]),
    )
    app = database.execute("SELECT * FROM applications WHERE id=?", (command["application_id"],)).fetchone()
    role = database.execute("SELECT * FROM roles WHERE id='role-00'").fetchone()
    orchestrator._start_batch_dispatch(app, role, "tamper-run", "a" * 64)
    original = dict(database.execute(
        "SELECT * FROM fixture_dispatch_outcomes WHERE application_id=?",
        (command["application_id"],),
    ).fetchone())
    replacement = {
        "dispatch_id": "other-dispatch", "application_id": "other-application",
        "payload_sha256": "9" * 64, "intent_hmac": "0" * 64,
    }[field]

    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            f"UPDATE fixture_dispatch_outcomes SET {field}=? WHERE application_id=?",
            (replacement, command["application_id"]),
        )

    persisted = dict(database.execute(
        "SELECT * FROM fixture_dispatch_outcomes WHERE application_id=?",
        (command["application_id"],),
    ).fetchone())
    assert persisted == original
    assert database.execute(
        "SELECT COUNT(*) FROM dispatches WHERE application_id=?", (command["application_id"],)
    ).fetchone()[0] == 1
    database.close()
@pytest.mark.parametrize(
    ("submit_mutation", "observation_mutation"),
    [
        ({"started": False}, {}),
        ({"confirmed": False}, {}),
        ({"manual_follow_up": True}, {}),
        ({"receipt_id": "other-receipt"}, {}),
        ({}, {"state": "manual_follow_up"}),
        ({}, {"page_fingerprint": "other-page"}),
        ({}, {"form_fingerprint": "other-form"}),
        ({}, {"receipt_id": "other-receipt"}),
    ],
)
def test_each_nonmatching_durable_proof_member_manualizes_and_cannot_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    submit_mutation: dict[str, object],
    observation_mutation: dict[str, object],
) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path)
    command = orchestrator.queue("role-00", "batch", f"proof-{submit_mutation}-{observation_mutation}")

    class MutatedProofAdapter:
        def __init__(self) -> None:
            from application_automation.adapters.aside_fixture import AsideFixtureAdapter

            self._adapter = AsideFixtureAdapter()

        def inspect(self, *args: object) -> object:
            return self._adapter.inspect(*args)

        def fill(self, *args: object) -> object:
            return self._adapter.fill(*args)

        def submit(self, *args: object) -> SubmitOutcome:
            return replace(self._adapter.submit(*args), **submit_mutation)

        def observe(self, *args: object) -> StatusObservation:
            return replace(self._adapter.observe(*args), **observation_mutation)

    monkeypatch.setattr("application_automation.orchestrator.AsideFixtureAdapter", MutatedProofAdapter)
    assert orchestrator.run(command["id"])["state"] == "completed"
    assert database.execute("SELECT state FROM commands").fetchone()[0] == "completed"
    assert database.execute("SELECT state FROM applications").fetchone()[0] == "manual_followup"
    assert database.execute("SELECT state FROM dispatches").fetchone()[0] == "manual_followup"
    assert database.execute("SELECT state FROM fixture_dispatch_outcomes").fetchone()[0] == "manual_followup"
    assert database.execute("SELECT state FROM daily_quota_reservations").fetchone()[0] == "consumed"
    assert database.execute("SELECT COUNT(*) FROM status_events WHERE event_kind='applied'").fetchone()[0] == 0
    assert database.execute("SELECT kind FROM checkpoints").fetchone()[0] == "manual_completion"
    with pytest.raises(OrchestrationError):
        orchestrator.run(command["id"])
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 1
    database.close()


def test_direct_edit_marker_does_not_hide_canonical_applied_or_pause(tmp_path: Path) -> None:
    orchestrator, database, _ = _orchestrator(tmp_path)
    command = orchestrator.queue("role-00", "batch", "direct-edit-status")
    assert orchestrator.run(command["id"])["state"] == "completed"
    append_event(database, "role-00", "direct_edit", payload={"marker": "conflict"})
    applied = orchestrator.snapshot()["roles"][0]
    assert applied["status"] == "applied"
    assert applied["automation"]["state"] == "applied"

    paused_orchestrator, paused_db, _ = _orchestrator(tmp_path / "paused")
    paused_command = paused_orchestrator.queue("role-00", "batch", "direct-edit-pause")
    assert paused_orchestrator.run(paused_command["id"], scenario="captcha")["state"] == "paused"
    append_event(paused_db, "role-00", "direct_edit", payload={"marker": "conflict"})
    paused = paused_orchestrator.snapshot()["roles"][0]
    assert paused["automation"]["state"] == "awaiting_user"
    assert paused["automation"]["pause"]["reason"] == "captcha"
    database.close()
    paused_db.close()


def test_trusted_clock_computes_vancouver_local_date_across_midnight_and_dst_transitions() -> None:
    """The injected trusted clock, not wall-clock time, drives the Vancouver-local quota date,
    including across a spring-forward and a fall-back DST transition."""
    connection = sqlite3.connect(":memory:")
    # Spring-forward boundary: 2026-03-08 (Vancouver stays PST, offset -8, until 2am local later that day).
    before_spring = datetime(2026, 3, 8, 7, 59, 59, tzinfo=timezone.utc)
    after_spring = datetime(2026, 3, 8, 8, 0, 0, tzinfo=timezone.utc)
    assert ApplicationOrchestrator(
        connection, fixture_mode=True, trusted_clock=lambda: before_spring
    )._trusted_vancouver_date() == "2026-03-07"
    assert ApplicationOrchestrator(
        connection, fixture_mode=True, trusted_clock=lambda: after_spring
    )._trusted_vancouver_date() == "2026-03-08"

    # Fall-back boundary: 2026-11-01 (Vancouver is still PDT, offset -7, until 2am local later that day).
    before_fall = datetime(2026, 11, 1, 6, 59, 59, tzinfo=timezone.utc)
    after_fall = datetime(2026, 11, 1, 7, 0, 0, tzinfo=timezone.utc)
    assert ApplicationOrchestrator(
        connection, fixture_mode=True, trusted_clock=lambda: before_fall
    )._trusted_vancouver_date() == "2026-10-31"
    assert ApplicationOrchestrator(
        connection, fixture_mode=True, trusted_clock=lambda: after_fall
    )._trusted_vancouver_date() == "2026-11-01"

    with pytest.raises(OrchestrationError, match="trusted clock is invalid"):
        ApplicationOrchestrator(
            connection, fixture_mode=True, trusted_clock=lambda: datetime(2026, 3, 8)
        )._trusted_vancouver_date()
    connection.close()


def test_daily_cap_stays_blocked_until_vancouver_midnight_then_slot_race_reruns_on_new_local_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consume 20 before the Vancouver local midnight boundary, prove the 21st stays blocked
    up to that boundary, then rerun the slot-twenty race on the new local date."""
    # The fixture policy's real (wall-clock) validity window is capped at 24h by schema CHECKs and
    # by a SQLite-native `julianday('now')` freshness trigger that cannot be frozen from Python.
    # Widening only the auto-provisioned capability/policy window (not any other timedelta use)
    # lets that real window legitimately straddle the injected Vancouver-local midnight boundary
    # below, while every other duration in the module is untouched.
    real_timedelta = orchestrator_module.timedelta

    def widened_timedelta(*args: object, **kwargs: object) -> timedelta:
        if args == () and kwargs == {"hours": 1}:
            return real_timedelta(hours=22)
        return real_timedelta(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestrator_module, "timedelta", widened_timedelta)

    vancouver = ZoneInfo("America/Vancouver")
    real_now = datetime.now(timezone.utc)
    next_local_midnight = (real_now.astimezone(vancouver) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    at_midnight = next_local_midnight.astimezone(timezone.utc)
    before_midnight = at_midnight - timedelta(seconds=1)
    assert before_midnight.astimezone(vancouver).date() != at_midnight.astimezone(vancouver).date()

    def freeze_local_date(database: sqlite3.Connection, clock) -> None:
        def _local_date(tz: str) -> str:
            if tz != "America/Vancouver":
                raise ValueError("unsupported policy timezone")
            return clock().astimezone(vancouver).date().isoformat()

        database.create_function("application_automation_policy_local_date", 1, _local_date, deterministic=False)

    catalog = _catalog(tmp_path, count=43)
    database = connect(tmp_path / "service.sqlite")
    apply_migrations(database)
    freeze_local_date(database, lambda: before_midnight)
    before_orchestrator = ApplicationOrchestrator(
        database, fixture_mode=True, catalog=catalog, trusted_clock=lambda: before_midnight
    )
    before_orchestrator.sync_catalog()

    for index in range(20):
        role_id = f"role-{index:02d}"
        command = before_orchestrator.queue(role_id, "batch", f"pre-midnight-{index}")
        assert before_orchestrator.run(command["id"])["state"] == "completed"
    assert database.execute(
        "SELECT COUNT(*) FROM daily_quota_reservations WHERE state='consumed'"
    ).fetchone()[0] == 20

    for label, role_id in (("first-blocked", "role-20"), ("second-blocked", "role-21")):
        command = before_orchestrator.queue(role_id, "batch", label)
        paused = before_orchestrator.run(command["id"])
        assert paused["state"] == "paused"
        assert database.execute(
            "SELECT kind FROM checkpoints WHERE application_id=?", (command["application_id"],)
        ).fetchone()[0] == "daily_cap"
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 20
    database.close()

    preseed_db = connect(tmp_path / "service.sqlite")
    freeze_local_date(preseed_db, lambda: at_midnight)
    preseed_orchestrator = ApplicationOrchestrator(
        preseed_db, fixture_mode=True, catalog=catalog, trusted_clock=lambda: at_midnight
    )
    preseed_orchestrator.sync_catalog()
    for index in range(22, 41):
        role_id = f"role-{index:02d}"
        command = preseed_orchestrator.queue(role_id, "batch", f"post-midnight-{index}")
        assert preseed_orchestrator.run(command["id"])["state"] == "completed"
    preseed_db.close()

    workers = []
    for _ in range(2):
        worker_db = connect(tmp_path / "service.sqlite")
        freeze_local_date(worker_db, lambda: at_midnight)
        worker = ApplicationOrchestrator(
            worker_db, fixture_mode=True, catalog=catalog, trusted_clock=lambda: at_midnight
        )
        worker.sync_catalog()
        workers.append(worker)
    commands = [
        workers[0].queue("role-41", "batch", "slot-race-a"),
        workers[1].queue("role-42", "batch", "slot-race-b"),
    ]
    barrier = threading.Barrier(2)
    results: list[tuple[dict[str, object], dict[str, object]]] = []
    errors: list[BaseException] = []

    def claim(worker: ApplicationOrchestrator, command: dict[str, object]) -> None:
        try:
            barrier.wait(timeout=1)
            results.append((command, worker.run(str(command["id"]))))
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=claim, args=(worker, command))
        for worker, command in zip(workers, commands, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(result["state"] for _, result in results) == ["completed", "paused"]

    winner, loser = sorted(results, key=lambda pair: pair[1]["state"] == "paused")
    final_db = connect(tmp_path / "service.sqlite")
    assert final_db.execute(
        "SELECT state FROM daily_quota_reservations WHERE application_id=?",
        (winner[0]["application_id"],),
    ).fetchone()[0] == "consumed"
    assert final_db.execute(
        "SELECT COUNT(*) FROM daily_quota_reservations WHERE application_id=?",
        (loser[0]["application_id"],),
    ).fetchone()[0] == 0
    assert final_db.execute(
        "SELECT kind FROM checkpoints WHERE application_id=?", (loser[0]["application_id"],)
    ).fetchone()[0] == "daily_cap"
    assert final_db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 40
    for worker in workers:
        worker.connection.close()
    final_db.close()
