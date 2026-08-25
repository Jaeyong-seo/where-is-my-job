"""Fail-closed command orchestration for the local application service."""
from __future__ import annotations

import hashlib
import math
import json
import sqlite3
import uuid
import hmac
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Mapping
from contextlib import contextmanager
from functools import wraps
from threading import RLock
from collections.abc import Callable, Generator

from .adapters.aside_fixture import (
    AsideFixtureAdapter, FIXTURE_DOMAIN, FIXTURE_FORM_FINGERPRINT,
    FIXTURE_PAGE_FINGERPRINT, FIXTURE_SCRIPT_PATH,
)
from .adapters.mcp import canonical_submit_payload_sha256
from .aside import (
    AsideProtocolError, AsideRunContext, AsideTransportError, DispatchIntent,
    FillPlan, PauseReason, ScriptRef,
)
from .crypto import domain_hmac, verify_domain_hmac
from .status import append_event, current_canonical_event
from .evidence import EvidenceError, append_evidence_event, verify_evidence_ledger
from .materials import RESUME_NAME_PATTERN
from .region import load_region

_CHECKPOINT_REASONS = frozenset(reason.value for reason in PauseReason) | frozenset(
    {"daily_cap", "policy_expired", "policy_revoked", "kill_switch", "breaker_open"}
)
_ADAPTER_FAILURE_REASONS = _CHECKPOINT_REASONS | frozenset({"adapter_failure"})

_FIXTURE_POLICY_SIGNING_KEY = b"application-automation/fixture-policy-signing-key/v1"
_FIXTURE_ASSERTION_SIGNING_KEY = b"application-automation/fixture-assertion-signing-key/v1"
_FIXTURE_POLICY_DOMAIN = "fixture_batch_policy.v1"
_FIXTURE_ASSERTION_DOMAIN = "fixture_candidate_assertion.v1"
_MODE_TO_COMMAND_KIND = {"dry_run": "dry_run", "fill_only": "fill", "batch": "dispatch"}
# Immutable expected digest of the pinned fixture form script; any drift fails closed before claim.
_FIXTURE_SCRIPT_EXPECTED_SHA256 = "5bbe62fea9359b3c6a23cee278b85f1e516d9eb2844af9e791cc8f3fb311e3cd"


def _fixture_policy_claims(
    *, policy_id: str, profile_id: str, scope_json: str, material_policy_json: str,
    min_fit_score: float, daily_cap: int, timezone_name: str, valid_from: str, expires_at: str,
    assertion_id: str, event_id: str, switch_id: str, capability_id: str,
) -> dict[str, Any]:
    """The full authority tuple bound into the fixture policy's keyed signature."""
    return {
        "policy_id": str(policy_id), "profile_id": str(profile_id),
        "scope_json": str(scope_json), "material_policy_json": str(material_policy_json),
        "min_fit_score": float(min_fit_score), "daily_cap": int(daily_cap),
        "timezone": str(timezone_name), "valid_from": str(valid_from), "expires_at": str(expires_at),
        "assertion_snapshot_id": str(assertion_id), "candidate_confirmation_event_id": str(event_id),
        "global_kill_switch_id": str(switch_id), "fixture_capability_id": str(capability_id),
    }


def _fixture_assertion_claims(*, assertion_id: str, profile_id: str, event_id: str, semantic_key: str) -> dict[str, Any]:
    return {
        "assertion_id": str(assertion_id), "profile_id": str(profile_id),
        "assertion_event_id": str(event_id), "semantic_key": str(semantic_key),
    }

class MaterialValidationError(ValueError):
    """Raised by the one shared strict material validator used by catalog ingestion and orchestration."""


_MATERIAL_ALLOWED_NAMES = frozenset({
    "resume.pdf", "resume.docx", "application.pdf", "application.docx",
})
_MATERIAL_ALLOWED_SUFFIXES = frozenset({".pdf", ".docx"})


def _is_allowed_material_name(name: str) -> bool:
    return name in _MATERIAL_ALLOWED_NAMES or RESUME_NAME_PATTERN.fullmatch(name) is not None


def validate_material_file(path: Path, *, expected_sha256: str) -> bytes:
    """The one strict material validator: resolved regular file, allowlisted name/type, exact hash.

    Consistent with API serving; never falls back from a directory to an inferred file name.
    """
    if (
        path.is_symlink()
        or not path.is_file()
        or path.suffix.lower() not in _MATERIAL_ALLOWED_SUFFIXES
        or not _is_allowed_material_name(path.name)
    ):
        raise MaterialValidationError("material is not an allowlisted regular file")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise MaterialValidationError("material is unavailable") from error
    if len(raw) > 10 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise MaterialValidationError("material hash does not match manifest")
    return raw


