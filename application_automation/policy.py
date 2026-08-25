"""Pure, fail-closed fixture-only eligibility and dispatch checks."""
from __future__ import annotations

from datetime import UTC, datetime
import math
from typing import Callable, Iterable, Mapping

from .crypto import canonical_json, sha256_artifact, verify_domain_hmac
from .assertions import AssertionRegistry
from .region import load_region
from .models import (
    AssertionResolution, BatchPolicy, DispatchIntent, EligibilityResult, FillPlan,
    FormSnapshot, MaterialBinding, PauseReason, ProviderFormBinding, RoleEligibility,
    RoleInput, Transport,
)

def _trusted_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("trusted clock must return a timezone-aware datetime")
    return now.astimezone(UTC)


def role_in_scope(role: RoleInput) -> bool:
    region = load_region()
    return role.location in region.locations or (role.remote and role.remote_country == region.country)


def policy_is_active(policy: BatchPolicy, now: datetime) -> PauseReason | None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return PauseReason.POLICY_EXPIRED
    now = now.astimezone(UTC)
    if policy.state == "revoked":
        return PauseReason.POLICY_REVOKED
    if policy.state != "active" or now < policy.valid_from or now >= policy.expires_at:
        return PauseReason.POLICY_EXPIRED
    return None


def fixture_capability_claims(binding: ProviderFormBinding) -> dict[str, object]:
    capability = binding.fixture_capability
    return {
        "environment": capability.environment,
        "adapter_id": capability.adapter_id,
        "origin": capability.origin,
        "capability_id": capability.capability_id,
        "capability_revision": capability.capability_revision,
        "confirmation_event_id": capability.confirmation_event_id,
        "confirmation_event_revision": capability.confirmation_event_revision,
    }


def policy_signature_claims(policy: BatchPolicy) -> dict[str, object]:
    """Every dispatch authority fact, including fixture provenance, is signed."""
    return {
        "authority_domain": "fixture_only",
        "policy_id": policy.policy_id,
        "policy_revision": policy.policy_revision,
        "state": policy.state,
        "valid_from": policy.valid_from.isoformat(),
        "expires_at": policy.expires_at.isoformat(),
        "min_fit_score": policy.min_fit_score,
        "timezone": policy.timezone,
        "daily_cap": policy.daily_cap,
        "assertion_snapshot_id": policy.assertion_snapshot_id,
        "assertion_snapshot_revision": policy.assertion_snapshot_revision,
        "candidate_profile_id": policy.candidate_profile_id,
        "candidate_profile_revision": policy.candidate_profile_revision,
        "assertion_snapshot_digest": policy.assertion_snapshot_digest,
        "provider_forms": [
            {
                "provider": binding.provider,
                "tenant": binding.tenant,
                "operation": binding.operation,
                "transport": binding.transport.value,
                "fixture_capability": fixture_capability_claims(binding),
                "snapshot_digest": binding.snapshot_digest,
            }
            for binding in policy.provider_forms
        ],
        "materials": [
            {"role_id": item.role_id, "application_id": item.application_id,
             "application_dir": item.application_dir, "artifact_sha256": item.artifact_sha256,
             "source_template_sha256": item.source_template_sha256}
            for item in policy.materials
        ],
        "permitted_assertion_keys": list(policy.permitted_assertion_keys),
        "global_kill_switch_revision": policy.global_kill_switch_revision,
        "provider_kill_switch_revisions": dict(policy.provider_kill_switch_revisions),
        "breaker_generation": policy.breaker_generation,
    }


def verify_policy_signature(policy: BatchPolicy, key: bytes) -> bool:
    return verify_domain_hmac(key, "batch_policy.v2", policy_signature_claims(policy), policy.signature_hmac)


def snapshot_digest(snapshot: FormSnapshot) -> str:
    """Hash all form behavior relevant to unattended completion in observed order."""
    return sha256_artifact(canonical_json({
        "provider": snapshot.provider,
        "tenant": snapshot.tenant,
        "canonical_url_pattern": snapshot.canonical_url_pattern,
        "aside_version": snapshot.aside_version,
        "script_sha256": snapshot.script_sha256,
        "redirect_chain": list(snapshot.redirect_chain),
        "frame_origins": list(snapshot.frame_origins),
        "ordered_steps": [
            {"step_key": step.step_key, "submit_control_signature": step.submit_control_signature,
             "ordered_fields": [
                 {"field_key": field.field_key, "normalized_label": field.normalized_label,
                  "input_type": field.input_type, "required": field.required,
                  "option_keys": list(field.option_keys), "semantic_class": field.semantic_class,
                  "step_key": field.step_key}
                 for field in step.ordered_fields
             ]}
            for step in snapshot.ordered_steps
        ],
    }))


