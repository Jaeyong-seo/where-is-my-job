"""Local deterministic Aside fixture; it has no browser or network dependency."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from application_automation.aside import (
    AsideDoctorResult,
    AsideProtocolError,
    AsideRunContext,
    DispatchIntent,
    FillOutcome,
    FillPlan,
    FormSnapshot,
    PauseReason,
    ScriptRef,
    StatusObservation,
    SubmitOutcome,
    canonical_field_digest,
    decode_result,
    validate_fixture_dispatch,
)

FIXTURE_DOMAIN = "fixture.local"
FIXTURE_PAGE_FINGERPRINT = "fixture-page-v1"
FIXTURE_FORM_FINGERPRINT = "fixture-form-v1"
FIXTURE_SCRIPT_ID = "fixture-form"
FIXTURE_SCRIPT_VERSION = "v1"
FIXTURE_SCRIPT_PATH = Path(__file__).parents[1] / "aside_scripts" / "fixture" / "v1" / "form.js"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PAUSES = {reason.value: reason for reason in PauseReason}
_SCENARIOS = frozenset({"happy", "ambiguous", *_PAUSES})


@dataclass
class _FixtureState:
    scenario: str
    inspected: bool = False
    filled: bool = False
    field_digest: str | None = None
    pause_reason: PauseReason | None = None
    dispatch_id: str | None = None
    receipt_id: str | None = None


class AsideFixtureAdapter:
    """Fixture state is isolated by a validated run identity, never adapter lifetime."""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str, str, str, str, str], _FixtureState] = {}

    def doctor(self) -> AsideDoctorResult:
        return AsideDoctorResult(True, True, "fixture-aside-v1", True, True)

    def inspect(self, ctx: AsideRunContext, script: ScriptRef) -> FormSnapshot:
        self._validate(ctx, script)
        pause = self._pause(ctx)
        state = self._start_run(ctx)
        state.inspected = pause is None
        result = self._result("inspect", pause, fields=["name", "email", "resume"])
        return FormSnapshot(
            result["page_fingerprint"],
            result["form_fingerprint"],
            result["domain"],
            tuple(result["fields"]),
            self._pause_result(result),
        )

    def fill(self, ctx: AsideRunContext, script: ScriptRef, plan: FillPlan) -> FillOutcome:
        self._validate(ctx, script)
        state = self._state(ctx)
        pause = self._pause(ctx)
        if pause is not None:
            result = self._result("fill", pause, filled=False, attached_resume_sha256=None)
        else:
            if not state.inspected:
                raise AsideProtocolError("fill requires a successful inspection")
            attached = self._validate_resume(plan)
            state.filled = True
            state.field_digest = canonical_field_digest(plan.fields)
            result = self._result("fill", None, filled=True, attached_resume_sha256=attached)
        return FillOutcome(
            result["filled"],
            result.get("attached_resume_sha256"),
            result["page_fingerprint"],
            result["form_fingerprint"],
            self._pause_result(result),
            state.field_digest if result["filled"] else None,
        )

    def submit(self, ctx: AsideRunContext, script: ScriptRef, dispatch: DispatchIntent) -> SubmitOutcome:
        self._validate(ctx, script)
        self._validate_dispatch(dispatch, ctx)
        state = self._state(ctx)
        if state.dispatch_id is not None:
            raise AsideProtocolError("a started dispatch is never retried")
        if state.pause_reason is not None:
            raise AsideProtocolError("a paused dispatch requires manual follow-up, not automatic retry")
        pause = self._pause(ctx)
        if pause is None and dispatch.field_digest is not None and dispatch.field_digest != state.field_digest:
            raise AsideProtocolError("dispatch does not reference durable fill evidence")
        if pause is not None:
            state.pause_reason = pause
            result = self._result("submit", pause, started=False, confirmed=False, manual_follow_up=False, receipt_id=None)
        else:
            if not state.filled:
                raise AsideProtocolError("submit requires a completed fill")
            state.dispatch_id = dispatch.dispatch_id
            if ctx.fixture_scenario == "ambiguous":
                result = self._result("submit", None, started=True, confirmed=False, manual_follow_up=True, receipt_id=None)
            else:
                state.receipt_id = "fixture-receipt-v1"
                result = self._result("submit", None, started=True, confirmed=True, manual_follow_up=False, receipt_id=state.receipt_id)
        return SubmitOutcome(
            result["started"],
            result["confirmed"],
            result["manual_follow_up"],
            result.get("receipt_id"),
            self._pause_result(result),
        )


    def observe(self, ctx: AsideRunContext, script: ScriptRef, dispatch_id: str) -> StatusObservation:
        self._validate(ctx, script)
        if not isinstance(dispatch_id, str) or not dispatch_id:
            raise AsideProtocolError("observe requires a dispatch ID")
        state = self._state(ctx)
        pause = self._pause(ctx)
        if pause is not None:
            result = self._result("observe", pause, state="awaiting_user", receipt_id=None)
        elif state.dispatch_id is None:
            result = self._result("observe", None, state="not_started", receipt_id=None)
        elif dispatch_id != state.dispatch_id:
            raise AsideProtocolError("observe dispatch identity drift")
        elif state.receipt_id is not None:
            result = self._result("observe", None, state="confirmed", receipt_id=state.receipt_id)
        else:
            result = self._result("observe", None, state="manual_follow_up", receipt_id=None)
        return StatusObservation(
            result["state"],
            result["page_fingerprint"],
            result["form_fingerprint"],
            result.get("receipt_id"),
            self._pause_result(result),
        )

    @staticmethod
    def _result(operation: str, pause: PauseReason | None, **values: Any) -> Mapping[str, Any]:
        page, form = _fingerprints(pause)
        result = {
            "schema": "application_automation.aside.v1",
            "operation": operation,
            "domain": FIXTURE_DOMAIN,
            "page_fingerprint": page,
            "form_fingerprint": form,
            **values,
        }
        if pause is not None:
            result["pause_reason"] = pause.value
        return decode_result(result, operation)

    @staticmethod
    def _validate_resume(plan: FillPlan) -> str | None:
        if plan.resume_path is None:
            if plan.resume_sha256 is not None:
                raise AsideProtocolError("resume hash supplied without an attachment")
            return None
        if not _is_digest(plan.resume_sha256):
            raise AsideProtocolError("attachment requires an expected hash")
        try:
            actual = hashlib.sha256(plan.resume_path.read_bytes()).hexdigest()
        except OSError as error:
            raise AsideProtocolError("resume attachment is unavailable") from error
        if actual != plan.resume_sha256:
            raise AsideProtocolError("resume attachment hash mismatch")
        return actual

    @staticmethod
    def _pause(ctx: AsideRunContext) -> PauseReason | None:
        if ctx.fixture_scenario not in _SCENARIOS:
            raise AsideProtocolError("unknown fixture scenario")
        return None if ctx.fixture_scenario in {"happy", "ambiguous"} else _PAUSES[ctx.fixture_scenario]

    def _start_run(self, ctx: AsideRunContext) -> _FixtureState:
        key = self._run_key(ctx)
        if key in self._runs:
            raise AsideProtocolError("fixture run key is already in use")
        state = _FixtureState(ctx.fixture_scenario)
        self._runs[key] = state
        return state

    def _state(self, ctx: AsideRunContext) -> _FixtureState:
        try:
            state = self._runs[self._run_key(ctx)]
        except KeyError as error:
            raise AsideProtocolError("operation requires a run inspection") from error
        if state.scenario != ctx.fixture_scenario:
            raise AsideProtocolError("fixture scenario drift")
        return state

    @staticmethod
    def _run_key(ctx: AsideRunContext) -> tuple[str, str, str, str, str, str]:
        return (ctx.provider, ctx.tenant, ctx.account_id_hmac, ctx.context_id_hmac, ctx.session_id_hmac, ctx.run_key)

    @staticmethod
    def _pause_result(result: Mapping[str, Any]) -> PauseReason | None:
        value = result.get("pause_reason")
        return PauseReason(value) if value is not None else None

    @staticmethod
    def _validate_dispatch(dispatch: DispatchIntent, ctx: AsideRunContext) -> None:
        validate_fixture_dispatch(dispatch, ctx)

    @staticmethod
    def _validate(ctx: AsideRunContext, script: ScriptRef) -> None:
        if not all(isinstance(value, str) and value for value in (ctx.provider, ctx.tenant, ctx.run_key)):
            raise AsideProtocolError("fixture context identity is incomplete")
        if not all(_is_digest(value) for value in (ctx.account_id_hmac, ctx.context_id_hmac, ctx.session_id_hmac)):
            raise AsideProtocolError("fixture account/context/session trust anchor is invalid")
        if ctx.aside_version != "fixture-aside-v1":
            raise AsideProtocolError("Aside version drift")
        if (
            script.script_id != FIXTURE_SCRIPT_ID
            or script.path != FIXTURE_SCRIPT_PATH
            or script.version != FIXTURE_SCRIPT_VERSION
            or script.allowed_domains != frozenset({FIXTURE_DOMAIN})
            or not _is_digest(script.sha256)
        ):
            raise AsideProtocolError("fixture script identity drift")
        try:
            digest = hashlib.sha256(script.path.read_bytes()).hexdigest()
        except OSError as error:
            raise AsideProtocolError("fixture script is unavailable") from error
        if script.sha256 != digest:
            raise AsideProtocolError("script hash drift")
        if ctx.expected_page_fingerprint != FIXTURE_PAGE_FINGERPRINT or ctx.expected_form_fingerprint != FIXTURE_FORM_FINGERPRINT:
            raise AsideProtocolError("fixture fingerprint drift")


def _fingerprints(pause: PauseReason | None) -> tuple[str, str]:
    if pause is PauseReason.FORM_DRIFT:
        return FIXTURE_PAGE_FINGERPRINT, "fixture-form-drift"
    if pause is PauseReason.POSTING_DRIFT:
        return "fixture-posting-drift", FIXTURE_FORM_FINGERPRINT
    if pause is PauseReason.UNEXPECTED_REDIRECT:
        return "fixture-page-drift", FIXTURE_FORM_FINGERPRINT
    return FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None
