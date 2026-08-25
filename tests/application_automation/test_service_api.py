from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

from fastapi.testclient import TestClient

import application_automation.api as api
from application_automation.api import create_app
from application_automation.store import apply_migrations, connect


def _application_dir(root: Path, role_id: str = "r1") -> Path:
    directory = root / "applications" / role_id
    directory.mkdir(parents=True)
    (directory / "job.md").write_text("# Fixture Engineer\n\nVancouver, BC", encoding="utf-8")
    (directory / "resume.md").write_text("fixture resume", encoding="utf-8")
    (directory / "resume.docx").write_bytes(b"fixture docx")
    (directory / "resume.pdf").write_bytes(b"%PDF-fixture")
    return directory


def _service(tmp_path: Path, *, fixture: bool = True):
    db = connect(tmp_path / "service.sqlite")
    apply_migrations(db)
    directory = _application_dir(tmp_path)
    dashboard = tmp_path / "dashboard.html"
    dashboard.write_text("<script>window.ok=1</script>", encoding="utf-8")
    source_data = tmp_path / "jobs.json"
    source_data.write_text('{"roles":[]}', encoding="utf-8")
    master_resume = tmp_path / "resume.md"
    master_resume.write_text("# Fixture resume", encoding="utf-8")
    material = directory / "resume.pdf"
    catalog = {
        "r1": {
            "score": 8,
            "location": "Vancouver, BC",
            "posting_active": True,
            "remote": False,
            "remote_country": None,
            "automation_status": "materials_ready",
            "canonical_identity": "fixture:role",
            "application_dir": str(directory),
            "material_path": str(material),
            "material_sha256": hashlib.sha256(material.read_bytes()).hexdigest(),
        }
    }
    return db, TestClient(
        create_app(
            db,
            bootstrap_token="bootstrap",
            fixture_mode=fixture,
            catalog=catalog,
            dashboard_path=dashboard,
            source_data_path=source_data,
            master_resume_path=master_resume,
        ),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )


def _login(client: TestClient) -> str:
    assert client.get("/api/v1/health", headers={"host": "example.invalid"}).status_code == 403
    assert client.get("/app/v1/", follow_redirects=False).headers["location"] == "/app/v1/bootstrap"
    assert client.get("/app/v1/bootstrap").status_code == 200
    assert client.get("/app/v1/?bootstrap=bootstrap", follow_redirects=False).headers["location"] == "/app/v1/bootstrap"
    response = client.post(
        "/app/v1/bootstrap",
        headers={
            "origin": "http://127.0.0.1",
            "content-type": "application/x-www-form-urlencoded",
        },
        content="token=bootstrap",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.app.state.bootstrap_token is None
    session = client.get("/api/v1/session")
    assert session.status_code == 200
    return session.json()["csrf_token"]


def _headers(token: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"origin": "http://127.0.0.1", "x-csrf-token": token}
    if idempotency_key is not None:
        headers["idempotency-key"] = idempotency_key
    return headers
def test_loopback_host_configuration_fails_closed(tmp_path: Path, monkeypatch) -> None:
    database = connect(tmp_path / "loopback.sqlite")
    apply_migrations(database)

    assert create_app(database, fixture_mode=True, loopback_host="127.0.0.1").state.fixture_mode is True
    monkeypatch.setattr(
        api.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )
    assert create_app(database, fixture_mode=True, loopback_host="fixture.local").state.fixture_mode is True
    monkeypatch.setattr(
        api.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            (api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0)),
        ],
    )
    try:
        create_app(database, fixture_mode=True, loopback_host="ambiguous.local")
    except ValueError as error:
        assert str(error) == "loopback_host must resolve exclusively to loopback"
    else:
        raise AssertionError("mixed loopback and public resolution must be rejected")
    for host in ("0.0.0.0", "192.0.2.1", "127.0.0.1:8000"):
        try:
            create_app(database, fixture_mode=True, loopback_host=host)
        except ValueError as error:
            assert str(error) in {
                "loopback_host must be an unambiguous loopback host",
                "loopback_host must resolve exclusively to loopback",
            }
        else:
            raise AssertionError(f"non-loopback host {host!r} must be rejected")
    database.close()


