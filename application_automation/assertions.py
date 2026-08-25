"""Candidate-confirmed assertion lookup with deliberately exact question matching."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import math
import secrets
from types import MappingProxyType
from typing import Iterable

from .models import AssertionResolution, CandidateAssertion, ExactAlias, FormField, PauseReason
from .crypto import canonical_json, domain_hmac, sha256_artifact

@dataclass(frozen=True)
class _BoundAssertionResolution(AssertionResolution):
    """An assertion result bound to the exact observed form field."""

    provider: str
    form_fingerprint: str
    field_key: str
    normalized_label: str
    registry_authentication: str



SPONSORSHIP_KEY = "work.sponsorship_now_or_future"
AVAILABILITY_KEY = "availability.notice_days"
DEMOGRAPHICS_KEY = "demographics.optional_response"
SALARY_KEY = "salary.policy"
LOCATION_CITY_KEY = "location.city"
LOCATION_PROVINCE_KEY = "location.province"
LOCATION_COUNTRY_KEY = "location.country"
def assertion_alias_snapshot_digest(
    assertions: Iterable[CandidateAssertion],
    aliases: Iterable[ExactAlias],
) -> str:
    """Digest the complete immutable assertion/alias authority snapshot."""
    return sha256_artifact(canonical_json({
        "assertions": sorted(
            [{
                "key": item.key, "value": item.value, "assertion_id": item.assertion_id,
                "assertion_revision": item.assertion_revision, "profile_id": item.profile_id,
                "profile_revision": item.profile_revision, "snapshot_id": item.snapshot_id,
                "snapshot_revision": item.snapshot_revision,
                "confirmation_event_id": item.confirmation_event_id,
                "confirmation_event_revision": item.confirmation_event_revision,
                "confirmed_at": item.confirmed_at.isoformat(),
                "form_fingerprint": item.form_fingerprint,
                "revoked_at": item.revoked_at.isoformat() if item.revoked_at else None,
            } for item in assertions],
            key=lambda item: (item["assertion_id"], item["assertion_revision"]),
        ),
        "aliases": sorted(
            [{
                "semantic_key": item.semantic_key, "alias": item.alias, "provider": item.provider,
                "form_fingerprint": item.form_fingerprint, "alias_id": item.alias_id,
                "alias_revision": item.alias_revision, "profile_id": item.profile_id,
                "profile_revision": item.profile_revision, "snapshot_id": item.snapshot_id,
                "snapshot_revision": item.snapshot_revision,
                "confirmation_event_id": item.confirmation_event_id,
                "confirmation_event_revision": item.confirmation_event_revision,
            } for item in aliases],
            key=lambda item: (item["alias_id"], item["alias_revision"]),
        ),
    }))


def _resolution_claims(resolution: _BoundAssertionResolution) -> dict[str, object]:
    return {
        "value": resolution.value, "assertion_key": resolution.assertion_key,
        "assertion_id": resolution.assertion_id, "assertion_revision": resolution.assertion_revision,
        "alias_id": resolution.alias_id, "alias_revision": resolution.alias_revision,
        "profile_id": resolution.profile_id, "profile_revision": resolution.profile_revision,
        "snapshot_id": resolution.snapshot_id, "snapshot_revision": resolution.snapshot_revision,
        "confirmation_event_id": resolution.confirmation_event_id,
        "confirmation_event_revision": resolution.confirmation_event_revision,
        "pause_reason": resolution.pause_reason.value if resolution.pause_reason else None,
        "provider": resolution.provider, "form_fingerprint": resolution.form_fingerprint,
        "field_key": resolution.field_key, "normalized_label": resolution.normalized_label,
    }


@dataclass(frozen=True)
class PostedSalaryRange:
    minimum: int | float
    maximum: int | float
    currency: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum, bool)
            or isinstance(self.maximum, bool)
            or not isinstance(self.minimum, (int, float))
            or not isinstance(self.maximum, (int, float))
            or not math.isfinite(self.minimum)
            or not math.isfinite(self.maximum)
            or self.minimum > self.maximum
            or not isinstance(self.currency, str)
            or not self.currency.strip()
        ):
            raise ValueError("posted salary range must have finite ordered bounds and a non-empty currency")


class AssertionRegistry:
    """An immutable registry for one confirmed assertion snapshot."""

    def __init__(self, assertions: Iterable[CandidateAssertion] = (), aliases: Iterable[ExactAlias] = ()) -> None:
        assertion_rows = tuple(assertions)
        alias_rows = tuple(aliases)
        assertion_map: dict[str, CandidateAssertion] = {}
        snapshot_ids: set[tuple[str, int]] = set()
        profile_ids: set[tuple[str, int]] = set()
        for assertion in assertion_rows:
            if assertion.key in assertion_map:
                raise ValueError(f"duplicate semantic assertion: {assertion.key}")
            assertion_map[assertion.key] = assertion
            snapshot_ids.add((assertion.snapshot_id, assertion.snapshot_revision))
            profile_ids.add((assertion.profile_id, assertion.profile_revision))
        if len(snapshot_ids) > 1 or len(profile_ids) > 1:
            raise ValueError("assertion registry must contain one profile and revisioned snapshot")
        alias_map: dict[tuple[str, str, str], ExactAlias] = {}
        for alias in alias_rows:
            alias_key = (alias.provider, alias.form_fingerprint, alias.alias)
            assertion = assertion_map.get(alias.semantic_key)
            if alias_key in alias_map:
                raise ValueError(f"duplicate exact alias: {alias.alias}")
            if assertion is None:
                raise ValueError(f"alias has no confirmed assertion: {alias.semantic_key}")
            if (
                (alias.profile_id, alias.profile_revision) != (assertion.profile_id, assertion.profile_revision)
                or (alias.snapshot_id, alias.snapshot_revision) != (assertion.snapshot_id, assertion.snapshot_revision)
                or (alias.confirmation_event_id, alias.confirmation_event_revision)
                != (assertion.confirmation_event_id, assertion.confirmation_event_revision)
            ):
                raise ValueError("alias provenance does not match assertion authority")
            alias_map[alias_key] = alias
        self.snapshot_id, self.snapshot_revision = next(iter(snapshot_ids), (None, None))
        self.profile_id, self.profile_revision = next(iter(profile_ids), (None, None))
        self.assertion_snapshot_digest = assertion_alias_snapshot_digest(assertion_rows, alias_rows)
        self._authentication_key = secrets.token_bytes(32)
        self._assertions = MappingProxyType(assertion_map)
        self._aliases = MappingProxyType(alias_map)

    def resolve(
        self,
        *,
        provider: str,
        form_fingerprint: str,
        field: FormField,
        posted_salary_range: PostedSalaryRange | None = None,
        now: datetime | None = None,
    ) -> AssertionResolution:
        """Resolve one field, returning a pause rather than inferring any answer."""
        def unresolved(reason: PauseReason) -> AssertionResolution:
            return _BoundAssertionResolution(
                None, None, None, None, None, None, None, None, None, None, None, None, reason,
                provider, form_fingerprint, field.field_key, field.normalized_label, "",
            )

        hard_stop = self._challenge_pause(field)
        if hard_stop is not None:
            return unresolved(hard_stop)
        alias = self._aliases.get((provider, form_fingerprint, field.normalized_label))
        semantic_key = alias.semantic_key if alias is not None else None
        if semantic_key is None:
            return unresolved(self._intrinsic_pause(field))
        assertion = self._assertions.get(semantic_key)
        if assertion is None or assertion.revoked_at is not None:
            return unresolved(PauseReason.UNKNOWN_QUESTION)
        if assertion.snapshot_id != self.snapshot_id:
            return unresolved(PauseReason.POLICY_BINDING_MISMATCH)
        if assertion.form_fingerprint not in (None, form_fingerprint):
            return unresolved(PauseReason.POLICY_BINDING_MISMATCH)
        effective_now = datetime.now(UTC) if now is None else now
        if (
            effective_now.tzinfo is None
            or effective_now.utcoffset() is None
            or assertion.confirmed_at.tzinfo is None
            or assertion.confirmed_at.utcoffset() is None
            or assertion.confirmed_at > effective_now
        ):
            return unresolved(PauseReason.UNKNOWN_QUESTION)
        rendered, pause_reason = self._render(semantic_key, assertion.value, field, posted_salary_range)
        if pause_reason is not None:
            return unresolved(pause_reason)
        if rendered is None:
            return unresolved(PauseReason.UNKNOWN_QUESTION)
        if (
            field.input_type in {"select", "radio", "checkbox"}
            or field.option_keys
        ) and rendered not in field.option_keys:
            return unresolved(self._option_pause(semantic_key))
        resolution = _BoundAssertionResolution(
            rendered, semantic_key, assertion.assertion_id, assertion.assertion_revision,
            alias.alias_id, alias.alias_revision, assertion.profile_id, assertion.profile_revision,
            assertion.snapshot_id, assertion.snapshot_revision, alias.confirmation_event_id,
            alias.confirmation_event_revision, None,
            provider, form_fingerprint, field.field_key, field.normalized_label, "",
        )
        object.__setattr__(
            resolution,
            "registry_authentication",
            domain_hmac(self._authentication_key, "assertion_registry.resolution.v1", _resolution_claims(resolution)),
        )
        return resolution
    def authenticates(self, resolutions: Iterable[AssertionResolution]) -> bool:
        """Require rows issued unchanged by this immutable registry."""
        for resolution in resolutions:
            if (
                not isinstance(resolution, _BoundAssertionResolution)
                or domain_hmac(
                    self._authentication_key,
                    "assertion_registry.resolution.v1",
                    _resolution_claims(resolution),
                ) != resolution.registry_authentication
            ):
                return False
        return True

    @staticmethod
    def _render(
        semantic_key: str,
        value: object,
        field: FormField,
        posted_salary_range: PostedSalaryRange | None,
    ) -> tuple[str | None, PauseReason | None]:
        if semantic_key == SPONSORSHIP_KEY:
            return ("No, now or in the future", None) if value is False else (None, PauseReason.LEGAL_QUESTION)
        if semantic_key == AVAILABILITY_KEY:
            return ("14 days", None) if value == 14 else (None, PauseReason.UNKNOWN_QUESTION)
        if semantic_key == DEMOGRAPHICS_KEY:
            if field.required or value != "prefer_not_to_answer":
                return None, PauseReason.REQUIRED_DEMOGRAPHICS
            return "Prefer not to answer", None
        if semantic_key == SALARY_KEY:
            if posted_salary_range is None or value != "negotiable_within_posted_range":
                return None, PauseReason.SALARY_UNVERIFIED
            label = field.normalized_label.casefold()
            exact_number_markers = ("exact", "amount", "minimum", "maximum", "desired salary", "expected salary")
            range_conflict_markers = ("below", "above", "under", "over", "outside", "greater than", "less than")
            if (
                field.input_type == "number"
                or any(marker in label for marker in exact_number_markers)
                or any(marker in label for marker in range_conflict_markers)
                or (any(marker in label for marker in ("cad", "usd", "eur", "gbp", "$")) and posted_salary_range.currency.casefold() not in label)
            ):
                return None, PauseReason.SALARY_UNVERIFIED
            return "Negotiable within posted range", None
        location_values = {
            LOCATION_CITY_KEY: "Vancouver",
            LOCATION_PROVINCE_KEY: "BC",
            LOCATION_COUNTRY_KEY: "Canada",
        }
        if semantic_key in location_values:
            expected = location_values[semantic_key]
            return (expected, None) if value == expected else (None, PauseReason.UNKNOWN_QUESTION)
        return None, PauseReason.UNKNOWN_QUESTION

    @staticmethod
    def _option_pause(semantic_key: str) -> PauseReason:
        if semantic_key == DEMOGRAPHICS_KEY:
            return PauseReason.REQUIRED_DEMOGRAPHICS
        if semantic_key == SALARY_KEY:
            return PauseReason.SALARY_UNVERIFIED
        if semantic_key == SPONSORSHIP_KEY:
            return PauseReason.LEGAL_QUESTION
        return PauseReason.UNKNOWN_QUESTION

    @staticmethod
    def _challenge_pause(field: FormField) -> PauseReason | None:
        return {
            "captcha": PauseReason.CAPTCHA,
            "mfa": PauseReason.MFA,
            "login": PauseReason.LOGIN,
            "security": PauseReason.SECURITY_CHALLENGE,
            "security_challenge": PauseReason.SECURITY_CHALLENGE,
            "rate_limit": PauseReason.RATE_LIMIT,
            "street_address": PauseReason.STREET_ADDRESS,
            "full_address": PauseReason.STREET_ADDRESS,
            "postal_code": PauseReason.STREET_ADDRESS,
            "unit": PauseReason.STREET_ADDRESS,
            "sensitive": PauseReason.SENSITIVE_QUESTION,
            "legal": PauseReason.LEGAL_QUESTION,
            "citizenship": PauseReason.LEGAL_QUESTION,
            "work_permit": PauseReason.LEGAL_QUESTION,
            "authorization": PauseReason.LEGAL_QUESTION,
            "attestation": PauseReason.ATTESTATION,
        }.get(field.semantic_class)

    @staticmethod
    def _intrinsic_pause(field: FormField) -> PauseReason:
        """Only explicit classifier values are trusted; unknown fields remain unknown."""
        if field.semantic_class in {"captcha"}:
            return PauseReason.CAPTCHA
        if field.semantic_class in {"mfa"}:
            return PauseReason.MFA
        if field.semantic_class in {"login"}:
            return PauseReason.LOGIN
        if field.semantic_class in {"security", "security_challenge"}:
            return PauseReason.SECURITY_CHALLENGE
        if field.semantic_class in {"street_address", "full_address", "postal_code", "unit"}:
            return PauseReason.STREET_ADDRESS
        if field.semantic_class in {"demographic", "demographics"}:
            return PauseReason.REQUIRED_DEMOGRAPHICS
        if field.semantic_class in {"sensitive"}:
            return PauseReason.SENSITIVE_QUESTION
        if field.semantic_class in {"legal", "citizenship", "work_permit", "authorization", "attestation"}:
            return PauseReason.ATTESTATION if field.semantic_class == "attestation" else PauseReason.LEGAL_QUESTION
        if field.semantic_class in {"salary", "compensation"}:
            return PauseReason.SALARY_UNVERIFIED
        return PauseReason.NEW_QUESTION
