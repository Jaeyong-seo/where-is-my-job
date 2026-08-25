"""Protected JSON-RPC transport for Aside fill, submit, and observe operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import stat
import subprocess
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from dataclasses import dataclass
import time
from uuid import uuid4

from application_automation.adapters.base import AsideCliAdapter
from application_automation.aside import (
    AsideDoctorResult, AsideProbeError, AsideProtocolError, AsideRunContext, AsideTransportError,
    DispatchIntent, FillOutcome, FillPlan, FormSnapshot, PauseReason, ScriptRef,
    StatusObservation, SubmitOutcome, canonical_dispatch_payload_sha256, canonical_field_digest,
    decode_result, validate_fixture_dispatch,
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PAYLOAD_NAME = re.compile(r"application-automation-([0-9a-f]{32})\.json\Z")
_MAX_RPC_LINE_BYTES = 64 * 1024

_MAX_REPL_OUTPUT_BYTES = 64 * 1024
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ASIDE_STATUS_LINE = re.compile(
    r"^\[(?:ok|info|warn|warning) \| [A-Za-z0-9][A-Za-z0-9 .,:;_()/+-]{0,255}\]$"
)

_MCP_TITLES = {
    "inspect": "Inspecting application form",
    "fill": "Filling application form",
    "submit": "Submitting application form",
    "observe": "Checking application status",
}
@dataclass(frozen=True)
class FixtureDispatchEvidence:
    dispatch: DispatchIntent
    state: str
    receipt_digest: str | None
    pause_reason: str | None = None


@dataclass(frozen=True)
class FillEvidence:
    """Durable proof of a completed fill, per the ``fixture_fill_evidence`` contract."""

    run_id: str
    application_id: str
    session_hmac: str
    page_fingerprint: str
    form_fingerprint: str
    field_digest: str
    resume_present: bool
    resume_sha256: str | None
    script_sha256: str
    executable_sha256: str
    dispatch_binding: str | None = None


def canonical_submit_payload(dispatch: DispatchIntent) -> dict[str, str | None]:
    """The immutable payload whose digest binds a fixture submit intent."""
    return {
        "dispatch_id": dispatch.dispatch_id,
        "application_id": dispatch.application_id,
        "session_id": dispatch.session_id,
        "run_id": dispatch.run_id,
        "page_fingerprint": dispatch.page_fingerprint,
        "form_fingerprint": dispatch.form_fingerprint,
        "resume_sha256": dispatch.resume_sha256,
        "field_digest": dispatch.field_digest,
    }

def canonical_submit_payload_sha256(dispatch: DispatchIntent) -> str:
    """Reuse the single canonical digest implementation owned by ``aside``."""
    return canonical_dispatch_payload_sha256(dispatch)


@dataclass(frozen=True)
class LiveAsideIdentity:
    """Opaque HMACs attested by the live, pinned MCP session."""

    account_id_hmac: str
    context_id_hmac: str
    session_id_hmac: str
    provider_hmac: str
    tenant_hmac: str


class LiveIdentityAttestor(Protocol):
    def attest(self, ctx: AsideRunContext) -> LiveAsideIdentity: ...


class FixtureOutcomeLedger(Protocol):
    def claim(self, ctx: AsideRunContext, dispatch: DispatchIntent) -> str: ...
    def record(
        self, dispatch_id: str, state: str, *, receipt_id: str | None = None, pause_reason: str | None = None,
    ) -> None: ...
    def evidence(self, ctx: AsideRunContext, dispatch_id: str) -> FixtureDispatchEvidence: ...


class FixtureFillEvidenceLedger(Protocol):
    def record(
        self,
        ctx: AsideRunContext,
        *,
        run_id: str,
        application_id: str,
        page_fingerprint: str,
        form_fingerprint: str,
        field_digest: str,
        resume_sha256: str | None,
        script_sha256: str,
        executable_sha256: str,
    ) -> None: ...
    def evidence(self, ctx: AsideRunContext, run_id: str, form_fingerprint: str) -> FillEvidence | None: ...
    def bind(self, run_id: str, form_fingerprint: str, dispatch_id: str) -> None: ...


class SQLiteFixtureFillEvidenceLedger:
    """Durable proof of a completed fill, backed by ``fixture_fill_evidence``."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record(
        self,
        ctx: AsideRunContext,
        *,
        run_id: str,
        application_id: str,
        page_fingerprint: str,
        form_fingerprint: str,
        field_digest: str,
        resume_sha256: str | None,
        script_sha256: str,
        executable_sha256: str,
    ) -> None:
        if resume_sha256 is not None and not _DIGEST.fullmatch(resume_sha256):
            raise AsideProtocolError("invalid resume digest")
        if not _DIGEST.fullmatch(field_digest) or not _DIGEST.fullmatch(script_sha256) or not _DIGEST.fullmatch(executable_sha256):
            raise AsideProtocolError("invalid fill evidence digest")
        with self._connection:
            row = self._connection.execute(
                "SELECT dispatch_binding FROM fixture_fill_evidence WHERE run_id=? AND form_fingerprint=?",
                (run_id, form_fingerprint),
            ).fetchone()
            if row is not None and row[0] is not None:
                raise AsideTransportError("fill evidence is already bound to a dispatch")
            self._connection.execute(
                """
                INSERT INTO fixture_fill_evidence
                (run_id, application_id, session_hmac, page_fingerprint, form_fingerprint,
                 field_digest, resume_present, resume_sha256, script_sha256, executable_sha256,
                 created_at, revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 1)
                ON CONFLICT(run_id, form_fingerprint) DO UPDATE SET
                    application_id=excluded.application_id, session_hmac=excluded.session_hmac,
                    field_digest=excluded.field_digest, resume_present=excluded.resume_present,
                    resume_sha256=excluded.resume_sha256, script_sha256=excluded.script_sha256,
                    executable_sha256=excluded.executable_sha256, revision=fixture_fill_evidence.revision+1
                """,
                (
                    run_id, application_id, ctx.session_id_hmac, page_fingerprint, form_fingerprint,
                    field_digest, 1 if resume_sha256 is not None else 0, resume_sha256,
                    script_sha256, executable_sha256,
                ),
            )

    def evidence(self, ctx: AsideRunContext, run_id: str, form_fingerprint: str) -> FillEvidence | None:
        row = self._connection.execute(
            """SELECT application_id, session_hmac, page_fingerprint, form_fingerprint, field_digest,
                      resume_present, resume_sha256, script_sha256, executable_sha256, dispatch_binding
               FROM fixture_fill_evidence WHERE run_id=? AND form_fingerprint=?""",
            (run_id, form_fingerprint),
        ).fetchone()
        if row is None:
            return None
        if str(row[1]) != ctx.session_id_hmac:
            raise AsideTransportError("fill evidence binding drift")
        resume_sha256 = row[6]
        if resume_sha256 is not None and not _DIGEST.fullmatch(resume_sha256):
            raise AsideTransportError("durable resume digest is malformed")
        if bool(row[5]) != (resume_sha256 is not None):
            raise AsideTransportError("durable resume presence drift")
        return FillEvidence(
            run_id, str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]),
            bool(row[5]), resume_sha256, str(row[7]), str(row[8]),
            str(row[9]) if row[9] is not None else None,
        )

    def bind(self, run_id: str, form_fingerprint: str, dispatch_id: str) -> None:
        with self._connection:
            changed = self._connection.execute(
                """UPDATE fixture_fill_evidence SET dispatch_binding=?, revision=revision+1
                   WHERE run_id=? AND form_fingerprint=? AND (dispatch_binding IS NULL OR dispatch_binding=?)""",
                (dispatch_id, run_id, form_fingerprint, dispatch_id),
            )
            if changed.rowcount == 1:
                return
            row = self._connection.execute(
                "SELECT dispatch_binding FROM fixture_fill_evidence WHERE run_id=? AND form_fingerprint=?",
                (run_id, form_fingerprint),
            ).fetchone()
            if row is None:
                raise AsideTransportError("fill evidence is unknown")
            if row[0] == dispatch_id:
                return
            raise AsideTransportError("fill evidence is already bound to a different dispatch")

