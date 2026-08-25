from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from application_automation.adapters.aside_fixture import (
    FIXTURE_DOMAIN,
    FIXTURE_FORM_FINGERPRINT,
    FIXTURE_PAGE_FINGERPRINT,
    FIXTURE_SCRIPT_PATH,
    AsideFixtureAdapter,
)
from application_automation.adapters.base import AsideCliAdapter, AsideProbeError, AsideProtocolError
from application_automation.adapters.mcp import (
    AsideMcpAdapter,
    LiveAsideIdentity,
    SQLiteFixtureFillEvidenceLedger,
    SQLiteFixtureOutcomeLedger,
    canonical_submit_payload_sha256,
)
from application_automation.aside import (
    AsideRunContext,
    AsideTransportError,
    DispatchIntent,
    FillPlan,
    PauseReason,
    ScriptRef,
    canonical_field_digest,
    decode_result,
)

_DIGEST = "d" * 64
_ACCOUNT = "a" * 64
_CONTEXT = "b" * 64
_SESSION = "c" * 64
_PROVIDER = "e" * 64
_TENANT = "f" * 64

_FIXTURE_SCRIPT_SHA256 = "28b3dc4c7a7ef4cb3b1462371baa55a8dd1a870287c5bc54f1ee74c3c1c8b8b3"
_EMPTY_FIELD_DIGEST = canonical_field_digest({})


def script_ref() -> ScriptRef:
    return ScriptRef(
        "fixture-form", "v1", FIXTURE_SCRIPT_PATH,
        hashlib.sha256(FIXTURE_SCRIPT_PATH.read_bytes()).hexdigest(),
        frozenset({FIXTURE_DOMAIN}),
    )


def context(scenario: str = "happy", run_key: str = "run-1") -> AsideRunContext:
    return AsideRunContext(
        "fixture-aside-v1", _DIGEST, _ACCOUNT, _CONTEXT, _SESSION, "fixture", "fixture",
        FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT, run_key, fixture_scenario=scenario,
    )


def dispatch(
    dispatch_id: str = "dispatch-1",
    *,
    application_id: str = "application-1",
    session_id: str = "session-1",
    run_id: str = "run-1",
    intent_hmac: str = "1" * 64,
    resume_sha256: str | None = None,
    field_digest: str | None = _EMPTY_FIELD_DIGEST,
) -> DispatchIntent:
    intent = DispatchIntent(
        dispatch_id, application_id, session_id, run_id, intent_hmac,
        "", FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT, resume_sha256, field_digest,
    )
    return replace(intent, payload_sha256=canonical_submit_payload_sha256(intent))


class Attestor:
    def __init__(self, identity: LiveAsideIdentity | None = None) -> None:
        self.identity = identity or LiveAsideIdentity(_ACCOUNT, _CONTEXT, _SESSION, _PROVIDER, _TENANT)

    def attest(self, _ctx: AsideRunContext) -> LiveAsideIdentity:
        return self.identity


def ledger_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("""
        CREATE TABLE fixture_dispatch_outcomes (
          dispatch_id TEXT PRIMARY KEY, application_id TEXT NOT NULL, provider TEXT NOT NULL,
          tenant TEXT NOT NULL, account_hmac TEXT NOT NULL, context_hmac TEXT NOT NULL,
          session_id TEXT NOT NULL, session_hmac TEXT NOT NULL, run_id TEXT NOT NULL,
          intent_hmac TEXT NOT NULL, payload_sha256 TEXT NOT NULL, page_fingerprint TEXT NOT NULL,
          form_fingerprint TEXT NOT NULL, resume_present INTEGER NOT NULL DEFAULT 0,
          resume_sha256 TEXT, field_digest TEXT, state TEXT NOT NULL,
          receipt_digest TEXT, attestation_digest TEXT, observed_intent_hmac TEXT, pause_reason TEXT,
          prepared_at TEXT NOT NULL, started_at TEXT, confirmed_at TEXT, terminal_at TEXT,
          revision INTEGER NOT NULL, UNIQUE(application_id, intent_hmac)
        )
    """)
    connection.execute("""
        CREATE TABLE fixture_fill_evidence (
          dispatch_binding TEXT, run_id TEXT NOT NULL, application_id TEXT NOT NULL,
          session_hmac TEXT NOT NULL, page_fingerprint TEXT NOT NULL, form_fingerprint TEXT NOT NULL,
          field_digest TEXT NOT NULL, resume_present INTEGER NOT NULL, resume_sha256 TEXT,
          script_sha256 TEXT NOT NULL, executable_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
          revision INTEGER NOT NULL, PRIMARY KEY (run_id, form_fingerprint)
        )
    """)
    return connection


def seed_fill_evidence(
    connection: sqlite3.Connection, ctx: AsideRunContext, dispatch: DispatchIntent,
) -> None:
    """Durably record the fill evidence a real ``fill()`` call would have produced."""
    assert dispatch.field_digest is not None
    SQLiteFixtureFillEvidenceLedger(connection).record(
        ctx,
        run_id=dispatch.run_id,
        application_id=dispatch.application_id,
        page_fingerprint=dispatch.page_fingerprint,
        form_fingerprint=dispatch.form_fingerprint,
        field_digest=dispatch.field_digest,
        resume_sha256=dispatch.resume_sha256,
        script_sha256=_FIXTURE_SCRIPT_SHA256,
        executable_sha256=_DIGEST,
    )


def mcp_adapter(database: sqlite3.Connection, *, attestor: Attestor | None = None) -> AsideMcpAdapter:
    return AsideMcpAdapter(
        "/fixture/aside", _DIGEST, "fixture-aside-v1",
        {("fixture-form", "v1"): _FIXTURE_SCRIPT_SHA256},
        expected_account_id_hmac=_ACCOUNT, expected_context_id_hmac=_CONTEXT,
        expected_session_id_hmac=_SESSION, expected_provider="fixture", expected_tenant="fixture",
        expected_provider_hmac=_PROVIDER, expected_tenant_hmac=_TENANT,
        identity_attestor=attestor or Attestor(), outcome_ledger=SQLiteFixtureOutcomeLedger(database),
        fill_evidence_ledger=SQLiteFixtureFillEvidenceLedger(database),
    )



