"""SQLite connection and migration support for application automation."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import os
import sqlite3
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
import fcntl
import stat
from typing import Any
from uuid import uuid4
from datetime import datetime
from zoneinfo import ZoneInfo

from .region import load_region


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
DEFAULT_DB_NAME = "application-automation.sqlite3"
_LEGACY_MIGRATION_TRANSITIONS = {
    "0007_critic5_relational_closure.sql": {
        "9a79f204a93750225264e556c209d07abdd8099bf9a51ad05019514e96e98e3e":
            "9f3c20a6d169242a39ebc5097df1d5c90c294317589353b0251bed5379daa7d2",
    },
}


def resolve_database_path(value: str | Path | None = None) -> Path:
    """Resolve a database path, defaulting to user data rather than the repository."""
    configured = value or os.environ.get("APPLICATION_AUTOMATION_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    data_home = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return (data_home / "application_automation" / DEFAULT_DB_NAME).resolve()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a cross-thread connection; callers must serialize access themselves."""
    database_path = resolve_database_path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        database_path, isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    connection.create_function(
        "application_automation_policy_local_date",
        1,
        _policy_local_date,
        deterministic=False,
    )
    return connection
def _policy_local_date(timezone: str) -> str:
    """Return today's ISO date for the only permitted batch-policy timezone."""
    if timezone != load_region().timezone:
        raise ValueError("unsupported policy timezone")
    return datetime.now(ZoneInfo(timezone)).date().isoformat()

class ServiceInstanceLockError(RuntimeError):
    """Raised when another local service owns a database's recovery lock."""


