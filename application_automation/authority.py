"""Fail-closed SQLite authority transactions for the fixture-only Phase-0 surface."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import sqlite3
from typing import Any, Callable, Mapping
from uuid import uuid4

from .crypto import canonical_json, domain_hmac, encrypt_aes_gcm, verify_domain_hmac
from .store import transaction


class AuthorityError(ValueError):
    """Raised when an authority operation cannot be proven safe."""


def _now(value: datetime | None = None) -> datetime:
    value = datetime.now(UTC) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AuthorityError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AuthorityError("stored timestamp is invalid") from exc
    return _now(parsed)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _id() -> str:
    return str(uuid4())


def _binding(value: Mapping[str, Any]) -> str:
    return canonical_json(value).decode("utf-8")


def _plaintext_digest(hmac_key: bytes, value: bytes) -> str:
    if not isinstance(value, bytes):
        raise AuthorityError("manual value must be bytes")
    return domain_hmac(hmac_key, "authority.manual.value_digest.v1", {"value": value.hex()})


def challenge_secret_hmac(hmac_key: bytes, secret: str) -> str:
    return domain_hmac(hmac_key, "authority.challenge.secret.v1", secret)


def _alias_binding(profile: sqlite3.Row, alias: sqlite3.Row, old_assertion: sqlite3.Row,
                   new_assertion: sqlite3.Row) -> dict[str, Any]:
    return {
        "purpose": "alias_reassign",
        "profile_id": profile["id"],
        "profile_revision": profile["revision"],
        "alias_id": alias["id"],
        "alias_revision": alias["revision"],
        "provider": alias["provider"],
        "normalized_label": alias["normalized_label"],
        "semantic_scope": alias["semantic_scope"],
        "form_fingerprint": alias["form_fingerprint"],
        "old_assertion_id": old_assertion["id"],
        "old_assertion_revision": old_assertion["revision"],
        "old_value_hmac": old_assertion["value_hmac"],
        "new_assertion_id": new_assertion["id"],
        "new_assertion_revision": new_assertion["revision"],
        "new_value_hmac": new_assertion["value_hmac"],
    }
def _session_binding(session: sqlite3.Row, *, profile_id: str, application_id: str | None = None) -> dict[str, Any]:
    binding = {
        "candidate_profile_id": profile_id,
        "web_session_id": session["id"],
        "web_session_profile_id": session["profile_id"],
        "web_session_revision": session["revision"],
        "web_session_state": session["state"],
        "web_session_expires_at": session["expires_at"],
        "service_instance_id": session["service_instance_id"],
        "dashboard_instance_id": session["dashboard_instance_id"],
    }
    if application_id is not None:
        binding["application_id"] = application_id
    return binding


def _validate_challenge_session(connection: sqlite3.Connection, challenge: sqlite3.Row | None,
                                profile_id: str, now: datetime, *, application_id: str | None = None) -> sqlite3.Row:
    if challenge is None or challenge["web_session_id"] is None:
        raise AuthorityError("challenge session is unavailable")
    if application_id is not None:
        application = connection.execute(
            "SELECT profile_id FROM applications WHERE id=?", (application_id,)
        ).fetchone()
        if application is None or application["profile_id"] != profile_id:
            raise AuthorityError("challenge application does not match")
    session = connection.execute("SELECT * FROM sessions WHERE id=?", (challenge["web_session_id"],)).fetchone()
    if (
        session is None or session["profile_id"] != profile_id or session["state"] != "active"
        or _parse_timestamp(session["expires_at"]) <= now
        or session["service_instance_id"] != challenge["service_instance_id"]
        or session["dashboard_instance_id"] != challenge["dashboard_instance_id"]
    ):
        raise AuthorityError("challenge session does not match")
    return session


def _alias_event_payload(
    *, event_id: str, event_kind: str, created_at: str,
    profile_id: str, profile_revision: int,
    assertion_id: str, assertion_revision: int, assertion_value_hmac: str,
    alias_id: str, alias_revision: int, provider: str, normalized_label: str,
    semantic_scope: str, form_fingerprint: str | None,
    confirmation_event_id: str, confirmation_event_revision: int,
    source_alias_id: str | None, authorization_binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The exact canonical versioned payload persisted for one alias-authority event."""
    return {
        "event_id": event_id,
        "event_kind": event_kind,
        "created_at": created_at,
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "assertion_id": assertion_id,
        "assertion_revision": assertion_revision,
        "assertion_value_hmac": assertion_value_hmac,
        "alias_id": alias_id,
        "alias_revision": alias_revision,
        "provider": provider,
        "normalized_label": normalized_label,
        "semantic_scope": semantic_scope,
        "form_fingerprint": form_fingerprint,
        "confirmation_event_id": confirmation_event_id,
        "confirmation_event_revision": confirmation_event_revision,
        "source_alias_id": source_alias_id,
        "authorization_binding": dict(authorization_binding) if authorization_binding is not None else None,
    }


