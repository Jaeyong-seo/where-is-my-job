from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from application_automation.assertions import (
    AVAILABILITY_KEY,
    DEMOGRAPHICS_KEY,
    SALARY_KEY,
    SPONSORSHIP_KEY,
    AssertionRegistry,
    PostedSalaryRange,
    assertion_alias_snapshot_digest,
)
from application_automation.crypto import CryptoError, canonical_json, decrypt_aes_gcm, domain_hmac, encrypt_aes_gcm, verify_domain_hmac
from application_automation.models import (
    BatchPolicy, CandidateAssertion, DispatchIntent, ExactAlias, FillPlan, FillValue, FixtureCapability, FormField,
    FormSnapshot, FormStep, MaterialBinding, PauseReason, ProviderFormBinding,
    RoleEligibility, RoleInput, Transport,
)
from application_automation.policy import (
    assertion_proof_digest, dispatch_matches_policy, evaluate_batch_eligibility, fill_plan_digest,
    policy_signature_claims, snapshot_digest,
)

NOW = datetime(2026, 7, 15, tzinfo=UTC)
POLICY_KEY = b"policy-test-key"
CAPABILITY = FixtureCapability("fixture", "fixture-aside", "https://fixtures.example", "capability-1", 1, "event-1", 1)
SNAPSHOT = FormSnapshot("greenhouse", "acme", "https://acme.example/*", "1.0", "a" * 64, (FormStep("main", (), "submit"),))
BINDING = ProviderFormBinding("greenhouse", "acme", "submit", Transport.ASIDE, CAPABILITY, snapshot_digest(SNAPSHOT))
MATERIAL = MaterialBinding("role-1", "applications/role-1", "b" * 64, "c" * 64, "application-1")


def policy(**changes: object) -> BatchPolicy:
    values: dict[str, object] = {
        "policy_id": "policy-1", "state": "active", "valid_from": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(hours=1), "min_fit_score": 5, "timezone": "America/Vancouver",
        "daily_cap": 20, "assertion_snapshot_id": "snapshot-1", "assertion_snapshot_revision": 1,
        "provider_forms": (BINDING,), "materials": (MATERIAL,), "permitted_assertion_keys": (),
        "global_kill_switch_revision": 1, "signature_hmac": "0" * 64, "policy_revision": 1,
        "provider_kill_switch_revisions": {"greenhouse": 1}, "breaker_generation": 1,
        "candidate_profile_id": "profile-1", "candidate_profile_revision": 1,
        "assertion_snapshot_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    }
    values.update(changes)
    unsigned = BatchPolicy(**values)  # type: ignore[arg-type]
    if "signature_hmac" in changes:
        return unsigned
    return replace(unsigned, signature_hmac=domain_hmac(POLICY_KEY, "batch_policy.v2", policy_signature_claims(unsigned)))


def eligible(**changes: object):
    current_policy = policy()
    plan = FillPlan("application-1", BINDING.snapshot_digest, (), MATERIAL.artifact_sha256, "role-1")
    values: dict[str, object] = {
        "role": RoleInput("role-1", "materials_ready", 5, "Vancouver, BC", False, None, True),
        "policy": current_policy, "trusted_clock": lambda: NOW, "observed_binding": BINDING,
        "snapshot": SNAPSHOT, "materials_current": True, "assertion_snapshot_id": "snapshot-1",
        "assertion_snapshot_revision": 1, "resolutions": (), "fill_plan": plan,
        "global_kill_switch_open": False, "provider_kill_switch_open": False, "breaker_open": False,
        "started_or_reserved_today": 0, "policy_signing_key": POLICY_KEY,
    }
    values.update(changes)
    return evaluate_batch_eligibility(**values)  # type: ignore[arg-type]


def assertion(key: str, value: object, *, revision: int = 1) -> CandidateAssertion:
    return CandidateAssertion(key, value, f"{key}-id", revision, "profile-1", 1, "snapshot-1", 1, "event-1", 1, NOW)


def alias(key: str, label: str) -> ExactAlias:
    return ExactAlias(key, label, "greenhouse", "form-1", f"{key}-alias", 1, "profile-1", 1, "snapshot-1", 1, "event-1", 1)


def test_eligible_baseline_is_explicit() -> None:
    result = eligible()
    assert (result.eligibility, result.pause_reason) == (RoleEligibility.ELIGIBLE, None)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"role": RoleInput("role-1", "materials_ready", 4.9, "Vancouver, BC", False, None, True)}, (RoleEligibility.INELIGIBLE, PauseReason.POLICY_BINDING_MISMATCH)),
        ({"role": RoleInput("role-1", "queued", 5, "Vancouver, BC", False, None, True)}, (RoleEligibility.INELIGIBLE, PauseReason.POLICY_BINDING_MISMATCH)),
        ({"policy": policy(expires_at=NOW)}, (RoleEligibility.INELIGIBLE, PauseReason.POLICY_EXPIRED)),
        ({"policy": policy(state="revoked")}, (RoleEligibility.INELIGIBLE, PauseReason.POLICY_REVOKED)),
        ({"started_or_reserved_today": 20}, (RoleEligibility.INELIGIBLE, PauseReason.DAILY_CAP)),
        ({"global_kill_switch_open": True}, (RoleEligibility.INELIGIBLE, PauseReason.KILL_SWITCH)),
        ({"provider_kill_switch_open": True}, (RoleEligibility.INELIGIBLE, PauseReason.KILL_SWITCH)),
        ({"breaker_open": True}, (RoleEligibility.INELIGIBLE, PauseReason.BREAKER_OPEN)),
        ({"observed_binding": replace(BINDING, snapshot_digest="d" * 64)}, (RoleEligibility.PAUSED, PauseReason.FORM_DRIFT)),
        ({"snapshot": replace(SNAPSHOT, tenant="other")}, (RoleEligibility.PAUSED, PauseReason.FORM_DRIFT)),
        ({"assertion_snapshot_revision": 2}, (RoleEligibility.INELIGIBLE, PauseReason.POLICY_BINDING_MISMATCH)),
    ],
)
def test_policy_failures_have_exact_fail_closed_oracles(changes: dict[str, object], expected: tuple[RoleEligibility, PauseReason]) -> None:
    result = eligible(**changes)
    assert (result.eligibility, result.pause_reason) == expected