class ServiceInstanceLock:
    """An exclusive, process-scoped lock associated with a SQLite database file."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        database_path = _connection_database_path(connection)
        self.path = database_path.with_name(f".{database_path.name}.service.lock")
        self._descriptor: int | None = None

    def acquire(self) -> None:
        """Acquire the private local lock without waiting for another service."""
        if self._descriptor is not None:
            raise RuntimeError("service instance lock is already acquired")
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise ServiceInstanceLockError("service instance lock is unavailable")
        descriptor: int | None = None
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | no_follow, 0o600)
            file_status = os.fstat(descriptor)
            path_status = os.stat(self.path, follow_symlinks=False)
            if (
                not stat.S_ISREG(file_status.st_mode)
                or file_status.st_uid != os.getuid()
                or stat.S_IMODE(file_status.st_mode) != 0o600
                or (file_status.st_dev, file_status.st_ino)
                != (path_status.st_dev, path_status.st_ino)
            ):
                raise OSError("unsafe service instance lock file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise ServiceInstanceLockError("service instance lock is unavailable") from error
        self._descriptor = descriptor

    def release(self) -> None:
        """Release the lock after all service-owned work has stopped."""
        if self._descriptor is None:
            return
        descriptor, self._descriptor = self._descriptor, None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> ServiceInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _connection_database_path(connection: sqlite3.Connection) -> Path:
    """Return the on-disk main database path or reject non-file SQLite targets."""
    for _, name, filename in connection.execute("PRAGMA database_list"):
        if name == "main" and filename:
            path = Path(filename)
            try:
                return path.resolve(strict=True)
            except OSError as error:
                raise ServiceInstanceLockError(
                    "service instance lock requires an on-disk SQLite database"
                ) from error
    raise ServiceInstanceLockError(
        "service instance lock requires an on-disk SQLite database"
    )


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute complete SQL statements without releasing the migration lock."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("migration contains an incomplete SQL statement")


_MIGRATION_OBJECT_NAME_PATTERN = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\s+(?:IF NOT EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)
_MIGRATION_DROPPED_NAME_PATTERN = re.compile(
    r"DROP\s+(?:TABLE|INDEX|TRIGGER|VIEW)\s+(?:IF EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)


def _script_object_names(script: str) -> set[str]:
    """Return the durable schema object names a migration script declares.

    Scratch objects that a migration both creates and drops within the same
    script (for example a preflight guard table) are excluded, since their
    absence after the script runs is expected rather than a sign of a
    marker-only or partially applied history.
    """
    created = {match.group(1) for match in _MIGRATION_OBJECT_NAME_PATTERN.finditer(script)}
    dropped = {match.group(1) for match in _MIGRATION_DROPPED_NAME_PATTERN.finditer(script)}
    return created - dropped


def migration_scripts() -> list[tuple[str, str]]:
    """Numbered migration scripts with the deployment region's timezone substituted.

    Checksums are computed over the substituted text, so a database created for
    one region refuses to migrate under another instead of silently reinterpreting
    its policy rows.
    """
    timezone = load_region().timezone
    return [
        (
            migration.name,
            migration.read_text(encoding="utf-8").replace("{{POLICY_TIMEZONE}}", timezone),
        )
        for migration in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    ]


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply numbered, checksummed SQL migrations under one SQLite write lock."""
    migrations = migration_scripts()
    scripts = dict(migrations)
    expected = {version: sha256(script.encode("utf-8")).hexdigest() for version, script in migrations}
    order_index = {version: index for index, (version, _) in enumerate(migrations)}

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "checksum TEXT"
            ") STRICT"
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(schema_migrations)")
        }
        if "checksum" not in columns:
            connection.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")

        applied = {
            row["version"]: row["checksum"]
            for row in connection.execute(
                "SELECT version, checksum FROM schema_migrations"
            )
        }
        unknown = set(applied) - set(expected)
        if unknown:
            raise RuntimeError(f"unknown applied migrations: {sorted(unknown)!r}")

        applied_indices = sorted(order_index[version] for version in applied)
        if applied_indices != list(range(len(applied_indices))):
            raise RuntimeError(
                f"applied migrations are not an exact prefix of the manifest: {sorted(applied)!r}"
            )

        existing_objects = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger','view')"
            )
        }

        for version, checksum in expected.items():
            recorded = applied.get(version)
            if recorded is None and version in applied:
                raise RuntimeError(f"migration checksum is missing: {version}")
            if recorded is None:
                continue
            if recorded == checksum:
                expected_objects = _script_object_names(scripts[version])
                if expected_objects and not (expected_objects & existing_objects):
                    raise RuntimeError(
                        f"recorded migration has no matching schema objects: {version}"
                    )
                continue
            if _LEGACY_MIGRATION_TRANSITIONS.get(version, {}).get(recorded) != checksum:
                raise RuntimeError(f"migration checksum mismatch: {version}")
            expected_objects = _script_object_names(scripts[version])
            missing_objects = expected_objects - existing_objects
            if missing_objects:
                raise RuntimeError(
                    f"legacy migration {version} is missing expected schema objects: "
                    f"{sorted(missing_objects)!r}"
                )
            connection.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version=?",
                (checksum, version),
            )
            applied[version] = checksum

        applied_versions = set(applied)
        for version, script in migrations:
            if version in applied_versions:
                continue
            _execute_script(connection, script)
            connection.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES (?, ?)",
                (version, expected[version]),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


@contextmanager
def transaction(
    connection: sqlite3.Connection, *, immediate: bool = False
) -> Generator[sqlite3.Connection, None, None]:
    """Run a transaction, optionally acquiring SQLite's write reservation first."""
    if connection.in_transaction:
        savepoint = f"application_automation_{uuid4().hex}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield connection
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def execute(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] | Mapping[str, Any] = (),
) -> sqlite3.Cursor:
    """Execute a parameterized statement; callers must not log parameter values."""
    return connection.execute(sql, parameters)


def query_all(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] | Mapping[str, Any] = (),
) -> list[sqlite3.Row]:
    """Return all rows for a parameterized query."""
    return list(execute(connection, sql, parameters))


def query_one(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] | Mapping[str, Any] = (),
) -> sqlite3.Row | None:
    """Return one row for a parameterized query, if present."""
    return execute(connection, sql, parameters).fetchone()


def execute_many(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Iterable[Sequence[Any] | Mapping[str, Any]],
) -> sqlite3.Cursor:
    """Execute a parameterized statement for multiple rows without value logging."""
    return connection.executemany(sql, parameters)
