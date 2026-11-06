"""Actual CLI/fixed-hook regressions using disposable SQLite labs and no network."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from mlo_lifecycle.adapter import Simulator
from mlo_lifecycle.journal import atomic
from mlo_lifecycle.model import BASE, read
from mlo_lifecycle.planner import plan
from support import authorization, cli, prepare


def instrumented(source, args):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(BASE / "src")}
    return subprocess.run([sys.executable, "-c", source, *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=45)


class CliSafetyTests(unittest.TestCase):
    def test_invalid_argument_never_echoes_pasted_secret(self):
        result = instrumented('''
import sys
from mlo_lifecycle.__main__ import main
sys.argv=['lifecycle','--unknown','secret-token-do-not-echo']
raise SystemExit(main())
''', [])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["phase"], "arguments")
        self.assertNotIn('secret-token', result.stderr)

    def test_runner_cannot_pass_missing_empty_skipped_or_failing_suite(self):
        for mode in ("missing", "empty", "skip", "failure", "pass"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / "scripts").mkdir()
                (folder / "tests").mkdir()
                runner = folder / "scripts/run-tests.py"
                runner.write_bytes((BASE / "scripts/run-tests.py").read_bytes())
                good = 'import unittest\nclass T(unittest.TestCase):\n def test_case(self): self.assertTrue(True)\n'
                for name in ("test_unit", "test_safety", "test_contracts"):
                    (folder / "tests" / (name + ".py")).write_text(good)
                target = folder / "tests/test_safety.py"
                if mode == "missing":
                    target.unlink()
                elif mode == "empty":
                    target.write_text('import unittest\n')
                elif mode == "skip":
                    target.write_text(good.replace('self.assertTrue(True)', 'self.skipTest("required")'))
                elif mode == "failure":
                    target.write_text(good.replace('self.assertTrue(True)', 'self.fail("required")'))
                result = subprocess.run([sys.executable, str(runner), "unit"], capture_output=True,
                                        text=True, timeout=10, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(result.returncode, 0 if mode == "pass" else 2, result.stderr)

    def test_backup_copy_sigkill_before_effect_receipt_never_auto_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle, reviewed, key, args = prepare(tmp, "purge-data")
            source = '''
import os,sys,signal
from mlo_lifecycle.adapter import Simulator
from mlo_lifecycle.journal import atomic
from mlo_lifecycle.model import digest,read
original=Simulator.apply
def interrupted(self,step):
    if step['action']=='backup-data':
        source=self.lab/'data'/(digest(step['target'])+'.json')
        atomic(source.with_suffix('.copy.json'),read(source))
        os.kill(os.getpid(),signal.SIGKILL)
    return original(self,step)
Simulator.apply=interrupted
from mlo_lifecycle.__main__ import main
sys.argv=['lifecycle','execute',*sys.argv[1:]]
raise SystemExit(main())
'''
            result = instrumented(source, args)
            self.assertEqual(result.returncode, -9)
            from mlo_lifecycle.journal import Journal
            journal = Journal(Path(tmp) / "lab", reviewed["sha256"], key)
            interrupted = next(k for k, v in journal.latest().items() if v["phase"] == "started")
            self.assertTrue(interrupted.startswith("backup-data:"))
            self.assertEqual(cli("observe", *args, "--step", interrupted)["status"], "unknown")
            cli("resume", *args, expected=2)
            self.assertTrue(all(Simulator(Path(tmp) / "lab", bundle).assets().values()))

    def test_real_clock_expiry_after_slow_freeze_blocks_every_delete(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-expiry-") as tmp:
            folder = Path(tmp)
            bundle, _, key, args = prepare(folder)
            reviewed = plan(bundle, ttl=3)
            atomic(folder / "plan.json", reviewed)
            approval, evidence = authorization(reviewed, bundle, key)
            atomic(folder / "approval.json", approval)
            atomic(folder / "evidence.json", evidence)
            source = '''
import sys,time
from mlo_lifecycle.adapter import Simulator
original=Simulator.apply
def delayed(self,step):
    result=original(self,step)
    if step['id']=='freeze': time.sleep(3.3)
    return result
Simulator.apply=delayed
from mlo_lifecycle.__main__ import main
sys.argv=['lifecycle','execute',*sys.argv[1:]]
raise SystemExit(main())
'''
            result = instrumented(source, args)
            self.assertEqual(result.returncode, 2, result.stderr)
            error = json.loads(result.stderr)
            self.assertEqual(error["error_code"], "plan_expired")
            self.assertEqual(error["step_id"], "idle")
            self.assertTrue(all(Simulator(folder / "lab", bundle).assets().values()))

    def test_real_subprocess_hook_rejects_drift_before_restore_and_before_stop(self):
        for boundary in ("verify-restore", "stop"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(prefix="mlo-c-copy-") as tmp:
                bundle, reviewed, key, args = prepare(tmp)
                source = '''
import sys
from pathlib import Path
from mlo_lifecycle.adapter import CommandStatefulBackend
original=CommandStatefulBackend._run
def changed(self,node,action,previous=None):
    if node=='mongodb' and action==BOUNDARY:
        with (self.folder/'independent/backup.db').open('ab') as stream: stream.write(b'drift-after-check')
    return original(self,node,action,previous)
CommandStatefulBackend._run=changed
from mlo_lifecycle.__main__ import main
sys.argv=['lifecycle','execute',*sys.argv[1:]]
raise SystemExit(main())
'''.replace("BOUNDARY", repr(boundary))
                result = instrumented(source, args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stderr)["error_code"], "backup_failure")
                self.assertFalse(read(Path(tmp) / "lab/db-mongodb/control.json")["stopped"])
                self.assertEqual(read(Path(tmp) / "lab/db-mongodb/stop.receipt.json")["phase"],
                                 "blocked" if boundary == "verify-restore" else "stop_ambiguous")
                self.assertTrue(all(Simulator(Path(tmp) / "lab", bundle).assets().values()))

    def test_existing_output_rejected_before_execute_and_resume(self):
        for operation in ("execute", "resume"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                bundle, reviewed, key, args = prepare(tmp)
                path = Path(tmp) / "existing.json"
                path.write_text('{"keep":"original"}')
                cli(operation, *args, "--output", path, expected=2)
                self.assertFalse((Path(tmp) / "lab/journal.jsonl").exists())
                self.assertTrue(all(Simulator(Path(tmp) / "lab", bundle).assets().values()))
                self.assertEqual(read(path), {"keep": "original"})

    def test_input_file_changed_during_effect_blocks_next_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle, reviewed, key, args = prepare(tmp)
            source = '''
import sys
from pathlib import Path
from mlo_lifecycle.adapter import Simulator
original=Simulator.apply
def changed(self,step):
    result=original(self,step)
    if step['id']=='freeze':
        p=Path(sys.argv[sys.argv.index('--bundle')+1])
        with p.open('a') as stream: stream.write(' ')
    return result
Simulator.apply=changed
from mlo_lifecycle.__main__ import main
sys.argv=['lifecycle','execute',*sys.argv[1:]]
raise SystemExit(main())
'''
            result = instrumented(source, args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stderr)["error_code"], "artifact_drift")
            self.assertTrue(all(Simulator(Path(tmp) / "lab", bundle).assets().values()))

    def test_report_save_failure_reports_completed_effects_without_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle, reviewed, key, args = prepare(tmp)
            source = '''
import sys
from mlo_lifecycle.cli_io import OutputReservation
def fail(self,value): raise OSError('secret-credential-must-not-appear')
OutputReservation.publish=fail
from mlo_lifecycle.__main__ import main
sys.argv=['lifecycle','execute',*sys.argv[1:]]
raise SystemExit(main())
'''
            path = Path(tmp) / "new-report.json"
            result = instrumented(source, [*args, "--output", path])
            self.assertEqual(result.returncode, 2)
            self.assertTrue(json.loads(result.stdout)["complete"])
            error = json.loads(result.stderr)
            self.assertEqual(error["error_code"], "report_write_failure")
            self.assertEqual(error["phase"], "report")
            self.assertNotIn("secret-credential", result.stderr)
            self.assertFalse(path.exists())
            self.assertTrue(cli("verify", *args)["complete"])

    def test_result_explicitly_describes_logical_simulator_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle, reviewed, key, args = prepare(tmp, "purge-data")
            result = cli("execute", *args)
            self.assertEqual(result["removal_semantics"], "synthetic-logical-removal")
            self.assertEqual(len(result["deleted"]), 80)
            self.assertEqual(result["retained"], ["bootstrap:aws_s3_bucket.state"])
            self.assertFalse(result["physical_resources_deleted"])
            self.assertFalse(result["physical_data_deleted"])
            self.assertEqual(result["backup_failure_domain"], "same-local-lab")
            self.assertTrue((Path(tmp) / "lab/db-postgresql/source.db").exists())
            self.assertEqual(len(list((Path(tmp) / "lab/data").glob("*.json"))), 40)

    def test_authorization_error_fields_are_safe_and_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle, reviewed, key, args = prepare(tmp)
            path = Path(tmp) / "evidence.json"
            evidence = read(path)
            evidence["runtime"]["issuer"] = "secret-token-do-not-echo"
            atomic(path, evidence)
            source = '''
import sys
from mlo_lifecycle.__main__ import main
sys.argv=['lifecycle','execute',*sys.argv[1:]]
raise SystemExit(main())
'''
            result = instrumented(source, args)
            self.assertEqual(result.returncode, 2)
            error = json.loads(result.stderr)
            self.assertEqual(error["error_code"], "authorization_failure")
            self.assertTrue(set(("phase", "step_id", "safe_reason", "journal_location", "recovery_action")) <= set(error))
            self.assertNotIn("secret-token", result.stderr)
            self.assertFalse((Path(tmp) / "lab/journal.jsonl").exists())