def _verify_durable_alias(connection: sqlite3.Connection, profile: sqlite3.Row, alias: sqlite3.Row,
                          assertion: sqlite3.Row, hmac_key: bytes) -> None:
    """Authority derives only from the exact payload persisted at event creation.

    No challenge-table scan and no timestamp inference: the event this alias names as its
    own confirmation is loaded directly, its persisted payload is authenticated against its
    stored HMAC, and every immutable resulting claim in that payload is compared against the
    live profile/assertion/alias rows. The source alias, if any, is named explicitly in the
    payload rather than inferred.
    """
    event = connection.execute(
        "SELECT * FROM assertion_events WHERE id=? AND profile_id=? AND assertion_id=? AND event_kind='alias_confirmed'",
        (alias["confirmation_event_id"], profile["id"], assertion["id"]),
    ).fetchone()
    if event is None:
        raise AuthorityError("durable alias confirmation does not match")
    try:
        payload = json.loads(event["payload_json"]) if event["payload_json"] is not None else None
    except (TypeError, ValueError):
        payload = None
    if (
        not isinstance(payload, dict)
        or not verify_domain_hmac(hmac_key, "authority.alias.event.v3", payload, event["payload_hmac"])
        or payload.get("event_id") != event["id"]
        or payload.get("event_kind") != "alias_confirmed"
        or payload.get("profile_id") != profile["id"]
        or payload.get("profile_revision") != profile["revision"]
        or payload.get("assertion_id") != assertion["id"]
        or payload.get("assertion_revision") != assertion["revision"]
        or payload.get("assertion_value_hmac") != assertion["value_hmac"]
        or payload.get("alias_id") != alias["id"]
        or payload.get("alias_revision") != alias["revision"]
        or payload.get("provider") != alias["provider"]
        or payload.get("normalized_label") != alias["normalized_label"]
        or payload.get("semantic_scope") != alias["semantic_scope"]
        or payload.get("form_fingerprint") != alias["form_fingerprint"]
        or payload.get("confirmation_event_id") != alias["confirmation_event_id"]
        or payload.get("confirmation_event_revision") != event["revision"]
        or not (payload.get("source_alias_id") is None or isinstance(payload.get("source_alias_id"), str))
    ):
        raise AuthorityError("durable alias confirmation does not match")


def _manual_binding(checkpoint: sqlite3.Row, resolution: sqlite3.Row, run: sqlite3.Row,
                    policy: sqlite3.Row, profile: sqlite3.Row, application: sqlite3.Row,
                    session: sqlite3.Row, plaintext: bytes, ttl: timedelta,
                    ceilings: Mapping[str, datetime], hmac_key: bytes) -> dict[str, Any]:
    return {
        "purpose": "manual_completion",
        "checkpoint_id": checkpoint["id"],
        "checkpoint_revision": checkpoint["revision"],
        "checkpoint_generation": checkpoint["generation"],
        "field_resolution_id": resolution["id"],
        "resolution_revision": resolution["revision"],
        "resolution_generation": resolution["generation"],
        "field_key": resolution["field_key"],
        "run_id": run["id"],
        "run_revision": run["revision"],
        "run_state": run["state"],
        "batch_policy_id": policy["id"],
        "batch_policy_revision": policy["revision"],
        "batch_policy_state": policy["state"],
        "batch_policy_expires_at": _timestamp(ceilings["persisted_policy"]),
        "candidate_profile_revision": profile["revision"],
        "candidate_profile_state": profile["state"],
        "application_profile_id": application["profile_id"],
        "application_role_id": application["role_id"],
        "application_revision": application["revision"],
        "application_state": application["state"],
        **_session_binding(session, profile_id=profile["id"], application_id=application["id"]),
        "value_digest": _plaintext_digest(hmac_key, plaintext),
        "ttl_seconds": int(ttl.total_seconds()),
        "expires_at_ceilings": {name: _timestamp(value) for name, value in ceilings.items()},
    }


