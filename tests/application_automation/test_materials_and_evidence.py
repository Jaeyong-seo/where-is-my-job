from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
import application_automation.materials as materials
import application_automation.projection as projection

from application_automation.aside import AsideProtocolError, PauseReason, decode_result
from application_automation.evidence import EvidenceError, append_event, make_evidence
from application_automation.materials import MaterialError, build_manifest, verify_manifest
from application_automation.projection import ProjectionError, ProjectionStore, capture_direct_edit, repair_mirror


def _materials(tmp_path: Path) -> Path:
    role = tmp_path / "applications" / "provider" / "role"
    role.mkdir(parents=True)
    (role / "resume.pdf").write_bytes(b"resume")
    (role / "resume.md").write_text("source", encoding="utf-8")
    (role / "job.md").write_text("brief", encoding="utf-8")
    return role


def test_materials_reject_broken_preferred_link_and_case_collision(tmp_path: Path) -> None:
    role = _materials(tmp_path)
    (role / "Jane Doe Resume.pdf").symlink_to(role / "missing.pdf")
    with pytest.raises(MaterialError, match="cannot safely open"):
        build_manifest(tmp_path, "provider-role", role)
    (role / "Jane Doe Resume.pdf").unlink()
    (role / "RESUME.PDF").write_bytes(b"second")
    if (role / "RESUME.PDF").samefile(role / "resume.pdf"):
        pytest.skip("case-insensitive filesystem cannot represent a case collision")
    with pytest.raises(MaterialError, match="case-colliding"):
        build_manifest(tmp_path, "provider-role", role)


def test_materials_prefer_pdf_when_pdf_and_docx_are_present(tmp_path: Path) -> None:
    role = _materials(tmp_path)
    (role / "resume.docx").write_bytes(b"docx")
    manifest = build_manifest(tmp_path, "provider-role", role)
    assert manifest.artifact_name == "resume.pdf"
    assert manifest.artifact_sha256 == "a83a31320d921b888a48fa5edd0b4b5a29984de6e96bf7b8ac7d29ba06caf616"
    assert manifest.source_template_sha256 == "41cf6794ba4200b839c53531555f0f3998df4cbb01a4d5cb0b94e3ca5e23947d"
    assert manifest.job_brief_sha256 == "29a8825bd242f14386ee528d76e0e8f1e38f3c8c4047d7b2d6df7493368a17d0"


def test_materials_fail_closed_without_descriptor_no_follow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(materials.os, "O_NOFOLLOW", 0)
    with pytest.raises(MaterialError, match="no-follow"):
        build_manifest(tmp_path, "provider-role", _materials(tmp_path))


def test_materials_fail_closed_without_directory_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(materials.os, "O_DIRECTORY", 0)
    with pytest.raises(MaterialError, match="no-follow"):
        build_manifest(tmp_path, "provider-role", _materials(tmp_path))


def test_materials_fail_closed_without_dir_fd_open_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(materials.os, "supports_dir_fd", frozenset())
    with pytest.raises(MaterialError, match="no-follow"):
        build_manifest(tmp_path, "provider-role", _materials(tmp_path))


def test_materials_fail_closed_without_listdir_fd_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(materials.os, "supports_fd", frozenset())
    with pytest.raises(MaterialError, match="no-follow"):
        build_manifest(tmp_path, "provider-role", _materials(tmp_path))


