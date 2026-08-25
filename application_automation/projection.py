"""Durable, conflict-first filesystem projections.

SQLite remains authoritative.  This module stages read-only release artifacts and captures
human edits before a mirror can be repaired; it never silently chooses a winner.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


class ProjectionError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectionRelease:
    release_id: str
    json_sha256: str
    html_sha256: str
    manifest_sha256: str
    path: Path


@dataclass(frozen=True)
class DirectEditCapture:
    source_kind: str
    capture_relative_path: str
    raw_sha256: str
    base_mirror_sha256: str | None
    parsed_json: Mapping[str, Any] | None
    source_state: str = "present"
    state: str = "captured"


@dataclass(frozen=True)
class MirrorConflict:
    capture: DirectEditCapture
    expected_sha256: str
    observed_sha256: str
    state: str = "open"


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProjectionError("projection payload is not JSON-serializable") from error


def _hash(raw: bytes) -> str:
    return sha256(raw).hexdigest()
def _nofollow_flag() -> int:
    """Return the platform no-follow flag or fail before any projection I/O."""
    try:
        flag = os.O_NOFOLLOW
    except AttributeError as error:
        raise ProjectionError("platform does not support O_NOFOLLOW") from error
    if not isinstance(flag, int) or flag == 0:
        raise ProjectionError("platform does not support O_NOFOLLOW")
    return flag


def _decode_json_mapping(raw: bytes) -> Mapping[str, Any]:
    """Decode release metadata without allowing scalar JSON to cross a trust boundary."""
    try:
        decoded = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectionError("projection metadata is unreadable or malformed") from error
    if not isinstance(decoded, Mapping):
        raise ProjectionError("projection metadata must be a JSON object")
    return decoded



_ALLOWED_PROJECTION_NAMES = frozenset({"jobs", "applications", "dashboard"})
_ALLOWED_SOURCE_KINDS = frozenset({"jobs_json", "dashboard_html"})
_RELEASE_ID_LENGTH = 32


def _is_release_id(value: str) -> bool:
    return len(value) == _RELEASE_ID_LENGTH and all(character in "0123456789abcdef" for character in value)


def _contained(root: Path, candidate: Path) -> Path:
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ProjectionError("projection path escapes its root") from error
    return candidate
def _reject_symlink_path(path: Path, *, directory: bool = False) -> Path:
    """Reject a path containing a symbolic-link component before using it."""
    lexical = path.expanduser().absolute()
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            break
        if current.is_symlink():
            raise ProjectionError(f"symbolic links are not permitted: {current}")
    if directory and lexical.exists() and not lexical.is_dir():
        raise ProjectionError(f"projection directory is not a directory: {lexical}")
    return lexical



def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | _nofollow_flag())
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_durable(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag(),
        0o666,
    )
    with os.fdopen(descriptor, "wb") as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
def _read_nofollow(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | _nofollow_flag())
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ProjectionError(f"cannot safely read projection path: {path}") from error
    try:
        remaining = os.fstat(descriptor).st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


_CATALOG_TOP_LEVEL_FIELDS = ("generated_at", "roles")
_CATALOG_ROLE_FIELDS = (
    "id",
    "company",
    "domain",
    "title",
    "location",
    "work_model",
    "channel",
    "source_url",
    "apply_url",
    "posted",
    "salary",
    "score",
    "tier",
    "status",
    "track",
    "keywords",
    "requirements",
    "match",
    "application_dir",
    "posting_active",
    "remote",
    "remote_country",
    "material_manifest",
)


def merge_projection(authoritative: Mapping[str, Any], existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a catalog release from the canonical schema, never from a mutable mirror.

    Callers must capture a divergent mirror with :func:`repair_mirror` before calling
    this function.  ``existing`` is accepted for compatibility but deliberately has no
    authority: direct edits, stale automation controls, and unknown fields remain only
    in the captured conflict evidence.
    """
    del existing
    if not isinstance(authoritative, Mapping):
        raise ProjectionError("authoritative projection must be a mapping")
    roles = authoritative.get("roles")
    if not isinstance(roles, list):
        raise ProjectionError("authoritative projection roles must be a list")
    projected_roles: list[dict[str, Any]] = []
    for role in roles:
        if not isinstance(role, Mapping):
            raise ProjectionError("authoritative projection role must be a mapping")
        projected_roles.append(
            {
                key: role[key]
                for key in _CATALOG_ROLE_FIELDS
                if key in role
            }
        )
    result = {
        key: authoritative[key]
        for key in _CATALOG_TOP_LEVEL_FIELDS
        if key != "roles" and key in authoritative
    }
    result["roles"] = projected_roles
    return result


