from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from application_automation.assertions import AssertionRegistry, SPONSORSHIP_KEY
from application_automation.crypto import domain_hmac
from application_automation.models import (
    BatchPolicy, CandidateAssertion, DispatchIntent, ExactAlias, FillPlan, FillValue,
    FixtureCapability, FormField, FormSnapshot, FormStep, MaterialBinding, PauseReason,
    ProviderFormBinding, RoleEligibility, RoleInput, Transport,
)
from application_automation.policy import (
    dispatch_matches_policy, evaluate_batch_eligibility, fill_plan_digest, policy_signature_claims,
    snapshot_digest, verify_policy_signature,
)

NOW = datetime(2026, 7, 15, tzinfo=UTC)
SIGNING_KEY = b"policy-signing-key"
CAPABILITY = FixtureCapability("fixture", "fixture-aside", "https://fixtures.example", "capability-1", 1, "event-1", 1)
SNAPSHOT = FormSnapshot("greenhouse", "acme", "https://acme.example/*", "1.0", "a" * 64, (FormStep("main", (), "submit"),))
BINDING = ProviderFormBinding("greenhouse", "acme", "submit", Transport.ASIDE, CAPABILITY, snapshot_digest(SNAPSHOT))
MATERIAL = MaterialBinding("role-1", "applications/role-1", "b" * 64, "c" * 64, "application-1")
EMPTY_ASSERTION_PROOF_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
EMPTY_FILL_PLAN_DIGEST = "9aa222d3bee423373c8db939a634461a24c6e285fee9eff23217f55976eef511"


def policy(**changes: object) -> BatchPolicy:
    values: dict[str, object] = {
        "policy_id": "policy-1", "state": "active", "valid_from": NOW - timedelta(minutes=1), "expires_at": NOW + timedelta(hours=1),
        "min_fit_score": 5, "timezone": "America/Vancouver", "daily_cap": 20,
        "assertion_snapshot_id": "snapshot-1", "assertion_snapshot_revision": 1,
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
    return replace(unsigned, signature_hmac=domain_hmac(SIGNING_KEY, "batch_policy.v2", policy_signature_claims(unsigned)))
def forge_timezone_claim(signed_policy: BatchPolicy) -> BatchPolicy:
    object.__setattr__(signed_policy, "timezone", "UTC")
    return signed_policy




def plan() -> FillPlan:
    return FillPlan("application-1", BINDING.snapshot_digest, (), MATERIAL.artifact_sha256, "role-1")


def eligible(**changes: object):
    signed = policy()
    values: dict[str, object] = {
        "role": RoleInput("role-1", "materials_ready", 5, "Vancouver, BC", False, None, True), "policy": signed,
        "trusted_clock": lambda: NOW, "observed_binding": BINDING, "snapshot": SNAPSHOT, "materials_current": True,
        "assertion_snapshot_id": "snapshot-1", "assertion_snapshot_revision": 1, "resolutions": (), "fill_plan": plan(),
        "global_kill_switch_open": False, "provider_kill_switch_open": False, "breaker_open": False,
        "started_or_reserved_today": 0, "policy_signing_key": SIGNING_KEY,
    }
    values.update(changes)
    return evaluate_batch_eligibility(**values)  # type: ignore[arg-type]


def intent() -> DispatchIntent:
    return DispatchIntent("dispatch-1", "application-1", "policy-1", 1, BINDING, "snapshot-1", 1, MATERIAL, BINDING.snapshot_digest, EMPTY_FILL_PLAN_DIGEST, EMPTY_ASSERTION_PROOF_DIGEST, 1, 1, 1, "role-1")


def dispatch_controls() -> dict[str, object]:
    return {"trusted_clock": lambda: NOW, "policy_signing_key": SIGNING_KEY, "live_global_kill_switch_revision": 1, "live_provider_kill_switch_revisions": {"greenhouse": 1}, "live_breaker_generation": 1, "snapshot": SNAPSHOT, "resolutions": (), "fill_plan": plan()}


def test_eligible_baseline_and_trusted_clock_controls() -> None:
    assert (eligible().eligibility, eligible().pause_reason) == (RoleEligibility.ELIGIBLE, None)
    stale = eligible(trusted_clock=lambda: NOW + timedelta(hours=2))
    assert (stale.eligibility, stale.pause_reason) == (RoleEligibility.INELIGIBLE, PauseReason.POLICY_EXPIRED)
    naive = eligible(trusted_clock=lambda: datetime(2026, 7, 15))
    assert (naive.eligibility, naive.pause_reason) == (RoleEligibility.INELIGIBLE, PauseReason.POLICY_EXPIRED)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"policy": policy(signature_hmac="0" * 64)}, PauseReason.POLICY_BINDING_MISMATCH),
        ({"policy_signing_key": b"wrong-policy-signing-key"}, PauseReason.POLICY_BINDING_MISMATCH),
        ({"materials_current": False}, PauseReason.POLICY_BINDING_MISMATCH),
        ({"assertion_snapshot_id": "snapshot-2"}, PauseReason.POLICY_BINDING_MISMATCH),
        ({"policy": policy(valid_from=NOW + timedelta(minutes=1), expires_at=NOW + timedelta(hours=1))}, PauseReason.POLICY_EXPIRED),
        ({"policy": policy(expires_at=NOW)}, PauseReason.POLICY_EXPIRED),
        ({"policy": policy(state="revoked")}, PauseReason.POLICY_REVOKED),
    ],
)
def test_eligibility_gate_rejects_untrusted_stale_and_inactive_authority(
    changes: dict[str, object], expected: PauseReason,
) -> None:
    result = eligible(**changes)
    assert (result.eligibility, result.pause_reason) == (RoleEligibility.INELIGIBLE, expected)


