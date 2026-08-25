"""Metadata-first, tamper-evident evidence primitives.

Raw page and screenshot material is deliberately quarantined: callers receive hashes and
safe metadata, never a best-effort rendering of content that may contain secrets or PII.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit
import sqlite3
import uuid
from .store import transaction


_SAFE_KINDS = frozenset({"html", "screenshot", "form", "receipt"})
_SAFE_EVENT_TYPES = frozenset({"observed", "stored", "captured", "quarantined", "retained", "deleted"})
_SAFE_PROVIDER_VALUES = frozenset({"greenhouse", "lever", "ashby"})
_SAFE_OPERATION_VALUES = frozenset({"inspect", "fill", "submit", "observe"})
_SAFE_OUTCOME_VALUES = frozenset({"completed", "failed", "paused", "manual_review"})
_SAFE_REASON_CODES = frozenset({"unknown_field", "sensitive_field", "legal_question", "address_question", "form_drift"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
_SAFE_LEDGER_EVENT_KINDS = frozenset({"inspect", "fill", "submit", "observe", "pause"})
_SAFE_LEDGER_STAGES = frozenset({"preflight", "inspect", "fill", "submit", "observe", "execution", "recovery"})
_SAFE_LEDGER_REASON_CODES = frozenset({
    "account_creation", "adapter_failure", "address", "address_question", "attestation",
    "breaker_open", "captcha", "daily_cap", "form_drift", "kill_switch", "legal_question",
    "login", "mfa", "new_question", "policy_expired", "policy_revoked", "posting_drift",
    "internal_failure", "preflight_failure",
    "provider_challenge", "rate_limit", "required_demographics", "salary",
    "salary_exact_number", "salary_unverified", "security", "security_challenge",
    "sensitive_field", "sensitive_question", "stale_running_dispatch",
    "stale_running_no_dispatch", "street_address", "unexpected_redirect",
    "unknown_field", "unknown_question",
})
_LEDGER_VERSION = "application-automation/evidence-ledger/v1"


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _is_count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 1_000_000


def _is_bool(value: Any) -> bool:
    return type(value) is bool


_METADATA_RULES: Mapping[str, Callable[[Any], bool]] = {
    "provider": lambda value: value in _SAFE_PROVIDER_VALUES,
    "operation": lambda value: value in _SAFE_OPERATION_VALUES,
    "outcome": lambda value: value in _SAFE_OUTCOME_VALUES,
    "reason_code": lambda value: value in _SAFE_REASON_CODES,
    "form_fingerprint": _is_identifier,
    "role_id": _is_identifier,
    "policy_version": _is_identifier,
    "fixture_version": _is_identifier,
    "content_sha256": _is_sha256,
    "field_count": _is_count,
    "attempt": _is_count,
    "requires_manual_review": _is_bool,
}


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class QuarantinedContent:
    kind: str
    sha256: str
    byte_count: int
    state: str = "quarantined"


@dataclass(frozen=True)
class EvidenceRecord:
    kind: str
    metadata: Mapping[str, Any]
    observed_at: str
    source_url: str | None = None
    source_domain: str | None = None
    content: QuarantinedContent | None = None


@dataclass(frozen=True)
class EvidenceEvent:
    sequence: int
    event_type: str
    occurred_at: str
    metadata: Mapping[str, Any]
    previous_hash: str | None
    event_hash: str


@dataclass(frozen=True)
class Tombstone:
    content_sha256: str
    reason: str
    deleted_at: str
@dataclass(frozen=True)
class EvidenceLedgerEvent:
    sequence: int
    event_kind: str
    stage: str
    reason_code: str | None
    receipt_present: bool
    confirmation: bool
    dispatch_present: bool
    occurred_at: str
    previous_hash: str | None
    event_hash: str


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceError("evidence datetimes must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _validate_canonical_datetime(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    return _canonical_datetime(parsed) == value


def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise EvidenceError("evidence metadata must be a mapping")
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or key not in _METADATA_RULES:
            raise EvidenceError(f"evidence metadata key is not permitted: {key!r}")
        if isinstance(value, (Mapping, list, tuple, set)) or not _METADATA_RULES[key](value):
            raise EvidenceError(f"evidence metadata value is invalid: {key}")
        safe[key] = value
    return safe


def normalize_url(value: str) -> str:
    """Return a canonical HTTP(S) URL without credentials, query, or fragment."""
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise EvidenceError("evidence URLs must be absolute HTTP(S) URLs")
    if parts.username or parts.password:
        raise EvidenceError("evidence URLs may not include credentials")
    scheme = parts.scheme.lower()
    host = parts.hostname.lower().rstrip(".")
    try:
        port = parts.port
    except ValueError as error:
        raise EvidenceError("evidence URL has an invalid port") from error
    authority = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    return urlunsplit((scheme, authority, "", "", ""))


def normalize_domain(value: str) -> str:
    """Normalize a host or URL to its hostname; no registrable-domain guesses are made."""
    candidate = value if "://" in value else f"https://{value}"
    return urlsplit(normalize_url(candidate)).hostname or ""


def redact_metadata(value: Any, *, key: str = "") -> Any:
    """Compatibility wrapper that validates instead of persisting redacted sensitive data."""
    del key
    return _safe_metadata(value)


def quarantine_content(kind: str, content: bytes | str | None) -> QuarantinedContent | None:
    """Hash content but never return it. Unknown content is also quarantined."""
    if kind not in _SAFE_KINDS:
        raise EvidenceError("evidence kind is not permitted")
    if content is None:
        return None
    if not isinstance(content, (bytes, str)):
        raise EvidenceError("evidence content must be bytes or a string")
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return QuarantinedContent(kind=kind, sha256=sha256(raw).hexdigest(), byte_count=len(raw))


def make_evidence(kind: str, metadata: Mapping[str, Any], *, source_url: str | None = None, content: bytes | str | None = None, observed_at: datetime | None = None) -> EvidenceRecord:
    if kind not in _SAFE_KINDS:
        raise EvidenceError("evidence kind is not permitted")
    normalized_url = normalize_url(source_url) if source_url else None
    instant = observed_at or datetime.now(timezone.utc)
    return EvidenceRecord(
        kind=kind,
        metadata=_safe_metadata(metadata),
        observed_at=_canonical_datetime(instant),
        source_url=normalized_url,
        source_domain=normalize_domain(normalized_url) if normalized_url else None,
        content=quarantine_content(kind, content),
    )


def _event_payload(sequence: int, event_type: str, occurred_at: str, metadata: Mapping[str, Any], previous_hash: str | None) -> dict[str, Any]:
    if type(sequence) is not int or sequence < 1:
        raise EvidenceError("evidence event sequence is invalid")
    if event_type not in _SAFE_EVENT_TYPES:
        raise EvidenceError("evidence event type is not permitted")
    if not _validate_canonical_datetime(occurred_at):
        raise EvidenceError("evidence event timestamp is not canonical")
    if previous_hash is not None and not _is_sha256(previous_hash):
        raise EvidenceError("evidence previous hash is invalid")
    return {"domain": "application-automation/evidence-event/v1", "sequence": sequence, "event_type": event_type, "occurred_at": occurred_at, "metadata": _safe_metadata(metadata), "previous_hash": previous_hash}


def append_event(events: Iterable[EvidenceEvent], event_type: str, metadata: Mapping[str, Any], *, occurred_at: datetime | None = None) -> EvidenceEvent:
    chain = tuple(events)
    if chain and not verify_event_chain(chain):
        raise EvidenceError("cannot append to a tampered evidence chain")
    sequence = len(chain) + 1
    instant = _canonical_datetime(occurred_at or datetime.now(timezone.utc))
    previous = chain[-1].event_hash if chain else None
    payload = _event_payload(sequence, event_type, instant, metadata, previous)
    return EvidenceEvent(sequence, event_type, instant, payload["metadata"], previous, sha256(_canonical(payload)).hexdigest())


def verify_event_chain(events: Iterable[EvidenceEvent]) -> bool:
    previous: str | None = None
    for sequence, event in enumerate(events, start=1):
        if event.sequence != sequence or event.previous_hash != previous:
            return False
        try:
            payload = _event_payload(event.sequence, event.event_type, event.occurred_at, event.metadata, event.previous_hash)
        except EvidenceError:
            return False
        if sha256(_canonical(payload)).hexdigest() != event.event_hash:
            return False
        previous = event.event_hash
    return True


def _ledger_payload(sequence: int, event_kind: str, stage: str, reason_code: str | None,
                    receipt_present: bool, confirmation: bool, dispatch_present: bool,
                    receipt_digest: str | None, attestation_digest: str | None,
                    key_version: int, occurred_at: str, previous_hash: str | None) -> dict[str, Any]:
    if type(sequence) is not int or sequence < 1:
        raise EvidenceError("evidence ledger sequence is invalid")
    if not isinstance(event_kind, str) or not isinstance(stage, str):
        raise EvidenceError("evidence ledger kind or stage is invalid")
    if event_kind not in _SAFE_LEDGER_EVENT_KINDS or stage not in _SAFE_LEDGER_STAGES:
        raise EvidenceError("evidence ledger kind or stage is not permitted")
    if reason_code is not None and (not isinstance(reason_code, str) or reason_code not in _SAFE_LEDGER_REASON_CODES):
        raise EvidenceError("evidence ledger reason code is not permitted")
    if (event_kind == "pause") != (reason_code is not None):
        raise EvidenceError("evidence ledger pause reason is invalid")
    if not all(_is_bool(value) for value in (receipt_present, confirmation, dispatch_present)):
        raise EvidenceError("evidence ledger facts must be booleans")
    if receipt_digest is not None and not _is_sha256(receipt_digest):
        raise EvidenceError("evidence ledger receipt digest is invalid")
    if attestation_digest is not None and not _is_sha256(attestation_digest):
        raise EvidenceError("evidence ledger attestation digest is invalid")
    if receipt_present != _is_sha256(receipt_digest) or confirmation != _is_sha256(attestation_digest):
        raise EvidenceError("evidence ledger digest facts are invalid")
    if type(key_version) is not int or key_version < 1:
        raise EvidenceError("evidence authentication key version is invalid")
    if event_kind in {"inspect", "fill"} and (receipt_present or confirmation or dispatch_present):
        raise EvidenceError("evidence ledger event has impossible dispatch facts")
    if event_kind == "pause" and (receipt_present or confirmation):
        raise EvidenceError("evidence ledger pause has impossible receipt facts")
    if not _validate_canonical_datetime(occurred_at):
        raise EvidenceError("evidence ledger timestamp is not canonical")
    if previous_hash is not None and not _is_sha256(previous_hash):
        raise EvidenceError("evidence ledger previous hash is invalid")
    return {
        "domain": _LEDGER_VERSION, "sequence": sequence, "event_kind": event_kind,
        "stage": stage, "reason_code": reason_code, "receipt_present": receipt_present,
        "confirmation": confirmation, "dispatch_present": dispatch_present,
        "receipt_digest": receipt_digest, "attestation_digest": attestation_digest,
        "key_version": key_version, "occurred_at": occurred_at, "previous_hash": previous_hash,
    }


def _authentication_key(value: bytes | str) -> bytes:
    if not isinstance(value, (bytes, str)):
        raise EvidenceError("evidence authentication key is required")
    key = value.encode("utf-8") if isinstance(value, str) else value
    if len(key) < 32:
        raise EvidenceError("evidence authentication key must be at least 32 bytes")
    return key


def _ledger_hmac(authentication_key: bytes, application_id: str, dispatch_id: str | None,
                 payload: Mapping[str, Any]) -> str:
    if not isinstance(application_id, str) or not application_id:
        raise EvidenceError("evidence application id is invalid")
    if dispatch_id is not None and (not isinstance(dispatch_id, str) or not dispatch_id):
        raise EvidenceError("evidence dispatch id is invalid")
    envelope = {
        "domain": "application-automation/evidence-ledger-hmac/v1",
        "application_id": application_id,
        "dispatch_id": dispatch_id,
        "sequence": payload["sequence"],
        "previous_hmac": payload["previous_hash"],
        "occurred_at": payload["occurred_at"],
        "payload": payload,
    }
    return hmac.new(authentication_key, _canonical(envelope), "sha256").hexdigest()


def _decode_ledger_row(row: sqlite3.Row, sequence: int, previous_hash: str | None,
                       authentication_key: bytes) -> EvidenceLedgerEvent:
    if row["content_sha256"] is not None or row["revision"] != 1:
        raise EvidenceError("evidence ledger row is invalid")
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, ValueError) as error:
        raise EvidenceError("evidence ledger metadata is invalid") from error
    required = {"domain", "sequence", "event_kind", "stage", "reason_code", "receipt_present",
                "confirmation", "dispatch_present", "occurred_at", "previous_hash", "event_hash",
                "key_version", "receipt_digest", "attestation_digest"}
    if not isinstance(metadata, dict) or set(metadata) != required or metadata["domain"] != _LEDGER_VERSION:
        raise EvidenceError("evidence ledger row is legacy or invalid")
    payload = _ledger_payload(
        metadata["sequence"], metadata["event_kind"], metadata["stage"], metadata["reason_code"],
        metadata["receipt_present"], metadata["confirmation"], metadata["dispatch_present"],
        metadata["receipt_digest"], metadata["attestation_digest"], metadata["key_version"],
        metadata["occurred_at"], metadata["previous_hash"],
    )
    expected_hmac = _ledger_hmac(authentication_key, row["application_id"], row["dispatch_id"], payload)
    if (row["kind"] != metadata["event_kind"] or row["created_at"] != metadata["occurred_at"]
            or metadata["sequence"] != sequence or metadata["previous_hash"] != previous_hash
            or metadata["dispatch_present"] != (row["dispatch_id"] is not None)
            or metadata["key_version"] != row["key_version"]
            or metadata["receipt_digest"] != row["receipt_digest"]
            or metadata["attestation_digest"] != row["attestation_digest"]
            or metadata["receipt_present"] != _is_sha256(metadata["receipt_digest"])
            or metadata["confirmation"] != _is_sha256(metadata["attestation_digest"])
            or not _is_sha256(metadata["event_hash"])
            or not hmac.compare_digest(expected_hmac, metadata["event_hash"])):
        raise EvidenceError("evidence ledger chain is invalid")
    return EvidenceLedgerEvent(
        metadata["sequence"], metadata["event_kind"], metadata["stage"], metadata["reason_code"],
        metadata["receipt_present"], metadata["confirmation"], metadata["dispatch_present"],
        metadata["occurred_at"], metadata["previous_hash"], metadata["event_hash"],
    )


def _ledger_head_hmac(authentication_key: bytes, application_id: str, key_version: int,
                      event_count: int, event_hash: str | None) -> str:
    if not isinstance(application_id, str) or not application_id:
        raise EvidenceError("evidence application id is invalid")
    if type(key_version) is not int or key_version < 1:
        raise EvidenceError("evidence authentication key version is invalid")
    if type(event_count) is not int or event_count < 0:
        raise EvidenceError("evidence ledger count is invalid")
    if event_hash is not None and not _is_sha256(event_hash):
        raise EvidenceError("evidence ledger head is invalid")
    if (event_count == 0) != (event_hash is None):
        raise EvidenceError("evidence ledger head is inconsistent")
    return hmac.new(authentication_key, _canonical({
        "domain": "application-automation/evidence-ledger-head/v1",
        "application_id": application_id,
        "key_version": key_version,
        "event_count": event_count,
        "event_hash": event_hash,
    }), "sha256").hexdigest()


def _fixture_confirmation(dispatch: sqlite3.Row, receipt_digest: str | None,
                          attestation_digest: str | None) -> bool:
    return (
        dispatch["state"] == "confirmed"
        and dispatch["environment"] == "fixture"
        and dispatch["started_at"] is not None
        and dispatch["finished_at"] is not None
        and _is_sha256(receipt_digest)
        and _is_sha256(attestation_digest)
    )


def _verified_ledger(connection: sqlite3.Connection, application_id: str, authentication_key: bytes,
                     key_version: int) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    if not isinstance(application_id, str) or not application_id:
        raise EvidenceError("evidence application id is invalid")
    if type(key_version) is not int or key_version < 1:
        raise EvidenceError("evidence authentication key version is invalid")
    head = connection.execute(
        "SELECT event_count,event_hash,head_hmac,key_version FROM evidence_ledger_heads WHERE application_id=?",
        (application_id,),
    ).fetchone()
    if head is None:
        raise EvidenceError("evidence ledger head is unavailable")
    if (type(head["event_count"]) is not int or type(head["key_version"]) is not int
            or not _is_sha256(head["head_hmac"]) or head["key_version"] != key_version):
        raise EvidenceError("evidence ledger head is invalid")
    rows = connection.execute(
        "SELECT application_id,dispatch_id,kind,metadata_json,content_sha256,created_at,revision,"
        "ledger_sequence,key_version,receipt_digest,attestation_digest "
        "FROM evidence WHERE application_id=? ORDER BY ledger_sequence ASC",
        (application_id,),
    ).fetchall()
    previous_hash: str | None = None
    for sequence, row in enumerate(rows, start=1):
        if row["ledger_sequence"] != sequence or row["key_version"] != key_version:
            raise EvidenceError("evidence ledger sequence is invalid")
        previous_hash = _decode_ledger_row(row, sequence, previous_hash, authentication_key).event_hash
    if len(rows) != head["event_count"] or head["event_hash"] != previous_hash:
        raise EvidenceError("evidence ledger head does not match rows")
    if not hmac.compare_digest(
        head["head_hmac"],
        _ledger_head_hmac(authentication_key, application_id, key_version, len(rows), previous_hash),
    ):
        raise EvidenceError("evidence ledger head authentication failed")
    return head, rows


def verify_evidence_ledger(connection: sqlite3.Connection, application_id: str,
                           *, authentication_key: bytes | str, key_version: int) -> bool:
    """Verify an append-only ledger against its authenticated persisted head."""
    try:
        key = _authentication_key(authentication_key)
        _, rows = _verified_ledger(connection, application_id, key, key_version)
    except (EvidenceError, sqlite3.Error, KeyError, TypeError):
        return False
    return bool(rows)


def append_evidence_event(connection: sqlite3.Connection, application_id: str, dispatch_id: str | None,
                          event_kind: str, *, authentication_key: bytes | str, key_version: int,
                          stage: str, reason_code: str | None = None,
                          receipt_digest: str | None = None,
                          attestation_digest: str | None = None,
                          occurred_at: datetime | None = None) -> EvidenceLedgerEvent:
    """Append a fixture-only submission attempt or deterministic confirmation proof."""
    key = _authentication_key(authentication_key)
    if type(key_version) is not int or key_version < 1:
        raise EvidenceError("evidence authentication key version is invalid")
    timestamp = _canonical_datetime(occurred_at or datetime.now(timezone.utc))
    with transaction(connection, immediate=True):
        if connection.execute("SELECT 1 FROM applications WHERE id=?", (application_id,)).fetchone() is None:
            raise EvidenceError("evidence application is unavailable")
        dispatch = None
        if dispatch_id is not None:
            dispatch = connection.execute(
                "SELECT application_id,state,environment,started_at,finished_at FROM dispatches WHERE id=?",
                (dispatch_id,),
            ).fetchone()
            if dispatch is None or dispatch["application_id"] != application_id:
                raise EvidenceError("evidence dispatch does not belong to application")
        if event_kind in {"submit", "observe"} and dispatch is None:
            raise EvidenceError("terminal evidence requires a dispatch")
        if event_kind == "submit" and (receipt_digest is not None or attestation_digest is not None):
            raise EvidenceError("submit events record attempts, not confirmation proof")
        confirmation = event_kind == "observe" and _fixture_confirmation(
            dispatch, receipt_digest, attestation_digest
        ) if dispatch is not None else False
        if event_kind == "observe" and not confirmation:
            raise EvidenceError("observation requires deterministic fixture confirmation proof")
        if event_kind not in {"observe", "submit"} and (
            receipt_digest is not None or attestation_digest is not None
        ):
            raise EvidenceError("only fixture confirmation may carry receipt proof")
        head = connection.execute(
            "SELECT event_count,event_hash,head_hmac,key_version FROM evidence_ledger_heads WHERE application_id=?",
            (application_id,),
        ).fetchone()
        if head is None:
            if connection.execute(
                "SELECT 1 FROM evidence WHERE application_id=? LIMIT 1",
                (application_id,),
            ).fetchone() is not None:
                raise EvidenceError("evidence ledger has orphan rows")
            previous_hash, sequence = None, 1
            connection.execute(
                "INSERT INTO evidence_ledger_heads(application_id,key_version,event_count,event_hash,head_hmac) "
                "VALUES(?,?,?,?,?)",
                (application_id, key_version, 0, None, _ledger_head_hmac(key, application_id, key_version, 0, None)),
            )
        else:
            head, _ = _verified_ledger(connection, application_id, key, key_version)
            previous_hash, sequence = head["event_hash"], head["event_count"] + 1
        payload = _ledger_payload(
            sequence, event_kind, stage, reason_code, _is_sha256(receipt_digest),
            confirmation, dispatch_id is not None, receipt_digest, attestation_digest, key_version,
            timestamp, previous_hash,
        )
        event_hash = _ledger_hmac(key, application_id, dispatch_id, payload)
        metadata = {**payload, "event_hash": event_hash}
        connection.execute(
            "INSERT INTO evidence(id,application_id,dispatch_id,kind,metadata_json,content_sha256,created_at,"
            "revision,ledger_sequence,key_version,receipt_digest,attestation_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), application_id, dispatch_id, event_kind,
             json.dumps(metadata, sort_keys=True, separators=(",", ":")), None, timestamp, 1,
             sequence, key_version, receipt_digest, attestation_digest),
        )
        connection.execute(
            "UPDATE evidence_ledger_heads SET event_count=?,event_hash=?,head_hmac=? WHERE application_id=?",
            (sequence, event_hash, _ledger_head_hmac(key, application_id, key_version, sequence, event_hash),
             application_id),
        )
    return EvidenceLedgerEvent(sequence, event_kind, stage, reason_code, _is_sha256(receipt_digest),
                               confirmation, dispatch_id is not None, timestamp, previous_hash, event_hash)

def retention_due(recorded_at: datetime, retain_until: datetime, *, now: datetime | None = None) -> bool:
    _canonical_datetime(recorded_at)
    _canonical_datetime(retain_until)
    instant = now or datetime.now(timezone.utc)
    _canonical_datetime(instant)
    return instant >= retain_until


def tombstone(content: QuarantinedContent, reason: str, *, deleted_at: datetime | None = None) -> Tombstone:
    if reason not in _SAFE_REASON_CODES:
        raise EvidenceError("tombstone reason code is not permitted")
    instant = _canonical_datetime(deleted_at or datetime.now(timezone.utc))
    return Tombstone(content.sha256, reason, instant)