def expected_field_keys(snapshot: FormSnapshot) -> tuple[str, ...]:
    return tuple(field.field_key for step in snapshot.ordered_steps for field in step.ordered_fields)


def unattended_binding_is_complete(binding: ProviderFormBinding) -> bool:
    capability = binding.fixture_capability
    return (
        binding.transport is Transport.ASIDE and binding.operation == "submit"
        and capability.environment == "fixture" and bool(capability.adapter_id)
        and bool(capability.origin) and bool(capability.confirmation_event_id)
        and capability.capability_revision > 0 and capability.confirmation_event_revision > 0
        and isinstance(binding.snapshot_digest, str) and len(binding.snapshot_digest) == 64
    )


def assertion_proof_digest(resolutions: Iterable[AssertionResolution], snapshot: FormSnapshot) -> str | None:
    fields = tuple(field for step in snapshot.ordered_steps for field in step.ordered_fields)
    rows = tuple(resolutions)
    if len(rows) != len(fields):
        return None
    proof_rows: list[dict[str, object]] = []
    resolution_bindings: set[tuple[object, ...]] = set()
    for field, resolution in zip(fields, rows, strict=True):
        provider = getattr(resolution, "provider", None)
        form_fingerprint = getattr(resolution, "form_fingerprint", None)
        field_key = getattr(resolution, "field_key", None)
        normalized_label = getattr(resolution, "normalized_label", None)
        binding = (
            provider, form_fingerprint, field_key, normalized_label, resolution.assertion_id,
            resolution.assertion_revision, resolution.alias_id, resolution.alias_revision,
        )
        if (
            not resolution.resolved
            or provider != snapshot.provider
            or not isinstance(form_fingerprint, str)
            or not form_fingerprint
            or field_key != field.field_key
            or normalized_label != field.normalized_label
            or binding in resolution_bindings
            or not all((resolution.assertion_key, resolution.assertion_id, resolution.alias_id,
                        resolution.profile_id, resolution.snapshot_id,
                        resolution.confirmation_event_id))
        ):
            return None
        resolution_bindings.add(binding)
        if any(value is None or value < 1 for value in (
            resolution.assertion_revision, resolution.alias_revision, resolution.profile_revision,
            resolution.snapshot_revision, resolution.confirmation_event_revision,
        )):
            return None
        proof_rows.append({
            "provider": provider, "form_fingerprint": form_fingerprint, "field_key": field_key,
            "normalized_label": normalized_label, "assertion_key": resolution.assertion_key,
            "assertion_id": resolution.assertion_id, "assertion_revision": resolution.assertion_revision,
            "alias_id": resolution.alias_id, "alias_revision": resolution.alias_revision,
            "profile_id": resolution.profile_id, "profile_revision": resolution.profile_revision,
            "snapshot_id": resolution.snapshot_id, "snapshot_revision": resolution.snapshot_revision,
            "confirmation_event_id": resolution.confirmation_event_id,
            "confirmation_event_revision": resolution.confirmation_event_revision,
        })
    return sha256_artifact(canonical_json(proof_rows))


def _proofs_permitted(
    policy: BatchPolicy,
    resolutions: Iterable[AssertionResolution],
    snapshot: FormSnapshot,
    assertion_registry: AssertionRegistry | None,
) -> bool:
    rows = tuple(resolutions)
    if (
        assertion_registry is None
        or not assertion_registry.authenticates(rows)
        or (
            assertion_registry.profile_id,
            assertion_registry.profile_revision,
            assertion_registry.snapshot_id,
            assertion_registry.snapshot_revision,
            assertion_registry.assertion_snapshot_digest,
        ) != (
            policy.candidate_profile_id,
            policy.candidate_profile_revision,
            policy.assertion_snapshot_id,
            policy.assertion_snapshot_revision,
            policy.assertion_snapshot_digest,
        )
    ):
        return False
    if not rows:
        return False
    return (
        assertion_proof_digest(rows, snapshot) is not None
        and all(
            row.assertion_key in policy.permitted_assertion_keys
            and (row.profile_id, row.profile_revision)
            == (policy.candidate_profile_id, policy.candidate_profile_revision)
            and (row.snapshot_id, row.snapshot_revision)
            == (policy.assertion_snapshot_id, policy.assertion_snapshot_revision)
            for row in rows
        )
    )