def test_cli_executable_hash_is_constructor_owned(tmp_path: Path) -> None:
    executable = tmp_path / "aside"
    executable.write_bytes(b"fixture executable")
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    expected = hashlib.sha256(executable.read_bytes()).hexdigest()

    adapter = AsideCliAdapter(str(executable), "fixture-aside-v1", expected)
    assert adapter.verify_executable() == str(executable)

    executable.write_bytes(b"drifted executable")
    with pytest.raises(AsideProbeError, match="hash drift"):
        adapter.verify_executable()

    with pytest.raises(AsideProbeError, match="not configured"):
        AsideCliAdapter(str(executable), "fixture-aside-v1")

    with pytest.raises(AsideProbeError, match="version pin"):
        AsideCliAdapter(str(executable), "", expected)
def test_unpinned_cli_discovery_never_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "application_automation.adapters.base.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("CLI probe"),
    )
    with pytest.raises(AsideProbeError, match="SHA-256 pin"):
        AsideCliAdapter("aside", "fixture-aside-v1", None).doctor()


def test_ledger_preserves_absent_and_empty_resume_digests(tmp_path: Path) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    ledger = SQLiteFixtureOutcomeLedger(connection)
    absent = dispatch("absent", application_id="absent-application")
    empty = dispatch(
        "empty",
        application_id="empty-application",
        resume_sha256=hashlib.sha256(b"").hexdigest(),
    )
    ledger.claim(context(), absent)
    ledger.claim(context(), empty)
    assert ledger.evidence(context(), absent.dispatch_id).dispatch.resume_sha256 is None
    assert ledger.evidence(context(), empty.dispatch_id).dispatch.resume_sha256 == hashlib.sha256(b"").hexdigest()


def test_post_claim_prelaunch_failure_is_retryable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    intent = dispatch()
    seed_fill_evidence(connection, context(), intent)
    source = FIXTURE_SCRIPT_PATH.read_text()
    calls = 0

    def trusted(*_args: object) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AsideProbeError("pin drift")
        return source, "/fixture/aside"

    monkeypatch.setattr(adapter, "_trusted_source", trusted)
    with pytest.raises(AsideProbeError, match="pin drift"):
        adapter.submit(context(), script_ref(), intent)
    assert SQLiteFixtureOutcomeLedger(connection).evidence(context(), "dispatch-1").state == "retryable_not_started"


def test_terminal_observation_contradictions_do_not_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    intent = dispatch()
    ledger = SQLiteFixtureOutcomeLedger(connection)
    ledger.claim(context(), intent)
    ledger.record(intent.dispatch_id, "possibly_started", receipt_id="receipt")
    ledger.record(intent.dispatch_id, "confirmed", receipt_id="receipt")
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")
    monkeypatch.setattr(
        adapter,
        "_call_repl",
        lambda *_args, **_kwargs: result("observe", "happy", state="manual_follow_up", receipt_id=None),
    )
    with pytest.raises(AsideProtocolError, match="contradictory confirmed"):
        adapter.observe(context(), script_ref(), intent.dispatch_id)
    assert ledger.evidence(context(), intent.dispatch_id).state == "confirmed"


def test_restart_observe_uses_durable_fixture_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.sqlite"
    connection = ledger_database(path)
    intent = dispatch()
    ledger = SQLiteFixtureOutcomeLedger(connection)
    ledger.claim(context(), intent)
    ledger.record(intent.dispatch_id, "possibly_started", receipt_id="receipt")
    connection.close()
    adapter = mcp_adapter(sqlite3.connect(path))
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")

    def observed(
        _executable: str, _program: str, operation: str, _timeout: float, _name: str,
        payload: dict[str, Any], _token: str, **_kwargs: Any,
    ) -> dict[str, Any]:
        assert operation == "observe"
        assert payload["fixture_phase"] == "started"
        return result("observe", "happy", state="confirmed", receipt_id="receipt")

    monkeypatch.setattr(adapter, "_call_repl", observed)
    assert adapter.observe(context(), script_ref(), intent.dispatch_id).state == "confirmed"

def result(operation: str, scenario: str, **values: Any) -> dict[str, Any]:
    pause = None if scenario in {"happy", "ambiguous"} else PauseReason(scenario)
    page, form = FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT
    if pause is PauseReason.FORM_DRIFT:
        form = "fixture-form-drift"
    elif pause is PauseReason.POSTING_DRIFT:
        page = "fixture-posting-drift"
    elif pause is PauseReason.UNEXPECTED_REDIRECT:
        page = "fixture-page-drift"
    output: dict[str, Any] = {
        "schema": "application_automation.aside.v1", "operation": operation,
        "domain": FIXTURE_DOMAIN, "page_fingerprint": page, "form_fingerprint": form, **values,
    }
    if pause is not None:
        output["pause_reason"] = pause.value
    return dict(decode_result(output, operation))


