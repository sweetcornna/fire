import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
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


@unittest.skipUnless(shutil.which("jq"), "trigger script requires jq")
class TriggerScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.requests_path = self.directory / "requests.jsonl"
        (self.directory / "token").write_text("offline-test-token", encoding="utf-8")
        binaries = self.directory / "bin"
        binaries.mkdir()
        (binaries / "jq").symlink_to(shutil.which("jq"))
        curl = binaries / "curl"
        curl.write_text(
            f"#!{sys.executable}\n" + textwrap.dedent("""\
                import json
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                method = args[args.index('-X') + 1] if '-X' in args else 'GET'
                request = {'method': method}
                if '--data' in args:
                    request['payload'] = json.loads(args[args.index('--data') + 1])
                with open(os.environ['MOCK_REQUEST_LOG'], 'a') as handle:
                    handle.write(json.dumps(request) + '\\n')
                output = Path(args[args.index('--output') + 1])
                if method == 'GET':
                    output.write_text(os.environ['MOCK_REPOSITORY_BODY'])
                    print(os.environ.get('MOCK_REPOSITORY_STATUS', '200'), end='')
                else:
                    output.write_text('')
                    print('204', end='')
                """),
            encoding="utf-8",
        )
        curl.chmod(0o700)
        flock = binaries / "flock"
        flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        flock.chmod(0o700)
        # Emulate the production host's GNU date option on macOS.
        date = binaries / "date"
        date.write_text(
            '#!/bin/sh\nif [ "$1" = "-Is" ]; then\n'
            '  exec /bin/date -u +%Y-%m-%dT%H:%M:%SZ\nfi\n'
            'exec /bin/date "$@"\n',
            encoding="utf-8",
        )
        date.chmod(0o700)
        self.env = {
            "PATH": f"{binaries}:/usr/bin:/bin",
            "HUOHUA_GITHUB_TOKEN_FILE": str(self.directory / "token"),
            "HUOHUA_TRIGGER_LOG": str(self.directory / "trigger.log"),
            "HUOHUA_TRIGGER_LOCK": str(self.directory / "trigger.lock"),
            "HUOHUA_GITHUB_REPOSITORY": "example/fire",
            "HUOHUA_WAIT_SECONDS": "0",
            "HUOHUA_DISPATCH_ATTEMPTS": "1",
            "MOCK_REQUEST_LOG": str(self.requests_path),
            "MOCK_REPOSITORY_BODY": '{"default_branch": "main"}',
        }

    def _run(self, *args, **environment):
        self.requests_path.write_text("", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(ROOT / "deploy" / "trigger_huohua.sh"), *args],
            cwd=ROOT, env={**self.env, **environment},
            capture_output=True, text=True, timeout=10,
        )
        requests = [json.loads(line) for line in self.requests_path.read_text().splitlines()]
        return result, requests

    def test_dispatch_resolves_the_repository_default_branch(self):
        result, requests = self._run(MOCK_REPOSITORY_BODY='{"default_branch": "trunk"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([request["method"] for request in requests], ["GET", "POST"])
        self.assertEqual(requests[-1]["payload"]["ref"], "trunk")

    def test_explicit_default_ref_is_accepted_and_normalized(self):
        for ref in ("main", "refs/heads/main"):
            with self.subTest(ref=ref):
                result, requests = self._run(HUOHUA_GITHUB_REF=ref)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(requests[-1]["payload"]["ref"], "main")

    def test_other_refs_fail_before_any_dispatch(self):
        for ref in ("dev", "refs/heads/dev", "refs/tags/main"):
            with self.subTest(ref=ref):
                result, requests = self._run(HUOHUA_GITHUB_REF=ref)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertIn("default branch", result.stderr)
                self.assertEqual([request["method"] for request in requests], ["GET"])

    def test_missing_default_branch_or_api_failure_prevents_dispatch(self):
        for environment in (
            {"MOCK_REPOSITORY_BODY": "{}"},
            {"MOCK_REPOSITORY_BODY": "not-json"},
            {"MOCK_REPOSITORY_STATUS": "403"},
        ):
            with self.subTest(environment=environment):
                result, requests = self._run(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([request["method"] for request in requests], ["GET"])

    def test_dry_run_resolves_default_branch_without_dispatch(self):
        result, requests = self._run(
            "--dry-run", MOCK_REPOSITORY_BODY='{"default_branch": "trunk"}'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ref=trunk", result.stdout)
        self.assertEqual([request["method"] for request in requests], ["GET"])


class WorkflowDeliveryScopeTests(unittest.TestCase):
    def test_diagnostic_input_selects_existing_non_sending_path(self):
        for filename in ("schedule.yml", "schedule_dev.yml"):
            source = (ROOT / ".github" / "workflows" / filename).read_text()
            diagnostic = source.split("      diagnose_only:\n", 1)[1].split("      preview_only:", 1)[0]
            self.assertIn("default: false", diagnostic)
            self.assertIn("type: boolean", diagnostic)
            self.assertIn("DIAGNOSE_FRIEND_MATCHING: ${{ inputs.diagnose_only && '1' || vars.DIAGNOSE_FRIEND_MATCHING || '0' }}", source)

    def test_interrupted_runs_attempt_state_save_and_artifact_backup(self):
        for filename in ("schedule.yml", "schedule_dev.yml"):
            with self.subTest(workflow=filename):
                source = (ROOT / ".github" / "workflows" / filename).read_text()
                save = source.split("    - uses: actions/cache/save@v4\n", 1)[1]
                save = save.split("\n    - ", 1)[0]
                condition = re.search(r"if: \$\{\{ (.+) \}\}", save).group(1)
                self.assertIn("always()", condition)
                self.assertNotIn("cancelled()", condition)
                self.assertIn("steps.delivery-date.outputs.today != ''", condition)
                self.assertIn("hashFiles('.state/delivery-state.json') != ''", condition)

                artifact = source.split("    - uses: actions/upload-artifact@v4\n", 1)[1]
                artifact = artifact.split("\n  workflow-keepalive:", 1)[0]
                self.assertIn("if: ${{ always() }}", artifact)
                self.assertIn("include-hidden-files: true", artifact)
                paths = textwrap.dedent(artifact.split("        path: |\n", 1)[1])
                self.assertEqual(set(paths.split()), {"logs/", ".state/delivery-state.json"})

    def test_delivery_cache_namespace_is_shared(self):
        cache_settings = []
        for filename in ("schedule.yml", "schedule_dev.yml"):
            source = (ROOT / ".github" / "workflows" / filename).read_text()
            self.assertIn("        ref: dev\n", source)
            keys = re.findall(r"^\s+key: (huohua-delivery-.+)$", source, re.MULTILINE)
            prefixes = re.findall(r"^          (huohua-delivery-.+)$", source, re.MULTILINE)
            self.assertEqual(len(keys), 2)
            self.assertEqual(len(prefixes), 2)
            self.assertEqual(keys[0], keys[1])
            for value in keys + prefixes:
                self.assertTrue(value.startswith("huohua-delivery-dev-"))
                self.assertNotIn("github.ref_name", value)
            cache_settings.append((keys, prefixes))
        self.assertEqual(cache_settings[0], cache_settings[1])

    def test_branch_guard_allows_only_default_branch_delivery_or_preview(self):
        for filename in ("schedule.yml", "schedule_dev.yml"):
            source = (ROOT / ".github" / "workflows" / filename).read_text()
            marker = "    - name: Validate delivery branch\n"
            self.assertIn(marker, source)
            self.assertLess(source.index(marker), source.index("    - uses: actions/checkout"))
            block = source.split(marker, 1)[1].split("\n    - ", 1)[0]
            self.assertIn("DELIVERY_REF: ${{ github.ref }}", block)
            self.assertIn("DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}", block)
            self.assertIn("PREVIEW_ONLY: ${{ inputs.preview_only || 'false' }}", block)
            command = textwrap.dedent(block.split("      run: |\n", 1)[1])
            is_dev_workflow = filename == "schedule_dev.yml"
            cases = (
                ("false", "refs/heads/main", "main", True),
                ("false", "refs/heads/trunk", "trunk", True),
                ("false", "refs/heads/dev", "main", is_dev_workflow),
                ("false", "refs/tags/main", "main", False),
                ("false", "refs/heads/main", "", False),
                ("true", "refs/heads/dev", "main", True),
            )
            for preview, ref, default, allowed in cases:
                with self.subTest(workflow=filename, preview=preview, ref=ref, default=default):
                    result = subprocess.run(
                        ["bash", "-c", command], capture_output=True, text=True, timeout=5,
                        env={"PATH": "/usr/bin:/bin", "PREVIEW_ONLY": preview,
                             "DELIVERY_REF": ref, "DEFAULT_BRANCH": default},
                    )
                    self.assertEqual(result.returncode == 0, allowed, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