def _fill_plan_matches_signed_material(
    plan: FillPlan,
    *,
    role_id: str,
    materials: Iterable[MaterialBinding],
) -> bool:
    return (
        bool(plan.application_id)
        and plan.role_id == role_id
        and isinstance(plan.material_sha256, str)
        and any(
            plan.application_id == material.application_id
            and plan.role_id == material.role_id
            and plan.material_sha256 == material.artifact_sha256
            for material in materials
        )
    )




def fill_plan_digest(plan: FillPlan, resolutions: Iterable[AssertionResolution], snapshot: FormSnapshot) -> str | None:
    rows = tuple(resolutions)
    expected = expected_field_keys(snapshot)
    proofs = assertion_proof_digest(rows, snapshot)
    if (
        proofs is None or plan.snapshot_digest != snapshot_digest(snapshot)
        or len(plan.values) != len(expected)
        or tuple(value.field_key for value in plan.values) != expected
    ):
        return None
    for value, resolution in zip(plan.values, rows, strict=True):
        if (
            value.assertion_id != resolution.assertion_id
            or value.assertion_revision != resolution.assertion_revision
            or value.alias_id != resolution.alias_id
            or value.alias_revision != resolution.alias_revision
            or value.value != resolution.value
        ):
            return None
    return sha256_artifact(canonical_json({"application_id": plan.application_id,
        "snapshot_digest": plan.snapshot_digest, "material_sha256": plan.material_sha256,
        "role_id": plan.role_id, "proof_digest": proofs,
        "values": [{"field_key": value.field_key, "value": value.value,
                    "assertion_id": value.assertion_id, "assertion_revision": value.assertion_revision,
                    "alias_id": value.alias_id, "alias_revision": value.alias_revision} for value in plan.values]}))


def evaluate_batch_eligibility(*, role: RoleInput, policy: BatchPolicy,
                               trusted_clock: Callable[[], datetime], observed_binding: ProviderFormBinding,
                               snapshot: FormSnapshot, materials_current: bool,
                               assertion_snapshot_id: str, assertion_snapshot_revision: int,
                               resolutions: Iterable[AssertionResolution], fill_plan: FillPlan,
                               global_kill_switch_open: bool, provider_kill_switch_open: bool,
                               breaker_open: bool, started_or_reserved_today: int,
                               policy_signing_key: bytes,
                               assertion_registry: AssertionRegistry | None = None) -> EligibilityResult:
    if not isinstance(policy_signing_key, bytes) or not policy_signing_key or not verify_policy_signature(policy, policy_signing_key):
        return EligibilityResult(RoleEligibility.INELIGIBLE, PauseReason.POLICY_BINDING_MISMATCH)
    try:
        now = _trusted_now(trusted_clock)
    except ValueError:
        return EligibilityResult(RoleEligibility.INELIGIBLE, PauseReason.POLICY_EXPIRED)
    state_reason = policy_is_active(policy, now)
    if state_reason:
        return EligibilityResult(RoleEligibility.INELIGIBLE, state_reason)
    if global_kill_switch_open or provider_kill_switch_open:
        return EligibilityResult(RoleEligibility.INELIGIBLE, PauseReason.KILL_SWITCH)
    if breaker_open:
        return EligibilityResult(RoleEligibility.INELIGIBLE, PauseReason.BREAKER_OPEN)
    if (role.status != "materials_ready" or not materials_current or not math.isfinite(role.score)
            or started_or_reserved_today < 0 or role.score < policy.min_fit_score
            or not role_in_scope(role) or not role.posting_active or role.started_or_submitted):
        return EligibilityResult(RoleEligibility.INELIGIBLE, PauseReason.POLICY_BINDING_MISMATCH)
    if started_or_reserved_today >= policy.daily_cap:
        return EligibilityResult(RoleEligibility.INELIGIBLE, PauseReason.DAILY_CAP)
    if assertion_snapshot_id != policy.assertion_snapshot_id or assertion_snapshot_revision != policy.assertion_snapshot_revision:
        return EligibilityResult(RoleEligibility.INELIGIBLE, PauseReason.POLICY_BINDING_MISMATCH)
    if not unattended_binding_is_complete(observed_binding) or observed_binding not in policy.provider_forms:
        return EligibilityResult(RoleEligibility.PAUSED, PauseReason.FORM_DRIFT)
    if snapshot_digest(snapshot) != observed_binding.snapshot_digest:
        return EligibilityResult(RoleEligibility.PAUSED, PauseReason.FORM_DRIFT)
    resolution_rows = tuple(resolutions)
    proof_digest = assertion_proof_digest(resolution_rows, snapshot)
    if proof_digest is None or not _proofs_permitted(policy, resolution_rows, snapshot, assertion_registry):
        return EligibilityResult(RoleEligibility.PAUSED, PauseReason.NEW_QUESTION)
    if fill_plan_digest(fill_plan, resolution_rows, snapshot) is None:
        return EligibilityResult(RoleEligibility.PAUSED, PauseReason.NEW_QUESTION)
    if not _fill_plan_matches_signed_material(fill_plan, role_id=role.role_id, materials=policy.materials):
        return EligibilityResult(RoleEligibility.PAUSED, PauseReason.POLICY_BINDING_MISMATCH)
    return EligibilityResult(RoleEligibility.ELIGIBLE)