@pytest.mark.parametrize(
    ("semantic", "reason"),
    [
        ("legal", PauseReason.LEGAL_QUESTION), ("work_permit", PauseReason.LEGAL_QUESTION),
        ("authorization", PauseReason.LEGAL_QUESTION), ("sensitive", PauseReason.SENSITIVE_QUESTION),
        ("attestation", PauseReason.ATTESTATION), ("street_address", PauseReason.STREET_ADDRESS),
    ],
)
def test_protected_classification_beats_confirmed_alias(semantic: str, reason: PauseReason) -> None:
    registry = AssertionRegistry((assertion(SPONSORSHIP_KEY, False),), (alias(SPONSORSHIP_KEY, "Sponsorship"),))
    result = registry.resolve(provider="greenhouse", form_fingerprint="form-1", field=FormField("x", "Sponsorship", "select", True, ("No, now or in the future",), semantic))
    assert (result.value, result.pause_reason) == (None, reason)


def test_alias_resolution_carries_authority_provenance_and_rejects_forgery() -> None:
    confirmed = assertion(SPONSORSHIP_KEY, False)
    exact = alias(SPONSORSHIP_KEY, "Sponsorship")
    resolved = AssertionRegistry((confirmed,), (exact,)).resolve(provider="greenhouse", form_fingerprint="form-1", field=FormField("x", "Sponsorship", "select", True, ("No, now or in the future",)), now=NOW)
    assert (
        resolved.value, resolved.assertion_id, resolved.alias_id, resolved.snapshot_revision,
        resolved.provider, resolved.form_fingerprint, resolved.field_key, resolved.normalized_label,
    ) == (
        "No, now or in the future", confirmed.assertion_id, exact.alias_id, 1,
        "greenhouse", "form-1", "x", "Sponsorship",
    )
    with pytest.raises(ValueError, match="provenance"):
        AssertionRegistry((confirmed,), (replace(exact, confirmation_event_revision=2),))

