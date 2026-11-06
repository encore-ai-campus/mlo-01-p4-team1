"""Regression checks for invalid producer/consumer and ownership boundaries."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("preparation", ROOT / "scripts/check_preparation.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.inventory = checks.read_json(checks.CONTRACTS / "inventory.example.json")
        self.services = checks.read_json(checks.CONTRACTS / "services.v1.json")
        self.plan = checks.read_json(checks.CONTRACTS / "runtime-plan.example.json")
        self.ownership = checks.read_json(checks.CONTRACTS / "ownership.v1.json")

    def rejects_inventory(self, mutation):
        mutation(self.inventory)
        with self.assertRaises(checks.ContractError):
            checks.validate_inventory(self.inventory)

    def test_valid_producer_consumer_fixture(self):
        checks.validate_inventory(self.inventory)
        checks.validate_services(self.services, self.inventory)
        checks.validate_runtime_plan(self.plan, self.inventory, self.services)

    def test_order_is_not_an_interface_dependency(self):
        self.inventory["nodes"].reverse()
        self.inventory["peering_pairs"].reverse()
        self.plan["node_plans"].reverse()
        checks.validate_inventory(self.inventory)
        checks.validate_runtime_plan(self.plan, self.inventory, self.services)

    def test_missing_node_is_rejected(self):
        self.rejects_inventory(lambda d: d["nodes"].pop())

    def test_duplicate_node_is_rejected(self):
        self.rejects_inventory(lambda d: d["nodes"].__setitem__(1, deepcopy(d["nodes"][0])))

    def test_wrong_account_is_rejected(self):
        self.rejects_inventory(lambda d: d["nodes"][0].update(account="encore5"))

    def test_instance_downgrade_is_rejected(self):
        self.rejects_inventory(lambda d: d["nodes"][0].update(instance_type="t3.small"))

    def test_public_control_is_rejected(self):
        self.rejects_inventory(lambda d: d["nodes"][0].update(subnet_kind="public"))

    def test_collapsed_physical_azs_are_rejected(self):
        self.rejects_inventory(lambda d: d["az_ids"].update(b=d["az_ids"]["a"]))

    def test_node_az_mapping_is_rejected(self):
        self.rejects_inventory(lambda d: d["nodes"][0].update(az_id=d["az_ids"]["b"]))

    def test_overlapping_vpcs_are_rejected(self):
        self.rejects_inventory(lambda d: d["accounts"]["encore2"].update(cidr="10.200.0.0/16"))

    def test_loopback_is_not_a_private_vpc(self):
        self.rejects_inventory(lambda d: d["accounts"]["encore1"].update(cidr="127.0.0.0/16"))

    def test_out_of_vpc_ip_is_rejected(self):
        self.rejects_inventory(lambda d: d["nodes"][0].update(private_ip="10.250.0.20"))

    def test_duplicate_instance_id_is_rejected(self):
        self.rejects_inventory(lambda d: d["nodes"][1].update(instance_id=d["nodes"][0]["instance_id"]))

    def test_missing_direct_peering_is_rejected(self):
        self.rejects_inventory(lambda d: d["peering_pairs"].__setitem__(-1,list(reversed(d["peering_pairs"][0]))))

    def test_storage_purpose_reuse_is_rejected(self):
        self.rejects_inventory(lambda d: d["storage"].update(state_bucket_ref=d["storage"]["raw_bucket_ref"]))

    def test_plaintext_secret_field_is_rejected(self):
        self.rejects_inventory(lambda d: d.update(password="not-a-real-secret"))

    def test_invalid_ipv4_is_rejected(self):
        self.rejects_inventory(lambda d: d["nodes"][0].update(private_ip="not-an-ip"))

    def test_plan_from_another_generation_is_rejected(self):
        self.plan["generation"]="different-generation"
        with self.assertRaises(checks.ContractError):
            checks.validate_runtime_plan(self.plan,self.inventory,self.services)

    def test_plan_cannot_mix_fixture_and_private_inventory(self):
        self.plan["example_only"]=False
        with self.assertRaises(checks.ContractError):
            checks.validate_runtime_plan(self.plan,self.inventory,self.services)

    def test_plan_cannot_claim_aws_readiness(self):
        self.plan["ready_for_aws"]=True
        with self.assertRaises(checks.ContractError):
            checks.validate_runtime_plan(self.plan,self.inventory,self.services)

    def test_plan_cannot_enable_destructive_action(self):
        self.plan["node_plans"][0]["destructive"]=True
        with self.assertRaises(checks.ContractError):
            checks.validate_runtime_plan(self.plan,self.inventory,self.services)

    def test_plan_cannot_skip_a_service(self):
        self.plan["node_plans"][0]["services"].pop()
        with self.assertRaises(checks.ContractError):
            checks.validate_runtime_plan(self.plan,self.inventory,self.services)

    def test_public_proxy_exposure_is_rejected(self):
        self.services["services"]["forward_proxy"]["exposure"]="public"
        with self.assertRaises(checks.ContractError):
            checks.validate_services(self.services,self.inventory)

    def test_same_host_port_collision_is_rejected(self):
        self.services["services"]["kibana"]["port"]=8000
        with self.assertRaises(checks.ContractError):
            checks.validate_services(self.services,self.inventory)

    def test_internet_source_is_rejected(self):
        self.services["flows"][0]["source_roles"]=["0.0.0.0/0"]
        with self.assertRaises(checks.ContractError):
            checks.validate_services(self.services,self.inventory)

    def test_owners_do_not_overlap(self):
        checks.validate_ownership(self.ownership)
        self.assertEqual(checks.owner_of("aws-infra/foundation/terraform/main.tf",self.ownership),"A")
        self.assertEqual(checks.owner_of("aws-infra/runtime/ansible/site.yml",self.ownership),"B")
        self.assertEqual(checks.owner_of("aws-infra/foundation/AGENTS.md",self.ownership),"common")

    def test_cross_role_changes_are_rejected(self):
        with self.assertRaises(checks.ContractError):
            checks.check_owner_paths("A",["aws-infra/runtime/AGENTS.md"],self.ownership)

    def test_lifecycle_owns_implementation_records_and_offline_ci(self):
        checks.validate_ownership(self.ownership)
        checks.check_owner_paths("C",[
            "aws-infra/lifecycle/scripts/lifecycle.py",
            "aws-infra/lifecycle/tests/test_resume.py",
            "docs/handoff/infra/agent-c/sessions/example.md",
            ".github/workflows/lifecycle-check.yml",
            ".github/workflows/infra-destroy.yml",
        ],self.ownership)

    def test_lifecycle_cannot_change_other_roles_or_common_files(self):
        paths=[
            "aws-infra/foundation/scripts/run_foundation.py",
            "aws-infra/runtime/src/mlo_runtime/stateful.py",
            "aws-infra/lifecycle/AGENTS.md",
            "aws-infra/contracts/ownership.v1.json",
            "Makefile",
            "docs/handoff/infra/agent-a/STATUS.md",
            "docs/handoff/infra/agent-b/STATUS.md",
            ".github/workflows/infra-apply.yml",
        ]
        for path in paths:
            with self.subTest(path=path),self.assertRaises(checks.ContractError):
                checks.check_owner_paths("C",[path],self.ownership)

    def test_other_roles_cannot_change_lifecycle(self):
        for owner in ("A","B"):
            with self.subTest(owner=owner),self.assertRaises(checks.ContractError):
                checks.check_owner_paths(owner,["aws-infra/lifecycle/scripts/lifecycle.py"],self.ownership)

    def test_lifecycle_rejects_symlink_escape(self):
        original_root=checks.ROOT
        try:
            with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryDirectory() as outside:
                checks.ROOT=Path(directory)
                lifecycle=checks.ROOT/"aws-infra/lifecycle"
                lifecycle.mkdir(parents=True)
                (lifecycle/"escape").symlink_to(Path(outside),target_is_directory=True)
                with self.assertRaises(checks.ContractError):
                    checks.check_owner_paths("C",["aws-infra/lifecycle/escape/payload.py"],self.ownership)
        finally:
            checks.ROOT=original_root

    def test_common_contract_changes_are_rejected(self):
        with self.assertRaises(checks.ContractError):
            checks.check_owner_paths("B",["aws-infra/contracts/inventory.schema.json"],self.ownership)

    def test_duplicate_ownership_rules_are_rejected(self):
        self.ownership["owners"]["B"]["files"].append("AGENTS.md")
        with self.assertRaises(checks.ContractError):
            checks.validate_ownership(self.ownership)

    def test_unassigned_and_parent_paths_are_rejected(self):
        self.assertIsNone(checks.owner_of("unassigned.txt",self.ownership))
        with self.assertRaises(checks.ContractError):
            checks.owner_of("../outside",self.ownership)

    def test_duplicate_json_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"duplicate.json"
            p.write_text('{"schema_version":"1.0.0","schema_version":"other"}')
            with self.assertRaises(checks.ContractError):
                checks.read_json(p)

    def test_missing_role_implementations_do_not_pass_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(checks.ContractError,"NOT READY"):
                checks.run_pipeline(Path(directory))


if __name__ == "__main__":
    unittest.main()