def dispatch_matches_policy(intent: DispatchIntent, policy: BatchPolicy, *, trusted_clock: Callable[[], datetime],
                            policy_signing_key: bytes, live_global_kill_switch_revision: int,
                            live_provider_kill_switch_revisions: Mapping[str, int],
                            live_breaker_generation: int, snapshot: FormSnapshot,
                            resolutions: Iterable[AssertionResolution], fill_plan: FillPlan,
                            assertion_registry: AssertionRegistry | None = None) -> bool:
    try:
        now = _trusted_now(trusted_clock)
    except ValueError:
        return False
    rows = tuple(resolutions)
    proof_digest = assertion_proof_digest(rows, snapshot)
    plan_digest = fill_plan_digest(fill_plan, rows, snapshot)
    if (
        not isinstance(policy_signing_key, bytes) or not policy_signing_key or not verify_policy_signature(policy, policy_signing_key)
        or policy_is_active(policy, now) is not None or not unattended_binding_is_complete(intent.binding)
        or snapshot_digest(snapshot) != intent.snapshot_digest
        or proof_digest is None or plan_digest is None
        or not _proofs_permitted(policy, rows, snapshot, assertion_registry)
        or intent.assertion_proof_digest != proof_digest or intent.fill_plan_digest != plan_digest
    ):
        return False
    provider_revision = live_provider_kill_switch_revisions.get(intent.binding.provider)
    return (
        intent.role_id == intent.material.role_id == fill_plan.role_id
        and bool(intent.application_id)
        and intent.application_id == intent.material.application_id == fill_plan.application_id
        and intent.material.artifact_sha256 == fill_plan.material_sha256
        and _fill_plan_matches_signed_material(fill_plan, role_id=intent.role_id, materials=policy.materials)
        and intent.policy_id == policy.policy_id
        and intent.policy_revision == policy.policy_revision and intent.assertion_snapshot_id == policy.assertion_snapshot_id
        and intent.assertion_snapshot_revision == policy.assertion_snapshot_revision
        and intent.binding in policy.provider_forms and intent.material in policy.materials
        and intent.snapshot_digest == intent.binding.snapshot_digest and len(intent.fill_plan_digest) == 64
        and len(intent.assertion_proof_digest) == 64
        and intent.global_kill_switch_revision == policy.global_kill_switch_revision == live_global_kill_switch_revision
        and provider_revision is not None and intent.provider_kill_switch_revision == provider_revision
        and policy.provider_kill_switch_revisions.get(intent.binding.provider) == provider_revision
        and intent.breaker_generation == policy.breaker_generation == live_breaker_generation
    )