def test_materials_reject_ancestor_escape_and_replacement_and_rehashes_exact_bytes(tmp_path: Path) -> None:
    role = _materials(tmp_path)
    outside = tmp_path / "outside"
    decoy = outside / "role"
    decoy.mkdir(parents=True)
    for name, content in (("resume.pdf", b"outside"), ("resume.md", b"outside"), ("job.md", b"outside")):
        (decoy / name).write_bytes(content)
    provider = role.parent
    moved = tmp_path / "provider-real"
    provider.rename(moved)
    provider.symlink_to(outside, target_is_directory=True)
    with pytest.raises((MaterialError, OSError)):
        build_manifest(tmp_path, "provider-role", role)
    provider.unlink()
    moved.rename(provider)
    first = build_manifest(tmp_path, "provider-role", role)
    (role / "resume.pdf").replace(role / "resume.md")
    with pytest.raises(MaterialError, match="missing role-specific"):
        build_manifest(tmp_path, "provider-role", role)
    (role / "resume.pdf").write_bytes(b"replacement")
    second = build_manifest(tmp_path, "provider-role", role)
    assert first.artifact_sha256 != second.artifact_sha256
    assert second == build_manifest(tmp_path, "provider-role", role)

def test_verify_manifest_rejects_stale_tampered_cross_role_and_escaping_paths(tmp_path: Path) -> None:
    role = _materials(tmp_path)
    manifest = build_manifest(tmp_path, "provider-role", role)
    verify_manifest(tmp_path, "provider-role", role, manifest)
    (role / "resume.pdf").write_bytes(b"stale bytes")
    with pytest.raises(MaterialError, match="stale"):
        verify_manifest(tmp_path, "provider-role", role, manifest)
    current = build_manifest(tmp_path, "provider-role", role)
    with pytest.raises(MaterialError, match="stale"):
        verify_manifest(tmp_path, "provider-role", role, {**current.to_dict(), "artifact_name": "other.pdf"})
    with pytest.raises(MaterialError, match="role does not match"):
        verify_manifest(tmp_path, "other-role", role, current)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    with pytest.raises(MaterialError, match="outside"):
        verify_manifest(tmp_path, "provider-role", outside, current)
    with pytest.raises(MaterialError, match="invalid"):
        verify_manifest(tmp_path, "provider-role", "applications/provider/role/../role", current)