def scripted_mcp(monkeypatch: pytest.MonkeyPatch, adapter: AsideMcpAdapter) -> None:
    """Mock only transport: preflight, pinning, ledger, and JS request construction stay live."""
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")
    phases: dict[str, str] = {}

    def call_repl(
        _executable: str, _program: str, operation: str, _timeout: float, _name: str,
        payload: dict[str, Any], _token: str, *,
        before_dispatch: Any = None, result_holder: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if operation == "submit" and before_dispatch is not None:
            before_dispatch()
        scenario = str(payload["scenario"])
        key = str(payload["run_key"])
        if operation == "inspect":
            phases[key] = "inspected"
            return result("inspect", scenario, fields=["name", "email", "resume"])
        if operation == "fill":
            if scenario == "happy" and phases.get(key) != "inspected":
                raise AsideProtocolError("fill requires inspection")
            if scenario not in {"happy", "ambiguous"}:
                return result("fill", scenario, filled=False, attached_resume_sha256=None)
            phases[key] = "filled"
            return result("fill", scenario, filled=True, attached_resume_sha256=payload.get("resume_sha256"))
        if operation == "submit":
            assert {
                "scenario",
                "run_key",
                "provider",
                "tenant",
                "account_id_hmac",
                "context_id_hmac",
                "session_id_hmac",
                "fixture_phase",
                "dispatch_id",
                "application_id",
                "session_id",
                "run_id",
                "intent_hmac",
                "payload_sha256",
                "page_fingerprint",
                "form_fingerprint",
                "resume_sha256",
            } <= set(payload)
            if scenario not in {"happy", "ambiguous"}:
                return result("submit", scenario, started=False, confirmed=False, manual_follow_up=False, receipt_id=None)
            phases[key] = "started"
            if scenario == "ambiguous":
                return result("submit", scenario, started=True, confirmed=False, manual_follow_up=True, receipt_id=None)
            return result("submit", scenario, started=True, confirmed=True, manual_follow_up=False, receipt_id="fixture-receipt-v1")
        if operation == "observe":
            assert {
                "dispatch_id",
                "application_id",
                "session_id",
                "run_id",
                "intent_hmac",
                "payload_sha256",
                "page_fingerprint",
                "form_fingerprint",
                "resume_sha256",
            } <= set(payload)
            if scenario in {"captcha", "mfa"}:
                return result("observe", scenario, state="awaiting_user", receipt_id=None)
            if scenario == "happy" and phases.get(key) == "started":
                return result("observe", scenario, state="confirmed", receipt_id="fixture-receipt-v1")
            return result("observe", scenario, state="manual_follow_up", receipt_id=None)
        raise AssertionError(f"unexpected MCP operation: {operation}")

    monkeypatch.setattr(adapter, "_call_repl", call_repl)


@pytest.mark.parametrize("scenario", ["happy", "ambiguous", *[reason.value for reason in PauseReason]])
def test_normalized_fixture_and_scripted_mcp_conformance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str,
) -> None:
    """The Python fixture and the fully scripted MCP/JS boundary share every safe outcome."""
    reference = script_ref()
    direct = AsideFixtureAdapter()
    database = ledger_database(tmp_path / "ledger.sqlite")
    mcp = mcp_adapter(database)
    scripted_mcp(monkeypatch, mcp)
    ctx = context(scenario, f"{scenario}-run")
    intent = dispatch(
        f"{scenario}-dispatch",
        application_id=f"{scenario}-application",
        run_id=ctx.run_key,
        intent_hmac=hashlib.sha256(scenario.encode()).hexdigest(),
    )

    direct_inspect, mcp_inspect = direct.inspect(ctx, reference), mcp.inspect(ctx, reference)
    assert (direct_inspect.domain, direct_inspect.page_fingerprint, direct_inspect.form_fingerprint, direct_inspect.pause_reason) == (mcp_inspect.domain, mcp_inspect.page_fingerprint, mcp_inspect.form_fingerprint, mcp_inspect.pause_reason)
    plan = FillPlan({}, application_id=intent.application_id)
    direct_fill, mcp_fill = direct.fill(ctx, reference, plan), mcp.fill(ctx, reference, plan)
    assert (direct_fill.filled, direct_fill.pause_reason, direct_fill.page_fingerprint, direct_fill.form_fingerprint) == (mcp_fill.filled, mcp_fill.pause_reason, mcp_fill.page_fingerprint, mcp_fill.form_fingerprint)
    if scenario not in {"happy", "ambiguous"}:
        # A paused fill never produces durable fill evidence, so the MCP path's
        # write-ahead authority correctly refuses to submit; only the pure Python
        # fixture (no durable ledger) still reports the paused attempt directly.
        direct_submit = direct.submit(ctx, reference, intent)
        assert direct_submit.started is False
        assert direct.observe(ctx, reference, intent.dispatch_id).state == "awaiting_user"
        with pytest.raises(AsideProtocolError, match="fill evidence"):
            mcp.submit(ctx, reference, intent)
        with pytest.raises(AsideTransportError, match="unknown"):
            mcp.observe(ctx, reference, intent.dispatch_id)
    else:
        direct_submit, mcp_submit = direct.submit(ctx, reference, intent), mcp.submit(ctx, reference, intent)
        assert direct_submit.started == mcp_submit.started
        assert not mcp_submit.confirmed
        assert direct.observe(ctx, reference, intent.dispatch_id).state == mcp.observe(ctx, reference, intent.dispatch_id).state
    database.close()