class ProjectionStore:
    """An immutable JSON/HTML release pair with an atomically updated current pointer."""

    def __init__(self, root: str | Path, projection_name: str) -> None:
        _nofollow_flag()
        if not isinstance(projection_name, str) or projection_name not in _ALLOWED_PROJECTION_NAMES:
            raise ProjectionError("projection name is not an allowed identifier")
        lexical_name = Path(projection_name)
        if lexical_name.is_absolute() or len(lexical_name.parts) != 1 or lexical_name.name != projection_name:
            raise ProjectionError("projection name must be one path component")
        root_path = _reject_symlink_path(Path(root), directory=True)
        self.root = root_path.resolve()
        self.name = projection_name
        self.base = _contained(self.root, self.root / projection_name)
        _reject_symlink_path(self.base)
        self.releases = self.base / "releases"
        self.staging = self.base / "staging"
        self.quarantine = self.base / "quarantine"
        self.pointer = self.base / "current.json"

    def stage(self, payload: Mapping[str, Any], html: str | bytes) -> ProjectionRelease:
        if not isinstance(payload, Mapping):
            raise ProjectionError("projection payload must be a mapping")
        if not isinstance(html, (str, bytes)):
            raise ProjectionError("projection html must be str or bytes")
        json_raw = _canonical(payload) + b"\n"
        html_raw = html.encode("utf-8") if isinstance(html, str) else bytes(html)
        for directory in (self.base, self.releases, self.staging):
            _reject_symlink_path(directory)
            directory.mkdir(parents=True, exist_ok=True)
        _fsync_directory(self.base)
        release_id = uuid4().hex
        staged = self.staging / release_id
        staged.mkdir()
        _write_durable(staged / ".writer.lock", b"active\n")
        _fsync_directory(staged)
        _fsync_directory(self.staging)
        _write_durable(staged / "projection.json", json_raw)
        _write_durable(staged / "dashboard.html", html_raw)
        manifest_base = {"release_id": release_id, "json_sha256": _hash(json_raw), "html_sha256": _hash(html_raw)}
        manifest_raw = _canonical(manifest_base) + b"\n"
        _write_durable(staged / "manifest.json", manifest_raw)
        completion_raw = _canonical({"release_id": release_id, "manifest_sha256": _hash(manifest_raw)}) + b"\n"
        _write_durable(staged / ".complete.json", completion_raw)
        _fsync_directory(staged)
        destination = self.releases / release_id
        os.replace(staged, destination)
        _fsync_directory(self.staging)
        _fsync_directory(self.releases)
        (destination / ".writer.lock").unlink()
        _fsync_directory(destination)
        _fsync_directory(self.releases)
        release = ProjectionRelease(release_id, manifest_base["json_sha256"], manifest_base["html_sha256"], _hash(manifest_raw), destination)
        self._write_pointer(release)
        return release

    def _write_pointer(self, release: ProjectionRelease) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        temporary = self.base / f".current-{uuid4().hex}.tmp"
        raw = _canonical({"release_id": release.release_id, "manifest_sha256": release.manifest_sha256}) + b"\n"
        _write_durable(temporary, raw)
        _fsync_directory(self.base)
        os.replace(temporary, self.pointer)
        _fsync_directory(self.base)

    def _read_release(self, release_id: str) -> ProjectionRelease:
        if not _is_release_id(release_id):
            raise ProjectionError("release identifier is malformed")
        directory = self.releases / release_id
        _reject_symlink_path(directory, directory=True)
        try:
            completion = json.loads(_read_nofollow(directory / ".complete.json"))
            manifest_raw = _read_nofollow(directory / "manifest.json")
            manifest = _decode_json_mapping(manifest_raw)
            json_raw = _read_nofollow(directory / "projection.json")
            html_raw = _read_nofollow(directory / "dashboard.html")
        except (OSError, ProjectionError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProjectionError(f"release is unreadable or malformed: {release_id}") from error
        manifest_sha256 = _hash(manifest_raw)
        if completion != {"release_id": release_id, "manifest_sha256": manifest_sha256}:
            raise ProjectionError(f"release completion record is invalid: {release_id}")
        if manifest.get("release_id") != release_id or manifest.get("json_sha256") != _hash(json_raw) or manifest.get("html_sha256") != _hash(html_raw):
            raise ProjectionError(f"release manifest is invalid: {release_id}")
        return ProjectionRelease(release_id, _hash(json_raw), _hash(html_raw), manifest_sha256, directory)

    def current(self) -> ProjectionRelease | None:
        _reject_symlink_path(self.pointer)
        if not self.pointer.exists():
            _reject_symlink_path(self.releases, directory=True)
            if self.releases.exists() and any(self.releases.iterdir()):
                raise ProjectionError("current projection pointer is absent; recovery will not infer a release")
            return None
        try:
            pointer = _decode_json_mapping(_read_nofollow(self.pointer))
        except (OSError, ProjectionError) as error:
            raise ProjectionError("current projection pointer is unreadable or malformed") from error
        release = self._read_release(str(pointer.get("release_id", "")))
        if pointer.get("manifest_sha256") != release.manifest_sha256:
            raise ProjectionError("current projection pointer does not match release")
        return release

    def recover(self) -> ProjectionRelease | None:
        """Recover completed stages; preserve malformed state for operator review."""
        for directory in (self.base, self.releases, self.staging, self.quarantine):
            _reject_symlink_path(directory)
            directory.mkdir(parents=True, exist_ok=True)
        _fsync_directory(self.base)
        current = self.current()
        for staged in self.staging.iterdir():
            _reject_symlink_path(staged)
            if not staged.is_dir():
                raise ProjectionError(f"staging entry is not a directory: {staged}")
            release = self._read_staged_release(staged)
            if release is None:
                if _entry_exists(staged / ".complete.json"):
                    self._quarantine(staged)
                continue
            destination = self.releases / staged.name
            if destination.exists():
                self._quarantine(staged)
                continue
            lock = staged / ".writer.lock"
            if lock.exists():
                lock.unlink()
                _fsync_directory(staged)
            os.replace(staged, destination)
            _fsync_directory(self.staging)
            _fsync_directory(self.releases)
        if current:
            return current
        if any(self.releases.iterdir()):
            raise ProjectionError("current projection pointer is absent; recovery will not infer a release")
        return None

    def _quarantine(self, staged: Path) -> None:
        """Preserve terminally malformed or colliding stages for operator review."""
        target = self.quarantine / f"{staged.name}-{uuid4().hex}"
        os.replace(staged, target)
        _fsync_directory(self.staging)
        _fsync_directory(self.quarantine)

    @staticmethod
    def _read_staged_release(directory: Path) -> ProjectionRelease | None:
        try:
            completion = json.loads(_read_nofollow(directory / ".complete.json"))
            release_id = directory.name
            if not _is_release_id(release_id):
                return None
            manifest_raw = _read_nofollow(directory / "manifest.json")
            manifest = _decode_json_mapping(manifest_raw)
            json_raw = _read_nofollow(directory / "projection.json")
            html_raw = _read_nofollow(directory / "dashboard.html")
        except (OSError, ProjectionError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        manifest_sha256 = _hash(manifest_raw)
        if completion != {"release_id": release_id, "manifest_sha256": manifest_sha256}:
            return None
        if manifest.get("release_id") != release_id or manifest.get("json_sha256") != _hash(json_raw) or manifest.get("html_sha256") != _hash(html_raw):
            return None
        return ProjectionRelease(release_id, _hash(json_raw), _hash(html_raw), manifest_sha256, directory)


def _entry_exists(path: Path) -> bool:
    """Return whether a directory entry exists without resolving symlinks."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True

def _read_source_once(source: Path) -> tuple[bytes, bool]:
    expanded = _reject_symlink_path(source)
    try:
        return _read_nofollow(expanded), False
    except FileNotFoundError:
        return b"", True


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _validate_base_mirror_sha256(base_mirror_sha256: str | None) -> None:
    if base_mirror_sha256 is not None and (not isinstance(base_mirror_sha256, str) or not _is_hex64(base_mirror_sha256)):
        raise ProjectionError("base mirror digest must be lowercase 64-hex")



def capture_direct_edit(path: str | Path, capture_root: str | Path, *, source_kind: str, base_mirror_sha256: str | None = None, _raw: bytes | None = None, _source_missing: bool | None = None) -> DirectEditCapture:
    """Durably copy one source read for SQL conflict ingestion before mirror repair."""
    if source_kind not in _ALLOWED_SOURCE_KINDS:
        raise ProjectionError("capture source kind is not permitted")
    _validate_base_mirror_sha256(base_mirror_sha256)
    source = Path(path)
    if _raw is None:
        raw, source_missing = _read_source_once(source)
    else:
        raw, source_missing = bytes(_raw), bool(_source_missing)
    digest = _hash(raw)
    captures = _reject_symlink_path(Path(capture_root), directory=True).resolve()
    captures.mkdir(parents=True, exist_ok=True)
    state = "missing" if source_missing else "present"
    filename = f"{source_kind}-{state}-{uuid4().hex}-{digest}.capture"
    target = _contained(captures, captures / filename)
    try:
        _write_durable(target, raw)
        _fsync_directory(captures)
    except FileExistsError:
        try:
            if target.is_symlink() or _read_nofollow(target) != raw:
                raise ProjectionError("existing capture does not match source bytes")
        except OSError as error:
            raise ProjectionError("existing capture cannot be verified") from error
    parsed: Mapping[str, Any] | None = None
    if source_kind == "jobs_json" and not source_missing:
        try:
            decoded = json.loads(raw)
            parsed = decoded if isinstance(decoded, Mapping) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
    return DirectEditCapture(source_kind, filename, digest, base_mirror_sha256, parsed, state)


def repair_mirror(path: str | Path, expected: bytes | str, capture_root: str | Path, *, source_kind: str, base_mirror_sha256: str | None = None) -> MirrorConflict | None:
    """Return a conflict after capture when a fixed mirror diverges; never overwrite it."""
    if not isinstance(expected, (str, bytes)):
        raise ProjectionError("expected mirror content must be str or bytes")
    _validate_base_mirror_sha256(base_mirror_sha256)
    expected_raw = expected.encode("utf-8") if isinstance(expected, str) else bytes(expected)
    observed, source_missing = _read_source_once(Path(path))
    if not source_missing and observed == expected_raw:
        return None
    capture = capture_direct_edit(
        path,
        capture_root,
        source_kind=source_kind,
        base_mirror_sha256=base_mirror_sha256,
        _raw=observed,
        _source_missing=source_missing,
    )
    return MirrorConflict(capture, _hash(expected_raw), _hash(observed))