@pytest.mark.parametrize(("field", "value"), [
    ("provider", "lever"), ("tenant", "other"), ("operation", "review"), ("transport", Transport.DIRECT),
    ("fixture_capability", replace(CAPABILITY, adapter_id="other")), ("snapshot_digest", "d" * 64),
])
def test_each_provider_binding_member_is_observed(field: str, value: object) -> None:
    result = eligible(observed_binding=replace(BINDING, **{field: value}))
    assert (result.eligibility, result.pause_reason) == (RoleEligibility.PAUSED, PauseReason.FORM_DRIFT)
@pytest.mark.parametrize("changed_snapshot", [
    replace(SNAPSHOT, provider="lever"), replace(SNAPSHOT, tenant="other"),
    replace(SNAPSHOT, canonical_url_pattern="https://other.example/*"),
    replace(SNAPSHOT, aside_version="2.0"), replace(SNAPSHOT, script_sha256="e" * 64),
    replace(SNAPSHOT, ordered_steps=(FormStep("review", (), "submit"),)),
    replace(SNAPSHOT, redirect_chain=("https://redirect.example",)),
    replace(SNAPSHOT, frame_origins=("https://frame.example",)),
])
def test_every_canonical_snapshot_member_is_bound(changed_snapshot: FormSnapshot) -> None:
    assert snapshot_digest(changed_snapshot) != BINDING.snapshot_digest
    result = eligible(snapshot=changed_snapshot)
    assert (result.eligibility, result.pause_reason) == (RoleEligibility.PAUSED, PauseReason.FORM_DRIFT)