def test_mcp_transport_payloads_are_exact_and_resume_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    database = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(database)
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")
    reference = script_ref()
    ctx = context("happy", "run-sentinel")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"resume-sentinel")
    resume_sha256 = hashlib.sha256(b"resume-sentinel").hexdigest()
    field_digest = canonical_field_digest({"name": "Candidate Sentinel", "email": "candidate@example.test"})
    canonical = {
        "dispatch_id": "dispatch-sentinel",
        "application_id": "application-sentinel",
        "session_id": "session-sentinel",
        "run_id": "run-sentinel",
        "page_fingerprint": FIXTURE_PAGE_FINGERPRINT,
        "form_fingerprint": FIXTURE_FORM_FINGERPRINT,
        "resume_sha256": resume_sha256,
        "field_digest": field_digest,
    }
    intent_hmac = "9" * 64
    payload_sha256 = hashlib.sha256(
        json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    intent = DispatchIntent(
        "dispatch-sentinel", "application-sentinel", "session-sentinel", "run-sentinel",
        intent_hmac, payload_sha256, FIXTURE_PAGE_FINGERPRINT, FIXTURE_FORM_FINGERPRINT,
        resume_sha256, field_digest,
    )
    base = {
        "scenario": "happy",
        "run_key": "run-sentinel",
        "provider": "fixture",
        "tenant": "fixture",
        "account_id_hmac": _ACCOUNT,
        "context_id_hmac": _CONTEXT,
        "session_id_hmac": _SESSION,
    }
    expected = {
        "inspect": {**base, "fixture_phase": "new"},
        "fill": {
            **base,
            "fixture_phase": "inspected",
            "fields": {"name": "Candidate Sentinel", "email": "candidate@example.test"},
            "resume_path": str(resume),
            "resume_sha256": resume_sha256,
        },
        "submit": {
            **base,
            "fixture_phase": "filled",
            **canonical,
            "intent_hmac": intent_hmac,
            "payload_sha256": payload_sha256,
        },
        "observe": {
            **base,
            "fixture_phase": "started",
            **canonical,
            "intent_hmac": intent_hmac,
            "payload_sha256": payload_sha256,
        },
    }

    def call_repl(
        _executable: str, _program: str, operation: str, _timeout: float, _name: str,
        payload: dict[str, Any], _token: str, **_kwargs: Any,
    ) -> dict[str, Any]:
        assert payload == expected[operation]
        if operation == "inspect":
            return result("inspect", "happy", fields=["name", "email", "resume"])
        if operation == "fill":
            return result("fill", "happy", filled=True, attached_resume_sha256=resume_sha256)
        if operation == "submit":
            return result(
                "submit", "happy", started=True, confirmed=True, manual_follow_up=False,
                receipt_id="fixture-receipt-v1",
            )
        if operation == "observe":
            return result("observe", "happy", state="confirmed", receipt_id="fixture-receipt-v1")
        raise AssertionError(f"unexpected MCP operation: {operation}")

    monkeypatch.setattr(adapter, "_call_repl", call_repl)
    adapter.inspect(ctx, reference)
    adapter.fill(
        ctx, reference,
        FillPlan(
            {"name": "Candidate Sentinel", "email": "candidate@example.test"}, resume, resume_sha256,
            application_id="application-sentinel",
        ),
    )
    adapter.submit(ctx, reference, intent)
    assert adapter.observe(ctx, reference, intent.dispatch_id).state == "confirmed"
    database.close()


@pytest.mark.parametrize(
    "ctx",
    [
        replace(context(), account_id_hmac=""),
        replace(context(), context_id_hmac="A" * 64),
        replace(context(), session_id_hmac="not-a-digest"),
        replace(context(), run_key=""),
    ],
)
def test_fixture_rejects_malformed_isolation_identities(ctx: AsideRunContext) -> None:
    with pytest.raises(AsideProtocolError, match="identity|trust anchor"):
        AsideFixtureAdapter().inspect(ctx, script_ref())


def test_fixture_rejects_resume_and_dispatch_digest_mismatch(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"fixture resume")
    adapter = AsideFixtureAdapter()
    adapter.inspect(context(), script_ref())
    with pytest.raises(AsideProtocolError, match="hash mismatch"):
        adapter.fill(context(), script_ref(), FillPlan({}, resume, "0" * 64))
    adapter.fill(context(), script_ref(), FillPlan({}))
    with pytest.raises(AsideProtocolError, match="trust anchor"):
        adapter.submit(context(), script_ref(), replace(dispatch(), payload_sha256="invalid"))


@pytest.mark.parametrize("adapter_kind", ["fixture", "mcp"])
def test_observe_before_submit_is_not_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter_kind: str) -> None:
    reference, ctx = script_ref(), context(run_key=f"before-{adapter_kind}")
    if adapter_kind == "fixture":
        adapter = AsideFixtureAdapter()
    else:
        adapter = mcp_adapter(ledger_database(tmp_path / "ledger.sqlite"))
        scripted_mcp(monkeypatch, adapter)
    adapter.inspect(ctx, reference)
    if adapter_kind == "fixture":
        assert adapter.observe(ctx, reference, "unknown").state == "not_started"
    else:
        with pytest.raises(AsideTransportError, match="unknown"):
            adapter.observe(ctx, reference, "unknown")