def test_canonical_aside_schema_operations_match_production_decoder() -> None:
    schema = json.loads((Path(__file__).parents[2] / "application_automation" / "aside-result.schema.json").read_text())
    branches = {branch["properties"]["operation"]["const"] for branch in schema["oneOf"]}
    assert branches == {"inspect", "fill", "submit", "observe"}
    assert all(branch["additionalProperties"] is False for branch in schema["oneOf"])
    definitions = schema["$defs"]
    pause_reasons = {reason.value for reason in PauseReason}
    assert set(definitions["non_null_pause_reason"]["enum"]) == pause_reasons
    assert len(definitions["pause_reason"]["anyOf"]) == 2
    assert {"type": "null"} in definitions["pause_reason"]["anyOf"]
    assert {"$ref": "#/$defs/non_null_pause_reason"} in definitions["pause_reason"]["anyOf"]
    assert definitions["nullable_sha256"]["anyOf"][1]["pattern"] == "^[0-9a-f]{64}$"
    conditional_refs = {
        "fill": {"#/$defs/paused_fill", "#/$defs/unpaused_fill"},
        "submit": {
            "#/$defs/paused_submit",
            "#/$defs/confirmed_submit",
            "#/$defs/manual_submit",
        },
        "observe": {
            "#/$defs/paused_observation",
            "#/$defs/confirmed_observation",
            "#/$defs/unconfirmed_observation",
        },
    }
    for operation, expected_refs in conditional_refs.items():
        branch = next(
            branch
            for branch in schema["oneOf"]
            if branch["properties"]["operation"]["const"] == operation
        )
        assert {condition["$ref"] for condition in branch["oneOf"]} == expected_refs
    assert definitions["paused_fill"]["required"] == ["pause_reason"]
    assert definitions["paused_fill"]["properties"] == {
        "pause_reason": {"$ref": "#/$defs/non_null_pause_reason"},
        "filled": {"const": False},
        "attached_resume_sha256": {"type": "null"},
    }
    assert definitions["unpaused_fill"]["properties"] == {
        "pause_reason": {"type": "null"},
        "filled": {"const": True},
    }
    assert definitions["paused_submit"]["required"] == ["pause_reason"]
    assert definitions["paused_submit"]["properties"] == {
        "pause_reason": {"$ref": "#/$defs/non_null_pause_reason"},
        "started": {"const": False},
        "confirmed": {"const": False},
        "manual_follow_up": {"const": False},
        "receipt_id": {"type": "null"},
    }
    assert definitions["confirmed_submit"]["properties"] == {
        "pause_reason": {"type": "null"},
        "started": {"const": True},
        "confirmed": {"const": True},
        "manual_follow_up": {"const": False},
        "receipt_id": {"type": "string", "minLength": 1},
    }
    assert definitions["manual_submit"]["properties"] == {
        "pause_reason": {"type": "null"},
        "started": {"const": True},
        "confirmed": {"const": False},
        "manual_follow_up": {"const": True},
        "receipt_id": {"type": "null"},
    }
    assert definitions["paused_observation"]["required"] == ["pause_reason"]
    assert definitions["paused_observation"]["properties"] == {
        "pause_reason": {"$ref": "#/$defs/non_null_pause_reason"},
        "state": {"const": "awaiting_user"},
        "receipt_id": {"type": "null"},
    }
    assert definitions["confirmed_observation"]["properties"] == {
        "pause_reason": {"type": "null"},
        "state": {"const": "confirmed"},
        "receipt_id": {"type": "string", "minLength": 1},
    }
    assert definitions["unconfirmed_observation"]["properties"] == {
        "pause_reason": {"type": "null"},
        "state": {"enum": ["not_started", "manual_follow_up"]},
        "receipt_id": {"type": "null"},
    }
    common = {
        "schema": "application_automation.aside.v1",
        "domain": "fixture.local",
        "page_fingerprint": "page",
        "form_fingerprint": "form",
    }
    examples = {
        "inspect": {"fields": ["name"]},
        "fill": {"filled": True, "attached_resume_sha256": "a" * 64},
        "submit": {"started": True, "confirmed": True, "manual_follow_up": False, "receipt_id": "receipt"},
        "observe": {"state": "confirmed", "receipt_id": "receipt"},
    }
    for operation, fields in examples.items():
        result = {**common, "operation": operation, **fields}
        assert decode_result(result, operation) == result
        assert make_evidence("form", {"operation": operation}).metadata["operation"] == operation
    paused_examples = {
        "inspect": {"fields": ["name"]},
        "fill": {"filled": False, "attached_resume_sha256": None},
        "submit": {
            "started": False,
            "confirmed": False,
            "manual_follow_up": False,
            "receipt_id": None,
        },
        "observe": {"state": "awaiting_user", "receipt_id": None},
    }
    for pause_reason in pause_reasons:
        for operation, fields in paused_examples.items():
            result = {
                **common,
                "operation": operation,
                **fields,
                "pause_reason": pause_reason,
            }
            assert decode_result(result, operation) == result

    with pytest.raises(AsideProtocolError, match="operation-inappropriate"):
        decode_result({**common, "operation": "inspect", "fields": ["name"], "extra": True}, "inspect")
    with pytest.raises(AsideProtocolError, match="operation-inappropriate"):
        decode_result({**common, "operation": "inspect", "fields": ["name"], "filled": True}, "inspect")
    with pytest.raises(AsideProtocolError, match="inspect fields"):
        decode_result({**common, "operation": "inspect"}, "inspect")
    with pytest.raises(AsideProtocolError, match="invalid attached_resume_sha256"):
        decode_result({**common, "operation": "fill", "filled": True, "attached_resume_sha256": "bad"}, "fill")
    with pytest.raises(AsideProtocolError, match="unknown pause"):
        decode_result({**common, "operation": "observe", "state": "awaiting_user", "receipt_id": None, "pause_reason": "invalid"}, "observe")
    with pytest.raises(AsideProtocolError, match="paused observation"):
        decode_result({**common, "operation": "observe", "state": "awaiting_user", "receipt_id": "receipt", "pause_reason": "captcha"}, "observe")