@dataclass(frozen=True)
class ChallengeConsumptionRequest:
    challenge_id: str
    secret: str
    binding: Mapping[str, Any]
    expected_revision: int


@dataclass(frozen=True)
class ChallengeConsumptionResult:
    challenge_id: str
    consumed_at: datetime
    revision: int


def _validate_challenge(row: sqlite3.Row | None, request: ChallengeConsumptionRequest, *, hmac_key: bytes,
                        now: datetime, purpose: str | None = None, checkpoint_id: str | None = None,
                        field_resolution_id: str | None = None) -> None:
    if row is None or row["state"] != "active" or row["revision"] != request.expected_revision:
        raise AuthorityError("challenge is stale or unavailable")
    if purpose is not None and row["purpose"] != purpose:
        raise AuthorityError("challenge purpose does not match")
    if checkpoint_id is not None and (row["checkpoint_id"] != checkpoint_id or row["field_resolution_id"] != field_resolution_id):
        raise AuthorityError("challenge identity does not match")
    if _parse_timestamp(row["expires_at"]) <= now:
        raise AuthorityError("challenge has expired")
    if row["binding_json"] != _binding(request.binding):
        raise AuthorityError("challenge binding does not match")
    if not verify_domain_hmac(hmac_key, "authority.challenge.secret.v1", request.secret, row["secret_hmac"]):
        raise AuthorityError("challenge secret does not match")


def consume_fresh_challenge(connection: sqlite3.Connection, request: ChallengeConsumptionRequest, *, hmac_key: bytes,
                            trusted_clock: Callable[[], datetime]) -> ChallengeConsumptionResult:
    now = _now(trusted_clock())
    with transaction(connection, immediate=True):
        row = connection.execute("SELECT * FROM one_use_challenges WHERE id=?", (request.challenge_id,)).fetchone()
        _validate_challenge(row, request, hmac_key=hmac_key, now=now)
        if connection.execute("UPDATE one_use_challenges SET state='consumed', consumed_at=?, revision=revision+1 WHERE id=? AND state='active' AND revision=?", (_timestamp(now), request.challenge_id, request.expected_revision)).rowcount != 1:
            raise AuthorityError("challenge consumption raced")
    return ChallengeConsumptionResult(request.challenge_id, now, request.expected_revision + 1)


@dataclass(frozen=True)
class AliasReassignmentRequest:
    challenge_id: str
    secret: str
    binding: Mapping[str, Any]
    expected_challenge_revision: int
    profile_id: str
    old_alias_id: str
    expected_old_alias_revision: int
    old_assertion_id: str
    expected_old_assertion_revision: int
    new_assertion_id: str
    expected_new_assertion_revision: int


@dataclass(frozen=True)
class AliasReassignmentResult:
    alias_id: str
    revoke_event_id: str
    confirm_event_id: str
    profile_revision: int
    assertion_snapshot_id: str
    assertion_snapshot_revision: int


