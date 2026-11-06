"""C-I2: seven provider-free roots, static protection review and retained state."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from mlo_lifecycle.model import BASE, ROOTS


class TerraformProtectionIntegration(unittest.TestCase):
    def test_seven_roots_protection_refusal_saved_reviewed_plan_and_retained_ids(self):
        tool = BASE / ".cache/toolchain/terraform"
        self.assertTrue(tool.is_file(), "NOT READY: pinned Terraform 1.11.4 missing")
        with tempfile.TemporaryDirectory(prefix="mlo-c-terraform-") as tmp:
            folder = Path(tmp)
            config = folder / "terraform.rc"
            mirror = folder / "empty-mirror"
            mirror.mkdir()
            config.write_text('disable_checkpoint = true\nprovider_installation { filesystem_mirror { path = "' + str(mirror) + '" } }\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "TF_")) and not k.lower().endswith("_proxy")}
            env.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull,
                       AWS_EC2_METADATA_DISABLED="true", CHECKPOINT_DISABLE="1", TF_INPUT="0",
                       TF_IN_AUTOMATION="1", TF_CLI_CONFIG_FILE=str(config),
                       HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9", NO_PROXY="")

            def run(root, *argv, expected=0):
                result = subprocess.run([str(tool), "-chdir=" + str(root), *argv], env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, expected, result.stderr[-2000:])
                return result.stdout + result.stderr

            version = subprocess.run([str(tool), "version", "-json"], env=env, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(version.stdout)["terraform_version"], "1.11.4")
            for name in ROOTS:
                with self.subTest(root=name):
                    root = folder / name
                    root.mkdir()
                    initial = {"terraform": {"required_version": "= 1.11.4"}, "resource": {"terraform_data": {
                        "retained": {"input": "persistent-" + name, "lifecycle": {"prevent_destroy": True}},
                        "compute": {"count": 1, "input": "compute-" + name,
                                    "depends_on": ["terraform_data.retained"], "lifecycle": {"prevent_destroy": True}},
                    }}}
                    path = root / "main.tf.json"
                    path.write_text(json.dumps(initial))
                    run(root, "init", "-backend=false", "-input=false")
                    run(root, "plan", "-input=false", "-out=create.tfplan")
                    run(root, "apply", "-input=false", "create.tfplan")
                    before = json.loads(run(root, "show", "-json"))
                    ids = {r["address"]: r["values"]["id"] for r in before["values"]["root_module"]["resources"]}
                    denied = run(root, "plan", "-destroy", "-input=false", expected=1)
                    self.assertIn("prevent_destroy", denied)
                    # A reviewed alternate *static* compute stanza; retained data stays declared and protected.
                    reviewed = copy.deepcopy(initial)
                    reviewed["resource"]["terraform_data"]["compute"].update(count=0, lifecycle={"prevent_destroy": False})
                    path.write_text(json.dumps(reviewed))
                    run(root, "plan", "-input=false", "-out=reviewed.tfplan")
                    changes = json.loads(run(root, "show", "-json", "reviewed.tfplan"))["resource_changes"]
                    self.assertEqual([r["address"] for r in changes if r["change"]["actions"] == ["delete"]], ["terraform_data.compute[0]"])
                    run(root, "apply", "-input=false", "reviewed.tfplan")
                    after = json.loads(run(root, "show", "-json"))
                    remaining = after["values"]["root_module"]["resources"]
                    self.assertEqual(len(remaining), 1)
                    self.assertEqual(remaining[0]["values"]["id"], ids["terraform_data.retained"])
                    self.assertIn("prevent_destroy", run(root, "plan", "-destroy", "-input=false", expected=1))


if __name__ == "__main__":
    unittest.main()
