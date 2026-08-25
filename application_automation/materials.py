"""Fail-closed material discovery and manifest verification."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
import stat
from pathlib import Path
import re
from typing import Any, Mapping


class MaterialError(ValueError):
    """Raised when a material binding cannot be proven safe and current."""


@dataclass(frozen=True)
class MaterialManifest:
    role_id: str
    application_dir: str
    artifact_name: str
    artifact_sha256: str
    source_template_name: str
    source_template_sha256: str
    job_brief_name: str
    job_brief_sha256: str
    manifest_sha256: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _no_follow_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if not isinstance(flag, int) or flag == 0:
        raise MaterialError("platform lacks required no-follow descriptor support")
    if not isinstance(directory_flag, int) or directory_flag == 0:
        raise MaterialError("platform lacks required no-follow descriptor support")
    if not hasattr(os, "supports_dir_fd") or os.open not in os.supports_dir_fd:
        raise MaterialError("platform lacks required no-follow descriptor support")
    if not hasattr(os, "supports_fd") or os.listdir not in os.supports_fd:
        raise MaterialError("platform lacks required no-follow descriptor support")
    return flag


def _open_directory(root: Path, parts: tuple[str, ...]) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag()
    try:
        descriptor = os.open(root, flags)
    except OSError as error:
        raise MaterialError("cannot safely open repository root") from error
    try:
        for part in parts:
            if part in {"", ".", ".."} or "/" in part:
                raise MaterialError("material path has an invalid component")
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as error:
                raise MaterialError("cannot safely open material directory") from error
            os.close(descriptor)
            descriptor = child
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise MaterialError("material path component is not a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _hash_file(directory_fd: int, name: str) -> str:
    """Hash the exact regular file opened relative to a retained directory descriptor."""
    if not name or "/" in name:
        raise MaterialError("material name is invalid")
    try:
        descriptor = os.open(name, os.O_RDONLY | _no_follow_flag(), dir_fd=directory_fd)
    except OSError as error:
        raise MaterialError(f"cannot safely open material: {name}") from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise MaterialError(f"material is not a regular file: {name}")
        digest = sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _application_parts(root: Path, value: str | Path) -> tuple[str, ...]:
    raw = Path(value)
    if raw.is_absolute():
        try:
            raw = raw.relative_to(root)
        except ValueError as error:
            raise MaterialError("material path is outside repository root") from error
    parts = raw.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise MaterialError("application directory is invalid")
    return parts


def _pick(directory_fd: int, names: tuple[str, ...], description: str) -> str:
    """Select a unique case-insensitive name from descriptor-anchored directory entries."""
    recognized = {name.casefold(): name for name in names}
    matches: dict[str, list[str]] = {name: [] for name in names}
    try:
        entries = os.listdir(directory_fd)
    except OSError as error:
        raise MaterialError(f"cannot inspect {description}") from error
    for name in entries:
        intended_name = recognized.get(name.casefold())
        if intended_name is None:
            continue
        try:
            descriptor = os.open(name, os.O_RDONLY | _no_follow_flag(), dir_fd=directory_fd)
        except OSError as error:
            raise MaterialError(f"cannot safely open {description}: {name}") from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise MaterialError(f"{description} is not a regular file: {name}")
        finally:
            os.close(descriptor)
        matches[intended_name].append(name)
    for intended_name, variants in matches.items():
        if len(variants) > 1:
            raise MaterialError(f"ambiguous case-colliding {description}: {intended_name}")
    for name in names:
        if matches[name]:
            return matches[name][0]
    raise MaterialError(f"missing role-specific {description}")
_ROLE_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?\Z")

# Personalized resume artifacts follow "<Name> Resume.pdf/docx"; the basename is
# whatever the user configured in config/user-profile.json (files.resume_basename).
RESUME_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9 .'\-]{0,80} Resume\.(pdf|docx)\Z")


def _artifact_candidates(directory_fd: int) -> tuple[str, ...]:
    """Generic artifact names plus personalized '<Name> Resume.*' entries, PDFs preferred."""
    try:
        entries = os.listdir(directory_fd)
    except OSError as error:
        raise MaterialError("cannot inspect PDF or DOCX artifact") from error
    personalized = [name for name in entries if RESUME_NAME_PATTERN.fullmatch(name)]
    personalized_pdf = sorted(name for name in personalized if name.endswith(".pdf"))
    personalized_docx = sorted(name for name in personalized if name.endswith(".docx"))
    return (
        *personalized_pdf, "resume.pdf", "application.pdf",
        *personalized_docx, "resume.docx", "application.docx",
    )


def build_manifest(repo_root: str | Path, role_id: str, application_dir: str | Path) -> MaterialManifest:
    """Build a deterministic, descriptor-anchored role-bound manifest."""
    if not isinstance(role_id, str) or not _ROLE_ID.fullmatch(role_id):
        raise MaterialError("role ID must be a canonical non-empty slug")
    root = Path(repo_root).expanduser().resolve(strict=True)
    parts = _application_parts(root, application_dir)
    directory_fd = _open_directory(root, parts)
    try:
        artifact = _pick(
            directory_fd,
            _artifact_candidates(directory_fd),
            "PDF or DOCX artifact",
        )
        template = _pick(directory_fd, ("resume.md",), "source template")
        brief = _pick(directory_fd, ("job.md",), "job brief")
        base = {
            "role_id": role_id,
            "application_dir": Path(*parts).as_posix(),
            "artifact_name": artifact,
            "artifact_sha256": _hash_file(directory_fd, artifact),
            "source_template_name": template,
            "source_template_sha256": _hash_file(directory_fd, template),
            "job_brief_name": brief,
            "job_brief_sha256": _hash_file(directory_fd, brief),
        }
    finally:
        os.close(directory_fd)
    return MaterialManifest(**base, manifest_sha256=sha256(_canonical_json(base)).hexdigest())


def verify_manifest(repo_root: str | Path, role_id: str, application_dir: str | Path, manifest: MaterialManifest | Mapping[str, Any]) -> MaterialManifest:
    """Reject stale, cross-role, tampered, or structurally unsafe material bindings."""
    supplied = manifest if isinstance(manifest, MaterialManifest) else MaterialManifest(**manifest)
    if supplied.role_id != role_id:
        raise MaterialError("manifest role does not match requested role")
    current = build_manifest(repo_root, role_id, application_dir)
    if supplied != current:
        raise MaterialError("material manifest is stale or has been tampered with")
    return current


resolve_materials = build_manifest