class SQLiteFixtureOutcomeLedger:
    """Atomic fixture-only dispatch ledger backed by ``fixture_dispatch_outcomes``."""

    _TERMINAL = frozenset({"confirmed", "manual_followup", "retryable_not_started"})

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def claim(self, ctx: AsideRunContext, dispatch: DispatchIntent) -> str:
        self._validate_dispatch(dispatch)
        with self._connection:
            row = self._connection.execute(
                "SELECT state FROM fixture_dispatch_outcomes WHERE dispatch_id=?",
                (dispatch.dispatch_id,),
            ).fetchone()
            if row is not None:
                if row[0] == "retryable_not_started":
                    changed = self._connection.execute(
                        """UPDATE fixture_dispatch_outcomes SET state='prepared', terminal_at=NULL,
                           pause_reason=NULL, revision=revision+1 WHERE dispatch_id=? AND state='retryable_not_started'
                           AND application_id=? AND provider=? AND tenant=? AND account_hmac=?
                           AND context_hmac=? AND session_id=? AND session_hmac=? AND run_id=?
                           AND intent_hmac=? AND payload_sha256=? AND page_fingerprint=?
                           AND form_fingerprint=? AND resume_sha256 IS ? AND field_digest IS ?""",
                        (
                            dispatch.dispatch_id, dispatch.application_id, ctx.provider, ctx.tenant,
                            ctx.account_id_hmac, ctx.context_id_hmac, dispatch.session_id,
                            ctx.session_id_hmac, dispatch.run_id, dispatch.intent_hmac,
                            dispatch.payload_sha256, dispatch.page_fingerprint, dispatch.form_fingerprint,
                            dispatch.resume_sha256, dispatch.field_digest,
                        ),
                    )
                    if changed.rowcount == 1:
                        return "prepared"
                    raise AsideTransportError("retryable dispatch binding drift")
                raise AsideTransportError("a durable dispatch outcome is observe-only")
            exclusive = self._connection.execute(
                "SELECT 1 FROM fixture_dispatch_outcomes WHERE application_id=? AND state != 'retryable_not_started'",
                (dispatch.application_id,),
            ).fetchone()
            if exclusive is not None:
                raise AsideTransportError("application already has a canonical non-retryable dispatch outcome")
            self._connection.execute(
                """
                INSERT INTO fixture_dispatch_outcomes
                (dispatch_id, application_id, provider, tenant, account_hmac, context_hmac,
                 session_id, session_hmac, run_id, intent_hmac, payload_sha256, page_fingerprint,
                 form_fingerprint, resume_present, resume_sha256, field_digest, observed_intent_hmac, state, prepared_at, revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', datetime('now'), 1)
                """,
                (
                    dispatch.dispatch_id, dispatch.application_id, ctx.provider, ctx.tenant,
                    ctx.account_id_hmac, ctx.context_id_hmac, dispatch.session_id, ctx.session_id_hmac,
                    dispatch.run_id, dispatch.intent_hmac, dispatch.payload_sha256,
                    dispatch.page_fingerprint, dispatch.form_fingerprint,
                    1 if dispatch.resume_sha256 is not None else 0, dispatch.resume_sha256,
                    dispatch.field_digest, dispatch.intent_hmac,
                ),
            )
        return "prepared"

    def record(
        self, dispatch_id: str, state: str, *, receipt_id: str | None = None, pause_reason: str | None = None,
    ) -> None:
        if state not in {"possibly_started", "confirmed", "manual_followup", "retryable_not_started"}:
            raise AsideProtocolError("invalid fixture outcome state")
        if receipt_id is not None and state not in {"possibly_started", "confirmed"}:
            raise AsideProtocolError("non-confirmed fixture outcome cannot have a receipt")
        if pause_reason is not None and state != "manual_followup":
            raise AsideProtocolError("pause reason requires a manual-followup outcome")
        for _ in range(8):
            with self._connection:
                row = self._connection.execute(
                    "SELECT state, intent_hmac, receipt_digest, revision FROM fixture_dispatch_outcomes WHERE dispatch_id=?",
                    (dispatch_id,),
                ).fetchone()
                if row is None:
                    raise AsideTransportError("dispatch intent is unknown")
                old_state, intent_hmac, durable_receipt_digest, revision = str(row[0]), str(row[1]), row[2], int(row[3])
                if old_state in self._TERMINAL:
                    if old_state != state:
                        raise AsideTransportError("terminal dispatch outcome is observe-only")
                    if (
                        old_state == "confirmed"
                        and receipt_id is not None
                        and durable_receipt_digest != hashlib.sha256(receipt_id.encode()).hexdigest()
                    ):
                        raise AsideProtocolError("observed receipt does not match durable submit receipt")
                    return
                if old_state == "prepared" and state not in {"possibly_started", "retryable_not_started", "manual_followup"}:
                    raise AsideTransportError("invalid prepared dispatch transition")
                if old_state == "possibly_started" and state not in {"confirmed", "manual_followup", "possibly_started"}:
                    raise AsideTransportError("invalid possibly-started dispatch transition")
                receipt_digest = hashlib.sha256(receipt_id.encode()).hexdigest() if receipt_id is not None else None
                if state == "confirmed" and (receipt_id is None or durable_receipt_digest != receipt_digest):
                    raise AsideProtocolError("observed receipt does not match durable submit receipt")
                # Every transition is a compare-and-swap on (dispatch_id, state, revision): a
                # lost race is retried, and a terminal winner is preserved by the check above.
                if state == "possibly_started":
                    changed = self._connection.execute(
                        """UPDATE fixture_dispatch_outcomes
                           SET state=?, receipt_digest=?, started_at=COALESCE(started_at, datetime('now')), revision=revision+1
                           WHERE dispatch_id=? AND state=? AND revision=?""",
                        (state, receipt_digest, dispatch_id, old_state, revision),
                    )
                elif state == "confirmed":
                    changed = self._connection.execute(
                        """UPDATE fixture_dispatch_outcomes
                           SET state=?, attestation_digest=?, observed_intent_hmac=?,
                               confirmed_at=datetime('now'), terminal_at=datetime('now'), revision=revision+1
                           WHERE dispatch_id=? AND state=? AND revision=?""",
                        (state, intent_hmac, intent_hmac, dispatch_id, old_state, revision),
                    )
                elif state == "manual_followup":
                    changed = self._connection.execute(
                        """UPDATE fixture_dispatch_outcomes
                           SET state=?, pause_reason=?, terminal_at=datetime('now'), revision=revision+1
                           WHERE dispatch_id=? AND state=? AND revision=?""",
                        (state, pause_reason, dispatch_id, old_state, revision),
                    )
                else:
                    changed = self._connection.execute(
                        """UPDATE fixture_dispatch_outcomes
                           SET state=?, terminal_at=datetime('now'), revision=revision+1
                           WHERE dispatch_id=? AND state=? AND revision=?""",
                        (state, dispatch_id, old_state, revision),
                    )
                if changed.rowcount == 1:
                    return
        raise AsideTransportError("dispatch outcome revision drift")

    def evidence(self, ctx: AsideRunContext, dispatch_id: str) -> FixtureDispatchEvidence:
        row = self._connection.execute(
            """SELECT application_id, session_id, run_id, intent_hmac, payload_sha256,
                      page_fingerprint, form_fingerprint, resume_sha256, state, receipt_digest,
                      provider, tenant, account_hmac, context_hmac, session_hmac, observed_intent_hmac,
                      field_digest, resume_present, pause_reason
               FROM fixture_dispatch_outcomes WHERE dispatch_id=?""",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            raise AsideTransportError("dispatch intent is unknown")
        if (
            str(row[10]) != ctx.provider or str(row[11]) != ctx.tenant
            or str(row[12]) != ctx.account_id_hmac or str(row[13]) != ctx.context_id_hmac
            or str(row[14]) != ctx.session_id_hmac or str(row[2]) != ctx.run_key
            or (row[15] is not None and str(row[15]) != str(row[3]))
        ):
            raise AsideTransportError("dispatch intent binding drift")
        resume_sha256 = row[7]
        if resume_sha256 is not None and not _DIGEST.fullmatch(resume_sha256):
            raise AsideTransportError("durable resume digest is malformed")
        if bool(row[17]) != (resume_sha256 is not None):
            raise AsideTransportError("durable resume presence drift")
        field_digest = row[16]
        if field_digest is not None and not _DIGEST.fullmatch(field_digest):
            raise AsideTransportError("durable field digest is malformed")
        return FixtureDispatchEvidence(
            DispatchIntent(
                dispatch_id, str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]),
                str(row[5]), str(row[6]), resume_sha256, field_digest,
            ),
            str(row[8]),
            str(row[9]) if row[9] is not None else None,
            str(row[18]) if row[18] is not None else None,
        )

    @staticmethod
    def _validate_dispatch(dispatch: DispatchIntent) -> None:
        values = (
            dispatch.dispatch_id, dispatch.application_id, dispatch.session_id, dispatch.run_id,
            dispatch.intent_hmac, dispatch.payload_sha256, dispatch.page_fingerprint,
            dispatch.form_fingerprint,
        )
        if not all(isinstance(value, str) and value for value in values):
            raise AsideProtocolError("incomplete dispatch intent")
        if not all(_DIGEST.fullmatch(value) for value in (dispatch.intent_hmac, dispatch.payload_sha256)):
            raise AsideProtocolError("invalid dispatch identity digest")
        if dispatch.resume_sha256 is not None and not _DIGEST.fullmatch(dispatch.resume_sha256):
            raise AsideProtocolError("invalid resume digest")
        if dispatch.field_digest is not None and not _DIGEST.fullmatch(dispatch.field_digest):
            raise AsideProtocolError("invalid field digest")