def test_successful_bootstrap_clears_retained_token(tmp_path: Path) -> None:
    database, client = _service(tmp_path)

    _login(client)

    assert client.app.state.bootstrap_token is None
    assert client.get("/app/v1/bootstrap").status_code == 410
    client.close()
    database.close()


def test_bootstrap_rejects_wrong_token_without_consuming_correct_token(tmp_path: Path) -> None:
    database, client = _service(tmp_path)

    rejected = client.post(
        "/app/v1/bootstrap",
        headers={
            "origin": "http://127.0.0.1",
            "content-type": "application/x-www-form-urlencoded",
        },
        content="token=not-bootstrap",
        follow_redirects=False,
    )
    assert rejected.status_code == 403
    assert client.get("/api/v1/session").status_code == 401

    csrf = _login(client)
    assert client.get("/api/v1/session").json()["csrf_token"] == csrf
    assert client.get("/app/v1/bootstrap").status_code == 410
    client.close()
    database.close()





def test_material_fixture_and_request_authentication_boundaries(tmp_path: Path) -> None:
    database, client = _service(tmp_path)
    csrf = _login(client)

    directory = tmp_path / "applications" / "r1"
    assert {path.name for path in directory.iterdir()} == {"job.md", "resume.md", "resume.docx", "resume.pdf"}
    assert client.get("/api/v1/snapshot", headers={"host": "localhost"}).status_code == 403
    assert client.get("/api/v1/snapshot", headers={"host": "127.0.0.1"}).status_code == 200
    assert client.get("/api/v1/materials/r1").status_code == 200
    assert client.get("/api/v1/materials/r1", headers={"host": "localhost"}).status_code == 403
    source = client.get("/app/v1/jobs/tracker.json")
    resume = client.get("/app/v1/applications/_master/resume.md")
    assert source.status_code == 200 and source.headers["content-type"] == "application/json"
    assert resume.status_code == 200 and resume.headers["content-type"].startswith("text/markdown")
    nested = directory / "nested"
    nested.mkdir()
    nested_resume = nested / "resume.pdf"
    nested_resume.write_bytes(b"%PDF-nested")
    alias = directory / "alias"
    alias.symlink_to(nested, target_is_directory=True)
    info = client.app.state.orchestrator.catalog["r1"]
    info["material_path"] = str(alias / "resume.pdf")
    info["material_sha256"] = hashlib.sha256(nested_resume.read_bytes()).hexdigest()
    assert client.get("/api/v1/materials/r1").status_code == 404
    assert client.post(
        "/api/v1/roles/r1/commands",
        headers={"x-csrf-token": csrf, "idempotency-key": "missing-origin"},
        json={"mode": "batch", "idempotency_key": "missing-origin"},
    ).status_code == 403
    assert client.post(
        "/api/v1/roles/r1/commands",
        headers={"origin": "http://evil.invalid", "x-csrf-token": csrf, "idempotency-key": "wrong-origin"},
        json={"mode": "batch", "idempotency_key": "wrong-origin"},
    ).status_code == 403
    assert client.post(
        "/api/v1/roles/r1/commands",
        headers={"origin": "http://127.0.0.1", "idempotency-key": "missing-csrf"},
        json={"mode": "batch", "idempotency_key": "missing-csrf"},
    ).status_code == 403
    writes_before = database.total_changes
    assert client.post(
        "/api/v1/roles/r1/commands",
        headers=_headers("wrong-csrf", "wrong-csrf"),
        json={"mode": "batch", "idempotency_key": "wrong-csrf"},
    ).status_code == 403
    assert database.total_changes == writes_before
    client.cookies.clear()
    assert client.get("/app/v1/jobs/tracker.json").status_code == 401
    assert client.get("/app/v1/applications/_master/resume.md").status_code == 401
    assert client.post(
        "/api/v1/roles/r1/commands",
        headers={"origin": "http://127.0.0.1", "x-csrf-token": csrf, "idempotency-key": "missing-cookie"},
        json={"mode": "batch", "idempotency_key": "missing-cookie"},
    ).status_code == 401
    assert database.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    client.close()
    database.close()