def test_resolution_proofs_reject_reordered_and_duplicated_field_authority() -> None:
    sponsorship = FormField("sponsorship", "Sponsorship", "select", True, ("No, now or in the future",))
    availability = FormField("availability", "Notice period", "text", True)
    field_snapshot = FormSnapshot(
        "greenhouse", "acme", "https://acme.example/*", "1.0", "a" * 64,
        (FormStep("details", (sponsorship, availability), "submit"),),
    )
    binding = replace(BINDING, snapshot_digest=snapshot_digest(field_snapshot))
    registry = AssertionRegistry(
        (assertion(SPONSORSHIP_KEY, False), assertion(AVAILABILITY_KEY, 14)),
        (alias(SPONSORSHIP_KEY, "Sponsorship"), alias(AVAILABILITY_KEY, "Notice period")),
    )
    sponsorship_resolution = registry.resolve(
        provider="greenhouse", form_fingerprint="form-1", field=sponsorship, now=NOW,
    )
    availability_resolution = registry.resolve(
        provider="greenhouse", form_fingerprint="form-1", field=availability, now=NOW,
    )
    signed = policy(
        provider_forms=(binding,),
        permitted_assertion_keys=(SPONSORSHIP_KEY, AVAILABILITY_KEY),
    )

    for rows in (
        (availability_resolution, sponsorship_resolution),
        (sponsorship_resolution, sponsorship_resolution),
    ):
        replay_plan = FillPlan(
            "application-1",
            binding.snapshot_digest,
            tuple(
                FillValue(field.field_key, row.value or "", row.assertion_id or "", row.assertion_revision or 0,
                          row.alias_id or "", row.alias_revision or 0)
                for field, row in zip((sponsorship, availability), rows, strict=True)
            ),
            MATERIAL.artifact_sha256,
            "role-1",
        )
        assert assertion_proof_digest(rows, field_snapshot) is None
        result = eligible(
            policy=signed,
            observed_binding=binding,
            snapshot=field_snapshot,
            resolutions=rows,
            fill_plan=replay_plan,
        )
        assert (result.eligibility, result.pause_reason) == (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION)


def test_eligibility_rejects_fill_plan_identity_outside_signed_material() -> None:
    for plan_override in (
        {"application_id": "application-2"},
        {"role_id": "role-2"},
        {"material_sha256": "d" * 64},
    ):
        result = eligible(fill_plan=replace(FillPlan(
            "application-1", BINDING.snapshot_digest, (), MATERIAL.artifact_sha256, "role-1",
        ), **plan_override))
        assert (result.eligibility, result.pause_reason) == (
            RoleEligibility.PAUSED, PauseReason.POLICY_BINDING_MISMATCH,
        )


def test_naive_and_future_assertions_fail_closed() -> None:
    exact = alias(SPONSORSHIP_KEY, "Sponsorship")
    with pytest.raises(ValueError, match="timezone-aware"):
        CandidateAssertion(SPONSORSHIP_KEY, False, "id", 1, "profile-1", 1, "snapshot-1", 1, "event-1", 1, datetime(2026, 7, 15))
    future = replace(assertion(SPONSORSHIP_KEY, False), confirmed_at=NOW + timedelta(seconds=1))
    result = AssertionRegistry((future,), (exact,)).resolve(provider="greenhouse", form_fingerprint="form-1", field=FormField("x", "Sponsorship", "text", True), now=NOW)
    assert result.pause_reason is PauseReason.UNKNOWN_QUESTION


def test_narrow_candidate_values_remain_bound() -> None:
    registry = AssertionRegistry((assertion(DEMOGRAPHICS_KEY, "prefer_not_to_answer"), assertion(AVAILABILITY_KEY, 14), assertion(SALARY_KEY, "negotiable_within_posted_range")), (alias(DEMOGRAPHICS_KEY, "Gender"), alias(AVAILABILITY_KEY, "Notice period"), alias(SALARY_KEY, "Salary expectations")))
    assert registry.resolve(provider="greenhouse", form_fingerprint="form-1", field=FormField("g", "Gender", "select", False, ("Prefer not to answer",), "demographic"), now=NOW).value == "Prefer not to answer"
    assert registry.resolve(provider="greenhouse", form_fingerprint="form-1", field=FormField("s", "Salary expectations", "text", False), posted_salary_range=PostedSalaryRange(100, 200, "CAD"), now=NOW).value == "Negotiable within posted range"
    assert registry.resolve(provider="greenhouse", form_fingerprint="form-1", field=FormField("n", "Notice period", "text", True), now=NOW).value == "14 days"