def reassign_alias(connection: sqlite3.Connection, request: AliasReassignmentRequest, *, hmac_key: bytes,
                   trusted_clock: Callable[[], datetime]) -> AliasReassignmentResult:
    if request.old_assertion_id == request.new_assertion_id:
        raise AuthorityError("alias reassignment requires different assertions")
    now = _now(trusted_clock())
    timestamp = _timestamp(now)
    revoke_event_id, confirm_event_id, alias_id = _id(), _id(), _id()
    with transaction(connection, immediate=True):
        profile = connection.execute("SELECT * FROM candidate_profiles WHERE id=? AND state='active'", (request.profile_id,)).fetchone()
        old_alias = connection.execute("SELECT * FROM question_aliases WHERE id=? AND profile_id=? AND revoked_at IS NULL", (request.old_alias_id, request.profile_id)).fetchone()
        old_assertion = connection.execute("SELECT * FROM candidate_assertions WHERE id=? AND profile_id=? AND state='active'", (request.old_assertion_id, request.profile_id)).fetchone()
        new_assertion = connection.execute("SELECT * FROM candidate_assertions WHERE id=? AND profile_id=? AND state='active'", (request.new_assertion_id, request.profile_id)).fetchone()
        challenge = connection.execute("SELECT * FROM one_use_challenges WHERE id=?", (request.challenge_id,)).fetchone()
        if profile is None or old_alias is None or old_assertion is None or new_assertion is None or old_alias["assertion_id"] != request.old_assertion_id:
            raise AuthorityError("alias reassignment identity is unavailable")
        if old_alias["revision"] != request.expected_old_alias_revision or old_assertion["revision"] != request.expected_old_assertion_revision or new_assertion["revision"] != request.expected_new_assertion_revision:
            raise AuthorityError("alias reassignment is stale")
        _verify_durable_alias(connection, profile, old_alias, old_assertion, hmac_key)
        session = _validate_challenge_session(connection, challenge, request.profile_id, now)
        derived_binding = {
            **_alias_binding(profile, old_alias, old_assertion, new_assertion),
            **_session_binding(session, profile_id=request.profile_id),
        }
        if _binding(request.binding) != _binding(derived_binding):
            raise AuthorityError("alias reassignment binding does not match persisted identity")
        challenge_request = ChallengeConsumptionRequest(
            request.challenge_id, request.secret, derived_binding, request.expected_challenge_revision,
        )
        _validate_challenge(challenge, challenge_request, hmac_key=hmac_key, now=now, purpose="alias_reassign")
        if challenge["candidate_profile_id"] != request.profile_id or challenge["alias_id"] != request.old_alias_id:
            raise AuthorityError("alias reassignment challenge identity does not match")
        revoke_record = _alias_event_payload(
            event_id=revoke_event_id, event_kind="alias_revoked", created_at=timestamp,
            profile_id=request.profile_id, profile_revision=profile["revision"] + 1,
            assertion_id=request.old_assertion_id, assertion_revision=request.expected_old_assertion_revision + 1,
            assertion_value_hmac=old_assertion["value_hmac"],
            alias_id=request.old_alias_id, alias_revision=request.expected_old_alias_revision + 1,
            provider=old_alias["provider"], normalized_label=old_alias["normalized_label"],
            semantic_scope=old_alias["semantic_scope"], form_fingerprint=old_alias["form_fingerprint"],
            confirmation_event_id=old_alias["confirmation_event_id"], confirmation_event_revision=1,
            source_alias_id=None, authorization_binding=derived_binding,
        )
        connection.execute(
            "INSERT INTO assertion_events(id,profile_id,assertion_id,event_kind,payload_hmac,payload_json,created_at,revision) VALUES(?,?,?,?,?,?,?,1)",
            (revoke_event_id, request.profile_id, request.old_assertion_id, "alias_revoked",
             domain_hmac(hmac_key, "authority.alias.event.v3", revoke_record),
             canonical_json(revoke_record).decode("utf-8"), timestamp),
        )
        if connection.execute("UPDATE question_aliases SET revoked_at=?, revision=revision+1 WHERE id=? AND revoked_at IS NULL AND revision=?", (timestamp, request.old_alias_id, request.expected_old_alias_revision)).rowcount != 1:
            raise AuthorityError("alias reassignment raced")
        confirm_record = _alias_event_payload(
            event_id=confirm_event_id, event_kind="alias_confirmed", created_at=timestamp,
            profile_id=request.profile_id, profile_revision=profile["revision"] + 1,
            assertion_id=request.new_assertion_id, assertion_revision=request.expected_new_assertion_revision + 1,
            assertion_value_hmac=new_assertion["value_hmac"],
            alias_id=alias_id, alias_revision=1,
            provider=old_alias["provider"], normalized_label=old_alias["normalized_label"],
            semantic_scope=old_alias["semantic_scope"], form_fingerprint=old_alias["form_fingerprint"],
            confirmation_event_id=confirm_event_id, confirmation_event_revision=1,
            source_alias_id=request.old_alias_id, authorization_binding=derived_binding,
        )
        connection.execute(
            "INSERT INTO assertion_events(id,profile_id,assertion_id,event_kind,payload_hmac,payload_json,created_at,revision) VALUES(?,?,?,?,?,?,?,1)",
            (confirm_event_id, request.profile_id, request.new_assertion_id, "alias_confirmed",
             domain_hmac(hmac_key, "authority.alias.event.v3", confirm_record),
             canonical_json(confirm_record).decode("utf-8"), timestamp),
        )
        connection.execute("INSERT INTO question_aliases(id,profile_id,assertion_id,confirmation_event_id,provider,normalized_label,semantic_scope,form_fingerprint,revision,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)", (alias_id, request.profile_id, request.new_assertion_id, confirm_event_id, old_alias["provider"], old_alias["normalized_label"], old_alias["semantic_scope"], old_alias["form_fingerprint"], timestamp))
        for assertion_id, revision in ((request.old_assertion_id, request.expected_old_assertion_revision), (request.new_assertion_id, request.expected_new_assertion_revision)):
            if connection.execute("UPDATE candidate_assertions SET revision=revision+1 WHERE id=? AND state='active' AND revision=?", (assertion_id, revision)).rowcount != 1:
                raise AuthorityError("assertion reassignment raced")
        if connection.execute("UPDATE one_use_challenges SET state='consumed',consumed_at=?,revision=revision+1 WHERE id=? AND state='active' AND revision=?", (timestamp, request.challenge_id, request.expected_challenge_revision)).rowcount != 1:
            raise AuthorityError("challenge reassignment raced")
        if connection.execute("UPDATE candidate_profiles SET revision=revision+1 WHERE id=? AND state='active' AND revision=?", (request.profile_id, profile["revision"])).rowcount != 1:
            raise AuthorityError("assertion snapshot rotation raced")
    return AliasReassignmentResult(alias_id, revoke_event_id, confirm_event_id, profile["revision"] + 1,
                                   request.profile_id, profile["revision"] + 1)