def test_sqlite_ledger_reopens_rejects_new_id_and_enforces_terminal_transitions(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    first = ledger_database(path)
    ledger = SQLiteFixtureOutcomeLedger(first)
    intent = dispatch()
    assert ledger.claim(context(), intent) == "prepared"
    ledger.record(intent.dispatch_id, "possibly_started", receipt_id="receipt")
    first.close()

    reopened = SQLiteFixtureOutcomeLedger(sqlite3.connect(path))
    assert reopened.evidence(context(), intent.dispatch_id).state == "possibly_started"
    with pytest.raises(AsideTransportError, match="canonical"):
        reopened.claim(context(), replace(intent, dispatch_id="dispatch-2"))
    reopened.record(intent.dispatch_id, "confirmed", receipt_id="receipt")
    assert reopened.evidence(context(), intent.dispatch_id).state == "confirmed"
    with pytest.raises(AsideTransportError, match="observe-only"):
        reopened.record(intent.dispatch_id, "manual_followup")


def test_mcp_preflight_failure_before_dispatch_write_is_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.sqlite"
    connection = ledger_database(path)
    adapter = mcp_adapter(connection)
    intent = dispatch()
    seed_fill_evidence(connection, context(), intent)
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")
    calls = 0

    def fails_before_write(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise AsideTransportError("handshake timeout")

    monkeypatch.setattr(adapter, "_call_repl", fails_before_write)
    with pytest.raises(AsideTransportError, match="handshake timeout"):
        adapter.submit(context(), script_ref(), intent)
    assert calls == 1
    assert SQLiteFixtureOutcomeLedger(connection).evidence(context(), intent.dispatch_id).state == "retryable_not_started"
    connection.close()
    replay = mcp_adapter(sqlite3.connect(path))
    scripted_mcp(monkeypatch, replay)
    # A retryable outcome (nothing was ever written) can be resubmitted from a fresh adapter.
    assert replay.submit(context(), script_ref(), intent).started is True


def test_mcp_cas_before_submit_bytes_keeps_possibly_started_on_later_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.sqlite"
    connection = ledger_database(path)
    adapter = mcp_adapter(connection)
    intent = dispatch()
    seed_fill_evidence(connection, context(), intent)
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")

    def fails_after_write(
        _executable: str, _program: str, _operation: str, _timeout: float, _name: str,
        _payload: dict[str, Any], _token: str, *, before_dispatch: Any = None, result_holder: Any = None,
    ) -> dict[str, Any]:
        if before_dispatch is not None:
            before_dispatch()
        raise AsideTransportError("connection reset")

    monkeypatch.setattr(adapter, "_call_repl", fails_after_write)
    with pytest.raises(AsideTransportError, match="connection reset"):
        adapter.submit(context(), script_ref(), intent)
    # The CAS to possibly-started ran before the submit bytes were sent, so the
    # ambiguous outcome after that point durably stays possibly-started, never
    # falling back to retryable (which would imply nothing had been attempted).
    assert SQLiteFixtureOutcomeLedger(connection).evidence(context(), intent.dispatch_id).state == "possibly_started"
    connection.close()
    replay = mcp_adapter(sqlite3.connect(path))
    scripted_mcp(monkeypatch, replay)
    with pytest.raises(AsideTransportError, match="observe-only"):
        replay.submit(context(), script_ref(), intent)
    assert replay.observe(context(), script_ref(), intent.dispatch_id).state == "manual_follow_up"


def test_submit_time_challenge_pause_is_durable_manual_followup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    scripted_mcp(monkeypatch, adapter)
    ctx, intent = context("captcha"), dispatch()
    adapter.inspect(ctx, script_ref())
    adapter.fill(ctx, script_ref(), FillPlan({}, application_id=intent.application_id))
    outcome = adapter.submit(ctx, script_ref(), intent)
    assert outcome.pause_reason is PauseReason.CAPTCHA
    evidence = SQLiteFixtureOutcomeLedger(connection).evidence(ctx, intent.dispatch_id)
    assert (evidence.state, evidence.pause_reason) == ("manual_followup", "captcha")
    # A challenge pause is terminal and human-gated: the same dispatch can never
    # be retried automatically, unlike a plain preflight failure.
    with pytest.raises(AsideTransportError, match="observe-only"):
        adapter.submit(ctx, script_ref(), intent)


def test_pause_then_happy_retry_is_rejected_per_application(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    scripted_mcp(monkeypatch, adapter)
    ctx, intent = context("captcha"), dispatch()
    adapter.inspect(ctx, script_ref())
    adapter.fill(ctx, script_ref(), FillPlan({}, application_id=intent.application_id))
    assert adapter.submit(ctx, script_ref(), intent).pause_reason is PauseReason.CAPTCHA
    assert SQLiteFixtureOutcomeLedger(connection).evidence(ctx, intent.dispatch_id).state == "manual_followup"
    # A different HMAC/dispatch for the same application, attempted as "happy",
    # must never be allowed to silently supersede the paused, human-gated outcome.
    retry_ctx = context("happy", run_key="run-2")
    retry_intent = dispatch(
        "dispatch-2", application_id=intent.application_id, intent_hmac="2" * 64, run_id="run-2",
    )
    adapter.inspect(retry_ctx, script_ref())
    adapter.fill(retry_ctx, script_ref(), FillPlan({}, application_id=retry_intent.application_id))
    with pytest.raises(AsideTransportError, match="canonical"):
        adapter.submit(retry_ctx, script_ref(), retry_intent)


@pytest.mark.parametrize("mutation", ["domain", "page_fingerprint", "form_fingerprint", "state", "receipt_id", "intent_hmac"])
def test_public_observe_rejects_inexact_provider_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str,
) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    intent = dispatch()
    ledger = SQLiteFixtureOutcomeLedger(connection)
    ledger.claim(context(), intent)
    ledger.record(intent.dispatch_id, "possibly_started", receipt_id="receipt")
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")

    def observed(*_args: object, **_kwargs: object) -> dict[str, Any]:
        values: dict[str, Any] = {"state": "confirmed", "receipt_id": "receipt"}
        if mutation == "domain":
            values["domain"] = "wrong.local"
        elif mutation == "page_fingerprint":
            values["page_fingerprint"] = "wrong-page"
        elif mutation == "form_fingerprint":
            values["form_fingerprint"] = "wrong-form"
        elif mutation == "state":
            values["state"] = "manual_follow_up"
        elif mutation == "receipt_id":
            values["receipt_id"] = "wrong-receipt"
        return result("observe", "happy", **values)
    if mutation == "intent_hmac":
        connection.execute(
            "UPDATE fixture_dispatch_outcomes SET intent_hmac=? WHERE dispatch_id=?",
            ("0" * 64, intent.dispatch_id),
        )

    monkeypatch.setattr(adapter, "_call_repl", observed)
    with pytest.raises(AsideTransportError, match="unknown"):
        adapter.observe(context(), script_ref(), f"wrong-{mutation}")
    if mutation == "intent_hmac":
        with pytest.raises(AsideTransportError, match="binding drift"):
            adapter.observe(context(), script_ref(), intent.dispatch_id)
    else:
        expected_errors = {
            "domain": "unexpected domain",
            "page_fingerprint": "page or form fingerprint drift",
            "form_fingerprint": "page or form fingerprint drift",
            "state": "non-confirmed observation cannot have a receipt",
            "receipt_id": "observed receipt does not match durable submit receipt",
        }
        with pytest.raises(AsideProtocolError, match=expected_errors[mutation]):
            adapter.observe(context(), script_ref(), intent.dispatch_id)
        assert ledger.evidence(context(), intent.dispatch_id).state == "possibly_started"


@pytest.mark.parametrize("script,ctx", [
    (ScriptRef("other", "v1", FIXTURE_SCRIPT_PATH, "0" * 64, frozenset({FIXTURE_DOMAIN})), context()),
    (replace(script_ref(), sha256="0" * 64), context()),
    (script_ref(), replace(context(), provider="other")),
    (script_ref(), replace(context(), tenant="other")),
    (ScriptRef("fixture-form", "v1", FIXTURE_SCRIPT_PATH, hashlib.sha256(FIXTURE_SCRIPT_PATH.read_bytes()).hexdigest(), frozenset({"evil.local"})), context()),
])
def test_nonfixture_authority_is_rejected_before_ledger_or_popen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: ScriptRef, ctx: AsideRunContext) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")
    monkeypatch.setattr(adapter, "_call_repl", lambda *_a, **_k: pytest.fail("transport"))
    with pytest.raises(AsideProtocolError):
        adapter.submit(ctx, script, dispatch())
    assert connection.execute("SELECT count(*) FROM fixture_dispatch_outcomes").fetchone()[0] == 0


@pytest.mark.parametrize("identity", [
    LiveAsideIdentity("x" * 64, _CONTEXT, _SESSION, _PROVIDER, _TENANT),
    LiveAsideIdentity(_ACCOUNT, _CONTEXT, _SESSION, "not-a-digest", _TENANT),
])
def test_immutable_executable_and_live_identity_fail_closed_before_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, identity: LiveAsideIdentity,
) -> None:
    adapter = mcp_adapter(ledger_database(tmp_path / "ledger.sqlite"), attestor=Attestor(identity))
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: pytest.fail("executable verification"))
    with pytest.raises(AsideProtocolError, match="attestation"):
        adapter.inspect(context(), script_ref())