def test_trusted_candidate_snapshot_is_required_for_resolved_proofs() -> None:
    field = FormField("sponsorship", "Sponsorship", "select", True, ("No, now or in the future",))
    field_snapshot = replace(
        SNAPSHOT,
        ordered_steps=(FormStep("details", (field,), "submit"),),
    )
    binding = replace(BINDING, snapshot_digest=snapshot_digest(field_snapshot))
    registry = AssertionRegistry(
        (assertion(SPONSORSHIP_KEY, False),),
        (alias(SPONSORSHIP_KEY, "Sponsorship"),),
    )
    resolution = registry.resolve(
        provider="greenhouse", form_fingerprint="form-1", field=field, now=NOW,
    )
    fill_plan = FillPlan(
        "application-1",
        binding.snapshot_digest,
        (FillValue(
            field.field_key, resolution.value or "", resolution.assertion_id or "",
            resolution.assertion_revision or 0, resolution.alias_id or "", resolution.alias_revision or 0,
        ),),
        MATERIAL.artifact_sha256,
        "role-1",
    )
    signed = policy(
        provider_forms=(binding,),
        permitted_assertion_keys=(SPONSORSHIP_KEY,),
        candidate_profile_id="profile-1",
        candidate_profile_revision=1,
        assertion_snapshot_digest=registry.assertion_snapshot_digest,
    )
    assert eligible(
        policy=signed,
        observed_binding=binding,
        snapshot=field_snapshot,
        resolutions=(resolution,),
        fill_plan=fill_plan,
        assertion_registry=registry,
    ).eligibility is RoleEligibility.ELIGIBLE
    proof_digest = assertion_proof_digest((resolution,), field_snapshot)
    plan_digest = fill_plan_digest(fill_plan, (resolution,), field_snapshot)
    assert proof_digest is not None and plan_digest is not None
    intent = DispatchIntent(
        "dispatch-1", "application-1", signed.policy_id, signed.policy_revision, binding,
        signed.assertion_snapshot_id, signed.assertion_snapshot_revision, MATERIAL,
        binding.snapshot_digest, plan_digest, proof_digest, 1, 1, 1, "role-1",
    )
    controls = {
        "trusted_clock": lambda: NOW,
        "policy_signing_key": POLICY_KEY,
        "live_global_kill_switch_revision": 1,
        "live_provider_kill_switch_revisions": {"greenhouse": 1},
        "live_breaker_generation": 1,
        "snapshot": field_snapshot,
        "resolutions": (resolution,),
        "fill_plan": fill_plan,
        "assertion_registry": registry,
    }
    assert dispatch_matches_policy(intent, signed, **controls)
    assert not dispatch_matches_policy(
        intent,
        signed,
        **{**controls, "resolutions": (replace(resolution, value="forged"),)},
    )

    for registry_override, policy_override, resolution_override, expected in (
        (None, {}, None, (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION)),
        (
            registry,
            {"candidate_profile_id": "profile-2"},
            None,
            (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION),
        ),
        (
            registry,
            {"candidate_profile_revision": 2},
            None,
            (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION),
        ),
        (
            registry,
            {"assertion_snapshot_revision": 2},
            None,
            (RoleEligibility.INELIGIBLE, PauseReason.POLICY_BINDING_MISMATCH),
        ),
        (
            registry,
            {"assertion_snapshot_digest": "d" * 64},
            None,
            (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION),
        ),
        (registry, {}, replace(resolution, value="forged"), (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION)),
        (registry, {}, replace(resolution, alias_revision=2), (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION)),
    ):
        replay_policy = policy(
            provider_forms=(binding,),
            permitted_assertion_keys=(SPONSORSHIP_KEY,),
            **{
                "candidate_profile_id": "profile-1",
                "candidate_profile_revision": 1,
                "assertion_snapshot_digest": registry.assertion_snapshot_digest,
                **policy_override,
            },
        )
        replay_resolution = resolution if resolution_override is None else resolution_override
        result = eligible(
            policy=replay_policy,
            observed_binding=binding,
            snapshot=field_snapshot,
            resolutions=(replay_resolution,),
            fill_plan=fill_plan,
            assertion_registry=registry_override,
        )
        assert (result.eligibility, result.pause_reason) == expected


def test_assertion_registry_rejects_mixed_profiles_and_load_bearing_empty_identity() -> None:
    with pytest.raises(ValueError, match="one profile"):
        AssertionRegistry(
            (assertion(SPONSORSHIP_KEY, False), replace(assertion(AVAILABILITY_KEY, 14), profile_id="profile-2")),
            (alias(SPONSORSHIP_KEY, "Sponsorship"), alias(AVAILABILITY_KEY, "Notice period")),
        )
    with pytest.raises(ValueError, match="role_id"):
        RoleInput("", "materials_ready", 5, "Vancouver, BC", False, None, True)
    with pytest.raises(ValueError, match="application_id"):
        FillPlan("", BINDING.snapshot_digest, (), MATERIAL.artifact_sha256, "role-1")
    with pytest.raises(ValueError, match="material_sha256"):
        FillPlan("application-1", BINDING.snapshot_digest, (), "", "role-1")


