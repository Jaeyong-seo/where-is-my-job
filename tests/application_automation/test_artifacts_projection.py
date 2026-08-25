from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import os

import pytest

from application_automation.evidence import EvidenceError, append_event, make_evidence, verify_event_chain
from application_automation.materials import MaterialError, build_manifest, verify_manifest
from application_automation.projection import ProjectionError, ProjectionStore, capture_direct_edit, merge_projection, repair_mirror
from application_automation.status import append_event as append_status_event, current_status
from application_automation.store import apply_migrations, connect
_MALFORMED_METADATA = (
    ("invalid_utf8", b"\xff"),
    ("invalid_json", b"{"),
    ("null", b"null"),
    ("boolean", b"true"),
    ("number", b"0"),
    ("string", b'"metadata"'),
    ("array", b"[]"),
)




def material_tree(tmp_path: Path) -> tuple[Path, Path]:
    role = tmp_path / "applications" / "gumloop" / "design-engineer"
    role.mkdir(parents=True)
    (role / "Jane Doe Resume.pdf").write_bytes(b"preferred-pdf")
    (role / "Jane Doe Resume.docx").write_bytes(b"docx")
    (role / "resume.pdf").write_bytes(b"generic-pdf")
    (role / "resume.md").write_text("source", encoding="utf-8")
    (role / "job.md").write_text("brief", encoding="utf-8")
    return tmp_path, role


def test_materials_bind_catalog_role_to_actual_convention_path(tmp_path: Path) -> None:
    root, role = material_tree(tmp_path)
    relative = "applications/gumloop/design-engineer"
    manifest = build_manifest(root, "gumloop-design-engineer", relative)
    assert manifest.application_dir == relative
    assert manifest.role_id == "gumloop-design-engineer"
    assert manifest.artifact_name == "Jane Doe Resume.pdf"
    assert build_manifest(root, "gumloop-design-engineer", role) == manifest
    assert verify_manifest(root, "gumloop-design-engineer", role, manifest) == manifest
    (role / "Jane Doe Resume.pdf").write_bytes(b"changed")
    with pytest.raises(MaterialError):
        verify_manifest(root, "gumloop-design-engineer", role, manifest)


def test_manifest_cannot_be_reused_for_another_role_or_path(tmp_path: Path) -> None:
    root, role = material_tree(tmp_path)
    manifest = build_manifest(root, "gumloop-design-engineer", role)
    other = root / "applications" / "other" / "role"
    other.mkdir(parents=True)
    for name in ("Jane Doe Resume.pdf", "resume.md", "job.md"):
        (other / name).write_bytes((role / name).read_bytes())
    with pytest.raises(MaterialError):
        verify_manifest(root, "other-role", role, manifest)
    with pytest.raises(MaterialError):
        verify_manifest(root, "gumloop-design-engineer", other, manifest)


def test_materials_reject_traversal_and_links(tmp_path: Path) -> None:
    root, role = material_tree(tmp_path)
    with pytest.raises(MaterialError):
        build_manifest(root, "role-1", "../role-1")
    linked = root / "linked"
    linked.symlink_to(role, target_is_directory=True)
    with pytest.raises(MaterialError):
        build_manifest(root, "role-1", linked)