def test_cli_catalog_normalizes_material_roots_without_duplication(tmp_path: Path) -> None:
    build_path = Path(__file__).resolve().parents[2] / "tools" / "apply_service.py"
    spec = importlib.util.spec_from_file_location("apply_service", build_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PROJECT_ROOT = tmp_path

    directory = _application_dir(tmp_path, "normalized")
    (directory / "Jane Doe Resume.pdf").write_bytes(b"%PDF-fixture")
    source = tmp_path / "jobs.json"
    source.write_text(
        json.dumps({
            "generated_at": "fixture",
            "roles": [{
                "id": "normalized",
                "application_dir": "applications/normalized",
                "status": "materials_ready",
                "location": "Vancouver, BC",
                "posting_active": True,
                "remote": False,
                "remote_country": None,
                "material_manifest": {
                    "path": "Jane Doe Resume.pdf",
                    "sha256": hashlib.sha256(
                        (directory / "Jane Doe Resume.pdf").read_bytes()
                    ).hexdigest(),
                },
            }],
        }),
        encoding="utf-8",
    )
    catalog = module._catalog(str(source))
    assert Path(catalog["normalized"]["application_dir"]) == directory
    assert Path(catalog["normalized"]["material_path"]) == directory / "Jane Doe Resume.pdf"

def test_cli_recover_requires_service_instance_ownership(
    tmp_path: Path, monkeypatch
) -> None:
    build_path = Path(__file__).resolve().parents[2] / "tools" / "apply_service.py"
    spec = importlib.util.spec_from_file_location("apply_service_recover", build_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    database = connect(tmp_path / "recover.sqlite")
    apply_migrations(database)
    app = create_app(database, fixture_mode=True, catalog={})
    with TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ):
        monkeypatch.setattr(
            "sys.argv",
            ["apply_service.py", "--fixture", "--db", str(tmp_path / "recover.sqlite"), "recover"],
        )
        with pytest.raises(SystemExit, match="service instance lock is unavailable"):
            module.main()

    database.close()

def test_project_catalog_is_automation_ready() -> None:
    project_root = Path(__file__).resolve().parents[2]
    build_path = project_root / "tools" / "apply_service.py"
    spec = importlib.util.spec_from_file_location("project_apply_service", build_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    catalog = module._catalog(str(project_root / "jobs" / "tracker.json"))

    roles = {key: value for key, value in catalog.items() if key != "_catalog_revision"}
    assert roles
    assert any(role["automation_status"] == "materials_ready" for role in roles.values())
    assert all(Path(role["material_path"]).is_file() for role in roles.values())


def test_idempotency_header_body_mismatch_and_conflict_persist_one_command(tmp_path: Path) -> None:
    database, client = _service(tmp_path)
    csrf = _login(client)
    assert client.get("/api/v1/session").json()["worker"] == {
        "state": "manual",
        "automatic_progress": False,
        "can_queue": True,
    }
    assert client.get("/api/v1/snapshot").json()["worker"] == {
        "state": "manual",
        "automatic_progress": False,
        "can_queue": True,
    }

    mismatch = client.post(
        "/api/v1/roles/r1/commands",
        headers=_headers(csrf, "header-key"),
        json={"mode": "batch", "idempotency_key": "body-key"},
    )
    assert mismatch.status_code == 409
    assert database.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0

    first = client.post(
        "/api/v1/roles/r1/commands",
        headers=_headers(csrf, "same-key"),
        json={"mode": "dry_run", "idempotency_key": "same-key"},
    )
    replay = client.post(
        "/api/v1/roles/r1/commands",
        headers=_headers(csrf, "same-key"),
        json={"mode": "dry_run", "idempotency_key": "same-key"},
    )
    conflict = client.post(
        "/api/v1/roles/r1/commands",
        headers=_headers(csrf, "same-key"),
        json={"mode": "fill_only", "idempotency_key": "same-key"},
    )
    assert first.status_code == replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert database.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    client.close()
    database.close()
def test_nonfixture_mutations_reject_before_orchestrator_or_writes(tmp_path: Path, monkeypatch) -> None:
    database, client = _service(tmp_path, fixture=False)
    csrf = _login(client)
    writes_before = database.total_changes

    def forbidden(*_args, **_kwargs):
        raise AssertionError("nonfixture mutations must not reach the orchestrator")

    monkeypatch.setattr(client.app.state.orchestrator, "queue", forbidden)
    monkeypatch.setattr(client.app.state.orchestrator, "cancel", forbidden)
    monkeypatch.setattr(client.app.state.orchestrator, "run", forbidden)

    assert client.post(
        "/api/v1/roles/r1/commands",
        headers=_headers(csrf, "real-mode"),
        json={"mode": "batch", "idempotency_key": "real-mode"},
    ).status_code == 403
    assert client.post("/api/v1/commands/command/cancel", headers=_headers(csrf)).status_code == 403
    assert client.post("/api/v1/commands/command/run", headers=_headers(csrf)).status_code == 403
    assert database.total_changes == writes_before
    assert database.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    client.close()
    database.close()


def test_nonloopback_peer_cannot_spoof_loopback_host(tmp_path: Path) -> None:
    database = connect(tmp_path / "peer.sqlite")
    apply_migrations(database)
    app = create_app(database, fixture_mode=True, catalog={})
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("192.0.2.1", 50000),
    ) as client:
        assert client.get("/api/v1/health", headers={"host": "127.0.0.1"}).status_code == 403
        assert client.get("/app/v1/bootstrap", headers={"host": "127.0.0.1"}).status_code == 403
    database.close()


def test_command_request_enums_reject_unknown_values(tmp_path: Path) -> None:
    database, client = _service(tmp_path)
    csrf = _login(client)

    assert client.post(
        "/api/v1/roles/r1/commands",
        headers=_headers(csrf, "unknown-mode"),
        json={"mode": "unknown", "idempotency_key": "unknown-mode"},
    ).status_code == 422
    assert client.post(
        "/api/v1/commands/unknown/run?scenario=unknown",
        headers=_headers(csrf),
    ).status_code == 422
    assert database.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    client.close()
    database.close()




def test_cancelled_command_cannot_run_or_dispatch(tmp_path: Path) -> None:
    database, client = _service(tmp_path)
    csrf = _login(client)
    command = client.post(
        "/api/v1/roles/r1/commands",
        headers=_headers(csrf, "cancel"),
        json={"mode": "batch", "idempotency_key": "cancel"},
    ).json()

    cancelled = client.post(f"/api/v1/commands/{command['id']}/cancel", headers=_headers(csrf))
    rejected_run = client.post(f"/api/v1/commands/{command['id']}/run", headers=_headers(csrf))
    assert cancelled.json()["state"] == "cancelled"
    assert rejected_run.status_code == 409
    assert rejected_run.json() == {"detail": "command is unavailable"}
    assert database.execute("SELECT state FROM applications WHERE id=?", (command["application_id"],)).fetchone()["state"] == "abandoned"
    assert database.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM status_events WHERE event_kind='applied'").fetchone()[0] == 0
    assert database.execute("SELECT event_kind FROM status_events ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "cancelled"
    client.close()
    database.close()
def test_registered_route_manifest_matches_exact_method_path_set(tmp_path: Path) -> None:
    """An explicit method/path manifest must exactly match every route FastAPI actually
    registers; a silently-added or silently-removed route must fail this test."""
    from fastapi.routing import APIRoute

    database = connect(tmp_path / "manifest.sqlite")
    apply_migrations(database)
    app = create_app(database, fixture_mode=True, catalog={})
    registered = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method != "HEAD"
    }
    expected = {
        ("GET", "/api/v1/health"),
        ("GET", "/app/v1/bootstrap"),
        ("POST", "/app/v1/bootstrap"),
        ("GET", "/app/v1/"),
        ("GET", "/app/v1/jobs/tracker.json"),
        ("GET", "/app/v1/applications/_master/resume.md"),
        ("GET", "/api/v1/session"),
        ("GET", "/api/v1/snapshot"),
        ("POST", "/api/v1/roles/{role_id}/commands"),
        ("GET", "/api/v1/commands/{command_id}"),
        ("POST", "/api/v1/commands/{command_id}/cancel"),
        ("POST", "/api/v1/commands/{command_id}/run"),
        ("GET", "/api/v1/materials/{role_id}"),
        ("GET", "/api/v1/evidence/{evidence_id}"),
    }
    assert registered == expected
    database.close()


def test_every_route_auth_boundary_has_zero_mutation_on_rejection(tmp_path: Path) -> None:
    all_routes = (
        "session", "snapshot", "dashboard", "jobs", "master_resume",
        "materials", "command", "evidence", "queue", "cancel", "run",
    )
    read_routes = {"session", "snapshot", "dashboard", "jobs", "master_resume", "materials", "command", "evidence"}
    for route in all_routes:
        database, client = _service(tmp_path / route)
        csrf = _login(client)
        command: dict[str, object] | None = None
        evidence_id: str | None = None
        if route in {"cancel", "run", "command"}:
            command = client.post(
                "/api/v1/roles/r1/commands",
                headers=_headers(csrf, f"{route}-setup"),
                json={"mode": "dry_run", "idempotency_key": f"{route}-setup"},
            ).json()
        if route == "evidence":
            completed = client.post(
                "/api/v1/roles/r1/commands",
                headers=_headers(csrf, f"{route}-setup"),
                json={"mode": "dry_run", "idempotency_key": f"{route}-setup"},
            ).json()
            run_response = client.post(
                f"/api/v1/commands/{completed['id']}/run", headers=_headers(csrf),
            )
            assert run_response.status_code == 200
            evidence_row = database.execute(
                "SELECT id FROM evidence WHERE application_id=? ORDER BY rowid LIMIT 1",
                (completed["application_id"],),
            ).fetchone()
            assert evidence_row is not None
            evidence_id = evidence_row["id"]

        def request(headers: dict[str, str]):
            if route == "session":
                return client.get("/api/v1/session", headers=headers)
            if route == "snapshot":
                return client.get("/api/v1/snapshot", headers=headers)
            if route == "dashboard":
                return client.get("/app/v1/", headers=headers, follow_redirects=False)
            if route == "jobs":
                return client.get("/app/v1/jobs/tracker.json", headers=headers)
            if route == "master_resume":
                return client.get("/app/v1/applications/_master/resume.md", headers=headers)
            if route == "materials":
                return client.get("/api/v1/materials/r1", headers=headers)
            if route == "command":
                assert command is not None
                return client.get(f"/api/v1/commands/{command['id']}", headers=headers)
            if route == "evidence":
                assert evidence_id is not None
                return client.get(f"/api/v1/evidence/{evidence_id}", headers=headers)
            if route == "queue":
                return client.post(
                    "/api/v1/roles/r1/commands",
                    headers=headers,
                    json={"mode": "dry_run", "idempotency_key": "rejected"},
                )
            assert command is not None
            return client.post(f"/api/v1/commands/{command['id']}/{route}", headers=headers)

        baseline = database.total_changes
        read_route = route in read_routes
        for headers, expected in (
            ({"host": "localhost"}, 403),
            ({}, 200 if read_route else 403),
            ({"origin": "http://127.0.0.1"}, 200 if read_route else 403),
            ({"origin": "http://127.0.0.1", "x-csrf-token": "forged"}, 200 if read_route else 403),
            ({"x-csrf-token": csrf}, 200 if read_route else 403),
            ({"origin": "http://evil.invalid", "x-csrf-token": csrf}, 200 if read_route else 403),
        ):
            assert request(headers).status_code == expected
            assert database.total_changes == baseline

        client.cookies.clear()
        assert request({"origin": "http://127.0.0.1", "x-csrf-token": csrf}).status_code == 401
        assert database.total_changes == baseline
        client.close()
        database.close()