def test_canonical_crypto_rejects_authenticated_tampering_and_domain_confusion() -> None:
    assert canonical_json({"b": [True], "a": "é"}) == b'{"a":"\xc3\xa9","b":[true]}'
    signature = domain_hmac(b"k", "policy", {"a": 1})
    assert verify_domain_hmac(b"k", "policy", {"a": 1}, signature)
    assert not verify_domain_hmac(b"q", "policy", {"a": 1}, signature)
    assert not verify_domain_hmac(b"k", "policy", {"a": 2}, signature)
    assert not verify_domain_hmac(b"k", "other-policy", {"a": 1}, signature)
    assert not verify_domain_hmac(b"k", "policy", {"a": 1}, signature.encode())

    key = b"x" * 32
    envelope = encrypt_aes_gcm(key, b"secret-value", domain="assertion", aad={"id": "1"})
    serialized = envelope.to_dict()
    assert "secret-value" not in repr(serialized)
    assert decrypt_aes_gcm(key, serialized, domain="assertion", aad={"id": "1"}) == b"secret-value"

    ciphertext = serialized["ciphertext"]
    tag = serialized["tag"]
    assert isinstance(ciphertext, str) and isinstance(tag, str)
    tampered_ciphertext = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    tampered_tag = ("A" if tag[0] != "A" else "B") + tag[1:]
    for field, value in (("ciphertext", tampered_ciphertext), ("tag", tampered_tag)):
        with pytest.raises(CryptoError, match="authentication failed"):
            decrypt_aes_gcm(key, {**serialized, field: value}, domain="assertion", aad={"id": "1"})
    for wrong_key, domain, aad in (
        (b"y" * 32, "assertion", {"id": "1"}),
        (key, "other-assertion", {"id": "1"}),
        (key, "assertion", {"id": "2"}),
    ):
        with pytest.raises(CryptoError, match="authentication failed"):
            decrypt_aes_gcm(wrong_key, serialized, domain=domain, aad=aad)


PINNED_ASSERTION = CandidateAssertion(
    SPONSORSHIP_KEY, False, "assertion-pin", 1, "profile-pin", 1, "snapshot-pin", 1, "event-pin", 1, NOW,
)
PINNED_ALIAS = ExactAlias(
    SPONSORSHIP_KEY, "Sponsorship", "greenhouse", "form-pin", "alias-pin", 1, "profile-pin", 1, "snapshot-pin", 1, "event-pin", 1,
)
PINNED_REGISTRY_DIGEST = "969de48bdd95ce3d3a4a1500816098b49806a8d4bcecedcbef769a26ee0671cc"


def test_assertion_alias_snapshot_digest_matches_independent_fixture_vector() -> None:
    # Audited SHA-256 vector for the fixed (PINNED_ASSERTION, PINNED_ALIAS) snapshot.
    assert len(PINNED_REGISTRY_DIGEST) == 64
    assert assertion_alias_snapshot_digest((PINNED_ASSERTION,), (PINNED_ALIAS,)) == PINNED_REGISTRY_DIGEST


def _field_snapshot() -> tuple[FormField, FormSnapshot, ProviderFormBinding]:
    field = FormField("sponsorship", "Sponsorship", "select", True, ("No, now or in the future",))
    field_snapshot = FormSnapshot(
        "greenhouse", "acme", "https://acme.example/*", "1.0", "a" * 64, (FormStep("details", (field,), "submit"),),
    )
    binding = replace(BINDING, snapshot_digest=snapshot_digest(field_snapshot))
    return field, field_snapshot, binding


