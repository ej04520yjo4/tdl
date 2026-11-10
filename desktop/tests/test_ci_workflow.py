"""The Windows CI workflow must test what the release build tests."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "desktop" / "build.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "desktop.yml"


def _go_test_packages(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if re.search(r"\bgo(?:\.exe)?\b.*\btest\s+\./", line) or "& $Go test" in line]
    if len(lines) != 1:
        raise AssertionError(f"expected one go test command, found {len(lines)}")
    return sorted(re.findall(r"\./[\w/.-]+", lines[0]))


class WorkflowTests(unittest.TestCase):
    def test_ci_runs_the_same_go_packages_as_the_release_build(self) -> None:
        build = _go_test_packages(BUILD_SCRIPT.read_text(encoding="utf-8"))
        workflow = _go_test_packages(WORKFLOW.read_text(encoding="utf-8"))
        self.assertTrue(build)
        self.assertEqual(build, workflow)

    def test_ci_runs_the_desktop_suite_on_windows(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("python -m pytest desktop/tests", workflow)
        self.assertIn("desktop/requirements-lock.txt", workflow)


class BuildScriptEncodingTests(unittest.TestCase):
    def test_build_script_keeps_chinese_names_in_windows_powershell(self) -> None:
        # Windows PowerShell 5.1 reads a script without a BOM in the ANSI code
        # page, turning the EXE name into mojibake and failing the build.
        raw = BUILD_SCRIPT.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "desktop/build.ps1 needs a UTF-8 BOM")
        self.assertIn("TDL下載助手", raw[3:].decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