def test_executable_hash_and_path_drift_are_stable_protocol_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter = mcp_adapter(ledger_database(tmp_path / "ledger.sqlite"))
    with pytest.raises(AsideProtocolError, match="caller executable hash"):
        adapter.inspect(replace(context(), cli_path_sha256="0" * 64), script_ref())
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/other/aside")
    with pytest.raises(AsideProbeError, match="path drift"):
        adapter.inspect(context(), script_ref())


@pytest.mark.parametrize("failure", ["open", "chmod", "serialization"])
def test_payload_creation_failures_do_not_expose_candidate_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    payload_path = tmp_path / f"application-automation-{'a' * 32}.json"
    token = "a" * 32
    descriptors: list[int] = []
    original_open = os.open

    def track_open(*args: object, **kwargs: object) -> int:
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    if failure == "open":
        monkeypatch.setattr(
            "application_automation.adapters.mcp.os.open",
            lambda *_: (_ for _ in ()).throw(OSError("open")),
        )
    else:
        monkeypatch.setattr("application_automation.adapters.mcp.os.open", track_open)
        if failure == "chmod":
            monkeypatch.setattr(
                "application_automation.adapters.mcp.os.fchmod",
                lambda *_: (_ for _ in ()).throw(OSError("chmod")),
            )
        else:
            def partial_dump(value: object, stream: Any, **_kwargs: object) -> None:
                stream.write(json.dumps(value))
                stream.flush()
                raise ValueError("serialization")

            monkeypatch.setattr("application_automation.adapters.mcp.json.dump", partial_dump)

    with pytest.raises((OSError, ValueError)):
        AsideMcpAdapter._write_payload({"name": "Private Candidate"}, token, payload_path)

    assert not payload_path.exists()
    assert not list(tmp_path.glob("application-automation-*.json"))
    assert all(
        b"Private Candidate" not in entry.read_bytes()
        for entry in tmp_path.iterdir()
        if entry.is_file()
    )
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
def test_teardown_failure_is_a_typed_transport_error() -> None:
    process = type(
        "Process",
        (),
        {
            "poll": lambda _self: None,
            "terminate": lambda _self: None,
            "wait": lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("aside", 0)),
            "kill": lambda _self: None,
        },
    )()
    with pytest.raises(AsideTransportError, match="reaped"):
        AsideMcpAdapter._teardown(process, 0)


def test_payload_cleanup_and_stale_sweep_require_owner_token_and_0600(tmp_path: Path) -> None:
    token = "a" * 32
    path = tmp_path / f"application-automation-{token}.json"
    path.write_text(json.dumps({"token": token, "payload": {}}))
    path.chmod(0o644)
    with pytest.raises(AsideTransportError, match="ownership drift"):
        AsideMcpAdapter._remove_payload(path, token)
    path.chmod(0o600)
    stale = tmp_path / f"application-automation-{'b' * 32}.json"
    stale.write_text(json.dumps({"token": "wrong", "payload": {}}))
    stale.chmod(0o600)
    os.utime(stale, (0, 0))
    with pytest.raises(AsideTransportError, match="ownership drift"):
        AsideMcpAdapter._reap_payloads(tmp_path, 1)


def test_decode_result_uses_stable_protocol_errors_for_malformed_inputs() -> None:
    with pytest.raises(AsideProtocolError, match="unknown result schema"):
        decode_result({}, "inspect")
    with pytest.raises(AsideProtocolError, match="unknown pause"):
        decode_result({"schema": "application_automation.aside.v1", "operation": "inspect", "domain": FIXTURE_DOMAIN, "page_fingerprint": "p", "form_fingerprint": "f", "fields": ["x"], "pause_reason": "bogus"}, "inspect")
def test_doctor_hash_pin_precedes_probes_and_version_drift_stops_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AsideCliAdapter("/fixture/aside", "fixture-aside-v1", _DIGEST)
    probes: list[tuple[str, ...]] = []
    monkeypatch.setattr(adapter, "verify_executable", lambda: (_ for _ in ()).throw(AsideProbeError("hash drift")))
    monkeypatch.setattr(adapter, "_probe", lambda command: probes.append(command) or (None, None))
    with pytest.raises(AsideProbeError, match="hash drift"):
        adapter.doctor()
    assert probes == []

    monkeypatch.setattr(adapter, "verify_executable", lambda: "/fixture/aside")
    monkeypatch.setattr(adapter, "_probe", lambda command: probes.append(command) or ("wrong-version", None))
    result = adapter.doctor()
    assert result.detail == "version_drift"
    assert probes == [("/fixture/aside", "--version")]