@pytest.mark.parametrize("role_id", ["", "../role", "Role", "role/child"])
def test_materials_reject_noncanonical_role_ids(tmp_path: Path, role_id: str) -> None:
    with pytest.raises(MaterialError):
        build_manifest(tmp_path, role_id, _materials(tmp_path))


def test_evidence_metadata_allowlist_aware_timestamps_and_origin_only_urls() -> None:
    with pytest.raises(EvidenceError):
        make_evidence("html", {"answer": "candidate answer"})
    with pytest.raises(EvidenceError):
        make_evidence("html", {"session_id": "secret"})
    with pytest.raises(EvidenceError):
        make_evidence("html", {"provider": "greenhouse"}, observed_at=datetime(2026, 1, 1))
    record = make_evidence("html", {"provider": "greenhouse"}, source_url="https://example.test/private/path?token=secret")
    assert record.source_url == "https://example.test"
    event = append_event((), "observed", {"provider": "greenhouse", "field_count": 3})
    assert event.metadata == {"provider": "greenhouse", "field_count": 3}
    for malformed in (0, object()):
        with pytest.raises(EvidenceError, match="bytes or a string"):
            make_evidence("html", {"provider": "greenhouse"}, content=malformed)


def test_projection_quarantines_malformed_stages_and_surfaces_corrupt_releases(tmp_path: Path) -> None:
    for name in ("../jobs", "/tmp/jobs", "jobs/other", "unknown"):
        with pytest.raises(ProjectionError):
            ProjectionStore(tmp_path, name)
    store = ProjectionStore(tmp_path, "jobs")
    store.staging.mkdir(parents=True)
    malformed = store.staging / ("a" * 32)
    malformed.mkdir()
    (malformed / ".complete.json").write_text("not-json", encoding="utf-8")
    assert store.recover() is None
    quarantined = list(store.quarantine.iterdir())
    assert len(quarantined) == 1
    assert (quarantined[0] / ".complete.json").read_text(encoding="utf-8") == "not-json"

    release = store.stage({"roles": []}, "<html></html>")
    (release.path / ".complete.json").unlink()
    with pytest.raises(ProjectionError, match="unreadable or malformed"):
        store.current()
    with pytest.raises(ProjectionError, match="unreadable or malformed"):
        store.recover()


def test_capture_is_event_unique_and_rejects_symlink_sources_and_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing.json"
    conflict = repair_mirror(missing, b"expected", tmp_path / "captures", source_kind="jobs_json")
    assert conflict is not None and conflict.capture.source_state == "missing"

    source = tmp_path / "jobs.json"
    source.write_bytes(b'{"manual":true}')

    class _FixedUuid:
        hex = "f" * 32

    monkeypatch.setattr(projection, "uuid4", lambda: _FixedUuid())
    capture = capture_direct_edit(source, tmp_path / "captures", source_kind="jobs_json")
    target = tmp_path / "captures" / capture.capture_relative_path
    assert target.read_bytes() == source.read_bytes()
    reused = capture_direct_edit(source, tmp_path / "captures", source_kind="jobs_json")
    assert reused.capture_relative_path == capture.capture_relative_path
    assert target.read_bytes() == source.read_bytes()

    source.write_bytes(b'{"manual":false}')
    mismatch = capture_direct_edit(source, tmp_path / "other-captures", source_kind="jobs_json")
    collision = tmp_path / "captures" / mismatch.capture_relative_path
    collision.write_bytes(b"wrong bytes")
    with pytest.raises(ProjectionError, match="does not match source bytes"):
        capture_direct_edit(source, tmp_path / "captures", source_kind="jobs_json")

    source_link = tmp_path / "jobs-link.json"
    source_link.symlink_to(source)
    with pytest.raises(ProjectionError, match="symbolic links"):
        capture_direct_edit(source_link, tmp_path / "captures", source_kind="jobs_json")

    captures_link = tmp_path / "captures-link"
    captures_link.symlink_to(tmp_path / "captures", target_is_directory=True)
    with pytest.raises(ProjectionError, match="symbolic links"):
        capture_direct_edit(source, captures_link, source_kind="jobs_json")


