#!/usr/bin/env python3
"""Local-only application automation service command line."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import stat
from pathlib import Path

from application_automation.adapters.aside_fixture import AsideFixtureAdapter
from application_automation.adapters.base import AsideCliAdapter, AsideProbeError
from application_automation.api import create_app
from application_automation.orchestrator import ApplicationOrchestrator, MaterialValidationError, validate_material_file
from application_automation.store import (
    MIGRATIONS_DIR,
    ServiceInstanceLock,
    ServiceInstanceLockError,
    apply_migrations,
    connect,
)
from application_automation.status import current_status
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _catalog(path: str | None) -> dict:
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("roles"), list):
        raise ValueError("catalog must contain a roles list")
    result = {"_catalog_revision": str(raw.get("generated_at", ""))}
    for role in raw["roles"]:
        if not isinstance(role, dict) or not isinstance(role.get("id"), str) or not role["id"]:
            raise ValueError("catalog role requires a nonempty id")
        if role["id"] in result:
            raise ValueError(f"catalog contains duplicate role id: {role['id']}")
        if not isinstance(role.get("location"), str) or not role["location"].strip():
            raise ValueError(f"catalog role {role['id']} requires an explicit location")
        if type(role.get("posting_active")) is not bool:
            raise ValueError(f"catalog role {role['id']} requires explicit posting_active")
        manifest = role.get("material_manifest")
        if not isinstance(manifest, dict):
            raise ValueError(f"catalog role {role['id']} requires a material_manifest")
        material_name = manifest.get("path")
        expected_hash = manifest.get("sha256")
        if not isinstance(material_name, str) or not material_name or Path(material_name).is_absolute():
            raise ValueError(f"catalog role {role['id']} has an invalid material manifest path")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash.lower()):
            raise ValueError(f"catalog role {role['id']} has an invalid material manifest sha256")
        application_dir = Path(role.get("application_dir", "")).expanduser()
        if not application_dir.is_absolute():
            application_dir = PROJECT_ROOT / application_dir
        try:
            application_dir = application_dir.resolve(strict=True)
            application_dir.relative_to(PROJECT_ROOT.resolve())
        except (OSError, ValueError) as error:
            raise ValueError("catalog application_dir escapes the project root") from error
        material = application_dir / material_name
        if material.is_symlink():
            raise ValueError(f"catalog role {role['id']} material must not be a symlink")
        try:
            material = material.resolve(strict=True)
            material.relative_to(application_dir)
        except (OSError, ValueError) as error:
            raise ValueError(f"catalog role {role['id']} material escapes application_dir") from error
        try:
            validate_material_file(material, expected_sha256=expected_hash.lower())
        except MaterialValidationError as error:
            raise ValueError(f"catalog role {role['id']} material failed strict validation") from error
        actual_hash = expected_hash.lower()
        result[role["id"]] = {
            **role,
            "company": role.get("company", ""),
            "catalog_status": str(role.get("status", "reviewing")),
            "application_dir": str(application_dir),
            "automation_status": {"materials_ready": "materials_ready", "applied": "applied",
                                  "closed": "closed", "rejected": "rejected"}.get(str(role.get("status", "reviewing")), "reviewing"),
            "remote": role.get("remote"),
            "remote_country": role.get("remote_country"),
            "canonical_identity": f"{role.get('domain', '')}:{role.get('title', '')}:{role.get('apply_url', '')}",
            "material_path": str(material),
            "material_sha256": actual_hash,
        }
    return result

def _verify_schema_current(connection: sqlite3.Connection) -> None:
    """Read-only proof that an owner command (serve/worker/recover) already migrated the schema.

    Read commands must never apply migrations themselves; a schema that is missing or stale
    fails closed rather than silently mutating the database outside owner-lock protection.
    """
    migrations = [
        (migration.name, hashlib.sha256(migration.read_text(encoding="utf-8").encode("utf-8")).hexdigest())
        for migration in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    ]
    try:
        applied = {
            row[0]: row[1]
            for row in connection.execute("SELECT version, checksum FROM schema_migrations")
        }
    except sqlite3.OperationalError as error:
        raise SystemExit(
            "database schema is not migrated; run serve, worker, or recover first"
        ) from error
    for version, checksum in migrations:
        if applied.get(version) != checksum:
            raise SystemExit(
                "database schema is not current; run serve, worker, or recover first"
            )

def main() -> int:
    parser = argparse.ArgumentParser(description="Local application automation service")
    parser.add_argument("--db")
    parser.add_argument("--catalog")
    parser.add_argument("--fixture", action="store_true", help="enable deterministic local fixture execution")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--bootstrap-token-file")
    serve.add_argument("--generate-bootstrap-token-file")
    serve.add_argument("--port", type=int, default=8765)
    sub.add_parser("doctor")
    aside_doctor = sub.add_parser("aside-doctor")
    aside_doctor.add_argument("--expected-version")
    aside_doctor.add_argument("--expected-executable-sha256")
    aside_doctor.add_argument("--enforce-pins", action="store_true")
    queue = sub.add_parser("queue")
    queue.add_argument("role_id")
    queue.add_argument("mode", choices=("dry_run", "fill_only", "batch"))
    queue.add_argument("idempotency_key")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true", required=True)
    sub.add_parser("recover")
    status = sub.add_parser("status")
    status.add_argument("role_id")
    args = parser.parse_args()
    if args.command == "aside-doctor" and args.fixture and (
        args.expected_version is not None
        or args.expected_executable_sha256 is not None
        or args.enforce_pins
    ):
        parser.error(
            "--fixture aside-doctor does not accept pin options; "
            "fixture results are not pin evidence"
        )
    if args.command in {"serve", "queue", "worker", "recover"} and args.fixture is not True:
        parser.error(f"{args.command} is fixture-only; pass --fixture")

    if args.command == "aside-doctor":
        adapter = AsideFixtureAdapter() if args.fixture else AsideCliAdapter(
            expected_version=args.expected_version,
            expected_sha256=args.expected_executable_sha256,
        )
        result = adapter.doctor()
        if args.expected_executable_sha256 and not args.fixture:
            try:
                adapter.verify_executable()
            except AsideProbeError:
                result = result.__class__(result.available, False, result.version, result.mcp_available, result.repl_available, result.pause_reason, "executable_hash_drift")
        print(json.dumps(result.__dict__, default=str))
        if (
            not result.available
            or result.detail is not None
            or (args.enforce_pins and (not args.expected_version or not args.expected_executable_sha256))
        ):
            return 1
        return 0

    db = connect(args.db)

    if args.command in {"serve", "worker", "recover"}:
        # Owner commands acquire the exclusive service instance lock immediately after connect(),
        # before any migration or catalog mutation, and hold it through the full owner operation.
        lock = ServiceInstanceLock(db)
        try:
            lock.acquire()
        except ServiceInstanceLockError as error:
            raise SystemExit("service instance lock is unavailable") from error
        try:
            apply_migrations(db)
            catalog = _catalog(args.catalog)
            if args.command == "recover":
                orchestrator = ApplicationOrchestrator(db, fixture_mode=args.fixture, catalog=catalog)
                print(json.dumps({"recovered": orchestrator.recover_stale_commands()}))
            elif args.command == "worker":
                orchestrator = ApplicationOrchestrator(db, fixture_mode=args.fixture, catalog=catalog)
                orchestrator.sync_catalog()
                orchestrator.recover_stale_commands()
                result = orchestrator.run_next()
                print(json.dumps(result if result is not None else {"state": "idle"}))
            else:
                token = _bootstrap_token(args)
                import uvicorn
                uvicorn.run(
                    create_app(
                        db, bootstrap_token=token, fixture_mode=args.fixture, catalog=catalog,
                        autonomous_worker=args.fixture, instance_lock=lock,
                    ),
                    host="127.0.0.1", port=args.port,
                )
        finally:
            lock.release()
        return 0

    # Read commands (status, doctor, queue) verify the schema is already migrated by an owner
    # command instead of applying migrations themselves, and never mutate without that proof.
    _verify_schema_current(db)
    catalog = _catalog(args.catalog)
    if args.command == "doctor":
        print(json.dumps({"database": "ok", "fixture_mode": args.fixture,
                          "catalog_roles": len(catalog) - ("_catalog_revision" in catalog)}))
        return 0
    if args.command == "queue":
        orchestrator = ApplicationOrchestrator(db, fixture_mode=args.fixture, catalog=catalog)
        orchestrator.sync_catalog()
        print(json.dumps(orchestrator.queue(args.role_id, args.mode, args.idempotency_key)))
        return 0
    print(json.dumps({"role_id": args.role_id, "status": current_status(db, args.role_id)}))
    return 0


def _bootstrap_token(args: argparse.Namespace) -> str:
    source = args.bootstrap_token_file
    generated = args.generate_bootstrap_token_file
    if source and generated:
        raise SystemExit("choose one bootstrap token file mode")
    if source:
        path = Path(source).expanduser()
        try:
            if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise OSError
            token = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise SystemExit("bootstrap token file must exist and have mode 0600") from error
        if len(token) < 32:
            raise SystemExit("bootstrap token file must contain at least 32 characters")
        return token
    if generated:
        path = Path(generated).expanduser()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                token = secrets.token_urlsafe(32)
                handle.write(token + "\n")
        except OSError as error:
            raise SystemExit("cannot create bootstrap token file") from error
        return token
    raise SystemExit("serve requires --bootstrap-token-file or --generate-bootstrap-token-file")


if __name__ == "__main__":
    raise SystemExit(main())