@dataclass(frozen=True)
class RequestValueSecretRequest:
    checkpoint_id: str
    field_resolution_id: str
    field_key: str
    plaintext: bytes
    expected_checkpoint_revision: int
    expected_resolution_revision: int
    key_version: int
    challenge_id: str
    challenge_secret: str
    challenge_binding: Mapping[str, Any]
    expected_challenge_revision: int
    ttl: timedelta | None = None
    run_expires_at: datetime | None = None
    policy_expires_at: datetime | None = None


@dataclass(frozen=True)
class RequestValueSecretResult:
    secret_id: str
    field_resolution_id: str
    generation: int
    expires_at: datetime
    replaced: bool


def create_or_replace_request_value_secret(connection: sqlite3.Connection, request: RequestValueSecretRequest, *,
                                           encryption_key: bytes, hmac_key: bytes,
                                           trusted_clock: Callable[[], datetime]) -> RequestValueSecretResult:
    """Create on the supplied generation or replace it with a new generation atomically."""
    now = _now(trusted_clock())
    if request.key_version <= 0 or not isinstance(request.plaintext, bytes) or not request.plaintext or len(request.plaintext) > 4096:
        raise AuthorityError("request value is not allowed")
    ttl = timedelta(minutes=30) if request.ttl is None else request.ttl
    if ttl <= timedelta(0) or ttl > timedelta(hours=24) or ttl.microseconds:
        raise AuthorityError("request value TTL must be whole seconds, positive, and no more than 24 hours")
    timestamp = _timestamp(now)
    secret_id, replacement_resolution_id = _id(), _id()
    with transaction(connection, immediate=True):
        checkpoint = connection.execute("SELECT * FROM checkpoints WHERE id=? AND state='open'", (request.checkpoint_id,)).fetchone()
        resolution = connection.execute("SELECT * FROM field_resolutions WHERE id=? AND checkpoint_id=? AND state='unresolved'", (request.field_resolution_id, request.checkpoint_id)).fetchone()
        challenge = connection.execute("SELECT * FROM one_use_challenges WHERE id=?", (request.challenge_id,)).fetchone()
        if checkpoint is None or resolution is None or checkpoint["revision"] != request.expected_checkpoint_revision or resolution["revision"] != request.expected_resolution_revision or resolution["field_key"] != request.field_key or resolution["generation"] != checkpoint["generation"]:
            raise AuthorityError("request value resolution is stale")
        run = connection.execute(
            "SELECT * FROM runs WHERE id=? AND application_id=? AND state='awaiting_user'",
            (checkpoint["run_id"], checkpoint["application_id"]),
        ).fetchone()
        application = connection.execute(
            "SELECT * FROM applications WHERE id=? AND state='awaiting_user'", (checkpoint["application_id"],)
        ).fetchone()
        if run is None or application is None or challenge is None:
            raise AuthorityError("request value ownership is unavailable")
        profile = connection.execute(
            "SELECT * FROM candidate_profiles WHERE id=? AND state='active'", (application["profile_id"],)
        ).fetchone()
        if profile is None:
            raise AuthorityError("request value ownership is unavailable")
        try:
            persisted_binding = json.loads(challenge["binding_json"])
            persisted_ceilings = persisted_binding["expires_at_ceilings"]
            policy_id = persisted_binding["batch_policy_id"]
            persisted_run_ceiling = _parse_timestamp(persisted_ceilings["run"])
            persisted_policy_ceiling = _parse_timestamp(persisted_ceilings["policy"])
        except (KeyError, TypeError, ValueError):
            raise AuthorityError("request value authority binding is invalid") from None
        policy = connection.execute(
            "SELECT * FROM batch_policies WHERE id=? AND candidate_profile_id=?",
            (policy_id, application["profile_id"]),
        ).fetchone()
        if (
            policy is None
            or policy["state"] != "active"
            or persisted_binding.get("run_id") != run["id"]
            or persisted_binding.get("run_revision") != run["revision"]
            or persisted_binding.get("run_state") != run["state"]
            or persisted_binding.get("batch_policy_revision") != policy["revision"]
            or persisted_binding.get("batch_policy_state") != policy["state"]
        ):
            raise AuthorityError("request value ownership is unavailable")
        policy_valid_from = _parse_timestamp(policy["valid_from"])
        policy_ceiling = _parse_timestamp(policy["expires_at"])
        if policy_valid_from > now or policy_ceiling <= now:
            raise AuthorityError("request value authority has expired")
        if request.run_expires_at is None or request.policy_expires_at is None:
            raise AuthorityError("run and policy expiry ceilings are required")
        supplied_run_ceiling = _now(request.run_expires_at)
        supplied_policy_ceiling = _now(request.policy_expires_at)
        if supplied_run_ceiling != persisted_run_ceiling or supplied_policy_ceiling != persisted_policy_ceiling:
            raise AuthorityError("request value expiry ceilings do not match persisted authority")
        session = _validate_challenge_session(
            connection, challenge, application["profile_id"], now, application_id=application["id"],
        )
        challenge_ceiling = _parse_timestamp(challenge["expires_at"])
        session_ceiling = _parse_timestamp(session["expires_at"])
        ceilings = {
            "challenge": challenge_ceiling,
            "run": persisted_run_ceiling,
            "policy": persisted_policy_ceiling,
            "persisted_policy": policy_ceiling,
            "session": session_ceiling,
        }
        if any(ceiling <= now for ceiling in ceilings.values()):
            raise AuthorityError("request value authority has expired")
        expires_at = min(now + ttl, *ceilings.values())
        derived_binding = _manual_binding(
            checkpoint, resolution, run, policy, profile, application, session, request.plaintext, ttl, ceilings, hmac_key,
        )
        if _binding(request.challenge_binding) != _binding(derived_binding):
            raise AuthorityError("manual-completion binding does not match persisted identity")
        challenge_request = ChallengeConsumptionRequest(
            request.challenge_id, request.challenge_secret, derived_binding, request.expected_challenge_revision,
        )
        _validate_challenge(challenge, challenge_request, hmac_key=hmac_key, now=now,
                            purpose="manual_completion", checkpoint_id=request.checkpoint_id,
                            field_resolution_id=request.field_resolution_id)
        if connection.execute("SELECT 1 FROM dispatches WHERE application_id=? AND started_at IS NOT NULL", (checkpoint["application_id"],)).fetchone():
            raise AuthorityError("started dispatch requires manual follow-up")
        old = connection.execute("SELECT * FROM request_value_secrets WHERE field_resolution_id=? AND state='active'", (request.field_resolution_id,)).fetchone()
        replaced = old is not None
        target_resolution_id, generation = request.field_resolution_id, checkpoint["generation"]
        if old is not None:
            tombstone = domain_hmac(hmac_key, "authority.request.tombstone.v1", {"secret_id": old["id"], "value_hmac": old["value_hmac"]})
            if connection.execute("UPDATE request_value_secrets SET ciphertext=NULL,nonce=NULL,value_hmac=NULL,state='tombstoned',destroyed_at=?,tombstone_hmac=?,revision=revision+1 WHERE id=? AND state='active' AND revision=?", (timestamp, tombstone, old["id"], old["revision"])).rowcount != 1:
                raise AuthorityError("request value replacement raced")
            if connection.execute("UPDATE field_resolutions SET state='expired_request',resolved_at=?,revision=revision+1 WHERE id=? AND state='unresolved' AND revision=?", (timestamp, request.field_resolution_id, request.expected_resolution_revision)).rowcount != 1:
                raise AuthorityError("request resolution replacement raced")
            if connection.execute("UPDATE checkpoints SET generation=generation+1,revision=revision+1 WHERE id=? AND state='open' AND revision=?", (request.checkpoint_id, request.expected_checkpoint_revision)).rowcount != 1:
                raise AuthorityError("checkpoint replacement raced")
            generation = checkpoint["generation"] + 1
            target_resolution_id = replacement_resolution_id
            connection.execute("UPDATE one_use_challenges SET state='revoked',revision=revision+1 WHERE checkpoint_id=? AND state='active' AND id<>?", (request.checkpoint_id, request.challenge_id))
            connection.execute("UPDATE dispatches SET state='rejected',authority_hmac=NULL,revision=revision+1 WHERE application_id=? AND started_at IS NULL AND authority_hmac IS NOT NULL", (checkpoint["application_id"],))
            if connection.execute("SELECT 1 FROM dispatches WHERE application_id=? AND started_at IS NULL AND authority_hmac IS NOT NULL", (checkpoint["application_id"],)).fetchone():
                raise AuthorityError("dispatch authority invalidation was incomplete")
            connection.execute("UPDATE runs SET preflight_hmac=NULL,revision=revision+1 WHERE application_id=? AND state IN ('queued','inspecting','filling')", (checkpoint["application_id"],))
            connection.execute("INSERT INTO field_resolutions(id,checkpoint_id,generation,field_key,state,created_at,revision) VALUES(?,?,?,?,'unresolved',?,1)", (target_resolution_id, request.checkpoint_id, generation, request.field_key, timestamp))
        resulting_checkpoint_revision = checkpoint["revision"] + (1 if replaced else 0)
        resulting_resolution_revision = 1 if replaced else resolution["revision"]
        metadata = {
            "secret_id": secret_id, "field_resolution_id": target_resolution_id, "generation": generation,
            "checkpoint_id": request.checkpoint_id, "checkpoint_revision": resulting_checkpoint_revision,
            "resolution_revision": resulting_resolution_revision, "source_checkpoint_revision": request.expected_checkpoint_revision,
            "source_resolution_revision": request.expected_resolution_revision, "challenge_id": request.challenge_id,
            "challenge_revision": request.expected_challenge_revision, "run_id": run["id"],
            "run_revision": run["revision"], "run_state": run["state"], "batch_policy_id": policy["id"],
            "batch_policy_revision": policy["revision"], "batch_policy_state": policy["state"],
            "batch_policy_expires_at": _timestamp(policy_ceiling),
            "key_version": request.key_version, "expires_at": _timestamp(expires_at),
            "run_expires_at": _timestamp(ceilings["run"]), "policy_expires_at": _timestamp(ceilings["policy"]),
            "value_digest": _plaintext_digest(hmac_key, request.plaintext),
        }
        envelope = encrypt_aes_gcm(encryption_key, request.plaintext, domain="authority.request_value.v2", aad=metadata)
        expiry = _timestamp(expires_at)
        connection.execute("INSERT INTO request_value_secrets(id,field_resolution_id,ciphertext,nonce,value_hmac,key_version,state,created_at,expires_at,revision) VALUES(?,?,?,?,?,?,'active',?,?,1)", (secret_id, target_resolution_id, canonical_json({"ciphertext": envelope.ciphertext, "tag": envelope.tag, "metadata": metadata}), envelope.nonce.encode("ascii"), domain_hmac(hmac_key, "authority.request.metadata.v2", metadata), request.key_version, timestamp, expiry))
        if connection.execute("UPDATE one_use_challenges SET state='consumed',consumed_at=?,revision=revision+1 WHERE id=? AND state='active' AND revision=?", (timestamp, request.challenge_id, request.expected_challenge_revision)).rowcount != 1:
            raise AuthorityError("request-value challenge raced")
    return RequestValueSecretResult(secret_id, target_resolution_id, generation, expires_at, replaced)
