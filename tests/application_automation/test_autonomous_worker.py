from __future__ import annotations

import hashlib
import socket
import threading
import time
from pathlib import Path
import pytest

from fastapi.testclient import TestClient
from application_automation.adapters.aside_fixture import AsideFixtureAdapter

from application_automation.api import create_app
from application_automation.orchestrator import ApplicationOrchestrator, OrchestrationError
from application_automation.store import (
    ServiceInstanceLockError,
    apply_migrations,
    connect,
)


def _catalog(tmp_path: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for role_id in ("first", "second"):
        directory = tmp_path / role_id
        directory.mkdir()
        (directory / "resume.md").write_text(role_id, encoding="utf-8")
        (directory / "resume.docx").write_bytes(b"fixture docx")
        resume = directory / "resume.pdf"
        resume.write_bytes(b"%PDF-fixture")
        result[role_id] = {
            "score": 8,
            "location": "Vancouver, BC",
            "posting_active": True,
            "remote": False,
            "remote_country": None,
            "automation_status": "materials_ready",
            "canonical_identity": f"fixture:{role_id}",
            "application_dir": str(directory),
            "material_path": str(resume),
            "material_sha256": hashlib.sha256(resume.read_bytes()).hexdigest(),
        }
    return result


def _orchestrator(
    tmp_path: Path, catalog: dict[str, dict[str, object]] | None = None
) -> ApplicationOrchestrator:
    database = connect(tmp_path / "worker.sqlite")
    apply_migrations(database)
    orchestrator = ApplicationOrchestrator(
        database, fixture_mode=True, catalog=_catalog(tmp_path) if catalog is None else catalog
    )
    orchestrator.sync_catalog()
    return orchestrator


def test_run_next_claims_accepted_commands_in_durable_order(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    first = orchestrator.queue("first", "dry_run", "first")
    second = orchestrator.queue("second", "dry_run", "second")

    assert orchestrator.run_next() == orchestrator.command(first["id"])
    assert orchestrator.run_next() == orchestrator.command(second["id"])
    assert orchestrator.run_next() is None
    assert orchestrator.command(first["id"])["state"] == "completed"
    assert orchestrator.command(second["id"])["state"] == "completed"
def test_run_next_ownership_race_has_one_owner_and_no_loser_side_effects(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    first = _orchestrator(tmp_path, catalog)
    command = first.queue("first", "batch", "ownership-race")
    second = ApplicationOrchestrator(
        connect(tmp_path / "worker.sqlite"),
        fixture_mode=True,
        catalog=catalog,
    )
    second.sync_catalog()
    barrier = threading.Barrier(2)
    results: list[dict[str, object] | None] = []
    errors: list[BaseException] = []

    def run_next(worker: ApplicationOrchestrator) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(worker.run_next())
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run_next, args=(worker,)) for worker in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    completed = [result for result in results if result is not None]
    assert len(completed) == 1
    assert completed[0]["id"] == command["id"]
    assert completed[0]["state"] == "completed"
    assert first.connection.execute(
        "SELECT COUNT(*) FROM runs WHERE command_id=?", (command["id"],)
    ).fetchone()[0] == 1
    assert first.connection.execute(
        "SELECT COUNT(*) FROM dispatches WHERE application_id=?", (command["application_id"],)
    ).fetchone()[0] == 1
    assert first.connection.execute(
        "SELECT COUNT(*) FROM daily_quota_reservations WHERE application_id=?",
        (command["application_id"],),
    ).fetchone()[0] == 1
    second.connection.close()
    first.connection.close()


def test_run_next_fails_poisoned_preflight_before_processing_later_commands(
    tmp_path: Path, monkeypatch
) -> None:
    orchestrator = _orchestrator(tmp_path)
    first = orchestrator.queue("first", "batch", "poison")
    second = orchestrator.queue("second", "dry_run", "valid")
    (tmp_path / "first" / "resume.pdf").write_bytes(b"changed after queueing")
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("corrupt preflight reached adapter")

    monkeypatch.setattr("application_automation.orchestrator.AsideFixtureAdapter", forbidden)

    failed = orchestrator.run_next()
    monkeypatch.setattr("application_automation.orchestrator.AsideFixtureAdapter", AsideFixtureAdapter)
    completed = orchestrator.run_next()

    assert failed is not None
    assert failed["id"] == first["id"]
    assert failed["state"] == "failed"
    assert completed is not None
    assert completed["id"] == second["id"]
    assert completed["state"] == "completed"
    assert orchestrator.connection.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE application_id=?", (first["application_id"],)
    ).fetchone()[0] == 1
    assert orchestrator.connection.execute("SELECT COUNT(*) FROM dispatches WHERE application_id=?", (first["application_id"],)).fetchone()[0] == 0
    assert orchestrator.connection.execute("SELECT COUNT(*) FROM daily_quota_reservations WHERE application_id=?", (first["application_id"],)).fetchone()[0] == 0
    evidence_kinds = [
        row[0] for row in orchestrator.connection.execute(
            "SELECT kind FROM evidence WHERE application_id=? ORDER BY ledger_sequence",
            (first["application_id"],),
        )
    ]
    assert evidence_kinds == ["pause"]
    assert orchestrator.run_next() is None


@pytest.mark.parametrize(
    "idle_seconds",
    [-0.001, 0, float("nan"), float("inf"), float("-inf")],
)
def test_worker_rejects_nonfinite_or_nonpositive_idle_delay_before_startup(
    tmp_path: Path, idle_seconds: float,
) -> None:
    database = connect(tmp_path / "invalid-worker.sqlite")
    apply_migrations(database)
    writes_before = database.total_changes

    with pytest.raises(ValueError, match="worker_idle_seconds must be a finite positive number"):
        create_app(
            database,
            fixture_mode=True,
            catalog=_catalog(tmp_path),
            autonomous_worker=True,
            worker_idle_seconds=idle_seconds,
        )

    assert database.total_changes == writes_before
    database.close()


def test_worker_accepts_positive_idle_delay_boundary(tmp_path: Path) -> None:
    database = connect(tmp_path / "positive-worker.sqlite")
    apply_migrations(database)

    app = create_app(
        database,
        fixture_mode=True,
        catalog=_catalog(tmp_path),
        autonomous_worker=True,
        worker_idle_seconds=0.000_001,
    )

    assert app.state.worker_enabled is True
    database.close()



def test_fixture_lifespan_worker_idles_and_cancels_cleanly(tmp_path: Path, monkeypatch) -> None:
    database = connect(tmp_path / "service.sqlite")
    apply_migrations(database)
    idle_called = threading.Event()

    def idle(self: ApplicationOrchestrator) -> None:
        idle_called.set()
        return None

    monkeypatch.setattr(ApplicationOrchestrator, "run_next", idle)
    app = create_app(database, fixture_mode=True, catalog=_catalog(tmp_path), autonomous_worker=True, worker_idle_seconds=0.001)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert app.state.worker_task is not None
        assert idle_called.wait(timeout=10)
        assert app.state.worker_error is None
        assert client.get("/api/v1/health").json() == {"status": "ok", "worker": "running"}
    assert app.state.worker_task is None


def test_fixture_worker_records_safe_orchestration_errors(tmp_path: Path, monkeypatch) -> None:
    database = connect(tmp_path / "worker-error.sqlite")
    apply_migrations(database)
    observed = threading.Event()

    def failing(self: ApplicationOrchestrator) -> None:
        observed.set()
        raise OrchestrationError("fixture worker preflight rejected")

    monkeypatch.setattr(ApplicationOrchestrator, "run_next", failing)
    app = create_app(database, fixture_mode=True, catalog=_catalog(tmp_path), autonomous_worker=True)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        task = app.state.worker_task
        assert task is not None
        assert observed.wait(timeout=10)
        # Poll task.done() instead of add_done_callback: registering a done
        # callback from the test thread does not reliably wake the portal's
        # event loop, so the callback can sit unscheduled while the loop idles.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not task.done():
            time.sleep(0.05)
        assert task.done()
        assert app.state.worker_error == "fixture worker unavailable"
        assert app.state.worker_failure == {
            "code": "fixture_worker_run_failed",
            "class": "OrchestrationError",
        }
        assert task.done()
        assert client.get("/api/v1/health").json() == {
            "status": "degraded",
            "worker": "unavailable",
        }
    assert app.state.worker_task is None
def test_crashed_worker_disables_queueing(tmp_path: Path, monkeypatch) -> None:
    database = connect(tmp_path / "worker-queue.sqlite")
    apply_migrations(database)
    failed = threading.Event()

    def failing(self: ApplicationOrchestrator) -> None:
        failed.set()
        raise RuntimeError("credential-like detail must not escape")

    monkeypatch.setattr(ApplicationOrchestrator, "run_next", failing)
    app = create_app(
        database,
        bootstrap_token="bootstrap",
        fixture_mode=True,
        catalog=_catalog(tmp_path),
        autonomous_worker=True,
    )
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert failed.wait(timeout=10)
        response = client.post(
            "/app/v1/bootstrap",
            headers={
                "origin": "http://127.0.0.1",
                "content-type": "application/x-www-form-urlencoded",
            },
            content="token=bootstrap",
        )
        assert response.status_code == 200
        session = client.get("/api/v1/session").json()
        assert session["worker"] == {
            "state": "unavailable",
            "automatic_progress": False,
            "can_queue": False,
        }
        assert client.get("/api/v1/snapshot").json()["worker"] == session["worker"]
        rejected = client.post(
            "/api/v1/roles/first/commands",
            headers={
                "origin": "http://127.0.0.1",
                "x-csrf-token": session["csrf_token"],
                "idempotency-key": "after-crash",
            },
            json={"mode": "dry_run", "idempotency_key": "after-crash"},
        )
        assert rejected.status_code == 409
        assert rejected.json() == {"detail": "fixture worker unavailable"}
        assert database.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
        assert app.state.worker_failure == {
            "code": "fixture_worker_run_failed",
            "class": "RuntimeError",
        }
    database.close()
def test_real_mode_never_starts_a_worker_or_runs_next(tmp_path: Path, monkeypatch) -> None:
    database = connect(tmp_path / "real.sqlite")
    apply_migrations(database)

    def forbidden(self: ApplicationOrchestrator) -> None:
        raise AssertionError("real mode must not call the worker")

    monkeypatch.setattr(ApplicationOrchestrator, "run_next", forbidden)
    app = create_app(database, fixture_mode=False, catalog=_catalog(tmp_path), autonomous_worker=True)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert app.state.worker_task is None
        assert client.get("/api/v1/health").json() == {"status": "ok", "worker": "manual"}


def test_run_next_rejects_real_mode_without_executor_authority(tmp_path: Path) -> None:
    database = connect(tmp_path / "real-orchestrator.sqlite")
    apply_migrations(database)
    orchestrator = ApplicationOrchestrator(database, fixture_mode=False)
    try:
        orchestrator.run_next()
    except OrchestrationError as error:
        assert str(error) == "real execution has no active authority"
    else:
        raise AssertionError("real mode run_next must be denied")


def test_real_mode_reopen_rejects_run_of_fixture_queued_command_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue a command through a fixture-mode orchestrator, then reopen the same DB with a
    real-mode (fixture_mode=False) orchestrator and call run(command_id) directly: it must be
    rejected before touching any adapter/network authority or mutating any row."""
    fixture_orchestrator = _orchestrator(tmp_path)
    command = fixture_orchestrator.queue("first", "batch", "reopen-real")
    fixture_orchestrator.connection.close()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("real mode attempted an external call")

    monkeypatch.setattr("application_automation.orchestrator.AsideFixtureAdapter", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    real_database = connect(tmp_path / "worker.sqlite")
    real_orchestrator = ApplicationOrchestrator(real_database, fixture_mode=False, catalog=fixture_orchestrator.catalog)

    with pytest.raises(OrchestrationError, match="^real execution has no active authority$"):
        real_orchestrator.run(command["id"])

    assert real_database.execute(
        "SELECT state FROM commands WHERE id=?", (command["id"],)
    ).fetchone()[0] == "accepted"
    assert real_database.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert real_database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    assert real_database.execute("SELECT COUNT(*) FROM daily_quota_reservations").fetchone()[0] == 0
    assert real_database.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    real_database.close()
def test_second_service_cannot_recover_while_another_service_owns_database(
    tmp_path: Path,
) -> None:
    first_database = connect(tmp_path / "shared.sqlite")
    second_database = connect(tmp_path / "shared.sqlite")
    apply_migrations(first_database)
    catalog = _catalog(tmp_path)
    first_app = create_app(
        first_database,
        fixture_mode=True,
        catalog=catalog,
        autonomous_worker=True,
        worker_idle_seconds=60,
    )
    second_app = create_app(
        second_database,
        fixture_mode=True,
        catalog=catalog,
        autonomous_worker=False,
    )

    with TestClient(
        first_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ):
        assert first_app.state.worker_task is not None
        with pytest.raises(ServiceInstanceLockError, match="service instance lock is unavailable"):
            with TestClient(
                second_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)
            ):
                pass

    first_database.close()
    second_database.close()


def test_stale_command_recovers_only_after_service_lock_releases(tmp_path: Path) -> None:
    first_database = connect(tmp_path / "recovery.sqlite")
    second_database = connect(tmp_path / "recovery.sqlite")
    apply_migrations(first_database)
    catalog = _catalog(tmp_path)
    first_app = create_app(
        first_database,
        fixture_mode=True,
        catalog=catalog,
        autonomous_worker=False,
    )
    second_app = create_app(
        second_database,
        fixture_mode=True,
        catalog=catalog,
        autonomous_worker=False,
    )

    with TestClient(
        first_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ):
        command = first_app.state.orchestrator.queue("first", "dry_run", "stale")
        first_database.execute(
            "UPDATE commands SET state='running' WHERE id=?", (command["id"],)
        )
        first_database.execute(
            "UPDATE applications SET state='filling' WHERE id=?",
            (command["application_id"],),
        )

        with pytest.raises(ServiceInstanceLockError):
            with TestClient(
                second_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)
            ):
                pass
        assert first_app.state.orchestrator.command(command["id"])["state"] == "running"

    with TestClient(
        second_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)
    ):
        assert second_app.state.orchestrator.command(command["id"])["state"] == "paused"

    first_database.close()
    second_database.close()


def test_worker_shutdown_waits_for_inflight_run_before_clearing_state(
    tmp_path: Path, monkeypatch
) -> None:
    database = connect(tmp_path / "inflight.sqlite")
    apply_migrations(database)
    started = threading.Event()
    allow_completion = threading.Event()
    completed = threading.Event()
    calls: list[None] = []

    def blocked_run_next(self: ApplicationOrchestrator) -> None:
        calls.append(None)
        started.set()
        assert allow_completion.wait(timeout=10)
        completed.set()
        return None

    monkeypatch.setattr(ApplicationOrchestrator, "run_next", blocked_run_next)
    app = create_app(
        database,
        fixture_mode=True,
        catalog=_catalog(tmp_path),
        autonomous_worker=True,
        worker_idle_seconds=60,
    )
    client = TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    )
    client.__enter__()
    assert started.wait(timeout=10)

    shutdown_complete = threading.Event()
    shutdown = threading.Thread(
        target=lambda: (client.__exit__(None, None, None), shutdown_complete.set())
    )
    shutdown.start()
    assert not shutdown_complete.wait(timeout=0.05)
    assert app.state.worker_task is not None

    allow_completion.set()
    shutdown.join(timeout=1)
    assert shutdown_complete.is_set()
    assert completed.is_set()
    assert calls == [None]
    assert app.state.worker_task is None
    database.close()


