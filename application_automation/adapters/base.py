"""Safe local probes for Aside.

PII-bearing operations deliberately live in :mod:`application_automation.adapters.mcp`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from typing import Final

from application_automation.aside import AsideDoctorResult, AsideProbeError, AsideProtocolError, PauseReason

__all__ = ("AsideCliAdapter", "AsideProtocolError", "AsideProbeError")

_ACCOUNT_ROW: Final[re.Pattern[str]] = re.compile(
    r"^\*\s+u0\s+[^\s@<>]+@[^\s@<>]+\s+signed in\s+profiles:\s+Profile 0\s*$",
    re.IGNORECASE,
)
_NEGATED_ACCOUNT: Final[re.Pattern[str]] = re.compile(
    r"\b(?:not\s+(?:signed\s+in|authenticated)|signed\s+out|unauthenticated|unknown)\b",
    re.IGNORECASE,
)


class AsideCliAdapter:
    """Restrict the CLI to safe doctor and inspect probes."""

    def __init__(
        self,
        aside_path: str = "aside",
        expected_version: str | None = None,
        expected_sha256: str | None = None,
    ) -> None:
        if not isinstance(expected_version, str) or not expected_version:
            raise AsideProbeError("Aside CLI version pin is not configured")
        if not _is_digest(expected_sha256):
            raise AsideProbeError("Aside executable SHA-256 pin is not configured")
        self._aside_path = aside_path
        self._expected_version = expected_version
        self._expected_sha256 = expected_sha256

    def doctor(self) -> AsideDoctorResult:
        executable = self.verify_executable()
        version, version_failure = self._probe((executable, "--version"))
        version_text = version.strip() if version is not None else None
        if version_failure is not None:
            return AsideDoctorResult(True, False, None, False, False, PauseReason.LOGIN, version_failure)
        if not version_text:
            return AsideDoctorResult(True, False, version_text, False, False, PauseReason.LOGIN, "missing_version")
        if self._expected_version is not None and version_text != self._expected_version:
            return AsideDoctorResult(True, False, version_text, False, False, PauseReason.LOGIN, "version_drift")
        account, account_failure = self._probe((executable, "account", "status"))
        mcp, mcp_failure = self._probe((executable, "mcp", "--help"))
        repl, repl_failure = self._probe((executable, "repl", "--help"))
        if account_failure is not None:
            return AsideDoctorResult(True, False, version_text, mcp is not None, repl is not None, PauseReason.LOGIN, account_failure)
        signed_in = self._account_is_signed_in(account or "")
        detail = None if signed_in else "account_not_signed_in"
        if not signed_in and (mcp_failure is not None or repl_failure is not None):
            detail = mcp_failure or repl_failure
        return AsideDoctorResult(
            True,
            signed_in,
            version_text,
            mcp is not None,
            repl is not None,
            PauseReason.LOGIN if not signed_in else None,
            detail,
        )

    def inspect_probe(self) -> None:
        """Confirm that the constructor-owned CLI version and hash pins still hold."""
        if self._expected_version is None or self._expected_sha256 is None:
            raise AsideProbeError("Aside CLI pins are not configured")
        executable = self.verify_executable()
        version, failure = self._probe((executable, "--version"))
        if failure is not None:
            raise AsideProbeError("Aside CLI version probe failed")
        if version is None or version.strip() != self._expected_version:
            raise AsideProbeError("Aside CLI version drift")

    def verify_executable(self) -> str:
        expected = self._expected_sha256
        if expected is None:
            raise AsideProbeError("Aside executable SHA-256 pin is not configured")
        executable = shutil.which(self._aside_path)
        if executable is None or not os.access(executable, os.X_OK):
            raise AsideProbeError("aside CLI is unavailable")
        try:
            with open(executable, "rb") as stream:
                actual = hashlib.sha256(stream.read()).hexdigest()
        except OSError as error:
            raise AsideProbeError("cannot hash Aside executable") from error
        if actual != expected:
            raise AsideProbeError("Aside executable hash drift")
        return executable

    @staticmethod
    def _run(argv: tuple[str, ...], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
        environment = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}
        return subprocess.run(argv, text=True, capture_output=True, timeout=timeout, env=environment, check=False)

    def _probe(self, argv: tuple[str, ...]) -> tuple[str | None, str | None]:
        try:
            completed = self._run(argv)
        except subprocess.TimeoutExpired:
            return None, "cli_probe_timeout"
        except OSError:
            return None, "cli_probe_launch_error"
        if completed.returncode != 0:
            return None, "cli_probe_nonzero_exit"
        return completed.stdout, None

    @staticmethod
    def _account_is_signed_in(output: str) -> bool:
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            # JSON is accepted only when it contains the exact anchored status row.
            output = value.get("status", "") if isinstance(value.get("status"), str) else ""
        lines = [" ".join(line.split()) for line in output.splitlines() if line.strip()]
        return _NEGATED_ACCOUNT.search(output) is None and sum(_ACCOUNT_ROW.fullmatch(line) is not None for line in lines) == 1


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