@pytest.mark.parametrize(
    ("assertion_changes", "alias_changes"),
    [
        ({"value": True}, {}),
        ({"assertion_id": "assertion-2"}, {}),
        ({"assertion_revision": 2}, {}),
        ({"confirmed_at": NOW - timedelta(days=1)}, {}),
        ({"form_fingerprint": "form-1"}, {}),
        ({"revoked_at": NOW}, {}),
        ({"key": "other.key"}, {"semantic_key": "other.key"}),
        ({"profile_id": "profile-2"}, {"profile_id": "profile-2"}),
        ({"profile_revision": 2}, {"profile_revision": 2}),
        ({"snapshot_id": "snapshot-2"}, {"snapshot_id": "snapshot-2"}),
        ({"snapshot_revision": 2}, {"snapshot_revision": 2}),
        ({"confirmation_event_id": "event-2"}, {"confirmation_event_id": "event-2"}),
        ({"confirmation_event_revision": 2}, {"confirmation_event_revision": 2}),
        ({}, {"alias": "Other Label"}),
        ({}, {"provider": "lever"}),
        ({}, {"form_fingerprint": "form-2"}),
        ({}, {"alias_id": "alias-2"}),
        ({}, {"alias_revision": 2}),
    ],
)
def test_each_assertion_or_alias_member_mutation_keeps_original_policy_and_fails_closed(
    assertion_changes: dict[str, object], alias_changes: dict[str, object],
) -> None:
    """Mutating any single CandidateAssertion/ExactAlias member changes the registry digest;
    against the ORIGINAL signed policy (unchanged), eligibility and dispatch must both reject."""
    field, field_snapshot, binding = _field_snapshot()
    base_assertion = assertion(SPONSORSHIP_KEY, False)
    base_alias = alias(SPONSORSHIP_KEY, "Sponsorship")
    baseline_registry = AssertionRegistry((base_assertion,), (base_alias,))
    baseline_resolution = baseline_registry.resolve(provider="greenhouse", form_fingerprint="form-1", field=field, now=NOW)
    baseline_plan = FillPlan(
        "application-1", binding.snapshot_digest,
        (FillValue(field.field_key, baseline_resolution.value, baseline_resolution.assertion_id,
                   baseline_resolution.assertion_revision, baseline_resolution.alias_id, baseline_resolution.alias_revision),),
        MATERIAL.artifact_sha256, "role-1",
    )
    signed = policy(
        provider_forms=(binding,), permitted_assertion_keys=(SPONSORSHIP_KEY,),
        candidate_profile_id="profile-1", candidate_profile_revision=1,
        assertion_snapshot_digest=baseline_registry.assertion_snapshot_digest,
    )
    assert eligible(
        policy=signed, observed_binding=binding, snapshot=field_snapshot,
        resolutions=(baseline_resolution,), fill_plan=baseline_plan, assertion_registry=baseline_registry,
    ).eligibility is RoleEligibility.ELIGIBLE

    mutated_assertion = replace(base_assertion, **assertion_changes)
    mutated_alias = replace(base_alias, **alias_changes)
    mutated_registry = AssertionRegistry((mutated_assertion,), (mutated_alias,))
    assert mutated_registry.assertion_snapshot_digest != baseline_registry.assertion_snapshot_digest

    resolve_field = field if "alias" not in alias_changes else FormField(
        "sponsorship", alias_changes["alias"], "select", True, ("No, now or in the future",),
    )
    resolve_provider = alias_changes.get("provider", "greenhouse")
    resolve_ff = alias_changes.get("form_fingerprint", "form-1")
    mutated_resolution = mutated_registry.resolve(
        provider=resolve_provider, form_fingerprint=resolve_ff, field=resolve_field, now=NOW,
    )
    mutated_plan = FillPlan("application-1", binding.snapshot_digest, (), MATERIAL.artifact_sha256, "role-1")

    result = eligible(
        policy=signed, observed_binding=binding, snapshot=field_snapshot,
        resolutions=(mutated_resolution,), fill_plan=mutated_plan, assertion_registry=mutated_registry,
    )
    assert (result.eligibility, result.pause_reason) == (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION)

    proof_digest = assertion_proof_digest((mutated_resolution,), field_snapshot)
    plan_digest = fill_plan_digest(mutated_plan, (mutated_resolution,), field_snapshot)
    intent = DispatchIntent(
        "dispatch-1", "application-1", signed.policy_id, signed.policy_revision, binding,
        signed.assertion_snapshot_id, signed.assertion_snapshot_revision, MATERIAL,
        binding.snapshot_digest, plan_digest or "a" * 64, proof_digest or "b" * 64, 1, 1, 1, "role-1",
    )
    controls = {
        "trusted_clock": lambda: NOW,
        "policy_signing_key": POLICY_KEY,
        "live_global_kill_switch_revision": 1,
        "live_provider_kill_switch_revisions": {"greenhouse": 1},
        "live_breaker_generation": 1,
        "snapshot": field_snapshot,
        "resolutions": (mutated_resolution,),
        "fill_plan": mutated_plan,
        "assertion_registry": mutated_registry,
    }
    assert not dispatch_matches_policy(intent, signed, **controls)
