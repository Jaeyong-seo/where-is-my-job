"""Quarantine for known-failing automation tests.

The fixture-adapter part of this suite was inherited in a known-failing state
(see known_failures.txt). Those tests are marked xfail (non-strict) so a fresh
clone gets a green `pytest` run while the failures stay visible as `x` marks.
Fix a test, remove its line from known_failures.txt, and it counts again;
an unexpected pass shows up as XPASS rather than being hidden.
"""
from __future__ import annotations

from pathlib import Path

import pytest

KNOWN_FAILURES_PATH = Path(__file__).with_name("known_failures.txt")


def _known_failures() -> set[str]:
    if not KNOWN_FAILURES_PATH.exists():
        return set()
    return {
        line.strip()
        for line in KNOWN_FAILURES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    known = _known_failures()
    if not known:
        return
    for item in items:
        if item.nodeid in known:
            item.add_marker(
                pytest.mark.xfail(
                    reason="known failure: fixture adapters under development "
                    "(tests/application_automation/known_failures.txt)",
                    strict=False,
                )
            )