def test_ledger_existing_prepared_is_exclusive_and_retry_reopen_binds_all_anchors(tmp_path: Path) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    ledger = SQLiteFixtureOutcomeLedger(connection)
    intent = dispatch()
    assert ledger.claim(context(), intent) == "prepared"
    with pytest.raises(AsideTransportError, match="observe-only"):
        ledger.claim(context(), intent)
    ledger.record(intent.dispatch_id, "retryable_not_started")
    with pytest.raises(AsideTransportError, match="binding drift"):
        ledger.claim(replace(context(), account_id_hmac="0" * 64), intent)
    assert ledger.evidence(context(), intent.dispatch_id).state == "retryable_not_started"
    assert ledger.claim(context(), intent) == "prepared"


@pytest.mark.parametrize(
    "ctx_mutation,intent_mutation",
    [
        ({"provider": "other"}, {}),
        ({"tenant": "other"}, {}),
        ({}, {"run_id": "other-run"}),
        ({}, {"payload_sha256": "0" * 64}),
    ],
)
def test_both_adapters_reject_fixture_dispatch_anchor_drift_before_submit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ctx_mutation: dict[str, str], intent_mutation: dict[str, str],
) -> None:
    ctx = replace(context(), **ctx_mutation)
    intent = replace(dispatch(), **intent_mutation)
    fixture = AsideFixtureAdapter()
    mcp_connection = ledger_database(tmp_path / "ledger.sqlite")
    mcp = mcp_adapter(mcp_connection)
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")
    monkeypatch.setattr(mcp, "_call_repl", lambda *_a, **_k: pytest.fail("transport"))
    for adapter in (fixture, mcp):
        with pytest.raises(AsideProtocolError):
            adapter.submit(ctx, script_ref(), intent)
    assert mcp_connection.execute("SELECT count(*) FROM fixture_dispatch_outcomes").fetchone()[0] == 0


@pytest.mark.parametrize("scenario", ["captcha", "mfa"])
def test_mcp_observe_preserves_awaiting_user_pause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str,
) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    scripted_mcp(monkeypatch, adapter)
    ctx, intent = context(scenario), dispatch()
    ledger = SQLiteFixtureOutcomeLedger(connection)
    ledger.claim(ctx, intent)
    ledger.record(intent.dispatch_id, "possibly_started")
    observed = adapter.observe(ctx, script_ref(), intent.dispatch_id)
    assert (observed.state, observed.pause_reason) == ("awaiting_user", PauseReason(scenario))


def test_mcp_observe_preserves_transport_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    intent = dispatch()
    ledger = SQLiteFixtureOutcomeLedger(connection)
    ledger.claim(context(), intent)
    ledger.record(intent.dispatch_id, "possibly_started")
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")
    monkeypatch.setattr(adapter, "_call_repl", lambda *_a, **_k: (_ for _ in ()).throw(AsideTransportError("observe failed")))
    with pytest.raises(AsideTransportError, match="observe failed"):
        adapter.observe(context(), script_ref(), intent.dispatch_id)
    assert ledger.evidence(context(), intent.dispatch_id).state == "possibly_started"
