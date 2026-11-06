import sys, os, tempfile, json, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from model import *
import run_foundation as runner


class TestGuard:
    operation = "create"

    def __init__(self, b):
        self.b = b

    def check(self):
        return {
            "scope": {
                k: self.b[k] for k in ["environment", "generation", "account_ids"]
            }
        }


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="foundation-wrapper-")
        self.root = Path(self.temp.name) / ".private"
        self.root.mkdir()
        self.b = fixture_binding()
        self.b.update(
            example_only=False,
            existing_encore3_reviewed=True,
            origin_header="actual-config-operator-supplied-placeholder",
        )
        self.b["state_bucket"] = "test-private-state"
        self.b["bucket_names"] = {
            k: "test-private-" + k for k in self.b["bucket_names"]
        }
        self.b["provisioner_arns"] = {
            a: v.replace("UNBOUND-operator", "operator")
            for a, v in self.b["provisioner_arns"].items()
        }
        self.binding = self.root / "binding.json"
        self.binding.write_text(json.dumps(self.b))
        outputs = self.root / "outputs"
        outputs.mkdir()
        common = {
            k: self.b[k]
            for k in ["environment", "generation", "example_only", "region", "az_ids"]
        }
        common["schema_version"] = "1.0.0"
        for i, a in enumerate(ACCOUNTS, 1):
            nodes = [
                dict(
                    n,
                    instance_id=f"i-a{j:016x}",
                    private_ip=f'10.{209+i}.{1+"abc".index(n["az_slot"])}.{20+j}',
                    az_id=self.b["az_ids"][n["az_slot"]],
                    runtime_role_ref="role-ref:" + n["name"],
                )
                for j, n in enumerate(NODES.values(), 1)
                if n["account"] == a
            ]
            part = dict(
                common,
                component=a,
                account={
                    "vpc_id": f"vpc-a{i:016x}",
                    "cidr": self.b["cidrs"][a],
                    "s3_endpoint_id": f"vpce-a{i:016x}",
                },
                nodes=nodes,
            )
            if a == "encore1":
                part["storage"] = dict(
                    {k + "_bucket_ref": v for k, v in self.b["bucket_names"].items()},
                    state_bucket_ref=self.b["state_bucket"],
                )
            (outputs / (a + ".json")).write_text(
                json.dumps(
                    {
                        "foundation": {
                            "value": part,
                            "sensitive": False,
                            "type": "object",
                        }
                    }
                )
            )
        (outputs / "connectivity.json").write_text(
            json.dumps(
                {
                    "foundation": {
                        "value": dict(
                            common,
                            component="connectivity",
                            peering_pairs=TOPOLOGY["peering_pairs"],
                        ),
                        "sensitive": False,
                        "type": "object",
                    }
                }
            )
        )
        self.log = self.root / "calls.jsonl"
        self.cli = self.root / "fake-cli"
        self.cli.write_text(
            "#!"
            + sys.executable
            + "\n"
            + """import os,sys,json,pathlib,configparser,shlex,subprocess
args=sys.argv[1:]
with open(os.environ['FAKE_LOG'],'a') as f:f.write(json.dumps(args)+'\\n')
if args==['version','-json']:print(json.dumps({'terraform_version':'1.11.4'}))
if args==['--version']:print('aws-cli/'+os.environ.get('FAKE_VERSION','2.37.10')+' fake-cli/1')
if os.environ.get('FAKE_FAIL') and os.environ['FAKE_FAIL'] in args:sys.exit(42)
if args[:2]==['configure','export-credentials']:
 if 'sdk-config' in os.environ.get('AWS_CONFIG_FILE',''):sys.exit(23)
 profile=args[args.index('--profile')+1]
 if os.environ.get('FAKE_MISSING')==profile:print('secret-must-not-leak',file=sys.stderr);sys.exit(24)
 print(json.dumps({'Version':1,'AccessKeyId':'fake-access-key','SecretAccessKey':'secret-must-not-leak','SessionToken':'token-must-not-leak','Expiration':'2000-01-01T00:00:00Z' if os.environ.get('FAKE_EXPIRED') else '2099-01-01T00:00:00Z'}))
 sys.exit(0)
def consume_sdk(profile):
 config=configparser.RawConfigParser();config.read(os.environ['AWS_CONFIG_FILE'])
 child=subprocess.run(shlex.split(config['profile '+profile]['credential_process']),capture_output=True,text=True)
 if child.returncode:sys.exit(25)
 value=json.loads(child.stdout)
 if value.get('Version')!=1 or value.get('AccessKeyId')!='fake-access-key':sys.exit(26)
 if os.environ.get('FAKE_SDK_FAIL'):print('secret-must-not-leak',file=sys.stderr);sys.exit(27)
if 'get-caller-identity' in args:
 a=args[args.index('--profile')+1];consume_sdk(a);i=int(a[-1]);print(json.dumps({'Account':str(111111111110+i) if not os.environ.get('FAKE_WRONG') else '999999999999'}))
if args and args[0] in ['init','plan','apply','output']:
 if os.environ.get('FAKE_PROVIDER_CREDENTIAL_FAIL'):print('provider could not resolve secret-must-not-leak',file=sys.stderr);sys.exit(28)
 consume_sdk('encore1')
for arg in args:
 if arg.startswith('-out='):pathlib.Path(arg[5:]).write_text('saved-plan')
if args[:2]==['output','-json']:
 a=pathlib.Path.cwd().name
 print((pathlib.Path(os.environ['FAKE_BINDING']).parent/'outputs'/(a+'.json')).read_text())
"""
        )
        self.cli.chmod(0o755)
        self.source_config = self.root / "original-config"
        self.source_config.write_text(
            "[profile encore1]\nlogin_session = original-login-session\n"
        )
        self.source_credentials = self.root / "original-credentials"
        self.source_credentials.write_text("")
        self.env = patch.dict(
            os.environ,
            {
                "FAKE_LOG": str(self.log),
                "FAKE_BINDING": str(self.binding),
                "AWS_CONFIG_FILE": str(self.source_config),
                "AWS_SHARED_CREDENTIALS_FILE": str(self.source_credentials),
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_stage(self, stage, enabled=True):
        runner.execute(
            stage,
            self.binding,
            self.root / "run",
            enabled=enabled,
            terraform=str(self.cli),
            aws=str(self.cli),
            operation_guard=TestGuard(self.b),
        )

    def calls(self):
        return (
            [json.loads(l) for l in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def test_missing_common_guard_blocks_before_mutating_stage(self):
        self.run_stage("preflight")
        before = self.calls()
        with self.assertRaises(ContractError):
            runner.execute(
                "bootstrap",
                self.binding,
                self.root / "run",
                enabled=True,
                terraform=str(self.cli),
                aws=str(self.cli),
            )
        self.assertEqual(self.calls(), before)
        self.assertIsNone(read_json(self.root / "run/receipt.json")["pending"])

    def test_real_gate_prevents_even_identity_call(self):
        with self.assertRaises(ContractError):
            self.run_stage("preflight", False)
        self.assertFalse(self.calls())

    def test_fixture_rejected_before_cli(self):
        self.binding.write_text(json.dumps(fixture_binding()))
        with self.assertRaises(ContractError):
            self.run_stage("preflight")
        self.assertFalse(self.calls())

    def test_account_mismatch_blocks_terraform_and_rerun(self):
        with patch.dict(os.environ, {"FAKE_WRONG": "1"}):
            with self.assertRaises(ContractError):
                self.run_stage("preflight")
        count = len(self.calls())
        with self.assertRaises(ContractError):
            self.run_stage("preflight")
        self.assertEqual(len(self.calls()), count)

    def test_out_of_order_stage_is_rejected(self):
        with self.assertRaises(ContractError):
            self.run_stage("apply")
        self.assertFalse(self.calls())

    def test_file_lock_blocks_cli(self):
        folder = self.root / "run"
        folder.mkdir()
        (folder / "run.lock").write_text("")
        with self.assertRaises(ContractError):
            self.run_stage("preflight")
        self.assertFalse(self.calls())

    def test_partial_bootstrap_preserves_receipt_and_stops_rerun(self):
        self.run_stage("preflight")
        self.run_stage("bootstrap-plan")
        with patch.dict(os.environ, {"FAKE_FAIL": "apply"}):
            with self.assertRaises(ContractError):
                self.run_stage("bootstrap")
        rec = read_json(self.root / "run/receipt.json")
        self.assertEqual(rec["pending"], "bootstrap")
        self.assertEqual(rec["completed"], ["preflight", "bootstrap-plan"])
        before = self.calls()
        with self.assertRaises(ContractError):
            self.run_stage("bootstrap")
        self.assertEqual(self.calls(), before)

    def test_repeated_completed_stage_and_changed_binding_rejected(self):
        self.run_stage("preflight")
        with self.assertRaises(ContractError):
            self.run_stage("preflight")
        self.b["generation"] = "new-generation"
        self.binding.write_text(json.dumps(self.b))
        with self.assertRaises(ContractError):
            self.run_stage("bootstrap")

    def test_five_saved_plans_and_digest_protection(self):
        for s in ["preflight", "bootstrap-plan", "bootstrap", "plan"]:
            self.run_stage(s)
        plans = list((self.root / "run").glob("encore*.tfplan"))
        self.assertEqual(len(plans), 5)
        (self.root / "run/encore1.tfplan").write_text("changed-plan")
        before = self.calls()
        with self.assertRaises(ContractError):
            self.run_stage("apply")
        self.assertEqual(
            sum(a[0] == "apply" for a in self.calls()),
            sum(a[0] == "apply" for a in before),
        )

    def test_fake_cli_network_phases_are_separate(self):
        for s in runner.STAGES[:7]:
            self.run_stage(s)
        rec = read_json(self.root / "run/receipt.json")
        self.assertEqual(rec["completed"], runner.STAGES[:7])
        self.assertEqual(len(rec["plans"]), 7)
        self.assertTrue(
            all("import" not in a and "destroy" not in a for a in self.calls())
        )

    def test_full_fake_run_exports_private_inventory_and_verifies(self):
        for s in runner.STAGES:
            self.run_stage(s)
        rec = read_json(self.root / "run/receipt.json")
        self.assertEqual(rec["completed"], runner.STAGES)
        inv = validate_inventory(read_json(self.root / "run/inventory.json"))
        self.assertEqual(len(inv["nodes"]), 16)
        self.assertFalse(inv["example_only"])
        self.assertEqual(
            (self.root / "run/inventory.json").stat().st_mode & 0o777, 0o600
        )

    def test_import_destroy_unavailable(self):
        for s in ["import", "destroy"]:
            with self.assertRaises(ContractError):
                self.run_stage(s)
        self.assertFalse(self.calls())

    def test_version_pin_accepts_current_rejects_other(self):
        with patch.dict(os.environ, {"FAKE_VERSION": "2.27.3"}):
            with self.assertRaises(ContractError):
                self.run_stage("preflight")
        self.assertFalse(any("get-caller-identity" in c for c in self.calls()))

    def test_missing_source_credentials_fail_before_sdk(self):
        with patch.dict(os.environ, {"FAKE_MISSING": "encore3"}):
            with self.assertRaises(ValueError):
                self.run_stage("preflight")
        self.assertFalse(any("get-caller-identity" in c for c in self.calls()))
        self.assertNotIn("secret-must-not-leak", self.log.read_text())

    def test_expired_session_blocks_before_sdk(self):
        with patch.dict(os.environ, {"FAKE_EXPIRED": "1"}):
            with self.assertRaises(ValueError):
                self.run_stage("preflight")
        self.assertFalse(any("get-caller-identity" in c for c in self.calls()))

    def test_sdk_process_failure_sanitized_and_original_config_unchanged(self):
        before = self.source_config.read_bytes()
        with patch.dict(os.environ, {"FAKE_SDK_FAIL": "1"}):
            with self.assertRaises(ContractError) as caught:
                self.run_stage("preflight")
        self.assertNotIn("secret-must-not-leak", str(caught.exception))
        self.assertEqual(before, self.source_config.read_bytes())
        saved = "".join(
            p.read_text() for p in (self.root / "run").iterdir() if p.is_file()
        )
        self.assertNotIn("secret-must-not-leak", saved)
        self.assertNotIn("token-must-not-leak", saved)

    def test_provider_credential_resolution_failure_keeps_pending(self):
        self.run_stage("preflight")
        with patch.dict(os.environ, {"FAKE_PROVIDER_CREDENTIAL_FAIL": "1"}):
            with self.assertRaises(ContractError):
                self.run_stage("bootstrap-plan")
        self.assertEqual(
            read_json(self.root / "run/receipt.json")["pending"], "bootstrap-plan"
        )

    def test_bootstrap_planning_never_applies_and_apply_refuses_modified_plan(self):
        self.run_stage("preflight")
        self.run_stage("bootstrap-plan")
        self.assertFalse(any(c[0] == "apply" for c in self.calls()))
        (self.root / "run/bootstrap.tfplan").write_text("changed")
        with self.assertRaises(ContractError):
            self.run_stage("bootstrap")
        self.assertFalse(any(c[0] == "apply" for c in self.calls()))