def test_capture_reads_the_expanded_tilde_source_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "jobs.json").write_bytes(b'{"manual":true}')
    capture = capture_direct_edit(Path("~/jobs.json"), tmp_path / "captures", source_kind="jobs_json")
    assert capture.source_state == "present"
    assert capture.raw_sha256 == hashlib.sha256(b'{"manual":true}').hexdigest()
    assert (tmp_path / "captures" / capture.capture_relative_path).read_bytes() == b'{"manual":true}'


def test_capture_rejects_ancestor_symlink_component_in_relative_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "jobs.json").write_bytes(b'{"manual":true}')
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProjectionError, match="symbolic links"):
        capture_direct_edit(Path("linked/jobs.json"), tmp_path / "captures", source_kind="jobs_json")
    assert not (tmp_path / "captures").exists()


@pytest.mark.parametrize("base_mirror_sha256", ["A" * 64, "a" * 63, "g" * 64, "not-hex-at-all", 12345])
def test_capture_rejects_malformed_base_mirror_digest_without_reading_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_mirror_sha256: object,
) -> None:
    source = tmp_path / "jobs.json"
    source.write_bytes(b'{"manual":true}')
    opened: list[Path] = []
    original_read_nofollow = projection._read_nofollow
    monkeypatch.setattr(
        projection,
        "_read_nofollow",
        lambda path: (opened.append(path), original_read_nofollow(path))[1],
    )
    with pytest.raises(ProjectionError, match="64-hex"):
        capture_direct_edit(
            source,
            tmp_path / "captures",
            source_kind="jobs_json",
            base_mirror_sha256=base_mirror_sha256,  # type: ignore[arg-type]
        )
    assert opened == []
    assert not (tmp_path / "captures").exists()


def test_capture_accepts_lowercase_64_hex_base_mirror_digest(tmp_path: Path) -> None:
    source = tmp_path / "jobs.json"
    source.write_bytes(b'{"manual":true}')
    digest = "a" * 64
    capture = capture_direct_edit(source, tmp_path / "captures", source_kind="jobs_json", base_mirror_sha256=digest)
    assert capture.base_mirror_sha256 == digest


@pytest.mark.parametrize("expected", [42, None, ["expected"], 3.5])
def test_repair_mirror_rejects_non_str_bytes_expected_content_without_reading_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected: object,
) -> None:
    source = tmp_path / "jobs.json"
    source.write_bytes(b'{"manual":true}')
    opened: list[Path] = []
    original_read_nofollow = projection._read_nofollow
    monkeypatch.setattr(
        projection,
        "_read_nofollow",
        lambda path: (opened.append(path), original_read_nofollow(path))[1],
    )
    with pytest.raises(ProjectionError, match="str or bytes"):
        repair_mirror(source, expected, tmp_path / "captures", source_kind="jobs_json")  # type: ignore[arg-type]
    assert opened == []
    assert not (tmp_path / "captures").exists()


def test_repair_mirror_rejects_malformed_base_mirror_digest_without_reading_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "jobs.json"
    source.write_bytes(b'{"manual":true}')
    opened: list[Path] = []
    original_read_nofollow = projection._read_nofollow
    monkeypatch.setattr(
        projection,
        "_read_nofollow",
        lambda path: (opened.append(path), original_read_nofollow(path))[1],
    )
    with pytest.raises(ProjectionError, match="64-hex"):
        repair_mirror(source, b"expected", tmp_path / "captures", source_kind="jobs_json", base_mirror_sha256="short")
    assert opened == []
    assert not (tmp_path / "captures").exists()