def test_call_repl_propagates_cleanup_failure_without_primary_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    adapter = mcp_adapter(ledger_database(tmp_path / "ledger.sqlite"))
    payload_path = tmp_path / f"application-automation-{'a' * 32}.json"
    process = type("Process", (), {"stdin": type("Input", (), {"write": lambda *_: 0, "flush": lambda *_: None})(), "stdout": object()})()
    monkeypatch.setattr("application_automation.adapters.mcp.subprocess.Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(adapter, "_rpc", lambda *_a, **_k: {})
    monkeypatch.setattr(adapter, "_validate_initialize", lambda *_: None)
    monkeypatch.setattr(adapter, "_validate_tools", lambda *_: None)
    monkeypatch.setattr(adapter, "_tool_text", lambda _response, label: str(payload_path) if label == "payload path" else "{}")
    monkeypatch.setattr(adapter, "_validate_payload_path", lambda *_: payload_path)
    monkeypatch.setattr(adapter, "_reap_payloads", lambda *_: None)
    monkeypatch.setattr(adapter, "_parse_result", lambda *_: {"domain": FIXTURE_DOMAIN})
    monkeypatch.setattr(adapter, "_remove_payload", lambda *_: (_ for _ in ()).throw(AsideTransportError("delete failed")))
    monkeypatch.setattr(adapter, "_teardown", lambda *_: None)
    with pytest.raises(AsideTransportError, match="delete failed"):
        adapter._call_repl("/fixture/aside", "", "inspect", 1, payload_path.name, {}, "a" * 32)


def test_call_repl_preserves_primary_error_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    adapter = mcp_adapter(ledger_database(tmp_path / "ledger.sqlite"))
    payload_path = tmp_path / f"application-automation-{'b' * 32}.json"
    process = type("Process", (), {"stdin": type("Input", (), {"write": lambda *_: 0, "flush": lambda *_: None})(), "stdout": object()})()
    monkeypatch.setattr("application_automation.adapters.mcp.subprocess.Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(adapter, "_rpc", lambda *_a, **_k: {})
    monkeypatch.setattr(adapter, "_validate_initialize", lambda *_: None)
    monkeypatch.setattr(adapter, "_validate_tools", lambda *_: None)
    monkeypatch.setattr(adapter, "_tool_text", lambda _response, label: str(payload_path) if label == "payload path" else "{}")
    monkeypatch.setattr(adapter, "_validate_payload_path", lambda *_: payload_path)
    monkeypatch.setattr(adapter, "_reap_payloads", lambda *_: None)
    monkeypatch.setattr(adapter, "_parse_result", lambda *_: (_ for _ in ()).throw(AsideTransportError("primary failed")))
    monkeypatch.setattr(adapter, "_remove_payload", lambda *_: (_ for _ in ()).throw(AsideTransportError("delete failed")))
    monkeypatch.setattr(adapter, "_teardown", lambda *_: (_ for _ in ()).throw(AsideTransportError("teardown failed")))
    with pytest.raises(AsideTransportError, match="primary failed") as captured:
        adapter._call_repl("/fixture/aside", "", "inspect", 1, payload_path.name, {}, "b" * 32)
    assert "cleanup failure: delete failed" in captured.value.__notes__
    assert "cleanup failure: teardown failed" in captured.value.__notes__



def test_submit_preserves_parsed_result_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    intent = dispatch()
    seed_fill_evidence(connection, context(), intent)
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")

    def call_repl_with_cleanup_failure(
        _executable: str, _program: str, operation: str, _timeout: float, _name: str,
        _payload: dict[str, Any], _token: str, *,
        before_dispatch: Any = None, result_holder: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        assert operation == "submit"
        if before_dispatch is not None:
            before_dispatch()
        parsed = result(
            "submit", "happy", started=True, confirmed=True, manual_follow_up=False,
            receipt_id="fixture-receipt-v1",
        )
        if result_holder is not None:
            result_holder.append(parsed)
        raise AsideTransportError("payload cleanup failed")

    monkeypatch.setattr(adapter, "_call_repl", call_repl_with_cleanup_failure)
    with pytest.raises(AsideTransportError, match="payload cleanup failed"):
        adapter.submit(context(), script_ref(), intent)
    evidence = SQLiteFixtureOutcomeLedger(connection).evidence(context(), intent.dispatch_id)
    assert evidence.state == "possibly_started"
    assert evidence.receipt_digest == hashlib.sha256(b"fixture-receipt-v1").hexdigest()


def test_submit_preserves_parsed_pause_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    intent = dispatch()
    seed_fill_evidence(connection, context("captcha"), intent)
    monkeypatch.setattr(AsideCliAdapter, "verify_executable", lambda *_: "/fixture/aside")

    def call_repl_with_cleanup_failure(
        _executable: str, _program: str, operation: str, _timeout: float, _name: str,
        _payload: dict[str, Any], _token: str, *,
        before_dispatch: Any = None, result_holder: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        assert operation == "submit"
        if before_dispatch is not None:
            before_dispatch()
        parsed = result(
            "submit", "captcha", started=False, confirmed=False, manual_follow_up=False, receipt_id=None,
        )
        if result_holder is not None:
            result_holder.append(parsed)
        raise AsideTransportError("payload cleanup failed")

    monkeypatch.setattr(adapter, "_call_repl", call_repl_with_cleanup_failure)
    with pytest.raises(AsideTransportError, match="payload cleanup failed"):
        adapter.submit(context("captcha"), script_ref(), intent)
    evidence = SQLiteFixtureOutcomeLedger(connection).evidence(context("captcha"), intent.dispatch_id)
    assert (evidence.state, evidence.pause_reason) == ("manual_followup", "captcha")


def test_record_cas_retries_through_concurrent_revision_change(tmp_path: Path) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    ledger = SQLiteFixtureOutcomeLedger(connection)
    intent = dispatch()
    ledger.claim(context(), intent)

    class RacyConnection:
        def __init__(self, real: sqlite3.Connection, dispatch_id: str) -> None:
            self._real = real
            self._dispatch_id = dispatch_id
            self._raced = False

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            if not self._raced and "SET state=?, receipt_digest=?" in sql:
                self._raced = True
                # A concurrent writer advances the revision between our read and write.
                self._real.execute(
                    "UPDATE fixture_dispatch_outcomes SET revision=revision+1 WHERE dispatch_id=?",
                    (self._dispatch_id,),
                )
            return self._real.execute(sql, parameters)

        def __enter__(self) -> "RacyConnection":
            self._real.__enter__()
            return self

        def __exit__(self, *exc: object) -> None:
            self._real.__exit__(*exc)

    racy = RacyConnection(connection, intent.dispatch_id)
    ledger._connection = racy  # noqa: SLF001 - inject contention below the public API
    ledger.record(intent.dispatch_id, "possibly_started", receipt_id="receipt")
    assert racy._raced is True
    assert SQLiteFixtureOutcomeLedger(connection).evidence(context(), intent.dispatch_id).state == "possibly_started"


def test_fresh_mcp_adapter_recovers_durable_fill_evidence_across_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.sqlite"
    connection = ledger_database(path)
    adapter = mcp_adapter(connection)
    scripted_mcp(monkeypatch, adapter)
    ctx, intent = context(), dispatch()
    adapter.inspect(ctx, script_ref())
    adapter.fill(ctx, script_ref(), FillPlan({}, application_id=intent.application_id))
    connection.close()
    # Simulate a crash: a brand-new adapter, with fresh in-memory fixture-phase
    # tracking, must still be able to submit using only durable fill evidence.
    fresh = mcp_adapter(sqlite3.connect(path))
    scripted_mcp(monkeypatch, fresh)
    outcome = fresh.submit(ctx, script_ref(), intent)
    assert outcome.started is True


def test_fresh_mcp_adapter_cannot_submit_without_durable_fill_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    connection = ledger_database(tmp_path / "ledger.sqlite")
    adapter = mcp_adapter(connection)
    scripted_mcp(monkeypatch, adapter)
    ctx = context(run_key="never-filled")
    intent = dispatch(run_id="never-filled")
    with pytest.raises(AsideProtocolError, match="fill evidence"):
        adapter.submit(ctx, script_ref(), intent)
    assert connection.execute("SELECT count(*) FROM fixture_dispatch_outcomes").fetchone()[0] == 0
