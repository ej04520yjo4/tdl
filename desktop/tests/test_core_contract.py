"""Offline checks against the real compiled Go core (no Telegram access)."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "bin" / "tdl-core.exe"


@unittest.skipUnless(CORE.exists(), "Compile bin/tdl-core.exe to check the real protocol")
class CoreContractTests(unittest.TestCase):
    def test_empty_download_reports_structured_error_in_isolated_directory(self):
        with tempfile.TemporaryDirectory(prefix="core-contract-", dir=ROOT / ".build") as temporary:
            env = dict(os.environ, TDL_GUI="1", TDL_DATA_DIR=temporary)
            # Keep stdin open until process exit. EOF intentionally cancels GUI tasks.
            child = subprocess.Popen(
                [str(CORE), "dl"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                child.wait(timeout=10)
                output = child.stdout.read().decode("utf-8")
                self.assertNotEqual(child.returncode, 0)
                events = [json.loads(line[len("@@TDL_GUI@@"):]) for line in output.splitlines() if line.startswith("@@TDL_GUI@@")]
                self.assertEqual(len(events), 1, output)
                self.assertEqual(events[0]["event"], "error")
                self.assertIn("no urls or files", events[0]["message"])
                self.assertTrue((Path(temporary) / "extensions").is_dir())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()


if __name__ == "__main__":
    unittest.main()
