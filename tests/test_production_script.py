import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "run-production.sh"


class ProductionScriptTests(unittest.TestCase):
    def test_failed_attempts_are_retried_and_status_is_successful(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            state_file = directory / "attempts"
            mock_python = directory / "mock-python"
            mock_python.write_text(
                "#!/bin/sh\n"
                f"n=0; [ -f '{state_file}' ] && n=$(cat '{state_file}')\n"
                "n=$((n + 1)); printf '%s' \"$n\" >"
                f"'{state_file}'\n"
                "[ \"$n\" -ge 3 ] || exit 1\n",
                encoding="utf-8",
            )
            mock_python.chmod(mock_python.stat().st_mode | stat.S_IXUSR)

            status_file = directory / "status"
            log_file = directory / "run.log"
            lock_file = directory / "run.lock"
            env = {
                **os.environ,
                "HUOHUA_BASE_DIR": str(ROOT),
                "HUOHUA_PYTHON_BIN": str(mock_python),
                "HUOHUA_MAX_ATTEMPTS": "3",
                "HUOHUA_RETRY_DELAY_SECONDS": "0",
                "HUOHUA_LOCK_FILE": str(lock_file),
                "HUOHUA_STATUS_FILE": str(status_file),
                "HUOHUA_RUN_LOG": str(log_file),
            }
            result = subprocess.run(
                [str(SCRIPT)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(state_file.read_text(encoding="utf-8"), "3")
            self.assertTrue(status_file.read_text(encoding="utf-8").startswith("success "))
            log = log_file.read_text(encoding="utf-8")
            self.assertIn("attempt=1 failed exit=1", log)
            self.assertIn("attempt=2 failed exit=1", log)
            self.assertIn("success", log)


if __name__ == "__main__":
    unittest.main()
