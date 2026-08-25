"""Typed, fail-closed contracts for application automation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import math
from types import MappingProxyType
from typing import Mapping

from .region import load_region


class ExecutionMode(str, Enum):
    DRY_RUN = "dry_run"
    FILL_ONLY = "fill_only"
    BATCH = "batch"
    ATTENDED = "attended"


class RunState(str, Enum):
    QUEUED = "queued"
    INSPECTING = "inspecting"
    FILLING = "filling"
    AWAITING_USER = "awaiting_user"
    DISPATCHING = "dispatching"
    MANUAL_FOLLOWUP = "manual_followup"
    COMPLETED = "completed"
    FAILED = "failed"


class CheckpointState(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class DispatchState(str, Enum):
    INTENDED = "intended"
    STARTED = "started"
    DISPATCHING = "dispatching"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    FOLLOWUP_MANUAL = "manual_followup"


class RoleEligibility(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    PAUSED = "paused"


class Transport(str, Enum):
    DIRECT = "direct"
    ASIDE = "aside"


class PauseReason(str, Enum):
    CAPTCHA = "captcha"
    MFA = "mfa"
    LOGIN = "login"
    SECURITY_CHALLENGE = "security_challenge"
    RATE_LIMIT = "rate_limit"
    NEW_QUESTION = "new_question"
    UNKNOWN_QUESTION = "unknown_question"
    SENSITIVE_QUESTION = "sensitive_question"
    LEGAL_QUESTION = "legal_question"
    ATTESTATION = "attestation"
    STREET_ADDRESS = "street_address"
    REQUIRED_DEMOGRAPHICS = "required_demographics"
    SALARY_UNVERIFIED = "salary_unverified"
    FORM_DRIFT = "form_drift"
    POSTING_DRIFT = "posting_drift"
    POLICY_EXPIRED = "policy_expired"
    POLICY_REVOKED = "policy_revoked"
    POLICY_BINDING_MISMATCH = "policy_binding_mismatch"
    KILL_SWITCH = "kill_switch"
    BREAKER_OPEN = "breaker_open"
    DAILY_CAP = "daily_cap"


def _required_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")


def _positive_revision(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")

def _sha256(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")

def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class CandidateAssertion:
    key: str
    value: object
    assertion_id: str
    assertion_revision: int
    profile_id: str
    profile_revision: int
    snapshot_id: str
    snapshot_revision: int
    confirmation_event_id: str
    confirmation_event_revision: int
    confirmed_at: datetime
    form_fingerprint: str | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("key", "assertion_id", "profile_id", "snapshot_id", "confirmation_event_id"):
            _required_text(getattr(self, name), name)
        for name in ("assertion_revision", "profile_revision", "snapshot_revision", "confirmation_event_revision"):
            _positive_revision(getattr(self, name), name)
        object.__setattr__(self, "confirmed_at", _utc(self.confirmed_at, "confirmed_at"))
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _utc(self.revoked_at, "revoked_at"))


@dataclass(frozen=True)
class ExactAlias:
    semantic_key: str
    alias: str
    provider: str
    form_fingerprint: str
    alias_id: str
    alias_revision: int
    profile_id: str
    profile_revision: int
    snapshot_id: str
    snapshot_revision: int
    confirmation_event_id: str
    confirmation_event_revision: int

    def __post_init__(self) -> None:
        for name in ("semantic_key", "alias", "provider", "form_fingerprint", "alias_id", "profile_id", "snapshot_id", "confirmation_event_id"):
            _required_text(getattr(self, name), name)
        for name in ("alias_revision", "profile_revision", "snapshot_revision", "confirmation_event_revision"):
            _positive_revision(getattr(self, name), name)


@dataclass(frozen=True)
class FormField:
    field_key: str
    normalized_label: str
    input_type: str
    required: bool
    option_keys: tuple[str, ...] = ()
    semantic_class: str | None = None
    step_key: str = "default"


@dataclass(frozen=True)
class FormStep:
    step_key: str
    ordered_fields: tuple[FormField, ...]
    submit_control_signature: str


@dataclass(frozen=True)
class FormSnapshot:
    provider: str
    tenant: str
    canonical_url_pattern: str
    aside_version: str
    script_sha256: str
    ordered_steps: tuple[FormStep, ...]
    redirect_chain: tuple[str, ...] = ()
    frame_origins: tuple[str, ...] = ()
    def __post_init__(self) -> None:
        for name in ("provider", "tenant", "canonical_url_pattern", "aside_version"):
            _required_text(getattr(self, name), name)
        _sha256(self.script_sha256, "script_sha256")


@dataclass(frozen=True)
class FillValue:
    field_key: str
    value: str
    assertion_id: str
    assertion_revision: int
    alias_id: str
    alias_revision: int
    def __post_init__(self) -> None:
        _required_text(self.field_key, "field_key")
        _required_text(self.value, "value")
        _required_text(self.assertion_id, "assertion_id")
        _positive_revision(self.assertion_revision, "assertion_revision")
        _required_text(self.alias_id, "alias_id")
        _positive_revision(self.alias_revision, "alias_revision")


@dataclass(frozen=True)
class FillPlan:
    application_id: str
    snapshot_digest: str
    values: tuple[FillValue, ...]
    material_sha256: str | None = None
    role_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.application_id, "application_id")
        _required_text(self.role_id, "role_id")
        _sha256(self.snapshot_digest, "snapshot_digest")
        if self.material_sha256 is None:
            raise ValueError("material_sha256 must be a SHA-256 hex digest")
        _sha256(self.material_sha256, "material_sha256")

@dataclass(frozen=True)
class FillOutcome:
    state: RunState
    filled_field_keys: tuple[str, ...] = ()
    pause_reason: PauseReason | None = None
    observed_form_fingerprint: str | None = None


@dataclass(frozen=True)
class FixtureCapability:
    environment: str
    adapter_id: str
    origin: str
    capability_id: str
    capability_revision: int
    confirmation_event_id: str
    confirmation_event_revision: int

    def __post_init__(self) -> None:
        if self.environment != "fixture":
            raise ValueError("only fixture capabilities are permitted")
        for name in ("adapter_id", "origin", "capability_id", "confirmation_event_id"):
            _required_text(getattr(self, name), name)
        for name in ("capability_revision", "confirmation_event_revision"):
            _positive_revision(getattr(self, name), name)


@dataclass(frozen=True)
class ProviderFormBinding:
    provider: str
    tenant: str
    operation: str
    transport: Transport
    fixture_capability: FixtureCapability
    snapshot_digest: str

    def __post_init__(self) -> None:
        _required_text(self.provider, "provider")
        _required_text(self.tenant, "tenant")
        _required_text(self.operation, "operation")
        _sha256(self.snapshot_digest, "snapshot_digest")

@dataclass(frozen=True)
class MaterialBinding:
    role_id: str
    application_dir: str
    artifact_sha256: str
    source_template_sha256: str
    application_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("role_id", "application_dir", "application_id"):
            _required_text(getattr(self, name), name)
        _sha256(self.artifact_sha256, "artifact_sha256")
        _sha256(self.source_template_sha256, "source_template_sha256")

@dataclass(frozen=True)
class BatchPolicy:
    policy_id: str
    state: str
    valid_from: datetime
    expires_at: datetime
    min_fit_score: float
    timezone: str
    daily_cap: int
    assertion_snapshot_id: str
    assertion_snapshot_revision: int
    provider_forms: tuple[ProviderFormBinding, ...]
    materials: tuple[MaterialBinding, ...]
    permitted_assertion_keys: tuple[str, ...]
    global_kill_switch_revision: int
    signature_hmac: str
    policy_revision: int
    candidate_profile_id: str
    candidate_profile_revision: int
    assertion_snapshot_digest: str
    provider_kill_switch_revisions: Mapping[str, int] = field(default_factory=dict)
    breaker_generation: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_from", _utc(self.valid_from, "valid_from"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
        object.__setattr__(self, "provider_forms", tuple(self.provider_forms))
        object.__setattr__(self, "materials", tuple(self.materials))
        object.__setattr__(self, "permitted_assertion_keys", tuple(self.permitted_assertion_keys))
        object.__setattr__(self, "provider_kill_switch_revisions", MappingProxyType(dict(self.provider_kill_switch_revisions)))
        region_timezone = load_region().timezone
        if self.timezone != region_timezone:
            raise ValueError(f"batch policy timezone must be {region_timezone}")
        if not math.isfinite(self.min_fit_score) or not 5 <= self.min_fit_score <= 10:
            raise ValueError("min_fit_score must be a finite value between 5 and 10")
        if not 1 <= self.daily_cap <= 20 or not self.provider_forms:
            raise ValueError("batch policy must have a bounded cap and fixture binding")
        _required_text(self.policy_id, "policy_id")
        _required_text(self.state, "state")
        _required_text(self.assertion_snapshot_id, "assertion_snapshot_id")
        for name in ("assertion_snapshot_revision", "global_kill_switch_revision", "policy_revision", "breaker_generation"):
            _positive_revision(getattr(self, name), name)
        _required_text(self.candidate_profile_id, "candidate_profile_id")
        _positive_revision(self.candidate_profile_revision, "candidate_profile_revision")
        _sha256(self.assertion_snapshot_digest, "assertion_snapshot_digest")
        if len(set(self.permitted_assertion_keys)) != len(self.permitted_assertion_keys) or not all(self.permitted_assertion_keys):
            raise ValueError("permitted assertion keys must be unique and non-empty")
        if not isinstance(self.signature_hmac, str) or len(self.signature_hmac) != 64 or any(c not in "0123456789abcdef" for c in self.signature_hmac):
            raise ValueError("batch policy signature must be a SHA-256 HMAC")
        if self.expires_at <= self.valid_from or (self.expires_at - self.valid_from).total_seconds() > 86400:
            raise ValueError("batch policy validity must be positive and not exceed 24 hours")


@dataclass(frozen=True)
class RoleInput:
    role_id: str
    status: str
    score: float
    location: str
    remote: bool
    remote_country: str | None
    posting_active: bool
    started_or_submitted: bool = False

    def __post_init__(self) -> None:
        _required_text(self.role_id, "role_id")
        if not math.isfinite(self.score) or not 0 <= self.score <= 10:
            raise ValueError("role score must be a finite value between 0 and 10")

@dataclass(frozen=True)
class EligibilityResult:
    eligibility: RoleEligibility
    pause_reason: PauseReason | None = None
    detail: str = ""


@dataclass(frozen=True)
class AssertionResolution:
    value: str | None
    assertion_key: str | None
    assertion_id: str | None
    assertion_revision: int | None
    alias_id: str | None
    alias_revision: int | None
    profile_id: str | None
    profile_revision: int | None
    snapshot_id: str | None
    snapshot_revision: int | None
    confirmation_event_id: str | None
    confirmation_event_revision: int | None
    pause_reason: PauseReason | None

    @property
    def resolved(self) -> bool:
        return self.value is not None and self.pause_reason is None


@dataclass(frozen=True)
class DispatchIntent:
    dispatch_id: str
    application_id: str
    policy_id: str
    policy_revision: int
    binding: ProviderFormBinding
    assertion_snapshot_id: str
    assertion_snapshot_revision: int
    material: MaterialBinding
    snapshot_digest: str
    fill_plan_digest: str
    assertion_proof_digest: str
    global_kill_switch_revision: int
    provider_kill_switch_revision: int
    breaker_generation: int
    role_id: str | None = None
    def __post_init__(self) -> None:
        for name in ("dispatch_id", "application_id", "policy_id", "role_id"):
            _required_text(getattr(self, name), name)
        _sha256(self.snapshot_digest, "snapshot_digest")
        _sha256(self.fill_plan_digest, "fill_plan_digest")
        _sha256(self.assertion_proof_digest, "assertion_proof_digest")
        for name in (
            "policy_revision",
            "assertion_snapshot_revision",
            "global_kill_switch_revision",
            "provider_kill_switch_revision",
            "breaker_generation",
        ):
            _positive_revision(getattr(self, name), name)
