"""Same-origin loopback FastAPI surface for local automation."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import base64
import hashlib
import secrets
import math
import sqlite3
from pathlib import Path
import ipaddress
import socket
from typing import Any, Literal, Mapping
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .orchestrator import ApplicationOrchestrator, MaterialValidationError, OrchestrationError, validate_material_file
from .store import ServiceInstanceLock


class CommandRequest(BaseModel):
    mode: Literal["dry_run", "fill_only", "batch"]
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


def _csp(html: str) -> str:
    import re
    hashes = [base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() for body in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.I | re.S)]
    scripts = " ".join("'sha256-" + digest + "'" for digest in hashes)
    return "default-src 'none'; connect-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; script-src " + (scripts or "'none'") + "; base-uri 'none'; frame-ancestors 'none'"


def _has_symlink_component(path: Path) -> bool:
    candidate = path.absolute()
    absolute = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        absolute /= part
        if absolute.is_symlink():
            return True
    return False
def _validated_loopback_host(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(character in value for character in "/@[]%"):
        raise ValueError("loopback_host must be an unambiguous loopback host")
    candidate = value.lower()
    try:
        addresses = {ipaddress.ip_address(candidate)}
    except ValueError:
        if ":" in candidate:
            raise ValueError("loopback_host must be an unambiguous loopback host") from None
        try:
            resolved = socket.getaddrinfo(candidate, None, type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(item[4][0]) for item in resolved}
        except (OSError, ValueError):
            raise ValueError("loopback_host must resolve exclusively to loopback") from None
    if not addresses or not all(address.is_loopback for address in addresses):
        raise ValueError("loopback_host must resolve exclusively to loopback")
    return candidate




def create_app(
    connection: sqlite3.Connection,
    *,
    bootstrap_token: str | None = None,
    fixture_mode: bool = False,
    catalog: Mapping[str, Mapping[str, Any]] | None = None,
    loopback_host: str = "127.0.0.1",
    dashboard_path: str | Path = "dashboard.html",
    source_data_path: str | Path = "jobs/tracker.json",
    master_resume_path: str | Path = "applications/_master/resume.md",
    autonomous_worker: bool = False,
    worker_idle_seconds: float = 0.5,
    instance_lock: ServiceInstanceLock | None = None,
) -> FastAPI:
    """Create a local-only API. The bootstrap secret is consumed exactly once."""
    if not math.isfinite(worker_idle_seconds) or worker_idle_seconds <= 0:
        raise ValueError("worker_idle_seconds must be a finite positive number")
    validated_loopback_host = _validated_loopback_host(loopback_host)
    token = bootstrap_token or secrets.token_urlsafe(32)
    sessions: dict[str, str] = {}
    orchestrator = ApplicationOrchestrator(connection, fixture_mode=fixture_mode, catalog=catalog)
    orchestrator.sync_catalog()
    worker_enabled = fixture_mode and autonomous_worker

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owns_lock = instance_lock is None
        active_lock = instance_lock or ServiceInstanceLock(connection)
        task: asyncio.Task[None] | None = None
        stop_requested = asyncio.Event()
        try:
            if owns_lock:
                active_lock.acquire()
            if fixture_mode:
                orchestrator.ensure_fixture_authority()
            orchestrator.recover_stale_commands()
            if worker_enabled:
                async def worker() -> None:
                    while not stop_requested.is_set():
                        try:
                            command = await asyncio.to_thread(orchestrator.run_next)
                        except Exception as error:
                            app.state.worker_error = "fixture worker unavailable"
                            app.state.worker_failure = {
                                "code": "fixture_worker_run_failed",
                                "class": type(error).__name__,
                            }
                            return
                        if command is None:
                            try:
                                await asyncio.wait_for(
                                    stop_requested.wait(), timeout=worker_idle_seconds
                                )
                            except asyncio.TimeoutError:
                                pass
                task = asyncio.create_task(
                    worker(), name="application-automation-fixture-worker"
                )
            app.state.worker_task = task
            yield
        finally:
            stop_requested.set()
            cancelled = False
            if task is not None:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                    await asyncio.shield(task)
            app.state.worker_task = None
            if owns_lock:
                active_lock.release()
            if cancelled:
                raise asyncio.CancelledError

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.bootstrap_token = token
    app.state.fixture_mode = fixture_mode
    app.state.orchestrator = orchestrator
    app.state.worker_error = None
    app.state.worker_enabled = worker_enabled
    app.state.worker_failure = None
    app.state.worker_task = None

    def safe_error(status: int, detail: str = "request rejected") -> HTTPException:
        return HTTPException(status_code=status, detail=detail)

    def host(request: Request) -> str:
        value = request.headers.get("host", "").lower()
        if value.startswith("["):
            closing = value.find("]")
            return value[1:closing] if closing > 0 else ""
        return value.rsplit(":", 1)[0] if value.count(":") == 1 else value

    def peer_is_loopback(request: Request) -> bool:
        client = request.client
        if client is None:
            return False
        try:
            return ipaddress.ip_address(client.host).is_loopback
        except ValueError:
            return False

    def local_request(request: Request) -> None:
        if not peer_is_loopback(request) or host(request) != validated_loopback_host:
            raise safe_error(403)

    def worker_state() -> dict[str, Any]:
        task = app.state.worker_task
        if app.state.worker_enabled:
            return {
                "state": "running" if task is not None and not task.done() else "unavailable",
                "automatic_progress": task is not None and not task.done(),
                "can_queue": task is not None and not task.done(),
            }
        return {
            "state": "manual",
            "automatic_progress": False,
            "can_queue": True,
        }

    def session(request: Request, mutation: bool = False) -> str:
        local_request(request)
        if mutation and not fixture_mode:
            raise safe_error(403)
        sid = request.cookies.get("application_automation_session")
        if not sid or sid not in sessions:
            raise safe_error(401)
        if mutation:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin != expected or request.headers.get("x-csrf-token") != sessions[sid]:
                raise safe_error(403)
        return sid

    def asset_response(path_value: str | Path, media_type: str) -> Response:
        path = Path(path_value).expanduser()
        try:
            if _has_symlink_component(path):
                raise OSError
            raw = path.resolve(strict=True).read_bytes()
        except OSError:
            raise safe_error(404)
        if len(raw) > 10 * 1024 * 1024:
            raise safe_error(404)
        return Response(
            raw,
            media_type=media_type,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.exception_handler(OrchestrationError)
    async def orchestration_error(_: Request, error: OrchestrationError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/api/v1/health")
    async def health(request: Request) -> dict[str, str]:
        local_request(request)
        readiness = worker_state()
        if readiness["state"] == "unavailable":
            return {"status": "degraded", "worker": "unavailable"}
        return {
            "status": "ok",
            "worker": str(readiness["state"]),
        }

    @app.get("/app/v1/bootstrap")
    async def bootstrap_page(request: Request):
        local_request(request)
        if not token:
            raise safe_error(410)
        html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width">
  <title>Local application service</title>
</head>
<body>
  <main>
    <h1>Connect local dashboard</h1>
    <p>Paste the one-use token from the mode-0600 bootstrap file.</p>
    <form method="post" action="/app/v1/bootstrap">
      <label>Bootstrap token <input name="token" type="password" required autocomplete="off"></label>
      <button type="submit">Connect</button>
    </form>
  </main>
</body>
</html>"""
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            },
        )

    @app.post("/app/v1/bootstrap")
    async def bootstrap(request: Request):
        nonlocal token
        local_request(request)
        expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if request.headers.get("origin") != expected_origin:
            raise safe_error(403)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise safe_error(415)
        body = await request.body()
        if len(body) > 1024:
            raise safe_error(413)
        try:
            values = parse_qs(body.decode("ascii"), strict_parsing=True)
        except (UnicodeDecodeError, ValueError):
            raise safe_error(400)
        supplied = values.get("token", [])
        if len(supplied) != 1 or not token or not secrets.compare_digest(supplied[0], token):
            raise safe_error(403)
        token = ""
        app.state.bootstrap_token = None
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sessions[sid] = csrf
        response = RedirectResponse("/app/v1/", status_code=303)
        response.set_cookie(
            "application_automation_session",
            sid,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    @app.get("/app/v1/")
    async def dashboard(request: Request):
        local_request(request)
        try:
            session(request)
        except HTTPException as error:
            if error.status_code == 401 and token:
                return RedirectResponse("/app/v1/bootstrap", status_code=303)
            raise
        try:
            html = Path(dashboard_path).read_text(encoding="utf-8")
        except OSError:
            raise safe_error(404)
        return HTMLResponse(html, headers={"Content-Security-Policy": _csp(html), "Cache-Control": "no-store"})

    @app.get("/app/v1/jobs/tracker.json")
    async def source_data(request: Request):
        session(request)
        return asset_response(source_data_path, "application/json")

    @app.get("/app/v1/applications/_master/resume.md")
    async def master_resume(request: Request):
        session(request)
        return asset_response(master_resume_path, "text/markdown")

    @app.get("/api/v1/session")
    async def get_session(request: Request) -> dict[str, Any]:
        sid = session(request)
        return {
            "csrf_token": sessions[sid],
            "fixture_mode": fixture_mode,
            "worker": worker_state(),
        }

    @app.get("/api/v1/snapshot")
    async def snapshot(request: Request) -> dict[str, Any]:
        session(request)
        snapshot = app.state.orchestrator.snapshot()
        snapshot["worker"] = worker_state()
        return snapshot

    @app.post("/api/v1/roles/{role_id}/commands")
    async def queue(role_id: str, body: CommandRequest, request: Request, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        session(request, True)
        if not worker_state()["can_queue"]:
            raise safe_error(409, "fixture worker unavailable")
        if not idempotency_key:
            raise safe_error(400, "Idempotency-Key header is required")
        if body.idempotency_key is not None and body.idempotency_key != idempotency_key:
            raise safe_error(409, "idempotency key must match the Idempotency-Key header")
        return app.state.orchestrator.queue(role_id, body.mode, idempotency_key)

    @app.get("/api/v1/commands/{command_id}")
    async def command(command_id: str, request: Request) -> dict[str, Any]:
        session(request)
        return app.state.orchestrator.command(command_id)

    @app.post("/api/v1/commands/{command_id}/cancel")
    async def cancel(command_id: str, request: Request) -> dict[str, Any]:
        session(request, True)
        return app.state.orchestrator.cancel(command_id)

    @app.post("/api/v1/commands/{command_id}/run")
    async def run(
        command_id: str,
        request: Request,
        scenario: Literal[
            "happy",
            "ambiguous",
            "captcha",
            "mfa",
            "security_challenge",
            "rate_limit",
            "provider_challenge",
            "login",
            "account_creation",
            "new_question",
            "unknown_question",
            "sensitive_question",
            "legal_question",
            "required_demographics",
            "street_address",
            "salary_unverified",
            "salary_exact_number",
            "attestation",
            "form_drift",
            "posting_drift",
            "unexpected_redirect",
        ] = "happy",
    ) -> dict[str, Any]:
        session(request, True)
        if worker_enabled and not worker_state()["automatic_progress"]:
            raise safe_error(409, "fixture worker unavailable")
        return app.state.orchestrator.run(command_id, scenario=scenario)
    @app.get("/api/v1/materials/{role_id}")
    async def material(role_id: str, request: Request):
        session(request)
        info = app.state.orchestrator.catalog.get(role_id)
        if not isinstance(info, Mapping):
            raise safe_error(404)
        root_value = info.get("application_dir")
        path_value = info.get("material_path")
        if not isinstance(root_value, str) or not isinstance(path_value, str):
            raise safe_error(404)
        root = Path(root_value).expanduser()
        candidate = Path(path_value).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        try:
            if _has_symlink_component(root) or _has_symlink_component(candidate):
                raise OSError
            resolved_root = root.resolve(strict=True)
            resolved_path = candidate.resolve(strict=True)
            resolved_path.relative_to(resolved_root)
            expected = info.get("material_sha256")
            if not isinstance(expected, str) or not expected:
                raise OSError
            raw = validate_material_file(resolved_path, expected_sha256=expected)
        except (OSError, ValueError, MaterialValidationError):
            raise safe_error(404)
        media_type = (
            "application/pdf"
            if resolved_path.suffix.lower() == ".pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        return Response(
            raw,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{resolved_path.name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/evidence/{evidence_id}")
    async def evidence(evidence_id: str, request: Request):
        session(request)
        return JSONResponse(
            app.state.orchestrator.evidence_event(evidence_id),
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    return app
