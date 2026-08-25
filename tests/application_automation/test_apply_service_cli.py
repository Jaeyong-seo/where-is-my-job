from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "tools" / "apply_service.py"


def _run_service(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(PROJECT_ROOT), environment.get("PYTHONPATH")))
    )
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, str(SERVICE), "--db", str(tmp_path / "service.sqlite3"), *args],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "pin_args",
    [
        ("--expected-version", "aside 1.2.3"),
        ("--expected-executable-sha256", "a" * 64),
        ("--enforce-pins",),
    ],
)
def test_fixture_aside_doctor_rejects_pin_options_before_initializing_runtime(
    tmp_path: Path, pin_args: tuple[str, ...]
) -> None:
    result = _run_service(tmp_path, "--fixture", "aside-doctor", *pin_args)

    assert result.returncode == 2
    assert "fixture results are not pin evidence" in result.stderr
    assert not (tmp_path / "service.sqlite3").exists()


def test_fixture_aside_doctor_remains_available_without_pins(tmp_path: Path) -> None:
    result = _run_service(tmp_path, "--fixture", "aside-doctor")

    assert result.returncode == 0
    assert json.loads(result.stdout)["available"] is True


def test_fully_pinned_real_aside_doctor_remains_available(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "aside"
    executable.parent.mkdir()
    executable.write_text(
        "#!/bin/sh\n"
        'case "$1:$2" in\n'
        '  --version:) printf "%s\\n" "aside 1.2.3" ;;\n'
        '  account:status) printf "%s\\n" "* u0 user@example.test signed in profiles: Profile 0" ;;\n'
        '  mcp:--help|repl:--help) printf "%s\\n" "ok" ;;\n'
        '  *) exit 1 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    result = _run_service(
        tmp_path,
        "aside-doctor",
        "--expected-version",
        "aside 1.2.3",
        "--expected-executable-sha256",
        digest,
        "--enforce-pins",
        env={"PATH": str(executable.parent)},
    )

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["version"] == "aside 1.2.3"
    assert report["detail"] is None
