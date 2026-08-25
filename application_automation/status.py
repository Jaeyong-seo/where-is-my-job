"""Durable application status events.

This module deliberately records status history only; catalog projections are owned elsewhere.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_ALLOWED_EVENT_KINDS = frozenset(
    {"queued", "awaiting_user", "applied", "rejected", "cancelled", "closed", "manual_followup", "direct_edit"}
)
_TRANSITIONS = {
    None: frozenset({"queued"}),
    "queued": frozenset({"queued", "awaiting_user", "applied", "rejected", "cancelled", "closed", "manual_followup"}),
    "awaiting_user": frozenset({"queued", "applied", "rejected", "cancelled", "closed", "manual_followup"}),
    "manual_followup": frozenset({"queued", "applied", "rejected", "cancelled", "closed"}),
}


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Generator[None, None, None]:
    """Serialize validation and insertion, composing safely with a caller transaction."""
    if connection.in_transaction:
        savepoint = f"status_event_{uuid.uuid4().hex}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _current_canonical_status(connection: sqlite3.Connection, role_id: str) -> str | None:
    row = connection.execute(
        "SELECT event_kind FROM status_events WHERE role_id=? AND event_kind <> 'direct_edit' "
        "ORDER BY revision DESC LIMIT 1",
        (role_id,),
    ).fetchone()
    return None if row is None else str(row["event_kind"])
def current_canonical_event(
    connection: sqlite3.Connection, role_id: str
) -> tuple[str, dict[str, Any]] | None:
    """Return the latest canonical event and payload, excluding direct-edit markers."""
    row = connection.execute(
        "SELECT event_kind,payload_json FROM status_events "
        "WHERE role_id=? AND event_kind <> 'direct_edit' ORDER BY revision DESC LIMIT 1",
        (role_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row["event_kind"]), json.loads(row["payload_json"])

def append_event(
    connection: sqlite3.Connection,
    role_id: str,
    event_kind: str,
    *,
    application_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Append one legal, role-revisioned status event."""
    if event_kind not in _ALLOWED_EVENT_KINDS:
        raise ValueError("invalid status event kind")
    with _immediate_transaction(connection):
        if application_id is not None:
            owner = connection.execute(
                "SELECT 1 FROM applications WHERE id=? AND role_id=?",
                (application_id, role_id),
            ).fetchone()
            if owner is None:
                raise ValueError("application does not belong to role")
        current = _current_canonical_status(connection, role_id)
        if event_kind != "direct_edit" and event_kind not in _TRANSITIONS.get(current, frozenset()):
            raise ValueError(f"illegal status transition: {current!r} -> {event_kind!r}")
        event_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO status_events(id, role_id, application_id, event_kind, payload_json, created_at, revision) "
            "VALUES (?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(revision), 0) + 1 FROM status_events WHERE role_id=?))",
            (
                event_id, role_id, application_id, event_kind, json.dumps(payload or {}, sort_keys=True),
                _now(), role_id,
            ),
        )
    return event_id


def current_status(connection: sqlite3.Connection, role_id: str) -> str | None:
    """Return the latest canonical hiring status, excluding direct-edit conflict markers."""
    return _current_canonical_status(connection, role_id)


def events_for_role(connection: sqlite3.Connection, role_id: str) -> list[dict[str, Any]]:
    return [
        {**dict(row), "payload": json.loads(row["payload_json"])}
        for row in connection.execute(
            "SELECT * FROM status_events WHERE role_id=? ORDER BY revision", (role_id,)
        )
    ]
