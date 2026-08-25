"""Typed, fail-closed contract for guarded Aside automation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol
import hashlib
import json
import re


class AsideError(RuntimeError):
    """Base failure at the Aside provider boundary."""


class AsideProbeError(AsideError):
    """A safe local Aside capability probe could not be trusted."""


class AsideTransportError(AsideError):
    """The protected MCP transport failed or returned an invalid response."""


class AsideProtocolError(AsideError):
    """Aside violated the reviewed deterministic result contract."""


class PauseReason(str, Enum):
    CAPTCHA = "captcha"
    MFA = "mfa"
    SECURITY_CHALLENGE = "security_challenge"
    RATE_LIMIT = "rate_limit"
    PROVIDER_CHALLENGE = "provider_challenge"
    LOGIN = "login"
    ACCOUNT_CREATION = "account_creation"
    NEW_QUESTION = "new_question"
    UNKNOWN_QUESTION = "unknown_question"
    SENSITIVE_QUESTION = "sensitive_question"
    LEGAL_QUESTION = "legal_question"
    REQUIRED_DEMOGRAPHICS = "required_demographics"
    STREET_ADDRESS = "street_address"
    SALARY_UNVERIFIED = "salary_unverified"
    SALARY_EXACT_NUMBER = "salary_exact_number"
    ATTESTATION = "attestation"
    FORM_DRIFT = "form_drift"
    POSTING_DRIFT = "posting_drift"
    UNEXPECTED_REDIRECT = "unexpected_redirect"


OBSERVATION_STATES = frozenset({"not_started", "confirmed", "manual_follow_up", "awaiting_user"})
RESULT_SCHEMA = "application_automation.aside.v1"


@dataclass(frozen=True)
class ScriptRef:
    script_id: str
    version: str
    path: Path
    sha256: str
    allowed_domains: frozenset[str]


@dataclass(frozen=True)
class AsideRunContext:
    aside_version: str
    cli_path_sha256: str
    account_id_hmac: str
    context_id_hmac: str
    session_id_hmac: str
    provider: str
    tenant: str
    expected_page_fingerprint: str
    expected_form_fingerprint: str
    run_key: str
    timeout_seconds: float = 30.0
    fixture_scenario: str = "happy"


@dataclass(frozen=True)
class AsideDoctorResult:
    available: bool
    signed_in: bool
    version: str | None
    mcp_available: bool
    repl_available: bool
    pause_reason: PauseReason | None = None
    detail: str | None = None


@dataclass(frozen=True)
class FormSnapshot:
    page_fingerprint: str
    form_fingerprint: str
    domain: str
    fields: tuple[str, ...] = ()
    pause_reason: PauseReason | None = None


@dataclass(frozen=True)
class FillPlan:
    fields: Mapping[str, str]
    resume_path: Path | None = None
    resume_sha256: str | None = None
    application_id: str | None = None


@dataclass(frozen=True)
class FillOutcome:
    filled: bool
    attached_resume_sha256: str | None
    page_fingerprint: str
    form_fingerprint: str
    pause_reason: PauseReason | None = None
    field_digest: str | None = None


@dataclass(frozen=True)
class DispatchIntent:
    dispatch_id: str
    application_id: str
    session_id: str
    run_id: str
    intent_hmac: str
    payload_sha256: str
    page_fingerprint: str
    form_fingerprint: str
    resume_sha256: str | None = None
    field_digest: str | None = None


@dataclass(frozen=True)
class SubmitOutcome:
    started: bool
    confirmed: bool
    manual_follow_up: bool
    receipt_id: str | None
    pause_reason: PauseReason | None = None


@dataclass(frozen=True)
class StatusObservation:
    state: str
    page_fingerprint: str
    form_fingerprint: str
    receipt_id: str | None = None
    pause_reason: PauseReason | None = None


def canonical_dispatch_payload_sha256(dispatch: DispatchIntent) -> str:
    payload = {
        "dispatch_id": dispatch.dispatch_id,
        "application_id": dispatch.application_id,
        "session_id": dispatch.session_id,
        "run_id": dispatch.run_id,
        "page_fingerprint": dispatch.page_fingerprint,
        "form_fingerprint": dispatch.form_fingerprint,
        "resume_sha256": dispatch.resume_sha256,
        "field_digest": dispatch.field_digest,
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def canonical_field_digest(fields: Mapping[str, str]) -> str:
    """The single canonical digest of a typed fill plan's fields.

    Reused verbatim by both fixture executors so a dispatch's ``field_digest``
    can be checked against durable fill evidence without a second implementation.
    """
    if not isinstance(fields, Mapping):
        raise AsideProtocolError("fill plan fields must be a typed mapping")
    normalized: dict[str, str] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise AsideProtocolError("fill plan fields must be typed strings")
        normalized[key] = value
    payload = {"schema": "application_automation.aside.fields.v1", "fields": normalized}
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def validate_fixture_dispatch(dispatch: DispatchIntent, ctx: AsideRunContext) -> None:
    values = (
        dispatch.dispatch_id, dispatch.application_id, dispatch.session_id, dispatch.run_id,
        dispatch.intent_hmac, dispatch.payload_sha256, dispatch.page_fingerprint,
        dispatch.form_fingerprint,
    )
    if not all(isinstance(value, str) and value for value in values):
        raise AsideProtocolError("dispatch identity is incomplete")
    if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (dispatch.intent_hmac, dispatch.payload_sha256)):
        raise AsideProtocolError("dispatch trust anchor is invalid")
    if dispatch.resume_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", dispatch.resume_sha256):
        raise AsideProtocolError("dispatch resume hash is invalid")
    if dispatch.field_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", dispatch.field_digest):
        raise AsideProtocolError("dispatch field digest is invalid")

    if ctx.provider != "fixture" or ctx.tenant != "fixture":
        raise AsideProtocolError("fixture provider or tenant drift")
    if dispatch.run_id != ctx.run_key:
        raise AsideProtocolError("dispatch run identity drift")
    if dispatch.page_fingerprint != ctx.expected_page_fingerprint or dispatch.form_fingerprint != ctx.expected_form_fingerprint:
        raise AsideProtocolError("dispatch fingerprint mismatch")
    if canonical_dispatch_payload_sha256(dispatch) != dispatch.payload_sha256:
        raise AsideProtocolError("dispatch payload digest drift")

def decode_result(result: Mapping[str, Any], operation: str) -> Mapping[str, Any]:
    """Validate the sole wire contract used by fixture JS and MCP scripts."""
    common = {"schema", "operation", "domain", "page_fingerprint", "form_fingerprint", "pause_reason"}
    operation_fields = {
        "inspect": {"fields"},
        "fill": {"filled", "attached_resume_sha256"},
        "submit": {"started", "confirmed", "manual_follow_up", "receipt_id"},
        "observe": {"state", "receipt_id"},
    }
    allowed = common | operation_fields.get(operation, set())
    if operation not in operation_fields:
        raise AsideProtocolError("unknown operation")
    if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA or result.get("operation") != operation:
        raise AsideProtocolError("unknown result schema")
    if set(result).difference(allowed):
        raise AsideProtocolError("operation-inappropriate result field")
    for key in ("domain", "page_fingerprint", "form_fingerprint"):
        if not isinstance(result.get(key), str) or not result[key]:
            raise AsideProtocolError(f"missing {key}")
    pause = result.get("pause_reason")
    if pause is not None:
        try:
            PauseReason(pause)
        except (TypeError, ValueError) as error:
            raise AsideProtocolError("unknown pause reason") from error
    if operation == "inspect":
        fields = result.get("fields")
        if not isinstance(fields, list) or not fields or not all(isinstance(field, str) and field for field in fields) or len(set(fields)) != len(fields):
            raise AsideProtocolError("inspect fields are incomplete")
    elif operation == "fill":
        filled = _require_bool(result, "filled")
        _optional_digest(result, "attached_resume_sha256")
        if pause is not None and (filled or result.get("attached_resume_sha256") is not None):
            raise AsideProtocolError("paused fill has a side effect")
        if pause is None and not filled:
            raise AsideProtocolError("unpaused fill must complete")
    elif operation == "submit":
        started, confirmed, follow_up = (_require_bool(result, key) for key in ("started", "confirmed", "manual_follow_up"))
        receipt = result.get("receipt_id")
        if receipt is not None and (not isinstance(receipt, str) or not receipt):
            raise AsideProtocolError("invalid receipt_id")
        if pause is not None and (started or confirmed or follow_up or receipt is not None):
            raise AsideProtocolError("paused submit has a side effect")
        if pause is None and not started:
            raise AsideProtocolError("unpaused submit must start or require manual follow-up")
        if confirmed != (started and not follow_up and receipt is not None):
            raise AsideProtocolError("invalid submit confirmation")
        if follow_up != (started and not confirmed and receipt is None):
            raise AsideProtocolError("invalid manual follow-up")
    elif operation == "observe":
        state = result.get("state")
        if state not in OBSERVATION_STATES:
            raise AsideProtocolError("unknown observation state")
        receipt = result.get("receipt_id")
        if receipt is not None and (not isinstance(receipt, str) or not receipt):
            raise AsideProtocolError("invalid receipt_id")
        if (pause is None) != (state != "awaiting_user"):
            raise AsideProtocolError("awaiting-user pause state mismatch")
        if pause is not None and receipt is not None:
            raise AsideProtocolError("paused observation cannot have a receipt")
        if pause is None and state == "confirmed" and receipt is None:
            raise AsideProtocolError("confirmed observation requires a receipt")
        if state != "confirmed" and receipt is not None:
            raise AsideProtocolError("non-confirmed observation cannot have a receipt")
    return result


def _require_bool(result: Mapping[str, Any], key: str) -> bool:
    value = result.get(key)
    if not isinstance(value, bool):
        raise AsideProtocolError(f"missing {key}")
    return value


def _optional_digest(result: Mapping[str, Any], key: str) -> None:
    value = result.get(key)
    if value is not None and (not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
        raise AsideProtocolError(f"invalid {key}")


class AsideExecutor(Protocol):
    def doctor(self) -> AsideDoctorResult: ...
    def inspect(self, ctx: AsideRunContext, script: ScriptRef) -> FormSnapshot: ...
    def fill(self, ctx: AsideRunContext, script: ScriptRef, plan: FillPlan) -> FillOutcome: ...
    def submit(self, ctx: AsideRunContext, script: ScriptRef, dispatch: DispatchIntent) -> SubmitOutcome: ...
    def observe(self, ctx: AsideRunContext, script: ScriptRef, dispatch_id: str) -> StatusObservation: ...
