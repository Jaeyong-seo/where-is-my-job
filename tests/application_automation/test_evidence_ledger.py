from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from application_automation.evidence import EvidenceError, append_evidence_event, make_evidence, verify_evidence_ledger
from application_automation.store import apply_migrations, connect

_KEY = b"0123456789abcdef0123456789abcdef"
_RECEIPT = "a" * 64
_ATTESTATION = "b" * 64
_INSTANT = "2026-01-01T00:00:00+00:00"


def _database(tmp_path):
    connection = connect(tmp_path / "ledger.sqlite")
    apply_migrations(connection)
    instant = "2026-01-01T00:00:00+00:00"
    for suffix in ("", "-foreign"):
        profile, role, application, run, dispatch = (f"p{suffix}", f"r{suffix}", f"a{suffix}", f"run{suffix}", f"d{suffix}")
        connection.execute("INSERT INTO candidate_profiles VALUES(?,?,?,?,?)", (profile, "candidate", "active", instant, 1))
        connection.execute("INSERT INTO roles(id,canonical_key,company_name,title,posting_snapshot_hmac,status,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (role, f"fixture:role{suffix}", "Fixture", "Engineer", "x", "materials_ready", 1, instant, instant))
        connection.execute("INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)", (application, profile, role, f"fixture:application{suffix}", "queued", 1, instant, instant))
        connection.execute("INSERT INTO runs(id,application_id,state,revision,created_at) VALUES(?,?,?,?,?)", (run, application, "completed", 1, instant))
        connection.execute("INSERT INTO dispatches(id,application_id,run_id,transport,state,form_fingerprint,started_at,finished_at,revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (dispatch, application, run, "manual", "confirmed", "fixture", instant, instant, 1, instant))
    return connection


def _append_confirmation(connection, when: datetime):
    append_evidence_event(connection, "a", "d", "submit", authentication_key=_KEY, key_version=1, stage="submit", occurred_at=when)
    return append_evidence_event(connection, "a", "d", "observe", authentication_key=_KEY, key_version=1, stage="observe", receipt_digest=_RECEIPT, attestation_digest=_ATTESTATION, occurred_at=when + timedelta(seconds=1))


def test_fixture_attempt_and_confirmed_observation_have_independent_digests(tmp_path):
    connection = _database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(EvidenceError, match="key is not permitted"):
        make_evidence("form", {"candidate_email": "candidate@example.test"})
    with pytest.raises(EvidenceError, match="at least 32 bytes"):
        append_evidence_event(connection, "a", None, "inspect", authentication_key=b"short", key_version=1, stage="inspect")

    inspect = append_evidence_event(connection, "a", None, "inspect", authentication_key=_KEY, key_version=1, stage="inspect", occurred_at=start)
    submit = append_evidence_event(connection, "a", "d", "submit", authentication_key=_KEY, key_version=1, stage="submit", occurred_at=start + timedelta(seconds=1))
    observation = append_evidence_event(connection, "a", "d", "observe", authentication_key=_KEY, key_version=1, stage="observe", receipt_digest=_RECEIPT, attestation_digest=_ATTESTATION, occurred_at=start + timedelta(seconds=2))
    assert [(event.sequence, event.previous_hash) for event in (inspect, submit, observation)] == [(1, None), (2, inspect.event_hash), (3, submit.event_hash)]
    assert (submit.receipt_present, submit.confirmation) == (False, False)
    assert (observation.receipt_present, observation.confirmation) == (True, True)
    assert [tuple(row) for row in connection.execute("SELECT ledger_sequence,kind,receipt_digest,attestation_digest FROM evidence WHERE application_id='a' ORDER BY ledger_sequence")] == [(1, "inspect", None, None), (2, "submit", None, None), (3, "observe", _RECEIPT, _ATTESTATION)]
    assert verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    assert not verify_evidence_ledger(connection, "a", authentication_key=b"x" * 32, key_version=1)
    connection.close()
def test_ledger_accepts_sanitized_internal_failure_reason(tmp_path):
    connection = _database(tmp_path)
    event = append_evidence_event(
        connection, "a", None, "pause", authentication_key=_KEY, key_version=1,
        stage="execution", reason_code="internal_failure",
    )
    assert event.reason_code == "internal_failure"
    assert verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.close()

def test_ledger_authenticates_each_confirmation_digest(tmp_path):
    connection = _database(tmp_path)
    _append_confirmation(connection, datetime(2026, 1, 1, tzinfo=timezone.utc))
    connection.execute("DROP TRIGGER evidence_append_only")
    for column, value in (("receipt_digest", "c" * 64), ("attestation_digest", "d" * 64)):
        connection.execute(
            f"UPDATE evidence SET {column}=?, metadata_json=json_set(metadata_json, '$.{column}', ?) "
            "WHERE application_id='a' AND ledger_sequence=2",
            (value, value),
        )
        assert not verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
        connection.execute(
            f"UPDATE evidence SET {column}=?, metadata_json=json_set(metadata_json, '$.{column}', ?) "
            "WHERE application_id='a' AND ledger_sequence=2",
            ((_RECEIPT if column == "receipt_digest" else _ATTESTATION),) * 2,
        )
        assert verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.close()
def test_ledger_rejects_metadata_column_prior_row_head_and_key_version_tampering(tmp_path):
    connection = _database(tmp_path)
    _append_confirmation(connection, datetime(2026, 1, 1, tzinfo=timezone.utc))
    connection.execute("DROP TRIGGER evidence_append_only")
    connection.execute(
        "UPDATE evidence SET receipt_digest=? WHERE application_id='a' AND ledger_sequence=2",
        ("c" * 64,),
    )
    assert not verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.execute(
        "UPDATE evidence SET receipt_digest=? WHERE application_id='a' AND ledger_sequence=2",
        (_RECEIPT,),
    )
    connection.execute(
        "UPDATE evidence SET ledger_sequence=9 WHERE application_id='a' AND ledger_sequence=1"
    )
    assert not verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.execute("UPDATE evidence SET ledger_sequence=1 WHERE application_id='a' AND ledger_sequence=9")
    connection.execute("UPDATE evidence SET key_version=2 WHERE application_id='a' AND ledger_sequence=1")
    assert not verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.execute("UPDATE evidence SET key_version=1 WHERE application_id='a' AND ledger_sequence=1")
    connection.execute("UPDATE evidence_ledger_heads SET event_hash=? WHERE application_id='a'", ("e" * 64,))
    assert not verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.close()


def test_missing_head_rejects_orphans_without_creating_a_head_or_appending(tmp_path):
    connection = _database(tmp_path)
    connection.execute(
        "INSERT INTO evidence(id,application_id,dispatch_id,kind,metadata_json,content_sha256,created_at,"
        "revision,ledger_sequence,key_version,receipt_digest,attestation_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("orphan", "a", None, "inspect", "{}", None, _INSTANT, 1, None, None, None, None),
    )
    before = connection.execute(
        "SELECT id,ledger_sequence FROM evidence WHERE application_id='a' ORDER BY id"
    ).fetchall()
    with pytest.raises(EvidenceError, match="orphan rows"):
        append_evidence_event(
            connection, "a", None, "inspect", authentication_key=_KEY, key_version=1,
            stage="inspect",
        )
    assert connection.execute(
        "SELECT event_count FROM evidence_ledger_heads WHERE application_id='a'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT id,ledger_sequence FROM evidence WHERE application_id='a' ORDER BY id"
    ).fetchall() == before
    connection.close()


def test_ledger_rejects_corrupt_history_and_malformed_persisted_data(tmp_path):
    connection = _database(tmp_path)
    _append_confirmation(connection, datetime(2026, 1, 1, tzinfo=timezone.utc))
    connection.execute("DROP TRIGGER evidence_no_delete")
    connection.execute("DELETE FROM evidence WHERE application_id='a' AND ledger_sequence=2")
    assert not verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    with pytest.raises(EvidenceError, match="head does not match rows"):
        append_evidence_event(connection, "a", None, "fill", authentication_key=_KEY, key_version=1, stage="fill")
    connection.close()

    (tmp_path / "malformed").mkdir()
    connection = _database(tmp_path / "malformed")
    _append_confirmation(connection, datetime(2026, 1, 1, tzinfo=timezone.utc))
    connection.execute("DROP TRIGGER evidence_append_only")
    original_metadata = connection.execute(
        "SELECT metadata_json FROM evidence WHERE application_id='a' AND ledger_sequence=1"
    ).fetchone()[0]
    connection.execute(
        "UPDATE evidence SET metadata_json=json_set(metadata_json, '$.event_kind', json('[]')) "
        "WHERE application_id='a' AND ledger_sequence=1"
    )
    assert not verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.execute(
        "UPDATE evidence SET metadata_json=? WHERE application_id='a' AND ledger_sequence=1",
        (original_metadata,),
    )
    connection.execute("UPDATE evidence_ledger_heads SET head_hmac='malformed' WHERE application_id='a'")
    assert not verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.close()


def test_ledger_rejects_foreign_dispatch_and_sequences_nonmonotonic_times(tmp_path):
    connection = _database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    append_evidence_event(connection, "a", None, "inspect", authentication_key=_KEY, key_version=1, stage="inspect", occurred_at=start)
    before = connection.execute("SELECT COUNT(*) FROM evidence WHERE application_id='a'").fetchone()[0]
    with pytest.raises(EvidenceError, match="does not belong"):
        append_evidence_event(connection, "a", "d-foreign", "submit", authentication_key=_KEY, key_version=1, stage="submit", occurred_at=start)
    with pytest.raises(EvidenceError, match="observation requires"):
        append_evidence_event(connection, "a", "d", "observe", authentication_key=_KEY, key_version=1, stage="observe", occurred_at=start)
    assert connection.execute("SELECT COUNT(*) FROM evidence WHERE application_id='a'").fetchone()[0] == before
    # Sequence, not mutable rowid or wall-clock order, is the ledger ordering contract.
    later = append_evidence_event(connection, "a", None, "fill", authentication_key=_KEY, key_version=1, stage="fill", occurred_at=start + timedelta(days=1))
    earlier = append_evidence_event(connection, "a", "d", "submit", authentication_key=_KEY, key_version=1, stage="submit", occurred_at=start)
    assert (later.sequence, earlier.sequence) == (2, 3)
    assert verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.close()


def test_ledger_is_append_only_and_anchored_across_reopen(tmp_path):
    connection = _database(tmp_path)
    _append_confirmation(connection, datetime(2026, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM evidence WHERE application_id='a' AND ledger_sequence=2")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE evidence SET created_at='2026-01-02T00:00:00+00:00' WHERE application_id='a'")
    assert verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    connection.execute("UPDATE evidence_ledger_heads SET event_count=1 WHERE application_id='a'")
    assert not verify_evidence_ledger(connection, "a", authentication_key=_KEY, key_version=1)
    with pytest.raises(EvidenceError, match="evidence ledger"):
        append_evidence_event(connection, "a", None, "fill", authentication_key=_KEY, key_version=1, stage="fill")
    assert connection.execute("SELECT COUNT(*) FROM evidence WHERE application_id='a'").fetchone()[0] == 2
    connection.close()
    reopened = connect(tmp_path / "ledger.sqlite")
    assert not verify_evidence_ledger(reopened, "a", authentication_key=_KEY, key_version=1)
    reopened.close()


def test_concurrent_appends_are_sequenced_and_reopen_verifies(tmp_path):
    path = tmp_path / "ledger.sqlite"
    bootstrap = _database(tmp_path)
    bootstrap.close()
    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def append(kind: str) -> None:
        connection = None
        try:
            connection = connect(path)
            barrier.wait(timeout=2)
            result = append_evidence_event(connection, "a", None, kind, authentication_key=_KEY, key_version=1, stage=kind)
        except BaseException as error:
            result = error
        finally:
            if connection is not None:
                connection.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=append, args=(kind,)) for kind in ("inspect", "fill")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=7)
    assert not any(thread.is_alive() for thread in threads)
    assert not any(isinstance(result, BaseException) for result in outcomes)
    reopened = connect(path)
    assert [row[0] for row in reopened.execute("SELECT ledger_sequence FROM evidence WHERE application_id='a' ORDER BY ledger_sequence")] == [1, 2]
    assert verify_evidence_ledger(reopened, "a", authentication_key=_KEY, key_version=1)
    reopened.close()