class AsideMcpAdapter:
    """Use ``aside mcp`` exclusively for guarded operations.

    Payloads are written to a private file and referenced from reviewed REPL code only
    by an opaque path and per-call token.  Only the fixture script is executable; its
    durable outcome is claimed only after deterministic local preflight.
    """

    def __init__(
        self,
        aside_path: str,
        approved_cli_sha256: str,
        expected_version: str,
        script_registry: Mapping[tuple[str, str], str],
        *,
        expected_account_id_hmac: str,
        expected_context_id_hmac: str,
        expected_session_id_hmac: str,
        expected_provider: str,
        expected_tenant: str,
        expected_provider_hmac: str,
        expected_tenant_hmac: str,
        identity_attestor: LiveIdentityAttestor,
        outcome_ledger: FixtureOutcomeLedger,
        fill_evidence_ledger: FixtureFillEvidenceLedger,
    ) -> None:
        approved_path = Path(aside_path)
        if (
            not approved_path.is_absolute()
            or not _DIGEST.fullmatch(approved_cli_sha256)
            or not all(
                _DIGEST.fullmatch(value or "")
                for value in (
                    expected_account_id_hmac,
                    expected_context_id_hmac,
                    expected_session_id_hmac,
                    expected_provider_hmac,
                    expected_tenant_hmac,
                )
            )
            or expected_provider != "fixture"
            or expected_tenant != "fixture"
        ):
            raise AsideProtocolError("Aside executable and identity anchors must be absolute and hashed")
        if not expected_version:
            raise AsideProtocolError("Aside version anchor is required")
        self._cli = AsideCliAdapter(str(approved_path), expected_version, approved_cli_sha256)
        self._approved_path = str(approved_path)
        self._approved_cli_sha256 = approved_cli_sha256
        self._expected_version = expected_version
        self._registry = dict(script_registry)
        self._expected_account_id_hmac = expected_account_id_hmac
        self._expected_context_id_hmac = expected_context_id_hmac
        self._expected_session_id_hmac = expected_session_id_hmac
        self._expected_provider = expected_provider
        self._expected_tenant = expected_tenant
        self._expected_provider_hmac = expected_provider_hmac
        self._expected_tenant_hmac = expected_tenant_hmac
        self._identity_attestor = identity_attestor
        self._outcomes = outcome_ledger
        self._fill_evidence = fill_evidence_ledger
        self._fixture_runs: dict[tuple[str, str, str, str, str, str], str] = {}
    def doctor(self) -> AsideDoctorResult:
        return self._cli.doctor()

    def inspect(self, ctx: AsideRunContext, script: ScriptRef) -> FormSnapshot:
        result = self._execute("inspect", ctx, script, {})
        return FormSnapshot(result["page_fingerprint"], result["form_fingerprint"], result["domain"], tuple(result["fields"]), self._pause(result))

    def fill(self, ctx: AsideRunContext, script: ScriptRef, plan: FillPlan) -> FillOutcome:
        if not isinstance(plan.application_id, str) or not plan.application_id:
            raise AsideProtocolError("fill requires an application identity for durable evidence")
        attached = self._resume_digest(plan)
        field_digest = canonical_field_digest(plan.fields)
        payload: dict[str, Any] = {"fields": dict(plan.fields)}
        if plan.resume_path is not None:
            payload["resume_path"] = str(plan.resume_path)
            payload["resume_sha256"] = attached
        result = self._execute("fill", ctx, script, payload)
        returned = result.get("attached_resume_sha256")
        if result.get("pause_reason") is None and attached != returned:
            raise AsideProtocolError("resume attachment digest mismatch")
        paused = result.get("pause_reason") is not None
        if not paused:
            # Durable proof of a completed fill: the sole authority a later submit()
            # is allowed to reference, independent of adapter or process lifetime.
            self._fill_evidence.record(
                ctx,
                run_id=ctx.run_key,
                application_id=plan.application_id,
                page_fingerprint=result["page_fingerprint"],
                form_fingerprint=result["form_fingerprint"],
                field_digest=field_digest,
                resume_sha256=attached,
                script_sha256=script.sha256,
                executable_sha256=self._approved_cli_sha256,
            )
        return FillOutcome(
            result["filled"], returned, result["page_fingerprint"], result["form_fingerprint"],
            self._pause(result), None if paused else field_digest,
        )

    def submit(self, ctx: AsideRunContext, script: ScriptRef, dispatch: DispatchIntent) -> SubmitOutcome:
        validate_fixture_dispatch(dispatch, ctx)
        self._require_fill_evidence(ctx, script, dispatch)
        submit_payload = {
            **canonical_submit_payload(dispatch),
            "intent_hmac": dispatch.intent_hmac,
            "payload_sha256": dispatch.payload_sha256,
        }
        source, _ = self._trusted_source(ctx, script)
        if source.count("/* APPLICATION_AUTOMATION_REQUEST */ null") != 1 or source.count("const input = APPLICATION_AUTOMATION_REQUEST;") != 1:
            raise AsideProtocolError("script request marker drift")
        claimed = self._outcomes.claim(ctx, dispatch)
        if claimed != "prepared":
            raise AsideTransportError("a durable dispatch outcome is observe-only")
        self._fill_evidence.bind(dispatch.run_id, dispatch.form_fingerprint, dispatch.dispatch_id)
        try:
            source, executable = self._trusted_source(ctx, script)
            fixture_phase = self._fixture_phase(ctx, submit_payload, self._fixture_run_key(ctx))
        except (AsideTransportError, AsideProtocolError, AsideProbeError):
            self._outcomes.record(dispatch.dispatch_id, "retryable_not_started")
            raise
        committed = False

        def mark_possibly_started() -> None:
            # CAS prepared -> possibly_started happens immediately before the submit
            # tools/call bytes are written; anything after this point durably stays
            # possibly-started even on transport failure, since the write may have landed.
            nonlocal committed
            self._outcomes.record(dispatch.dispatch_id, "possibly_started")
            committed = True

        result_holder: list[Mapping[str, Any]] = []
        try:
            result = self._execute(
                "submit", ctx, script, submit_payload,
                trusted=(source, executable), fixture_phase=fixture_phase,
                before_dispatch=mark_possibly_started, result_holder=result_holder,
            )
        except (AsideTransportError, AsideProtocolError):
            if result_holder:
                # The submit RPC was sent and produced a validated result before a
                # later cleanup step failed; preserve that durable evidence instead
                # of losing it to the propagating cleanup error.
                parsed = result_holder[0]
                parsed_pause = self._pause(parsed)
                if parsed_pause is not None:
                    self._outcomes.record(dispatch.dispatch_id, "manual_followup", pause_reason=parsed_pause.value)
                else:
                    self._outcomes.record(dispatch.dispatch_id, "possibly_started", receipt_id=parsed.get("receipt_id"))
            elif not committed:
                self._outcomes.record(dispatch.dispatch_id, "retryable_not_started")
            raise
        pause = self._pause(result)
        if pause is not None:
            # A challenge pause reported at submit is a durable, human-gated outcome:
            # never retryable, and never silently re-runnable as a different scenario.
            self._outcomes.record(dispatch.dispatch_id, "manual_followup", pause_reason=pause.value)
        else:
            self._outcomes.record(dispatch.dispatch_id, "possibly_started", receipt_id=result.get("receipt_id"))
        # Submit output is an attempt only; confirmation requires correlated observation.
        return SubmitOutcome(result["started"], False, result["manual_follow_up"], result.get("receipt_id"), pause)

    def _require_fill_evidence(self, ctx: AsideRunContext, script: ScriptRef, dispatch: DispatchIntent) -> None:
        if dispatch.field_digest is None:
            raise AsideProtocolError("submit requires a dispatch bound to durable fill evidence")
        evidence = self._fill_evidence.evidence(ctx, dispatch.run_id, dispatch.form_fingerprint)
        if evidence is None:
            raise AsideProtocolError("submit requires durable fill evidence")
        if (
            evidence.application_id != dispatch.application_id
            or evidence.field_digest != dispatch.field_digest
            or evidence.resume_sha256 != dispatch.resume_sha256
            or evidence.page_fingerprint != dispatch.page_fingerprint
            or evidence.form_fingerprint != dispatch.form_fingerprint
            or evidence.script_sha256 != script.sha256
            or evidence.executable_sha256 != self._approved_cli_sha256
        ):
            raise AsideProtocolError("dispatch does not reference durable fill evidence")

    def observe(self, ctx: AsideRunContext, script: ScriptRef, dispatch_id: str) -> StatusObservation:
        if not isinstance(dispatch_id, str) or not dispatch_id:
            raise AsideProtocolError("observe requires a dispatch intent ID")
        evidence = self._outcomes.evidence(ctx, dispatch_id)
        dispatch = evidence.dispatch
        if canonical_submit_payload_sha256(dispatch) != dispatch.payload_sha256:
            raise AsideProtocolError("durable dispatch payload digest drift")
        if evidence.state == "retryable_not_started":
            return StatusObservation("not_started", dispatch.page_fingerprint, dispatch.form_fingerprint)
        if evidence.state == "prepared":
            raise AsideTransportError("prepared dispatch is not observable")
        try:
            result = self._execute(
                "observe",
                ctx,
                script,
                {
                    **canonical_submit_payload(dispatch),
                    "intent_hmac": dispatch.intent_hmac,
                    "payload_sha256": dispatch.payload_sha256,
                },
            )
        except (AsideTransportError, AsideProtocolError):
            raise
        pause = self._pause(result)
        exact_confirmation = (
            pause is None
            and result["state"] == "confirmed"
            and result.get("receipt_id") is not None
            and evidence.receipt_digest == hashlib.sha256(str(result["receipt_id"]).encode()).hexdigest()
            and result["page_fingerprint"] == dispatch.page_fingerprint
            and result["form_fingerprint"] == dispatch.form_fingerprint
        )
        if evidence.state == "confirmed":
            if not exact_confirmation:
                raise AsideProtocolError("contradictory confirmed dispatch observation")
            return StatusObservation(
                "confirmed", dispatch.page_fingerprint, dispatch.form_fingerprint, str(result["receipt_id"])
            )
        if evidence.state == "manual_followup":
            if exact_confirmation:
                raise AsideProtocolError("contradictory manual-follow-up dispatch observation")
            if pause is not None:
                if evidence.pause_reason is not None and pause.value != evidence.pause_reason:
                    raise AsideProtocolError("contradictory manual-follow-up dispatch observation")
                return StatusObservation(
                    "awaiting_user", dispatch.page_fingerprint, dispatch.form_fingerprint, None, pause,
                )
            if result["state"] != "manual_follow_up":
                raise AsideProtocolError("contradictory manual-follow-up dispatch observation")
            return StatusObservation(
                "manual_follow_up", dispatch.page_fingerprint, dispatch.form_fingerprint
            )
        if exact_confirmation:
            if evidence.state == "possibly_started":
                self._outcomes.record(dispatch_id, "confirmed", receipt_id=str(result["receipt_id"]))
            return StatusObservation(
                "confirmed", dispatch.page_fingerprint, dispatch.form_fingerprint, str(result["receipt_id"])
            )
        if pause is not None:
            return StatusObservation(
                "awaiting_user", dispatch.page_fingerprint, dispatch.form_fingerprint, None, pause,
            )
        if evidence.state == "possibly_started":
            if result["state"] == "confirmed":
                raise AsideProtocolError("observed receipt does not match durable submit receipt")
            self._outcomes.record(dispatch_id, "manual_followup")
        return StatusObservation(
            "manual_follow_up", dispatch.page_fingerprint, dispatch.form_fingerprint,
        )

    def _execute(
        self,
        operation: str,
        ctx: AsideRunContext,
        script: ScriptRef,
        payload: Mapping[str, Any],
        *,
        trusted: tuple[str, str] | None = None,
        fixture_phase: str | None = None,
        before_dispatch: Callable[[], None] | None = None,
        result_holder: list[Mapping[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        source, executable = trusted or self._trusted_source(ctx, script)
        token = uuid4().hex
        payload_name = f"application-automation-{token}.json"
        marker = "/* APPLICATION_AUTOMATION_REQUEST */ null"
        input_marker = "const input = APPLICATION_AUTOMATION_REQUEST;"
        if source.count(marker) != 1 or source.count(input_marker) != 1:
            raise AsideProtocolError("script request marker drift")
        transport_payload = dict(payload)
        fixture_key = self._fixture_run_key(ctx)
        transport_payload.update(
            scenario=ctx.fixture_scenario,
            run_key=ctx.run_key,
            provider=ctx.provider,
            tenant=ctx.tenant,
            account_id_hmac=ctx.account_id_hmac,
            context_id_hmac=ctx.context_id_hmac,
            session_id_hmac=ctx.session_id_hmac,
            fixture_phase=fixture_phase or self._fixture_phase(ctx, transport_payload, fixture_key),
        )
        # Aside resolves the opaque filename inside its private agent root. Candidate
        # values stay in a mode-0600 file and never occur in argv or REPL source.
        request = json.dumps(
            {"operation": operation, "input": {"payload_path": payload_name, "token": token}},
            separators=(",", ":"),
        )
        prelude = (
            f'const applicationAutomationEnvelope = JSON.parse(await fs.readFile({json.dumps(payload_name)}, "utf8"));\n'
            'if (applicationAutomationEnvelope.token !== APPLICATION_AUTOMATION_REQUEST.input.token) '
            'throw new Error("application automation payload token mismatch");\n'
            'const applicationAutomationPayload = applicationAutomationEnvelope.payload;\n'
        )
        body = source.replace(marker, request).replace(
            input_marker,
            prelude
            + "const input = { ...APPLICATION_AUTOMATION_REQUEST, input: applicationAutomationPayload };",
        )
        result = self._call_repl(
            executable,
            body,
            operation,
            ctx.timeout_seconds,
            payload_name,
            transport_payload,
            token,
            before_dispatch=before_dispatch,
            result_holder=result_holder,
        )
        if result["domain"] not in script.allowed_domains:
            raise AsideProtocolError("unexpected domain")
        if result.get("pause_reason") is None:
            self._require_fingerprints(ctx, result["page_fingerprint"], result["form_fingerprint"])
        if fixture_key is not None:
            self._advance_fixture_run(operation, result, fixture_key)
        return result

    @staticmethod
    def _fixture_run_key(ctx: AsideRunContext) -> tuple[str, str, str, str, str, str]:
        return (ctx.provider, ctx.tenant, ctx.account_id_hmac, ctx.context_id_hmac, ctx.session_id_hmac, ctx.run_key)

    def _fixture_phase(
        self,
        ctx: AsideRunContext,
        payload: Mapping[str, Any],
        key: tuple[str, str, str, str, str, str],
    ) -> str:
        dispatch_id = payload.get("dispatch_id")
        if isinstance(dispatch_id, str) and dispatch_id:
            evidence = self._outcomes.evidence(ctx, dispatch_id)
            if evidence.state in {"possibly_started", "confirmed", "manual_followup"}:
                return "started"
            if evidence.state not in {"prepared", "retryable_not_started"}:
                raise AsideTransportError("unknown durable fixture lifecycle")
            # Never trust the mere existence of a prepared claim: the fixture lifecycle
            # phase is derived only from durable, independently recorded fill evidence.
            fill = self._fill_evidence.evidence(ctx, evidence.dispatch.run_id, evidence.dispatch.form_fingerprint)
            if fill is None or fill.field_digest != evidence.dispatch.field_digest:
                raise AsideTransportError("submit requires durable fill evidence")
            return "filled"
        return self._fixture_runs.get(key, "new")
    def _advance_fixture_run(
        self,
        operation: str,
        result: Mapping[str, Any],
        key: tuple[str, str, str, str, str, str],
    ) -> None:
        if operation == "inspect" and result.get("pause_reason") is None:
            self._fixture_runs[key] = "inspected"
        elif operation == "fill" and result.get("pause_reason") is None:
            self._fixture_runs[key] = "filled"
        elif operation == "submit" and result["started"]:
            self._fixture_runs[key] = "started"
    def _trusted_source(self, ctx: AsideRunContext, script: ScriptRef) -> tuple[str, str]:
        if script.script_id != "fixture-form" or script.allowed_domains != frozenset({"fixture.local"}):
            raise AsideProtocolError("MCP has fixture-form authority only")
        if not ctx.aside_version or ctx.aside_version != self._expected_version:
            raise AsideProtocolError("Aside version drift")
        if ctx.cli_path_sha256 != self._approved_cli_sha256:
            raise AsideProtocolError("caller executable hash does not match the approved anchor")
        expected_identity = (
            self._expected_account_id_hmac,
            self._expected_context_id_hmac,
            self._expected_session_id_hmac,
        )
        actual_identity = (ctx.account_id_hmac, ctx.context_id_hmac, ctx.session_id_hmac)
        if not all(_DIGEST.fullmatch(value or "") for value in expected_identity + actual_identity):
            raise AsideProtocolError("invalid account/context/session trust anchor")
        if actual_identity != expected_identity:
            raise AsideProtocolError("account/context/session identity drift")
        if (
            not isinstance(ctx.provider, str)
            or not ctx.provider
            or not isinstance(ctx.tenant, str)
            or not ctx.tenant
            or not isinstance(ctx.run_key, str)
            or not ctx.run_key
            or ctx.provider != self._expected_provider
            or ctx.tenant != self._expected_tenant
        ):
            raise AsideProtocolError("provider, tenant, or run identity drift")
        live = self._identity_attestor.attest(ctx)
        live_identity = (
            live.account_id_hmac,
            live.context_id_hmac,
            live.session_id_hmac,
            live.provider_hmac,
            live.tenant_hmac,
        )
        if not all(_DIGEST.fullmatch(value or "") for value in live_identity):
            raise AsideProtocolError("live MCP identity attestation is malformed")
        if live_identity[:3] != expected_identity or live.provider_hmac != self._expected_provider_hmac or live.tenant_hmac != self._expected_tenant_hmac:
            raise AsideProtocolError("live MCP identity attestation drift")
        executable = self._cli.verify_executable()
        if executable != self._approved_path:
            raise AsideProbeError("Aside executable path drift")
        expected_hash = self._registry.get((script.script_id, script.version))
        if expected_hash is None or expected_hash != script.sha256 or not _DIGEST.fullmatch(script.sha256):
            raise AsideProtocolError("script is not registry-owned")
        try:
            with script.path.open("rb") as stream:
                source_bytes = stream.read()
            source = source_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise AsideProtocolError("script is unavailable") from error
        if hashlib.sha256(source_bytes).hexdigest() != script.sha256:
            raise AsideProtocolError("script hash drift")
        return source, executable

    @staticmethod
    def _write_payload(payload: Mapping[str, Any], token: str, path: Path) -> None:
        descriptor = -1
        created = False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
            created = True
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump({"token": token, "payload": payload}, stream, separators=(",", ":"))
        except BaseException:
            try:
                if descriptor >= 0:
                    os.close(descriptor)
            finally:
                if created:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            raise

    @staticmethod
    def _remove_payload(path: Path, token: str) -> None:
        try:
            details = path.lstat()
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != (stat.S_IRUSR | stat.S_IWUSR)
            ):
                raise AsideTransportError("Aside MCP payload ownership drift")
            with path.open(encoding="utf-8") as stream:
                envelope = json.load(stream)
            if not isinstance(envelope, Mapping) or envelope.get("token") != token:
                raise AsideTransportError("Aside MCP payload ownership drift")
            path.unlink()
        except FileNotFoundError:
            raise AsideTransportError("Aside MCP payload disappeared before cleanup")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise AsideTransportError("Aside MCP payload cleanup failed") from error
    @staticmethod
    def _reap_payloads(parent: Path, timeout: float) -> None:
        cutoff = time.time() - max(timeout, 30.0)
        try:
            for path in parent.iterdir():
                match = _PAYLOAD_NAME.fullmatch(path.name)
                if match is None or path.stat().st_mtime > cutoff:
                    continue
                AsideMcpAdapter._remove_payload(path, match.group(1))
        except AsideTransportError:
            raise
        except OSError as error:
            raise AsideTransportError("Aside MCP payload reap failed") from error

    def _call_repl(
        self,
        executable: str,
        program: str,
        operation: str,
        timeout: float,
        payload_name: str,
        payload: Mapping[str, Any],
        token: str,
        *,
        before_dispatch: Callable[[], None] | None = None,
        result_holder: list[Mapping[str, Any]] | None = None,
    ) -> Mapping[str, Any]:

        if timeout <= 0:
            raise AsideTransportError("invalid MCP timeout")
        environment = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}
        deadline = time.monotonic() + timeout
        process: subprocess.Popen[str] | None = None
        payload_path: Path | None = None
        payload_created = False
        primary_error: BaseException | None = None
        try:
            process = subprocess.Popen(
                (executable, "mcp"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=environment,
            )
            assert process.stdin is not None and process.stdout is not None
            initialized = self._rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "application-automation", "version": "1"},
                    },
                },
                deadline,
            )
            self._validate_initialize(initialized)
            self._notify(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = self._rpc(
                process,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                deadline,
            )
            self._validate_tools(tools)
            resolved = self._rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "repl",
                        "arguments": {
                            "title": "Preparing secure application data",
                            "code": f"console.log(await fs.resolvePath({json.dumps(payload_name)}))",
                        },
                    },
                },
                deadline,
            )
            payload_path = self._validate_payload_path(
                self._tool_text(resolved, "payload path"),
                payload_name,
            )
            self._reap_payloads(payload_path.parent, timeout)
            self._write_payload(payload, token, payload_path)
            payload_created = True
            if before_dispatch is not None:
                before_dispatch()
            response = self._rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "repl",
                        "arguments": {"title": _MCP_TITLES[operation], "code": program},
                    },
                },
                deadline,
            )
            parsed = self._parse_result(self._tool_text(response, "REPL"), operation)
            if result_holder is not None:
                # Preserve the validated result even if cleanup below fails and this
                # call ultimately raises: the caller can still durably record it.
                result_holder.append(parsed)
            return parsed
        except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as error:
            primary_error = AsideTransportError("Aside MCP transport failed")
            raise primary_error from error
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_errors: list[AsideTransportError] = []
            if payload_created and payload_path is not None:
                try:
                    self._remove_payload(payload_path, token)
                except AsideTransportError as error:
                    cleanup_errors.append(error)
            if process is not None:
                try:
                    self._teardown(process, deadline)
                except AsideTransportError as error:
                    cleanup_errors.append(error)
            if cleanup_errors:
                if primary_error is not None:
                    for error in cleanup_errors:
                        primary_error.add_note(f"cleanup failure: {error}")
                else:
                    cleanup_error = cleanup_errors[0]
                    for error in cleanup_errors[1:]:
                        cleanup_error.add_note(f"additional cleanup failure: {error}")
                    raise cleanup_error

    def _validate_initialize(self, response: Mapping[str, Any]) -> None:
        result = response.get("result")
        server = result.get("serverInfo") if isinstance(result, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or result.get("protocolVersion") != "2024-11-05"
            or not isinstance(server, Mapping)
            or server.get("name") != "aside"
            or server.get("version") != self._expected_version
        ):
            raise AsideProtocolError("Aside MCP identity drift")

    @staticmethod
    def _validate_tools(response: Mapping[str, Any]) -> None:
        result = response.get("result")
        tools = result.get("tools") if isinstance(result, Mapping) else None
        if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], Mapping):
            raise AsideProtocolError("Aside MCP tool registry drift")
        schema = tools[0].get("inputSchema")
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        required = schema.get("required") if isinstance(schema, Mapping) else None
        if (
            tools[0].get("name") != "repl"
            or not isinstance(schema, Mapping)
            or schema.get("type") != "object"
            or required != ["title", "code"]
            or not isinstance(properties, Mapping)
            or set(properties) != {"title", "code"}
            or any(
                not isinstance(properties[name], Mapping)
                or properties[name].get("type") != "string"
                for name in ("title", "code")
            )
        ):
            raise AsideProtocolError("Aside MCP REPL schema drift")

    @staticmethod
    def _tool_text(response: Mapping[str, Any], label: str) -> str:
        result = response.get("result")
        content = result.get("content") if isinstance(result, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or result.get("isError") is not False
            or not isinstance(content, list)
            or len(content) != 1
            or not isinstance(content[0], Mapping)
            or content[0].get("type") != "text"
            or not isinstance(content[0].get("text"), str)
        ):
            raise AsideTransportError(f"Aside MCP returned an invalid {label} response")
        text = content[0]["text"]
        try:
            output_size = len(text.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise AsideTransportError(f"Aside MCP {label} output is not valid UTF-8") from error
        if output_size > _MAX_REPL_OUTPUT_BYTES:
            raise AsideTransportError(f"Aside MCP {label} output exceeds the limit")
        return text

    @staticmethod
    def _validate_payload_path(output: str, payload_name: str) -> Path:
        if "\n" in output or "\r" in output:
            raise AsideProtocolError("Aside MCP payload path drift")
        path = Path(output)
        if not path.is_absolute() or path.name != payload_name:
            raise AsideProtocolError("Aside MCP payload path drift")
        resolved = path.resolve(strict=False)
        aside_root = (Path.home() / ".aside").resolve(strict=True)
        try:
            resolved.relative_to(aside_root)
        except ValueError as error:
            raise AsideProtocolError("Aside MCP payload path escaped its root") from error
        parent = resolved.parent
        details = parent.stat()
        if (
            not parent.is_dir()
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise AsideProtocolError("Aside MCP payload root is not private")
        return resolved

    @staticmethod
    def _notify(process: subprocess.Popen[str], notification: Mapping[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(notification, separators=(",", ":")) + "\n")
        process.stdin.flush()

    @staticmethod
    def _rpc(process: subprocess.Popen[str], request: Mapping[str, Any], deadline: float) -> Mapping[str, Any]:
        assert process.stdin is not None
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        while True:
            line = AsideMcpAdapter._read_line(process, deadline)
            if not line:
                raise AsideTransportError("Aside MCP closed without a response")
            response = json.loads(line)
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                raise AsideTransportError("Aside MCP sent an invalid JSON-RPC message")
            if "id" not in response:
                raise AsideTransportError("Aside MCP sent an unexpected notification")
            if response.get("id") != request["id"] or "error" in response or not isinstance(response.get("result"), Mapping):
                raise AsideTransportError("Aside MCP rejected request")
            return response

    @staticmethod
    def _read_line(process: subprocess.Popen[str], deadline: float) -> str:
        assert process.stdout is not None
        chunks: list[bytes] = []
        descriptor = process.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AsideTransportError("Aside MCP response timed out")
            ready, _, _ = select.select([descriptor], [], [], remaining)
            if not ready:
                raise AsideTransportError("Aside MCP response timed out")
            chunk = os.read(descriptor, 1)
            if not chunk:
                try:
                    return b"".join(chunks).decode("utf-8")
                except UnicodeDecodeError as error:
                    raise AsideTransportError("Aside MCP sent invalid UTF-8") from error
            if chunk == b"\n":
                try:
                    return b"".join(chunks).decode("utf-8")
                except UnicodeDecodeError as error:
                    raise AsideTransportError("Aside MCP sent invalid UTF-8") from error
            chunks.append(chunk)
            if len(chunks) > _MAX_RPC_LINE_BYTES:
                raise AsideTransportError("Aside MCP response exceeds the limit")

    @staticmethod
    def _teardown(process: subprocess.Popen[str], deadline: float) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
            return
        except subprocess.TimeoutExpired:
            pass
        process.kill()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as error:
            raise AsideTransportError("Aside MCP process could not be reaped") from error

    @staticmethod
    def _parse_result(output: str, operation: str) -> Mapping[str, Any]:
        try:
            output_size = len(output.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise AsideProtocolError("REPL output is not valid UTF-8") from error
        if output_size > _MAX_REPL_OUTPUT_BYTES:
            raise AsideProtocolError("REPL output exceeds the limit")
        prefix = "APPLICATION_AUTOMATION_RESULT:"
        result: str | None = None
        for line in output.splitlines():
            normalized = _ANSI_ESCAPE.sub("", line)
            if not normalized:
                continue
            if normalized.startswith(prefix):
                if result is not None:
                    raise AsideProtocolError("expected exactly one result prefix")
                result = normalized[len(prefix):]
            elif _ASIDE_STATUS_LINE.fullmatch(normalized) is None:
                raise AsideProtocolError("unexpected non-control REPL output")
        if result is None:
            raise AsideProtocolError("expected exactly one result prefix")
        try:
            return decode_result(json.loads(result), operation)
        except json.JSONDecodeError as error:
            raise AsideProtocolError("invalid JSON result") from error

    @staticmethod
    def _resume_digest(plan: FillPlan) -> str | None:
        if plan.resume_path is None:
            if plan.resume_sha256 is not None:
                raise AsideProtocolError("resume hash supplied without an attachment")
            return None
        if not _DIGEST.fullmatch(plan.resume_sha256 or ""):
            raise AsideProtocolError("attachment requires a SHA-256")
        try:
            with plan.resume_path.open("rb") as stream:
                actual = hashlib.sha256(stream.read()).hexdigest()
        except OSError as error:
            raise AsideProtocolError("resume attachment is unavailable") from error
        if actual != plan.resume_sha256:
            raise AsideProtocolError("resume attachment hash mismatch")
        return actual

    @staticmethod
    def _require_fingerprints(ctx: AsideRunContext, page: str, form: str) -> None:
        if page != ctx.expected_page_fingerprint or form != ctx.expected_form_fingerprint:
            raise AsideProtocolError("page or form fingerprint drift")

    @staticmethod
    def _pause(result: Mapping[str, Any]) -> PauseReason | None:
        value = result.get("pause_reason")
        return PauseReason(value) if value is not None else None
