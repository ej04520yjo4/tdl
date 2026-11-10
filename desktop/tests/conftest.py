"""Stop a hung test instead of letting the build wait forever.

A desktop UI test once hung during a full run and could not be reproduced
afterwards.  Every test now has a hard limit: when it is exceeded, the stack
of every thread is printed (to find the cause next time) and the run exits
with an error, so build.ps1 reports "Desktop tests failed" instead of
stopping without output.
"""

from __future__ import annotations

import faulthandler
import os
from pathlib import Path

import pytest

# Many tests create their temporary folders under the ignored ".build"
# directory (next to the build tools) instead of the system temp folder.  A
# fresh checkout, such as CI, does not have it yet.
(Path(__file__).resolve().parents[2] / ".build").mkdir(exist_ok=True)

# The slowest tests take a few seconds; this only catches a real hang.
TEST_TIMEOUT_SECONDS = float(os.environ.get("TDL_TEST_TIMEOUT", "180"))

_stderr_fd = -1


def pytest_configure(config: pytest.Config) -> None:
    global _stderr_fd
    # pytest captures file descriptor 2 while a test runs; its faulthandler
    # plugin keeps a copy of the real stderr, which shows the stacks.
    try:
        from _pytest.faulthandler import fault_handler_stderr_fd_key

        _stderr_fd = config.stash[fault_handler_stderr_fd_key]
    except (ImportError, KeyError):
        _stderr_fd = os.dup(2)


@pytest.fixture(autouse=True)
def _hang_guard():  # type: ignore[no-untyped-def]
    if TEST_TIMEOUT_SECONDS <= 0 or _stderr_fd < 0:
        yield
        return
    faulthandler.dump_traceback_later(TEST_TIMEOUT_SECONDS, exit=True, file=_stderr_fd)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