def test_evidence_rejects_sensitive_metadata_quarantines_and_detects_tampering() -> None:
    with pytest.raises(EvidenceError, match="not permitted"):
        make_evidence(
            "html",
            {"email": "person@example.com", "nested": {"token": "secret"}},
        )
    record = make_evidence(
        "html",
        {
            "provider": "greenhouse",
            "operation": "inspect",
            "outcome": "completed",
            "field_count": 3,
        },
        source_url="HTTPS://Example.COM:443/path?x=1",
        content="<html>private</html>",
    )
    assert record.source_url == "https://example.com"
    assert record.content and record.content.state == "quarantined"
    first = append_event((), "observed", record.metadata, occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = append_event((first,), "stored", {}, occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert verify_event_chain((first, second))
    assert not verify_event_chain((first, second.__class__(**{**second.__dict__, "event_type": "changed"})))


def test_projection_repairs_only_exact_allowlisted_catalog_bytes_after_conflicts(tmp_path: Path) -> None:
    authoritative_role = {
        "application_dir": "applications/fixture/r1",
        "apply_url": "https://apply.fixture.invalid/r1",
        "channel": "board",
        "company": "Fixture Co",
        "domain": "fixture.invalid",
        "id": "r1",
        "keywords": ["python", "systems"],
        "location": "Remote",
        "match": {"score": 8},
        "material_manifest": "manifest-r1",
        "posted": "2026-07-01",
        "posting_active": True,
        "remote": True,
        "remote_country": "CA",
        "requirements": ["build"],
        "salary": "120000",
        "score": 8.5,
        "source_url": "https://fixture.invalid/r1",
        "status": "materials_ready",
        "tier": "A",
        "title": "Engineer",
        "track": "primary",
        "work_model": "remote",
        "unknown_authority": "ignore",
    }
    authoritative = {
        "generated_at": "2026-07-15",
        "candidate": {"email": "candidate@example.test"},
        "roles": [authoritative_role],
        "unknown_authority": True,
    }
    direct_role = {
        "application_dir": "attacker/application",
        "apply_url": "https://attacker.invalid/apply",
        "channel": "attacker-channel",
        "company": "Attacker Co",
        "domain": "attacker.invalid",
        "id": "attacker-id",
        "keywords": ["attacker"],
        "location": "Attacker location",
        "match": {"score": 0},
        "material_manifest": "attacker-manifest",
        "posted": "2099-01-01",
        "posting_active": False,
        "remote": False,
        "remote_country": "ZZ",
        "requirements": ["attacker requirement"],
        "salary": "0",
        "score": 0,
        "source_url": "https://attacker.invalid/source",
        "status": "applied",
        "tier": "Z",
        "title": "Attacker title",
        "track": "attacker-track",
        "work_model": "onsite",
        "unknown": "edit",
    }
    expected_raw = (
        b'{"generated_at":"2026-07-15","roles":[{"application_dir":"applications/fixture/r1",'
        b'"apply_url":"https://apply.fixture.invalid/r1","channel":"board","company":"Fixture Co",'
        b'"domain":"fixture.invalid","id":"r1","keywords":["python","systems"],"location":"Remote",'
        b'"match":{"score":8},"material_manifest":"manifest-r1","posted":"2026-07-01","posting_active":true,'
        b'"remote":true,"remote_country":"CA","requirements":["build"],"salary":"120000","score":8.5,'
        b'"source_url":"https://fixture.invalid/r1","status":"materials_ready","tier":"A",'
        b'"title":"Engineer","track":"primary","work_model":"remote"}]}\n'
    )
    merged = merge_projection(authoritative)
    serialized_merged = json.dumps(merged, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"

    assert serialized_merged == expected_raw
    assert b"candidate@example.test" not in expected_raw
    assert b"unknown_authority" not in expected_raw
    assert b"unknown" not in expected_raw

    direct_edits = {
        "all_projected_fields_overwritten": {
            "candidate": {"email": "attacker@example.test"},
            "unknown_authority": True,
            "roles": [direct_role],
        },
        "role_deleted": {"roles": []},
        "role_added": {"roles": [direct_role, {"id": "added", "company": "Added Co"}]},
        "role_duplicated": {"roles": [direct_role, direct_role]},
        "roles_not_a_list": {"roles": {"id": "r1"}},
        "role_not_an_object": {"roles": [direct_role, "malformed"]},
        "roles_null": {"roles": None},
    }
    captures = tmp_path / "captures"
    for name, direct_edit in direct_edits.items():
        source = tmp_path / f"{name}.json"
        direct_raw = json.dumps(direct_edit, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        assert (
            json.dumps(
                merge_projection(authoritative, direct_edit),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
            == expected_raw
        )
        source.write_bytes(direct_raw)

        conflict = repair_mirror(source, serialized_merged, captures, source_kind="jobs_json")

        assert conflict is not None
        assert conflict.capture.parsed_json == direct_edit
        assert (captures / conflict.capture.capture_relative_path).read_bytes() == direct_raw
        assert source.read_bytes() == direct_raw

        source.write_bytes(serialized_merged)
        assert repair_mirror(source, serialized_merged, captures, source_kind="jobs_json") is None
        assert source.read_bytes() == expected_raw

    store = ProjectionStore(tmp_path, "jobs")
    release = store.stage(merged, "<html></html>")
    assert (release.path / "projection.json").read_bytes() == expected_raw
    assert store.current() == release
    store.pointer.unlink()
    with pytest.raises(ProjectionError, match="pointer is absent"):
        store.current()
    with pytest.raises(ProjectionError, match="pointer is absent"):
        store.recover()


@pytest.mark.parametrize(("case", "raw"), _MALFORMED_METADATA)
def test_current_rejects_non_object_or_undecodable_pointer_metadata(tmp_path: Path, case: str, raw: bytes) -> None:
    store = ProjectionStore(tmp_path / case, "jobs")
    store.stage({}, "<html></html>")
    store.pointer.write_bytes(raw)

    with pytest.raises(ProjectionError, match="pointer is unreadable or malformed"):
        store.current()

    assert store.pointer.read_bytes() == raw


@pytest.mark.parametrize(("case", "raw"), _MALFORMED_METADATA)
def test_current_rejects_non_object_or_undecodable_manifest_metadata(tmp_path: Path, case: str, raw: bytes) -> None:
    store = ProjectionStore(tmp_path / case, "jobs")
    release = store.stage({}, "<html></html>")
    manifest = release.path / "manifest.json"
    manifest.write_bytes(raw)

    with pytest.raises(ProjectionError, match="release is unreadable or malformed"):
        store.current()

    assert manifest.read_bytes() == raw


@pytest.mark.parametrize(("case", "raw"), _MALFORMED_METADATA)
def test_recover_quarantines_staged_non_object_or_undecodable_manifest_metadata(tmp_path: Path, case: str, raw: bytes) -> None:
    store = ProjectionStore(tmp_path / case, "jobs")
    release = store.stage({}, "<html></html>")
    staged = store.staging / release.release_id
    os.replace(release.path, staged)
    store.pointer.unlink()
    manifest = staged / "manifest.json"
    manifest.write_bytes(raw)

    assert store.recover() is None

    quarantined = next(store.quarantine.iterdir())
    assert (quarantined / "manifest.json").read_bytes() == raw

@pytest.mark.parametrize(
    "authoritative",
    (
        {},
        {"roles": None},
        {"roles": {}},
        {"roles": "not-a-list"},
        {"roles": [None]},
        {"roles": ["not-an-object"]},
        {"roles": [b"not-an-object"]},
    ),
)
def test_merge_projection_rejects_malformed_authoritative_roles(authoritative: object) -> None:
    with pytest.raises(ProjectionError, match="authoritative projection"):
        merge_projection(authoritative)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", (b"\xff", b"{", b"[]"))
def test_recover_quarantines_staged_invalid_completion_metadata(tmp_path: Path, raw: bytes) -> None:
    store = ProjectionStore(tmp_path, "jobs")
    release = store.stage({}, "<html></html>")
    staged = store.staging / release.release_id
    os.replace(release.path, staged)
    store.pointer.unlink()
    (staged / ".complete.json").write_bytes(raw)

    assert store.recover() is None

    quarantined = next(store.quarantine.iterdir())
    assert (quarantined / ".complete.json").read_bytes() == raw
    assert not any(store.staging.iterdir())


def test_recover_quarantines_staged_dangling_completion_symlink(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path, "jobs")
    release = store.stage({}, "<html></html>")
    staged = store.staging / release.release_id
    os.replace(release.path, staged)
    store.pointer.unlink()
    completion = staged / ".complete.json"
    completion.unlink()
    completion.symlink_to("missing-completion.json")

    assert store.recover() is None

    quarantined = next(store.quarantine.iterdir())
    assert (quarantined / ".complete.json").is_symlink()
    assert not any(store.staging.iterdir())


def test_projection_fails_closed_without_nofollow_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    with pytest.raises(ProjectionError, match="does not support O_NOFOLLOW"):
        ProjectionStore(tmp_path, "jobs")

    assert not (tmp_path / "jobs").exists()
    assert not (tmp_path / "jobs" / "releases").exists()


@pytest.mark.parametrize("payload", [None, "not-a-mapping", 42, ["role"]])
def test_stage_rejects_non_mapping_payload_without_side_effects(tmp_path: Path, payload: object) -> None:
    store = ProjectionStore(tmp_path, "jobs")
    with pytest.raises(ProjectionError, match="mapping"):
        store.stage(payload, "<html></html>")  # type: ignore[arg-type]
    assert not store.base.exists()


@pytest.mark.parametrize("html", [42, None, ["<html/>"], 3.5])
def test_stage_rejects_non_str_bytes_html_without_side_effects(tmp_path: Path, html: object) -> None:
    store = ProjectionStore(tmp_path, "jobs")
    with pytest.raises(ProjectionError, match="html must be str or bytes"):
        store.stage({"generated_at": "now", "roles": []}, html)  # type: ignore[arg-type]
    assert not store.base.exists()


def test_stage_rejects_nan_payload_without_side_effects(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path, "jobs")
    with pytest.raises(ProjectionError, match="not JSON-serializable"):
        store.stage({"generated_at": "now", "roles": [], "score": float("nan")}, "<html></html>")
    assert not store.base.exists()
    assert not store.staging.exists()
    assert not store.releases.exists()


def test_stage_accepts_valid_mapping_and_str_html(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path, "jobs")
    release = store.stage({"generated_at": "now", "roles": []}, "<html></html>")
    assert store.current() is not None and store.current().release_id == release.release_id


def status_connection(tmp_path: Path):
    connection = connect(tmp_path / "status.sqlite3")
    apply_migrations(connection)
    now = datetime(2026, 7, 15, tzinfo=timezone.utc).isoformat()
    connection.execute("INSERT INTO candidate_profiles VALUES('profile','Fixture','active',?,1)", (now,))
    for role_id in ("role-1", "role-2"):
        connection.execute(
            "INSERT INTO roles VALUES(?,?, 'Fixture','Engineer','https://fixture.invalid','fixture',8,'materials_ready','x',1,?,?)",
            (role_id, f"fixture:{role_id}", now, now),
        )
    connection.execute(
        "INSERT INTO applications VALUES('application-1','profile','role-1','fixture:application-1','draft',1,?,?)",
        (now, now),
    )
    connection.execute(
        "INSERT INTO applications VALUES('application-2','profile','role-2','fixture:application-2','draft',1,?,?)",
        (now, now),
    )
    return connection


def test_status_events_enforce_ownership_terminal_facts_and_fixture_transitions(tmp_path: Path) -> None:
    connection = status_connection(tmp_path)

    append_status_event(connection, "role-1", "queued", application_id="application-1")
    append_status_event(connection, "role-1", "awaiting_user", application_id="application-1")
    append_status_event(connection, "role-1", "queued", application_id="application-1")
    append_status_event(connection, "role-1", "applied", application_id="application-1")
    append_status_event(connection, "role-1", "direct_edit", application_id="application-1", payload={"conflict": "catalog_edit"})

    assert current_status(connection, "role-1") == "applied"
    with pytest.raises(ValueError, match="illegal status transition"):
        append_status_event(connection, "role-1", "queued", application_id="application-1")
    with pytest.raises(ValueError, match="does not belong"):
        append_status_event(connection, "role-1", "queued", application_id="application-2")


def test_capture_preserves_source_bytes_and_creates_event_unique_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "jobs.json"
    raw = b'{"manual":true}\n'
    source.write_bytes(raw)
    source_stat = source.stat()
    fsync_calls: list[int] = []
    original_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda descriptor: (fsync_calls.append(descriptor), original_fsync(descriptor))[1])

    first = capture_direct_edit(source, tmp_path / "captures", source_kind="jobs_json")
    second = capture_direct_edit(source, tmp_path / "captures", source_kind="jobs_json")

    assert first.capture_relative_path != second.capture_relative_path
    assert first.raw_sha256 == second.raw_sha256
    assert (tmp_path / "captures" / first.capture_relative_path).read_bytes() == raw
    assert (tmp_path / "captures" / second.capture_relative_path).read_bytes() == raw
    assert source.read_bytes() == raw
    assert source.stat().st_mtime_ns == source_stat.st_mtime_ns
    assert fsync_calls
def test_fixture_capabilities_cannot_authorize_live_traffic() -> None:
    config = json.loads((Path(__file__).parents[2] / "config" / "provider-capabilities.json").read_text(encoding="utf-8"))
    assert config["default"]["allow_live_traffic"] is False
    assert all(provider["mode"] == "fixture_only" and provider["allow_live_traffic"] is False for provider in config["providers"].values())