class OrchestrationError(ValueError):
    """A safe, user-actionable command rejection."""

    def __init__(self, message: str, *, checkpoint_reason: str | None = None) -> None:
        super().__init__(message)
        self.checkpoint_reason = checkpoint_reason


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return str(uuid.uuid4())
def _fixture_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _serialized(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize use of a cross-thread SQLite connection per service instance."""
    @wraps(method)
    def wrapped(self: "ApplicationOrchestrator", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped




class ApplicationOrchestrator:
    """SQLite-backed command queue. Real-provider execution has no authority here."""

    def __init__(self, connection: sqlite3.Connection, *, fixture_mode: bool = False,
                 catalog: Mapping[str, Mapping[str, Any]] | None = None,
                 trusted_clock: Callable[[], datetime] | None = None) -> None:
        self.connection = connection
        self.fixture_mode = fixture_mode
        source = dict(catalog or {})
        self.catalog_revision = str(source.pop("_catalog_revision", ""))
        self.catalog = source
        self._lock = RLock()
        self._evidence_key = b"application-automation/fixture-evidence-hmac/v1" if fixture_mode else None
        self._trusted_clock: Callable[[], datetime] = trusted_clock or (lambda: datetime.now(timezone.utc))

    def _trusted_policy_date(self) -> str:
        """Return today's region-local date from the injected trusted clock."""
        now = self._trusted_clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise OrchestrationError("trusted clock is invalid")
        return now.astimezone(ZoneInfo(load_region().timezone)).date().isoformat()

    @contextmanager
    def _immediate_transaction(self) -> Generator[None, None, None]:
        """Use an immediate transaction, or a savepoint when already nested."""
        if self.connection.in_transaction:
            self.connection.execute("SAVEPOINT orchestrator_queue")
            try:
                yield
            except BaseException:
                self.connection.execute("ROLLBACK TO SAVEPOINT orchestrator_queue")
                self.connection.execute("RELEASE SAVEPOINT orchestrator_queue")
                raise
            else:
                self.connection.execute("RELEASE SAVEPOINT orchestrator_queue")
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()
    def sync_catalog(self) -> None:
        """Project catalog roles without rewriting any queued or started intent."""
        with self._lock, self._immediate_transaction():
            for role_id, info in self.catalog.items():
                if not isinstance(info, Mapping):
                    continue
                existing = self.connection.execute("SELECT id FROM roles WHERE id=?", (role_id,)).fetchone()
                protected = self.connection.execute(
                    "SELECT 1 FROM applications WHERE role_id=? AND state IN "
                    "('queued','filling','awaiting_user','dispatching','submitted','manual_followup')",
                    (role_id,),
                ).fetchone()
                score = info.get("score")
                values = (
                    str(info.get("canonical_identity", role_id)), str(info.get("company", "")),
                    str(info.get("title", "")), info.get("apply_url"), info.get("material_path"),
                    float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score) else 0.0,
                    str(info.get("automation_status", "discovered")),
                    hashlib.sha256(role_id.encode()).hexdigest(), _now(),
                )
                if existing is None:
                    self.connection.execute(
                        "INSERT INTO roles(id,canonical_key,company_name,title,apply_url,application_dir,"
                        "score,status,posting_snapshot_hmac,revision,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,1,?,?)", (role_id, *values, _now()),
                    )
                elif not protected:
                    self.connection.execute(
                        "UPDATE roles SET canonical_key=?,company_name=?,title=?,apply_url=?,"
                        "application_dir=?,score=?,status=?,posting_snapshot_hmac=?,updated_at=?,"
                        "revision=revision+1 WHERE id=?", (*values, role_id),
                    )


    @_serialized
    def ensure_fixture_authority(self) -> None:
        """Provision the one local fixture policy before exposing queue controls."""
        if not self.fixture_mode:
            raise OrchestrationError("real execution has no active authority")
        with self._immediate_transaction():
            self._fixture_policy(self._profile_id())
    def _role(self, role_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
        if row is None:
            raise OrchestrationError("role is unavailable")
        return row

    def _catalog_entry(self, role_id: str) -> Mapping[str, Any]:
        return self.catalog.get(role_id, {})

    def _validate_role(self, role: sqlite3.Row) -> None:
        info = self._catalog_entry(str(role["id"]))
        required = {
            "score", "location", "remote", "remote_country", "posting_active",
            "automation_status", "canonical_identity", "material_path", "material_sha256",
        }
        if not required.issubset(info):
            raise OrchestrationError("role eligibility data is incomplete")
        score = info["score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or score < 5
            or info["automation_status"] != "materials_ready"
            or role["status"] != "materials_ready"
            or info["posting_active"] is not True
        ):
            raise OrchestrationError("role is not eligible")
        location = info["location"]
        remote = info["remote"]
        country = info["remote_country"]
        if not isinstance(location, str) or not isinstance(remote, bool) or (
            country is not None and not isinstance(country, str)
        ):
            raise OrchestrationError("role eligibility data is invalid")
        region = load_region()
        in_scope = location in region.locations
        if not (in_scope or (remote and country == region.country)):
            raise OrchestrationError("role is not eligible")
        canonical = info["canonical_identity"]
        if not isinstance(canonical, str) or not canonical or canonical != role["canonical_key"]:
            raise OrchestrationError("role identity changed")
        material = info["material_path"]
        material_hash = info["material_sha256"]
        if not isinstance(material, str) or not material or not isinstance(material_hash, str) or not material_hash:
            raise OrchestrationError("current materials are required")
        try:
            validate_material_file(Path(material).expanduser(), expected_sha256=material_hash)
        except MaterialValidationError as error:
            raise OrchestrationError("materials changed") from error

    def _profile_id(self) -> str:
        row = self.connection.execute("SELECT id FROM candidate_profiles WHERE state='active' ORDER BY created_at LIMIT 1").fetchone()
        if row:
            return str(row["id"])
        profile_id = _id()
        self.connection.execute(
            "INSERT INTO candidate_profiles(id,display_name,state,created_at,revision) VALUES(?,?, 'active',?,1)",
            (profile_id, "Local candidate", _now()),
        )
        return profile_id
    def _fixture_policy(self, profile_id: str) -> None:
        """Install only a profile-scoped, signed, unexpired fixture authority."""
        if not self.fixture_mode:
            return
        now = datetime.now(timezone.utc)
        active = self.connection.execute(
            "SELECT p.scope_json,p.material_policy_json,p.signature_hmac,k.scope_key,k.state,"
            "pk.state AS provider_kill_switch_state,b.state AS breaker_state,"
            "p.environment,p.fixture_adapter_id,p.fixture_origin,p.fixture_capability_id,"
            "c.environment AS capability_environment,c.adapter_id,c.origin,c.state AS capability_state "
            "FROM batch_policies p JOIN kill_switches k ON k.id=p.global_kill_switch_id "
            "LEFT JOIN kill_switches pk ON pk.scope_kind='provider' AND pk.scope_key='fixture' "
            "LEFT JOIN breakers b ON b.provider='fixture' AND b.tenant='fixture' "
            "LEFT JOIN capabilities c ON c.id=p.fixture_capability_id "
            "WHERE p.candidate_profile_id=? AND p.state='active'",
            (profile_id,),
        ).fetchone()
        if active is not None:
            return
        if self.connection.execute(
            "SELECT 1 FROM batch_policies WHERE candidate_profile_id=? LIMIT 1", (profile_id,)
        ).fetchone() is not None:
            return
        event_id, assertion_id, switch_id, provider_switch_id, breaker_id, policy_id, capability_id = (
            _id(), _id(), _id(), _id(), _id(), _id(), _id()
        )
        self.connection.execute(
            "INSERT INTO assertion_events(id,profile_id,assertion_id,event_kind,payload_hmac,created_at,revision) VALUES(?,?,?,'confirmed','fixture-only',?,1)",
            (event_id, profile_id, assertion_id, _now()),
        )
        assertion_value_hmac = domain_hmac(
            _FIXTURE_ASSERTION_SIGNING_KEY, _FIXTURE_ASSERTION_DOMAIN,
            _fixture_assertion_claims(
                assertion_id=assertion_id, profile_id=profile_id, event_id=event_id,
                semantic_key='fixture_only_authority',
            ),
        )
        self.connection.execute(
            "INSERT INTO candidate_assertions(id,profile_id,assertion_event_id,semantic_key,value_hmac,state,confirmed_at,revision,created_at) VALUES(?,?,?,?,?,'active',?,1,?)",
            (assertion_id, profile_id, event_id, 'fixture_only_authority', assertion_value_hmac, _now(), _now()))
        self.connection.execute(
            "INSERT INTO kill_switches(id,scope_kind,scope_key,state,reason,created_at,revision) VALUES(?,'global','global','closed','fixture-only',?,1)",
            (switch_id, _now()))
        self.connection.execute(
            "INSERT INTO kill_switches(id,scope_kind,scope_key,state,reason,created_at,revision) VALUES(?,'provider','fixture','closed','fixture-only',?,1)",
            (provider_switch_id, _now()))
        self.connection.execute(
            "INSERT INTO breakers(id,provider,tenant,state,reason,opened_at,revision) VALUES(?,'fixture','fixture','closed','fixture-only',NULL,1)",
            (breaker_id,))
        self.connection.execute(
            "INSERT INTO capabilities(id,provider,tenant,operation,transport,form_fingerprint,state,"
            "expires_at,capability_json,revision,created_at,environment,adapter_id,origin) "
            "VALUES(?,'fixture','fixture','submit','aside',?,'active',?, '{}',1,?,'fixture','fixture-aside-v1',?)",
            (capability_id, FIXTURE_FORM_FINGERPRINT, (now + timedelta(hours=1)).isoformat(), _now(), FIXTURE_DOMAIN),
        )
        valid_from = (now - timedelta(minutes=1)).isoformat()
        expires_at = (now + timedelta(hours=1)).isoformat()
        policy_signature = domain_hmac(
            _FIXTURE_POLICY_SIGNING_KEY, _FIXTURE_POLICY_DOMAIN,
            _fixture_policy_claims(
                policy_id=policy_id, profile_id=profile_id, scope_json='{"fixture_only":true}',
                material_policy_json='{"fixture_only":true}', min_fit_score=5, daily_cap=20,
                timezone_name=load_region().timezone, valid_from=valid_from, expires_at=expires_at,
                assertion_id=assertion_id, event_id=event_id, switch_id=switch_id, capability_id=capability_id,
            ),
        )
        self.connection.execute(
            "INSERT INTO batch_policies(id,candidate_profile_id,policy_version,state,scope_json,min_fit_score,"
            "timezone,daily_cap,provider_form_allowlist_json,assertion_snapshot_id,material_policy_json,"
            "checkpoint_classes_json,valid_from,expires_at,global_kill_switch_id,signature_hmac,key_version,"
            "candidate_confirmation_event_id,revision,created_at,environment,fixture_adapter_id,fixture_origin,"
            "fixture_capability_id) VALUES(?,?,1,'active',?,5,?,20,?,?,?,?,?,?,?,?,"
            "1,?,1,?,'fixture','fixture-aside-v1',?,?)",
            (policy_id, profile_id, '{"fixture_only":true}', load_region().timezone,
             '{"fixture_only":true}', assertion_id,
             '{"fixture_only":true}', '[]', valid_from,
             expires_at, switch_id, policy_signature, event_id, _now(), FIXTURE_DOMAIN, capability_id))

    def _effective_policy(self, profile_id: str, *, provision_fixture: bool = True) -> sqlite3.Row | None:
        if not self.fixture_mode:
            return None
        if self.fixture_mode and provision_fixture:
            self._fixture_policy(profile_id)
        policy = self.connection.execute(
            "SELECT p.*,k.scope_key AS global_scope_key,c.environment AS capability_environment,"
            "pk.state AS provider_kill_switch_state,b.state AS breaker_state,"
            "c.adapter_id AS capability_adapter_id,c.origin AS capability_origin,c.state AS capability_state,"
            "c.expires_at AS capability_expires_at,c.provider AS capability_provider,"
            "c.tenant AS capability_tenant,c.operation AS capability_operation,"
            "c.transport AS capability_transport,c.form_fingerprint AS capability_form_fingerprint,"
            "a.state AS assertion_state,a.value_hmac AS assertion_value_hmac,"
            "e.event_kind AS assertion_event_kind "
            "FROM batch_policies p JOIN kill_switches k ON k.id=p.global_kill_switch_id "
            "JOIN kill_switches pk ON pk.scope_kind='provider' AND pk.scope_key='fixture' "
            "JOIN breakers b ON b.provider='fixture' AND b.tenant='fixture' "
            "JOIN candidate_assertions a ON a.id=p.assertion_snapshot_id AND a.profile_id=p.candidate_profile_id "
            "JOIN assertion_events e ON e.id=p.candidate_confirmation_event_id AND e.profile_id=p.candidate_profile_id "
            "JOIN capabilities c ON c.id=p.fixture_capability_id "
            "WHERE p.candidate_profile_id=? AND p.state='active' AND p.valid_from<=? AND p.expires_at>? "
            "AND c.expires_at>? AND k.state='closed' AND pk.state='closed' AND b.state='closed' "
            "AND a.state='active' AND e.event_kind='confirmed' "
            "AND p.signature_hmac IS NOT NULL "
            "ORDER BY p.created_at DESC LIMIT 1",
            (profile_id, _now(), _now(), _now()),
        ).fetchone()
        if policy is None:
            return None
        if (
            policy["scope_json"] != '{"fixture_only":true}'
            or policy["material_policy_json"] != '{"fixture_only":true}'
            or policy["global_scope_key"] != "global"
            or policy["environment"] != "fixture"
            or policy["fixture_adapter_id"] != "fixture-aside-v1"
            or policy["fixture_origin"] != FIXTURE_DOMAIN
            or policy["capability_environment"] != "fixture"
            or policy["capability_adapter_id"] != "fixture-aside-v1"
            or policy["capability_origin"] != FIXTURE_DOMAIN
            or policy["capability_state"] != "active"
            or policy["capability_provider"] != "fixture"
            or policy["capability_tenant"] != "fixture"
            or policy["capability_operation"] != "submit"
            or policy["capability_transport"] != "aside"
            or policy["capability_form_fingerprint"] != FIXTURE_FORM_FINGERPRINT
            or not isinstance(policy["assertion_value_hmac"], str)
            or not verify_domain_hmac(
                _FIXTURE_ASSERTION_SIGNING_KEY, _FIXTURE_ASSERTION_DOMAIN,
                _fixture_assertion_claims(
                    assertion_id=str(policy["assertion_snapshot_id"]), profile_id=str(policy["candidate_profile_id"]),
                    event_id=str(policy["candidate_confirmation_event_id"]), semantic_key="fixture_only_authority",
                ),
                str(policy["assertion_value_hmac"]),
            )
            or not isinstance(policy["signature_hmac"], str)
            or not verify_domain_hmac(
                _FIXTURE_POLICY_SIGNING_KEY, _FIXTURE_POLICY_DOMAIN,
                _fixture_policy_claims(
                    policy_id=str(policy["id"]), profile_id=str(policy["candidate_profile_id"]),
                    scope_json=str(policy["scope_json"]), material_policy_json=str(policy["material_policy_json"]),
                    min_fit_score=policy["min_fit_score"], daily_cap=policy["daily_cap"],
                    timezone_name=str(policy["timezone"]), valid_from=str(policy["valid_from"]),
                    expires_at=str(policy["expires_at"]), assertion_id=str(policy["assertion_snapshot_id"]),
                    event_id=str(policy["candidate_confirmation_event_id"]),
                    switch_id=str(policy["global_kill_switch_id"]), capability_id=str(policy["fixture_capability_id"]),
                ),
                str(policy["signature_hmac"]),
            )
        ):
            return None
        return policy
    def _authority_checkpoint_reason(self, profile_id: str) -> str:
        """Classify fixture authority loss without provisioning a replacement."""
        policy = self.connection.execute(
            "SELECT p.state,p.expires_at,k.state AS kill_switch_state,"
            "pk.state AS provider_kill_switch_state,b.state AS breaker_state,"
            "c.state AS capability_state,c.expires_at AS capability_expires_at "
            "FROM batch_policies p JOIN kill_switches k ON k.id=p.global_kill_switch_id "
            "LEFT JOIN kill_switches pk ON pk.scope_kind='provider' AND pk.scope_key='fixture' "
            "LEFT JOIN breakers b ON b.provider='fixture' AND b.tenant='fixture' "
            "LEFT JOIN capabilities c ON c.id=p.fixture_capability_id "
            "WHERE p.candidate_profile_id=? ORDER BY p.created_at DESC LIMIT 1",
            (profile_id,),
        ).fetchone()
        if policy is None:
            return "policy_revoked"
        now = _now()
        if policy["state"] != "active" or policy["capability_state"] != "active":
            return "policy_revoked"
        if policy["expires_at"] <= now or (
            policy["capability_expires_at"] is None or policy["capability_expires_at"] <= now
        ):
            return "policy_expired"
        if policy["kill_switch_state"] != "closed" or policy["provider_kill_switch_state"] != "closed":
            return "kill_switch"
        if policy["breaker_state"] != "closed":
            return "breaker_open"
        return "policy_revoked"
    def _confirmed_fixture_outcome(self, dispatch_id: str) -> bool:
        outcome = self.connection.execute(
            "SELECT o.*,d.application_id AS dispatch_application_id,d.run_id AS dispatch_run_id,"
            "d.form_fingerprint AS dispatch_form_fingerprint,d.state AS dispatch_state "
            "FROM fixture_dispatch_outcomes o JOIN dispatches d ON d.id=o.dispatch_id "
            "WHERE o.dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if outcome is None or outcome["state"] != "confirmed":
            return False
        payload_sha256 = canonical_submit_payload_sha256(
            DispatchIntent(
                dispatch_id, str(outcome["application_id"]), str(outcome["session_id"]),
                str(outcome["run_id"]), str(outcome["intent_hmac"]), str(outcome["payload_sha256"]),
                FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT, str(outcome["resume_sha256"]),
            )
        )
        expected_intent = _fixture_digest(f"intent:{dispatch_id}:{payload_sha256}")
        receipt_digest = outcome["receipt_digest"]
        return (
            outcome["dispatch_application_id"] == outcome["application_id"]
            and outcome["dispatch_run_id"] == outcome["run_id"]
            and outcome["provider"] == "fixture"
            and outcome["tenant"] == "fixture"
            and outcome["dispatch_state"] == "confirmed"
            and outcome["page_fingerprint"] == FIXTURE_PAGE_FINGERPRINT
            and outcome["form_fingerprint"] == outcome["dispatch_form_fingerprint"] == FIXTURE_FORM_FINGERPRINT
            and outcome["payload_sha256"] == payload_sha256
            and outcome["intent_hmac"] == expected_intent
            and isinstance(receipt_digest, str)
            and receipt_digest == _fixture_digest("fixture-receipt-v1")
            and outcome["observed_intent_hmac"] == expected_intent
            and outcome["attestation_digest"]
            == _fixture_digest(f"attestation:{expected_intent}:{receipt_digest}")
        )

    def _policy_projection(
        self, profile_id: str, *, provision_fixture: bool = True
    ) -> tuple[sqlite3.Row | None, int, str]:
        policy = self._effective_policy(profile_id, provision_fixture=provision_fixture)
        local_date = self._trusted_policy_date()
        used = 0 if policy is None else self.connection.execute(
            "SELECT COUNT(*) FROM daily_quota_reservations WHERE policy_id=? AND local_date=? "
            "AND state IN ('reserved','consumed')", (policy["id"], local_date)
        ).fetchone()[0]
        return policy, used, local_date
    def _start_batch_dispatch(
        self, app: sqlite3.Row, role: sqlite3.Row, run_id: str, resume_sha256: str
    ) -> tuple[DispatchIntent, str]:
        """Atomically bind a fixture dispatch identity before a single submit attempt."""
        with self._immediate_transaction():
            if not self.fixture_mode:
                raise OrchestrationError("real execution has no active authority")
            current_app = self.connection.execute("SELECT * FROM applications WHERE id=?", (app["id"],)).fetchone()
            current_role = self._role(str(role["id"]))
            if current_app is None or current_app["state"] != "filling":
                raise OrchestrationError("application is not ready to dispatch")
            self._validate_role(current_role)
            profile_id = self._profile_id()
            policy, used, local_date = self._policy_projection(profile_id)
            if policy is None or policy["daily_cap"] != 20:
                raise OrchestrationError(
                    "no active fixture batch policy",
                    checkpoint_reason=self._authority_checkpoint_reason(profile_id),
                )
            if used >= 20:
                raise OrchestrationError("daily batch quota reached", checkpoint_reason="daily_cap")
            if self.connection.execute("SELECT 1 FROM dispatches WHERE application_id=? AND started_at IS NOT NULL", (app["id"],)).fetchone():
                raise OrchestrationError("application dispatch already started")
            dispatch_id, action_id, session_id = _id(), _id(), run_id
            started_at = _now()
            self.connection.execute(
                "INSERT INTO sessions(id,profile_id,service_instance_id,dashboard_instance_id,state,created_at,expires_at,revision) "
                "VALUES(?,?,?,'fixture','active',?,?,1)",
                (session_id, profile_id, _fixture_digest(f"service:{profile_id}"), started_at,
                 (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()),
            )
            assertion_digest = _fixture_digest(
                f"assertion:{policy['assertion_snapshot_id']}:{policy['assertion_value_hmac']}"
            )
            account_hmac = _fixture_digest(f"account:{profile_id}:{assertion_digest}")
            context_hmac = _fixture_digest(f"context:{app['id']}")
            session_hmac = _fixture_digest(f"session:{session_id}")
            intent = DispatchIntent(
                dispatch_id, str(app["id"]), session_id, run_id, "",
                "", FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT, resume_sha256,
            )
            payload_sha256 = canonical_submit_payload_sha256(intent)
            intent_hmac = _fixture_digest(f"intent:{dispatch_id}:{payload_sha256}")
            self.connection.execute(
                "INSERT INTO dispatches(id,application_id,run_id,transport,state,batch_policy_id,authority_hmac,"
                "form_fingerprint,started_at,revision,created_at,environment,fixture_adapter_id,fixture_origin,"
                "fixture_capability_id) VALUES(?,?,?,'aside','intent',?,'fixture-only',?,?,1,?,"
                "'fixture','fixture-aside-v1',?,?)",
                (dispatch_id, app["id"], run_id, policy["id"], FIXTURE_FORM_FINGERPRINT, None,
                 started_at, FIXTURE_DOMAIN, policy["fixture_capability_id"]),
            )
            self.connection.execute(
                "INSERT INTO fixture_dispatch_outcomes(dispatch_id,application_id,provider,tenant,account_hmac,"
                "context_hmac,session_id,session_hmac,run_id,intent_hmac,payload_sha256,page_fingerprint,"
                "form_fingerprint,resume_sha256,state,prepared_at,revision) VALUES(?,?,"
                "'fixture','fixture',?,?,?,?,?,?,?,?,?,?,'prepared',?,1)",
                (dispatch_id, app["id"], account_hmac, context_hmac, session_id, session_hmac, run_id,
                 intent_hmac, payload_sha256, FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT,
                 resume_sha256, started_at),
            )
            self.connection.execute(
                "INSERT INTO daily_quota_reservations(id,policy_id,local_date,application_id,dispatch_id,state,created_at,consumed_at,revision) VALUES(?,?,?,?,?,'consumed',?,?,1)",
                (_id(), policy["id"], local_date, app["id"], dispatch_id, started_at, started_at))
            if self.connection.execute(
                "UPDATE dispatches SET state='dispatching',started_at=?,revision=revision+1 "
                "WHERE id=? AND state='intent' AND started_at IS NULL",
                (started_at, dispatch_id),
            ).rowcount != 1:
                raise OrchestrationError("application dispatch could not be started")
            self.connection.execute(
                "INSERT INTO actions(id,run_id,action_kind,side_effect_class,state,created_at,revision) VALUES(?,?,'submit','submit','started',?,1)",
                (action_id, run_id, started_at))
            self.connection.execute("UPDATE applications SET state='dispatching',updated_at=?,revision=revision+1 WHERE id=?", (started_at, app["id"]))
            self.connection.execute("UPDATE runs SET state='dispatching',revision=revision+1 WHERE id=?", (run_id,))
            return DispatchIntent(dispatch_id, str(app["id"]), session_id, run_id, intent_hmac,
                                  payload_sha256, FIXTURE_PAGE_FINGERPRINT,
                                  FIXTURE_FORM_FINGERPRINT, resume_sha256), action_id

    @_serialized
    def queue(self, role_id: str, mode: str, idempotency_key: str) -> dict[str, Any]:
        with self._immediate_transaction():
            if mode not in {"dry_run", "fill_only", "batch"} or not idempotency_key or len(idempotency_key) > 200:
                raise OrchestrationError("invalid command")
            if not self.fixture_mode:
                raise OrchestrationError("real execution has no active authority")
            role = self._role(role_id)
            self._validate_role(role)
            profile_id = self._profile_id()
            info = self._catalog_entry(role_id)
            fingerprint = {
                "catalog_revision": self.catalog_revision,
                "canonical_identity": str(info.get("canonical_identity") or role["canonical_key"]),
                "material_sha256": str(info.get("material_sha256")),
                "mode": mode,
                "profile_id": profile_id,
                "role_id": role_id,
            }
            fingerprint_json = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"))
            existing = self.connection.execute("SELECT * FROM commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                payload = json.loads(existing["payload_json"])
                if payload.get("fingerprint") != fingerprint:
                    raise OrchestrationError("idempotency key conflicts with a different request")
                return dict(existing)
            application = self.connection.execute("SELECT * FROM applications WHERE profile_id=? AND role_id=?", (profile_id, role_id)).fetchone()
            if application is None:
                application_id = _id()
                self.connection.execute(
                    "INSERT INTO applications(id,profile_id,role_id,canonical_identity,state,revision,created_at,updated_at) VALUES(?,?,?,?, 'queued',1,?,?)",
                    (application_id, profile_id, role_id, role["canonical_key"], _now(), _now()),
                )
            else:
                application_id = str(application["id"])
                if application["state"] in {"submitted", "manual_followup", "dispatching", "awaiting_user", "abandoned"}:
                    raise OrchestrationError("application already started")
                if self.connection.execute(
                    "SELECT 1 FROM commands WHERE application_id=? AND state IN ('accepted','running','paused')",
                    (application_id,),
                ).fetchone():
                    raise OrchestrationError("application already has an active command")
            command_id = _id()
            payload = {"fingerprint": fingerprint, "fingerprint_sha256": hashlib.sha256(fingerprint_json.encode()).hexdigest(), "mode": mode}
            self.connection.execute(
                "INSERT INTO commands(id,application_id,idempotency_key,command_kind,payload_json,state,created_at,revision) VALUES(?,?,?,?,?,'accepted',?,1)",
                (command_id, application_id, idempotency_key, _MODE_TO_COMMAND_KIND[mode], json.dumps(payload, sort_keys=True), _now()),
            )
            append_event(self.connection, role_id, "queued", application_id=application_id, payload={"command_id": command_id, "fingerprint": fingerprint})
            return dict(self.connection.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())

    @_serialized
    def cancel(self, command_id: str) -> dict[str, Any]:
        with self._immediate_transaction():
            command = self.connection.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
            if command is None:
                raise OrchestrationError("command is unavailable")
            if command["state"] in {"cancelled", "completed"}:
                return dict(command)
            if command["state"] != "accepted":
                raise OrchestrationError("command is already claimed")
            if self.connection.execute(
                "UPDATE commands SET state='cancelled', revision=revision+1 WHERE id=? AND state='accepted'",
                (command_id,),
            ).rowcount != 1:
                raise OrchestrationError("command is already claimed")
            app = self.connection.execute(
                "SELECT role_id FROM applications WHERE id=?", (command["application_id"],)
            ).fetchone()
            if app is None:
                raise OrchestrationError("command is unavailable")
            self.connection.execute(
                "UPDATE applications SET state='abandoned',revision=revision+1,updated_at=? "
                "WHERE id=? AND state='queued'",
                (_now(), command["application_id"]),
            )
            append_event(self.connection, app["role_id"], "cancelled", application_id=command["application_id"], payload={"command_id": command_id})
            return dict(self.connection.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())
    @_serialized
    def run_next(self, *, scenario: str = "happy") -> dict[str, Any] | None:
        """Claim the oldest command while tolerating independent SQLite workers."""
        if not self.fixture_mode:
            raise OrchestrationError("real execution has no active authority")
        while True:
            row = self.connection.execute(
                "SELECT id FROM commands WHERE state='accepted' ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            command_id = str(row["id"])
            try:
                return self.run(command_id, scenario=scenario)
            except OrchestrationError as error:
                if str(error) in {"command is already claimed", "command is unavailable"}:
                    continue
                command = self.command(command_id)
                if command["state"] == "accepted":
                    return self._fail_unclaimed_command(command_id)
                raise
            except Exception:
                command = self.command(command_id)
                if command["state"] == "accepted":
                    self._fail_unclaimed_command(command_id)
                elif command["state"] == "running":
                    self._unexpected_failure(command_id)
                raise



    @_serialized
    def run(self, command_id: str, *, scenario: str = "happy") -> dict[str, Any]:
        if not self.fixture_mode:
            raise OrchestrationError("real execution has no active authority")
        authority_reason: str | None = None
        with self._immediate_transaction():
            command = self.connection.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
            if command is None or command["state"] in {"completed", "cancelled"}:
                raise OrchestrationError("command is unavailable")
            if command["state"] != "accepted":
                raise OrchestrationError("command is already claimed")
            try:
                payload = json.loads(command["payload_json"])
            except (TypeError, ValueError) as error:
                raise OrchestrationError("command payload is corrupt") from error
            if not isinstance(payload, dict):
                raise OrchestrationError("command payload is corrupt")
            mode = payload.get("mode")
            if mode not in _MODE_TO_COMMAND_KIND or command["command_kind"] != _MODE_TO_COMMAND_KIND[mode]:
                raise OrchestrationError("command mode is invalid")
            try:
                script_hash = hashlib.sha256(FIXTURE_SCRIPT_PATH.read_bytes()).hexdigest()
            except OSError as error:
                raise OrchestrationError("fixture script is unavailable") from error
            if script_hash != _FIXTURE_SCRIPT_EXPECTED_SHA256:
                raise OrchestrationError("fixture script drift")
            if self.connection.execute(
                "UPDATE commands SET state='running',revision=revision+1 WHERE id=? AND state='accepted'", (command_id,)
            ).rowcount != 1:
                raise OrchestrationError("command is already claimed")
            app = self.connection.execute("SELECT * FROM applications WHERE id=?", (command["application_id"],)).fetchone()
            if app is None:
                raise OrchestrationError("command is unavailable")
            role = self._role(str(app["role_id"]))
            run_id = _id()
            self.connection.execute(
                "INSERT INTO runs(id,application_id,command_id,state,aside_version,script_sha256,revision,created_at) "
                "VALUES(?,?,?,'inspecting',?,?,1,?)",
                (run_id, app["id"], command_id, "fixture-aside-v1", script_hash, _now()),
            )
            expected = payload.get("fingerprint", {})
            info = self._catalog_entry(str(role["id"]))
            if expected != {
                "catalog_revision": self.catalog_revision,
                "canonical_identity": str(info.get("canonical_identity") or role["canonical_key"]),
                "material_sha256": str(info.get("material_sha256")),
                "mode": payload.get("mode"),
                "profile_id": str(app["profile_id"]),
                "role_id": str(role["id"]),
            }:
                raise OrchestrationError("queued command identity changed")
            self._validate_role(role)
            self._fixture_policy(str(app["profile_id"]))
            assertion = self.connection.execute(
                "SELECT id,value_hmac FROM candidate_assertions WHERE profile_id=? "
                "AND semantic_key='fixture_only_authority' AND state='active' "
                "ORDER BY created_at DESC LIMIT 1",
                (app["profile_id"],),
            ).fetchone()
            if assertion is None:
                raise OrchestrationError("fixture assertion snapshot is unavailable")
            assertion_digest = _fixture_digest(f"assertion:{assertion['id']}:{assertion['value_hmac']}")
            if payload["mode"] == "batch":
                policy, _, _ = self._policy_projection(str(app["profile_id"]))
                if policy is None:
                    authority_reason = self._authority_checkpoint_reason(str(app["profile_id"]))
            self.connection.execute("UPDATE applications SET state='filling',updated_at=?,revision=revision+1 WHERE id=?", (_now(), app["id"]))
        dispatch_id: str | None = None
        script = ScriptRef("fixture-form", "v1", FIXTURE_SCRIPT_PATH, script_hash, frozenset({FIXTURE_DOMAIN}))
        account_hmac_value = _fixture_digest(f"account:{app['profile_id']}:{assertion_digest}")
        ctx = AsideRunContext(
            "fixture-aside-v1", _fixture_digest("fixture-cli-v1"),
            account_hmac_value, _fixture_digest(f"context:{app['id']}"),
            _fixture_digest(f"session:{run_id}"), "fixture", "fixture",
            FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT, run_id, fixture_scenario=scenario,
        )
        adapter = AsideFixtureAdapter()
        active_action_id: str | None = None
        submit_action_id: str | None = None
        if authority_reason is not None:
            return self._pause(
                run_id, app, role, command_id, "preflight", authority_reason
            )
        try:
            active_action_id = self._start_action(run_id, "inspect")
            inspected = adapter.inspect(ctx, script)
            with self._immediate_transaction():
                self._finish_action(active_action_id, "completed")
                self._evidence(app["id"], None, "inspect", stage="inspect")
            active_action_id = None
            if inspected.pause_reason or inspected.form_fingerprint != FIXTURE_FORM_FINGERPRINT:
                return self._pause(run_id, app, role, command_id, "inspect", inspected.pause_reason or "form_drift")
            if payload["mode"] == "dry_run":
                return self._complete(run_id, app, role, command_id, "inspect")
            material_path_value = info.get("material_path")
            material_hash = info.get("material_sha256")
            if (
                not isinstance(material_path_value, str) or not material_path_value
                or not isinstance(material_hash, str) or not material_hash
            ):
                raise OrchestrationError("current materials are required")
            material = Path(material_path_value).expanduser()
            try:
                validate_material_file(material, expected_sha256=material_hash)
            except MaterialValidationError as error:
                raise OrchestrationError("current materials are required") from error
            fill_fields = {
                "name": f"fixture-candidate-{assertion_digest[:16]}",
                "email": f"fixture-{assertion_digest[:16]}@fixture.local",
            }
            active_action_id = self._start_action(run_id, "fill")
            filled = adapter.fill(ctx, script, FillPlan(fill_fields, material, material_hash))
            with self._immediate_transaction():
                self._finish_action(active_action_id, "completed")
                self._evidence(app["id"], None, "fill", stage="fill")
            active_action_id = None
            if filled.pause_reason:
                return self._pause(run_id, app, role, command_id, "fill", filled.pause_reason)
            if payload["mode"] == "fill_only":
                return self._complete(run_id, app, role, command_id, "fill")
            if filled.attached_resume_sha256 != material_hash:
                raise OrchestrationError("attached resume does not match approved material")
            dispatch, submit_action_id = self._start_batch_dispatch(
                app, role, run_id, str(filled.attached_resume_sha256)
            )
            dispatch_id = dispatch.dispatch_id
            ctx = AsideRunContext(
                "fixture-aside-v1", _fixture_digest("fixture-cli-v1"),
                account_hmac_value, _fixture_digest(f"context:{app['id']}"),
                _fixture_digest(f"session:{dispatch.session_id}"), "fixture", "fixture",
                FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT, run_id, fixture_scenario=scenario,
            )
            outcome = adapter.submit(ctx, script, dispatch)
            submit_proof_complete = (
                outcome.started is True
                and outcome.confirmed is True
                and outcome.manual_follow_up is False
                and outcome.pause_reason is None
                and outcome.receipt_id == "fixture-receipt-v1"
            )
            attempted_submit = outcome.started is True and outcome.pause_reason is None
            with self._immediate_transaction():
                self._finish_action(submit_action_id, "completed" if attempted_submit else "failed")
                if attempted_submit:
                    if self.connection.execute(
                        "UPDATE fixture_dispatch_outcomes SET state='possibly_started',started_at=?,revision=revision+1 "
                        "WHERE dispatch_id=? AND state='prepared'",
                        (_now(), dispatch_id),
                    ).rowcount != 1:
                        attempted_submit = False
                    else:
                        self._evidence(app["id"], dispatch_id, "submit", stage="submit")
            submit_action_id = None
            active_action_id = self._start_action(run_id, "observe")
            observed = adapter.observe(ctx, script, dispatch_id)
            confirmation_complete = False
            with self._immediate_transaction():
                self._finish_action(active_action_id, "completed")
                if (
                    submit_proof_complete
                    and observed.state == "confirmed"
                    and observed.pause_reason is None
                    and observed.page_fingerprint == dispatch.page_fingerprint
                    and observed.form_fingerprint == dispatch.form_fingerprint
                    and observed.receipt_id == outcome.receipt_id == "fixture-receipt-v1"
                ):
                    receipt_digest = _fixture_digest(observed.receipt_id)
                    attestation_digest = _fixture_digest(
                        f"attestation:{dispatch.intent_hmac}:{receipt_digest}"
                    )
                    confirmed_at = _now()
                    outcome_confirmed = self.connection.execute(
                        "UPDATE fixture_dispatch_outcomes SET state='confirmed',receipt_digest=?,"
                        "attestation_digest=?,observed_intent_hmac=?,confirmed_at=?,terminal_at=?,revision=revision+1 "
                        "WHERE dispatch_id=? AND state='possibly_started'",
                        (receipt_digest, attestation_digest, dispatch.intent_hmac, confirmed_at,
                         confirmed_at, dispatch_id),
                    ).rowcount == 1
                    dispatch_confirmed = self.connection.execute(
                        "UPDATE dispatches SET state='confirmed',finished_at=?,revision=revision+1 "
                        "WHERE id=? AND state='dispatching'",
                        (confirmed_at, dispatch_id),
                    ).rowcount == 1
                    if outcome_confirmed and dispatch_confirmed:
                        self._evidence(app["id"], dispatch_id, "observe", stage="observe",
                                       receipt_digest=receipt_digest, attestation_digest=attestation_digest)
                        confirmation_complete = self._confirmed_fixture_outcome(dispatch_id)
            active_action_id = None
            if (
                not submit_proof_complete
                or not attempted_submit
                or not confirmation_complete
                or outcome.pause_reason
                or outcome.manual_follow_up
                or observed.pause_reason
                or observed.state != "confirmed"
                or observed.page_fingerprint != dispatch.page_fingerprint
                or observed.form_fingerprint != dispatch.form_fingerprint
                or observed.receipt_id != outcome.receipt_id
                or observed.receipt_id != "fixture-receipt-v1"
            ):
                challenge_reason = outcome.pause_reason or observed.pause_reason
                reason_code = (
                    challenge_reason.value if isinstance(challenge_reason, PauseReason) else challenge_reason
                ) or "adapter_failure"
                return self._adapter_failure(run_id, app, role, command_id, dispatch_id, reason_code=reason_code)
            with self._immediate_transaction():
                self.connection.execute("UPDATE runs SET state='completed',finished_at=?,revision=revision+1 WHERE id=?", (_now(), run_id))
                self.connection.execute("UPDATE applications SET state='submitted',updated_at=?,revision=revision+1 WHERE id=?", (_now(), app["id"]))
                append_event(self.connection, role["id"], "applied", application_id=app["id"], payload={"stage": "observe"})
                self.connection.execute("UPDATE commands SET state='completed',revision=revision+1 WHERE id=?", (command_id,))
            return self.command(command_id)
        except Exception as error:
            if isinstance(error, OrchestrationError) and error.checkpoint_reason is not None:
                return self._pause(
                    run_id, app, role, command_id, "execution", error.checkpoint_reason, dispatch_id
                )
            if dispatch_id is not None and isinstance(error, (AsideTransportError, AsideProtocolError)):
                return self._adapter_failure(
                    run_id, app, role, command_id, dispatch_id, reason_code="adapter_failure"
                )
            self._unexpected_failure(
                command_id, run_id=run_id, app=app, role=role,
                active_action_id=active_action_id, submit_action_id=submit_action_id,
                dispatch_id=dispatch_id,
            )
            raise

    def _complete(self, run_id: str, app: sqlite3.Row, role: sqlite3.Row, command_id: str, stage: str) -> dict[str, Any]:
        with self._immediate_transaction():
            self.connection.execute("UPDATE runs SET state='completed',finished_at=?,revision=revision+1 WHERE id=?", (_now(), run_id))
            self.connection.execute("UPDATE applications SET state='queued',updated_at=?,revision=revision+1 WHERE id=?", (_now(), app["id"]))
            self.connection.execute("UPDATE commands SET state='completed',revision=revision+1 WHERE id=?", (command_id,))
            append_event(self.connection, role["id"], "queued", application_id=app["id"], payload={"stage": stage, "status": "completed"})
        return self.command(command_id)

    def _start_action(self, run_id: str, action_kind: str) -> str:
        action_id = _id()
        side_effect_class = {"inspect": "none", "fill": "fill", "observe": "none"}[action_kind]
        with self._immediate_transaction():
            self.connection.execute(
                "INSERT INTO actions(id,run_id,action_kind,side_effect_class,state,created_at,revision) VALUES(?,?,?,?, 'started',?,1)",
                (action_id, run_id, action_kind, side_effect_class, _now()),
            )
        return action_id

    def _finish_action(self, action_id: str, state: str) -> None:
        if state not in {"completed", "failed"}:
            raise OrchestrationError("invalid action completion")
        if self.connection.execute(
            "UPDATE actions SET state=?,completed_at=?,revision=revision+1 WHERE id=? AND state='started'",
            (state, _now(), action_id),
        ).rowcount != 1:
            raise OrchestrationError("action is not started")
    def _fail_unclaimed_command(self, command_id: str) -> dict[str, Any]:
        """Durably expose a rejected preflight without inventing a drift reason."""
        with self._immediate_transaction():
            command = self.connection.execute(
                "SELECT * FROM commands WHERE id=? AND state='accepted'", (command_id,)
            ).fetchone()
            if command is None:
                return self.command(command_id)
            app = self.connection.execute(
                "SELECT * FROM applications WHERE id=?", (command["application_id"],)
            ).fetchone()
            if app is None:
                self.connection.execute(
                    "UPDATE commands SET state='failed',revision=revision+1 WHERE id=? AND state='accepted'",
                    (command_id,),
                )
                return self.command(command_id)
            if self.connection.execute(
                "UPDATE commands SET state='failed',revision=revision+1 WHERE id=? AND state='accepted'",
                (command_id,),
            ).rowcount != 1:
                return self.command(command_id)
            checkpoint_id = _id()
            self.connection.execute(
                "INSERT INTO checkpoints(id,application_id,run_id,kind,state,created_at,revision) "
                "VALUES(?,?,NULL,'manual_completion','open',?,1)",
                (checkpoint_id, app["id"], _now()),
            )
            self._evidence(app["id"], None, "pause", stage="preflight", reason_code="preflight_failure")
            self.connection.execute(
                "UPDATE applications SET state='awaiting_user',updated_at=?,revision=revision+1 "
                "WHERE id=? AND state='queued'",
                (_now(), app["id"]),
            )
            role = self._role(str(app["role_id"]))
            append_event(
                self.connection, role["id"], "awaiting_user", application_id=app["id"],
                payload={
                    "stage": "preflight", "reason": "preflight_failure", "checkpoint_id": checkpoint_id,
                    "status": "awaiting_user", "evidence": {"command_id": command_id},
                },
            )
            return self.command(command_id)

    def _evidence(self, application_id: str, dispatch_id: str | None, event_kind: str, *, stage: str,
                  reason_code: str | None = None, receipt_digest: str | None = None,
                  attestation_digest: str | None = None) -> None:
        if self._evidence_key is None:
            raise OrchestrationError("real execution has no evidence authority")
        try:
            append_evidence_event(
                self.connection, application_id, dispatch_id, event_kind,
                authentication_key=self._evidence_key, key_version=1, stage=stage,
                reason_code=reason_code, receipt_digest=receipt_digest,
                attestation_digest=attestation_digest,
            )
        except EvidenceError as error:
            raise OrchestrationError("evidence ledger is invalid") from error

    def _pause(self, run_id: str, app: sqlite3.Row, role: sqlite3.Row, command_id: str, stage: str, reason: str | PauseReason, dispatch_id: str | None = None) -> dict[str, Any]:
        reason_code = reason.value if isinstance(reason, PauseReason) else reason
        if reason_code not in _CHECKPOINT_REASONS:
            raise OrchestrationError("unsupported checkpoint reason")
        checkpoint_id = _id()
        pause = {"stage": stage, "reason": reason_code, "checkpoint_id": checkpoint_id, "status": "awaiting_user", "evidence": {"run_id": run_id}}
        with self._immediate_transaction():
            self.connection.execute("INSERT INTO checkpoints(id,application_id,run_id,kind,state,created_at,revision) VALUES(?,?,?,?,'open',?,1)", (checkpoint_id, app["id"], run_id, reason_code, _now()))
            self._evidence(app["id"], dispatch_id, "pause", stage=stage, reason_code=reason_code)
            self.connection.execute("UPDATE runs SET state='awaiting_user',finished_at=?,revision=revision+1 WHERE id=?", (_now(), run_id))
            self.connection.execute("UPDATE applications SET state='awaiting_user',updated_at=?,revision=revision+1 WHERE id=?", (_now(), app["id"]))
            self.connection.execute("UPDATE commands SET state='paused',revision=revision+1 WHERE id=?", (command_id,))
            append_event(self.connection, role["id"], "awaiting_user", application_id=app["id"], payload=pause)
        return self.command(command_id)

    def _unexpected_failure(
        self,
        command_id: str,
        *,
        run_id: str | None = None,
        app: sqlite3.Row | None = None,
        role: sqlite3.Row | None = None,
        active_action_id: str | None = None,
        submit_action_id: str | None = None,
        dispatch_id: str | None = None,
    ) -> None:
        """Durably fail closed without exposing an internal failure detail."""
        with self._immediate_transaction():
            command = self.connection.execute(
                "SELECT * FROM commands WHERE id=?", (command_id,)
            ).fetchone()
            if command is None:
                return
            if app is None:
                app = self.connection.execute(
                    "SELECT * FROM applications WHERE id=?", (command["application_id"],)
                ).fetchone()
            if app is None:
                self.connection.execute(
                    "UPDATE commands SET state='failed',revision=revision+1 WHERE id=? "
                    "AND state NOT IN ('completed','cancelled','failed')",
                    (command_id,),
                )
                return
            if role is None:
                role = self._role(str(app["role_id"]))
            if run_id is None:
                run = self.connection.execute(
                    "SELECT id FROM runs WHERE command_id=? ORDER BY created_at DESC LIMIT 1",
                    (command_id,),
                ).fetchone()
                run_id = None if run is None else str(run["id"])
            for action_id in (active_action_id, submit_action_id):
                if action_id is not None:
                    self.connection.execute(
                        "UPDATE actions SET state='failed',completed_at=?,revision=revision+1 "
                        "WHERE id=? AND state='started'",
                        (_now(), action_id),
                    )
            checkpoint_id = _id()
            self.connection.execute(
                "INSERT INTO checkpoints(id,application_id,run_id,kind,state,created_at,revision) "
                "VALUES(?,?,?,'manual_completion','open',?,1)",
                (checkpoint_id, app["id"], run_id, _now()),
            )
            if run_id is not None:
                self.connection.execute(
                    "UPDATE runs SET state=?,finished_at=?,revision=revision+1 WHERE id=?",
                    ("manual_followup" if dispatch_id is not None else "failed", _now(), run_id),
                )
            application_state = "manual_followup" if dispatch_id is not None else "awaiting_user"
            event_kind = "manual_followup" if dispatch_id is not None else "awaiting_user"
            self.connection.execute(
                "UPDATE applications SET state=?,updated_at=?,revision=revision+1 WHERE id=?",
                (application_state, _now(), app["id"]),
            )
            self.connection.execute(
                "UPDATE commands SET state='failed',revision=revision+1 WHERE id=? "
                "AND state NOT IN ('completed','cancelled','failed')",
                (command_id,),
            )
            if dispatch_id is not None:
                self.connection.execute(
                    "UPDATE dispatches SET state='manual_followup',finished_at=?,revision=revision+1 "
                    "WHERE id=? AND state<>'confirmed'",
                    (_now(), dispatch_id),
                )
                self.connection.execute(
                    "UPDATE fixture_dispatch_outcomes SET state='possibly_started',started_at=?,revision=revision+1 "
                    "WHERE dispatch_id=? AND state='prepared'",
                    (_now(), dispatch_id),
                )
                self.connection.execute(
                    "UPDATE fixture_dispatch_outcomes SET state='manual_followup',terminal_at=?,revision=revision+1 "
                    "WHERE dispatch_id=? AND state='possibly_started'",
                    (_now(), dispatch_id),
                )
            self._evidence(
                app["id"], dispatch_id, "pause", stage="execution", reason_code="internal_failure"
            )
            append_event(
                self.connection, role["id"], event_kind, application_id=app["id"],
                payload={
                    "stage": "failure", "reason": "internal_failure",
                    "checkpoint_id": checkpoint_id, "status": application_state,
                    "evidence": {"command_id": command_id},
                },
            )

    def _adapter_failure(
        self,
        run_id: str,
        app: sqlite3.Row,
        role: sqlite3.Row,
        command_id: str,
        dispatch_id: str | None = None,
        *,
        reason_code: str = "adapter_failure",
    ) -> dict[str, Any]:
        """Persist an explicit post-dispatch uncertainty for manual follow-up.

        The exact challenge reason (captcha, mfa, form_drift, ...) is preserved end to end,
        recorded in the checkpoint, status event, and evidence ledger alike; only a genuinely
        ambiguous post-start outcome (no structured pause reason available) defaults to the
        evidence ledger's generic "adapter_failure" reason code.
        """
        if reason_code not in _ADAPTER_FAILURE_REASONS:
            raise OrchestrationError("unsupported adapter failure reason")
        checkpoint_id = _id()
        with self._immediate_transaction():
            self.connection.execute(
                "INSERT INTO checkpoints(id,application_id,run_id,kind,state,created_at,revision) VALUES(?,?,?,'manual_completion','open',?,1)",
                (checkpoint_id, app["id"], run_id, _now()),
            )
            self.connection.execute(
                "UPDATE actions SET state='failed',completed_at=?,revision=revision+1 "
                "WHERE run_id=? AND state='started'",
                (_now(), run_id),
            )
            self._evidence(app["id"], dispatch_id, "pause", stage="execution", reason_code=reason_code)
            self.connection.execute("UPDATE runs SET state='manual_followup',finished_at=?,revision=revision+1 WHERE id=?", (_now(), run_id))
            self.connection.execute("UPDATE applications SET state='manual_followup',updated_at=?,revision=revision+1 WHERE id=?", (_now(), app["id"]))
            self.connection.execute("UPDATE commands SET state='completed',revision=revision+1 WHERE id=?", (command_id,))
            if dispatch_id:
                self.connection.execute(
                    "UPDATE dispatches SET state='manual_followup',finished_at=?,revision=revision+1 "
                    "WHERE id=? AND state<>'confirmed'",
                    (_now(), dispatch_id),
                )
                self.connection.execute(
                    "UPDATE fixture_dispatch_outcomes SET state='possibly_started',started_at=?,revision=revision+1 "
                    "WHERE dispatch_id=? AND state='prepared'",
                    (_now(), dispatch_id),
                )
                self.connection.execute(
                    "UPDATE fixture_dispatch_outcomes SET state='manual_followup',terminal_at=?,revision=revision+1 "
                    "WHERE dispatch_id=? AND state='possibly_started'",
                    (_now(), dispatch_id),
                )
            append_event(
                self.connection, role["id"], "manual_followup", application_id=app["id"],
                payload={"stage": "execution", "reason": reason_code, "checkpoint_id": checkpoint_id,
                         "status": "manual_followup", "evidence": {"run_id": run_id}},
            )
        return self.command(command_id)

    @_serialized
    def recover_stale_commands(self) -> int:
        """Fail closed after a worker crash; never requeue a possibly submitted command."""
        recovered = 0
        with self._immediate_transaction():
            self.connection.execute(
                "UPDATE actions SET state='failed',completed_at=?,revision=revision+1 WHERE state='started'",
                (_now(),),
            )
            commands = list(self.connection.execute("SELECT * FROM commands WHERE state='running'"))
            for command in commands:
                app = self.connection.execute("SELECT * FROM applications WHERE id=?", (command["application_id"],)).fetchone()
                if app is None:
                    self.connection.execute("UPDATE commands SET state='failed',revision=revision+1 WHERE id=?", (command["id"],))
                    recovered += 1
                    continue
                role = self._role(str(app["role_id"]))
                run = self.connection.execute(
                    "SELECT * FROM runs WHERE command_id=? ORDER BY created_at DESC LIMIT 1", (command["id"],)
                ).fetchone()
                dispatch = self.connection.execute(
                    "SELECT * FROM dispatches WHERE application_id=? AND started_at IS NOT NULL ORDER BY started_at DESC LIMIT 1",
                    (app["id"],),
                ).fetchone()
                checkpoint_id = _id()
                if dispatch is not None:
                    applied = self.connection.execute(
                        "SELECT 1 FROM status_events WHERE role_id=? AND application_id=? "
                        "AND event_kind='applied' LIMIT 1",
                        (role["id"], app["id"]),
                    ).fetchone()
                    if applied is not None and self._confirmed_fixture_outcome(str(dispatch["id"])):
                        if run is not None:
                            self.connection.execute(
                                "UPDATE runs SET state='completed',finished_at=?,revision=revision+1 WHERE id=?",
                                (_now(), run["id"]),
                            )
                        self.connection.execute(
                            "UPDATE applications SET state='submitted',updated_at=?,revision=revision+1 WHERE id=?",
                            (_now(), app["id"]),
                        )
                        self.connection.execute(
                            "UPDATE commands SET state='completed',revision=revision+1 WHERE id=?",
                            (command["id"],),
                        )
                    else:
                        self.connection.execute(
                            "INSERT INTO checkpoints(id,application_id,run_id,kind,state,created_at,revision) VALUES(?,?,?,'manual_completion','open',?,1)",
                            (checkpoint_id, app["id"], None if run is None else run["id"], _now()),
                        )
                        self._evidence(app["id"], dispatch["id"], "pause", stage="recovery", reason_code="stale_running_dispatch")
                        if run is not None:
                            self.connection.execute("UPDATE runs SET state='manual_followup',finished_at=?,revision=revision+1 WHERE id=?", (_now(), run["id"]))
                        if dispatch["state"] != "confirmed":
                            self.connection.execute("UPDATE dispatches SET state='manual_followup',finished_at=?,revision=revision+1 WHERE id=?", (_now(), dispatch["id"]))
                        self.connection.execute(
                            "UPDATE fixture_dispatch_outcomes SET state='possibly_started',started_at=?,revision=revision+1 "
                            "WHERE dispatch_id=? AND state='prepared'",
                            (_now(), dispatch["id"]),
                        )
                        self.connection.execute(
                            "UPDATE fixture_dispatch_outcomes SET state='manual_followup',terminal_at=?,revision=revision+1 "
                            "WHERE dispatch_id=? AND state='possibly_started'",
                            (_now(), dispatch["id"]),
                        )
                        self.connection.execute("UPDATE applications SET state='manual_followup',updated_at=?,revision=revision+1 WHERE id=?", (_now(), app["id"]))
                        self.connection.execute("UPDATE commands SET state='completed',revision=revision+1 WHERE id=?", (command["id"],))
                        append_event(self.connection, role["id"], "manual_followup", application_id=app["id"], payload={"stage": "recovery", "reason": "stale_running_dispatch", "checkpoint_id": checkpoint_id, "status": "manual_followup", "evidence": {"command_id": command["id"]}})
                else:
                    self.connection.execute(
                        "INSERT INTO checkpoints(id,application_id,run_id,kind,state,created_at,revision) VALUES(?,?,?,'manual_completion','open',?,1)",
                        (checkpoint_id, app["id"], None if run is None else run["id"], _now()),
                    )
                    self._evidence(app["id"], None, "pause", stage="recovery", reason_code="stale_running_no_dispatch")
                    if run is not None:
                        self.connection.execute("UPDATE runs SET state='awaiting_user',finished_at=?,revision=revision+1 WHERE id=?", (_now(), run["id"]))
                    self.connection.execute("UPDATE applications SET state='awaiting_user',updated_at=?,revision=revision+1 WHERE id=?", (_now(), app["id"]))
                    self.connection.execute("UPDATE commands SET state='paused',revision=revision+1 WHERE id=?", (command["id"],))
                    append_event(self.connection, role["id"], "awaiting_user", application_id=app["id"], payload={"stage": "recovery", "reason": "stale_running_no_dispatch", "checkpoint_id": checkpoint_id, "status": "awaiting_user", "evidence": {"command_id": command["id"]}})
                recovered += 1
        return recovered

    @_serialized
    def command(self, command_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None: raise OrchestrationError("command is unavailable")
        result = dict(row)
        application = self.connection.execute(
            "SELECT state FROM applications WHERE id=?", (row["application_id"],)
        ).fetchone()
        application_state = None if application is None else str(application["state"])
        checkpoint = self.connection.execute(
            "SELECT kind FROM checkpoints WHERE application_id=? AND state='open' ORDER BY created_at DESC LIMIT 1",
            (row["application_id"],),
        ).fetchone()
        result["application_state"] = application_state
        result["open_checkpoint_reason"] = None if checkpoint is None else str(checkpoint["kind"])
        result["terminal_outcome"] = self._terminal_outcome(str(row["state"]), application_state)
        return result

    @staticmethod
    def _terminal_outcome(command_state: str, application_state: str | None) -> str:
        """A non-maskable outcome signal: a 'completed' command never alone implies success."""
        if command_state in {"accepted", "running"}:
            return "in_progress"
        if command_state == "cancelled":
            return "cancelled"
        if command_state == "paused":
            return "awaiting_user"
        if command_state == "failed":
            return "manual_followup" if application_state == "manual_followup" else "failed"
        if command_state == "completed":
            if application_state == "manual_followup":
                return "manual_followup"
            if application_state == "submitted":
                return "applied"
            return "success"
        return "unknown"

    @_serialized
    def evidence_event(self, evidence_id: str) -> dict[str, Any]:
        if self._evidence_key is None:
            raise OrchestrationError("real execution has no evidence authority")
        row = self.connection.execute(
            "SELECT id,application_id,kind,metadata_json,created_at FROM evidence WHERE id=?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise OrchestrationError("evidence is unavailable")
        if not verify_evidence_ledger(
            self.connection,
            row["application_id"],
            authentication_key=self._evidence_key,
            key_version=1,
        ):
            raise OrchestrationError("evidence ledger is invalid")
        return {
            "id": row["id"],
            "kind": row["kind"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    @_serialized
    def snapshot(self) -> dict[str, Any]:
        roles = []
        for role in self.connection.execute("SELECT * FROM roles ORDER BY id"):
            application = self.connection.execute(
                "SELECT * FROM applications WHERE role_id=? ORDER BY updated_at DESC LIMIT 1", (role["id"],)
            ).fetchone()
            command = None if application is None else self.connection.execute(
                "SELECT * FROM commands WHERE application_id=? ORDER BY created_at DESC LIMIT 1", (application["id"],)
            ).fetchone()
            checkpoint = None if application is None else self.connection.execute(
                "SELECT kind FROM checkpoints WHERE application_id=? AND state='open' ORDER BY created_at DESC LIMIT 1",
                (application["id"],)
            ).fetchone()
            canonical_event = current_canonical_event(self.connection, str(role["id"]))
            status_kind = None if canonical_event is None else canonical_event[0]
            status_payload = {} if canonical_event is None else canonical_event[1]
            projected_status = (
                status_kind
                if status_kind in {"applied", "rejected", "cancelled", "closed"}
                else self._catalog_entry(role["id"]).get("catalog_status", role["status"])
            )
            automation_state = (
                "applied"
                if projected_status == "applied"
                else "manual_follow_up"
                if status_kind == "manual_followup"
                else "idle"
                if application is None
                else application["state"]
            )
            roles.append({
                "role_id": role["id"],
                "status": projected_status,
                "automation": {
                    "state": automation_state,
                    "command_id": None if command is None else command["id"],
                    "checkpoint_code": None if checkpoint is None else checkpoint["kind"],
                    "pause": status_payload if status_kind in {"awaiting_user", "manual_followup"} else None,
                },
            })
        profile = self.connection.execute(
            "SELECT id FROM candidate_profiles WHERE state='active' ORDER BY created_at LIMIT 1"
        ).fetchone()
        policy, used, _ = (
            self._policy_projection(str(profile["id"]), provision_fixture=False)
            if profile is not None
            else (None, 0, "")
        )
        return {
            "catalog_revision": self.catalog_revision,
            "roles": roles,
            "automation": {
                "kill_switch_active": policy is None,
                "daily_quota": {"used": used, "limit": int(policy["daily_cap"]) if policy is not None else 0},
                "fixture_mode": self.fixture_mode,
            },
        }