@pytest.mark.parametrize(("field", "value"), [
    ("environment", "production"), ("adapter_id", ""), ("origin", ""), ("capability_id", ""),
    ("capability_revision", 0), ("confirmation_event_id", ""), ("confirmation_event_revision", 0),
])
def test_fixture_capability_rejects_real_empty_and_partial_authority(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        FixtureCapability(**{**CAPABILITY.__dict__, field: value})


def test_absent_empty_malformed_and_partial_fixture_authority_rejects() -> None:
    for absent in ("environment", "adapter_id", "origin", "capability_id", "capability_revision", "confirmation_event_id", "confirmation_event_revision"):
        values = dict(CAPABILITY.__dict__)
        values.pop(absent)
        with pytest.raises(TypeError, match=absent):
            FixtureCapability(**values)
    for malformed in ({"adapter_id": None}, {"capability_revision": True}, {"confirmation_event_revision": "1"}):
        with pytest.raises(ValueError):
            FixtureCapability(**{**CAPABILITY.__dict__, **malformed})


@pytest.mark.parametrize(("changes", "message"), [
    ({"min_fit_score": 4.9}, "between 5 and 10"), ({"min_fit_score": 10.1}, "between 5 and 10"),
    ({"daily_cap": 0}, "bounded cap"), ({"daily_cap": 21}, "bounded cap"),
    ({"valid_from": NOW, "expires_at": NOW}, "validity must be positive"),
    ({"expires_at": NOW + timedelta(hours=24, seconds=1)}, "validity must be positive"),
])
def test_policy_numeric_and_validity_boundaries(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        policy(**changes)
    assert policy(min_fit_score=5, daily_cap=1, valid_from=NOW, expires_at=NOW + timedelta(hours=24)).daily_cap == 1
    assert policy(min_fit_score=10).min_fit_score == 10


@pytest.mark.parametrize("field", [
    "policy_id", "state", "valid_from", "expires_at", "min_fit_score", "timezone", "daily_cap",
    "assertion_snapshot_id", "assertion_snapshot_revision", "provider_forms", "materials",
    "permitted_assertion_keys", "global_kill_switch_revision", "signature_hmac", "policy_revision",
])
def test_each_mandatory_policy_constructor_field_has_precise_diagnostic(field: str) -> None:
    values = dict(policy().__dict__)
    values.pop(field)
    with pytest.raises(TypeError, match=field):
        BatchPolicy(**values)


@pytest.mark.parametrize("tamper", [
    lambda p: replace(p, policy_id="policy-2"), lambda p: replace(p, policy_revision=2),
    lambda p: replace(p, state="revoked"), lambda p: replace(p, valid_from=NOW - timedelta(minutes=2)),
    lambda p: replace(p, expires_at=NOW + timedelta(hours=2)), lambda p: replace(p, min_fit_score=6),
    forge_timezone_claim,
    lambda p: replace(p, assertion_snapshot_id="snapshot-2"), lambda p: replace(p, assertion_snapshot_revision=2),
    lambda p: replace(p, provider_forms=(replace(BINDING, provider="lever"),)),
    lambda p: replace(p, provider_forms=(replace(BINDING, tenant="other"),)),
    lambda p: replace(p, provider_forms=(replace(BINDING, operation="review"),)),
    lambda p: replace(p, provider_forms=(replace(BINDING, transport=Transport.DIRECT),)),
    lambda p: replace(p, provider_forms=(replace(BINDING, snapshot_digest="d" * 64),)),
    lambda p: replace(p, materials=(replace(MATERIAL, role_id="role-2"),)),
    lambda p: replace(p, materials=(replace(MATERIAL, application_dir="other"),)),
    lambda p: replace(p, materials=(replace(MATERIAL, artifact_sha256="e" * 64),)),
    lambda p: replace(p, materials=(replace(MATERIAL, source_template_sha256="f" * 64),)),
    lambda p: replace(p, materials=(replace(MATERIAL, application_id="application-2"),)),
    lambda p: replace(p, permitted_assertion_keys=(SPONSORSHIP_KEY,)),
    lambda p: replace(p, global_kill_switch_revision=2), lambda p: replace(p, provider_kill_switch_revisions={"greenhouse": 2}),
    lambda p: replace(p, breaker_generation=2),
])
def test_signed_policy_rejects_each_policy_and_material_claim(tamper: object) -> None:
    signed = policy()
    assert verify_policy_signature(signed, SIGNING_KEY)
    assert not verify_policy_signature(tamper(signed), SIGNING_KEY)  # type: ignore[operator]
def test_signed_daily_cap_tampering_rejects_signature_eligibility_and_dispatch() -> None:
    tampered = replace(policy(), daily_cap=19)

    assert not verify_policy_signature(tampered, SIGNING_KEY)
    eligibility = eligible(policy=tampered)
    assert (eligibility.eligibility, eligibility.pause_reason) == (
        RoleEligibility.INELIGIBLE,
        PauseReason.POLICY_BINDING_MISMATCH,
    )
    assert not dispatch_matches_policy(intent(), tampered, **dispatch_controls())


@pytest.mark.parametrize("tamper", [
    lambda p: replace(p, candidate_profile_id="other-profile"),
    lambda p: replace(p, candidate_profile_revision=2),
    lambda p: replace(p, assertion_snapshot_digest="d" * 64),
])
def test_signed_candidate_triad_tampering_rejects_signature_eligibility_and_dispatch(tamper: object) -> None:
    """The candidate_profile_id/candidate_profile_revision/assertion_snapshot_digest triad is signed;
    mutating any member without re-signing must fail signature, eligibility, and dispatch checks."""
    signed = policy()
    tampered = tamper(signed)  # type: ignore[operator]
    assert verify_policy_signature(signed, SIGNING_KEY)
    assert not verify_policy_signature(tampered, SIGNING_KEY)
    eligibility = eligible(policy=tampered)
    assert (eligibility.eligibility, eligibility.pause_reason) == (
        RoleEligibility.INELIGIBLE,
        PauseReason.POLICY_BINDING_MISMATCH,
    )
    assert not dispatch_matches_policy(intent(), tampered, **dispatch_controls())


@pytest.mark.parametrize("capability", [
    replace(CAPABILITY, adapter_id="other"), replace(CAPABILITY, origin="https://other.example"),
    replace(CAPABILITY, capability_id="capability-2"), replace(CAPABILITY, capability_revision=2),
    replace(CAPABILITY, confirmation_event_id="event-2"), replace(CAPABILITY, confirmation_event_revision=2),
])
def test_signed_policy_binds_every_fixture_capability_member(capability: FixtureCapability) -> None:
    assert not verify_policy_signature(replace(policy(), provider_forms=(replace(BINDING, fixture_capability=capability),)), SIGNING_KEY)


def test_unpermitted_or_partial_resolution_proofs_pause() -> None:
    field = FormField("answer", "Sponsorship", "select", True, ("No, now or in the future",))
    field_snapshot = FormSnapshot("greenhouse", "acme", "https://acme.example/*", "1.0", "a" * 64, (FormStep("details", (field,), "submit"),))
    binding = replace(BINDING, snapshot_digest=snapshot_digest(field_snapshot))
    assertion = CandidateAssertion(SPONSORSHIP_KEY, False, "assertion-1", 1, "profile-1", 1, "snapshot-1", 1, "event-1", 1, NOW)
    alias = ExactAlias(SPONSORSHIP_KEY, "Sponsorship", "greenhouse", "form-1", "alias-1", 1, "profile-1", 1, "snapshot-1", 1, "event-1", 1)
    resolution = AssertionRegistry((assertion,), (alias,)).resolve(provider="greenhouse", form_fingerprint="form-1", field=field, now=NOW)
    proof_plan = FillPlan("application-1", binding.snapshot_digest, (FillValue("answer", resolution.value or "", "assertion-1", 1, "alias-1", 1),), MATERIAL.artifact_sha256, "role-1")
    signed = policy(provider_forms=(binding,), permitted_assertion_keys=())
    result = eligible(policy=signed, observed_binding=binding, snapshot=field_snapshot, resolutions=(resolution,), fill_plan=proof_plan)
    assert (result.eligibility, result.pause_reason) == (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION)
    partial = eligible(policy=signed, observed_binding=binding, snapshot=field_snapshot, resolutions=(), fill_plan=FillPlan("application-1", binding.snapshot_digest, (), MATERIAL.artifact_sha256, "role-1"))
    assert (partial.eligibility, partial.pause_reason) == (RoleEligibility.PAUSED, PauseReason.NEW_QUESTION)


def test_dispatch_uses_audited_empty_proof_and_fill_plan_digest_vectors() -> None:
    # Audited SHA-256 vectors for [] and the empty plan bound to SNAPSHOT and MATERIAL.
    assert EMPTY_ASSERTION_PROOF_DIGEST == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    assert EMPTY_FILL_PLAN_DIGEST == "9aa222d3bee423373c8db939a634461a24c6e285fee9eff23217f55976eef511"
    assert dispatch_matches_policy(intent(), policy(), **dispatch_controls())
    assert not dispatch_matches_policy(
        replace(intent(), fill_plan_digest="a" * 64, assertion_proof_digest="b" * 64),
        policy(),
        **dispatch_controls(),
    )

@pytest.mark.parametrize("plan_override", [
    {"application_id": "application-2"},
    {"role_id": "role-2"},
    {"material_sha256": "d" * 64},
])
def test_dispatch_rejects_fill_plan_cross_identity_replay(plan_override: dict[str, object]) -> None:
    replay_plan = replace(plan(), **plan_override)
    replay_digest = fill_plan_digest(replay_plan, (), SNAPSHOT)
    assert replay_digest is not None
    replay_intent = replace(intent(), fill_plan_digest=replay_digest)
    assert not dispatch_matches_policy(
        replay_intent,
        policy(),
        **{**dispatch_controls(), "fill_plan": replay_plan},
    )


@pytest.mark.parametrize(("field", "value"), [
    ("policy_id", "policy-2"), ("policy_revision", 2), ("assertion_snapshot_id", "snapshot-2"),
    ("assertion_snapshot_revision", 2), ("binding", replace(BINDING, tenant="other")),
    ("material", replace(MATERIAL, source_template_sha256="f" * 64)), ("snapshot_digest", "d" * 64),
    ("fill_plan_digest", "a" * 64), ("assertion_proof_digest", "b" * 64),
    ("global_kill_switch_revision", 2), ("provider_kill_switch_revision", 2), ("breaker_generation", 2),
    ("role_id", "role-2"), ("application_id", "application-2"),
])
def test_dispatch_rejects_each_authority_identity_and_revision_member(field: str, value: object) -> None:
    assert not dispatch_matches_policy(replace(intent(), **{field: value}), policy(), **dispatch_controls())


@pytest.mark.parametrize(("control", "value"), [
    ("live_global_kill_switch_revision", 2), ("live_provider_kill_switch_revisions", {"greenhouse": 2}), ("live_breaker_generation", 2),
])
def test_dispatch_rejects_each_stale_live_safety_revision(control: str, value: object) -> None:
    controls = dispatch_controls()
    controls[control] = value
    assert not dispatch_matches_policy(intent(), policy(), **controls)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("signed_policy", "controls"),
    [
        (policy(signature_hmac="0" * 64), {}),
        (policy(), {"policy_signing_key": b"wrong-policy-signing-key"}),
        (policy(materials=(replace(MATERIAL, artifact_sha256="e" * 64),)), {}),
        (policy(assertion_snapshot_id="snapshot-2"), {}),
        (policy(valid_from=NOW + timedelta(minutes=1), expires_at=NOW + timedelta(hours=1)), {}),
        (policy(expires_at=NOW), {}),
        (policy(state="revoked"), {}),
    ],
)
def test_dispatch_gate_rejects_untrusted_stale_and_inactive_authority(
    signed_policy: BatchPolicy, controls: dict[str, object],
) -> None:
    assert not dispatch_matches_policy(intent(), signed_policy, **{**dispatch_controls(), **controls})  # type: ignore[arg-type]
