"""Offline proposal contracts; no live exporter, distributed store or AWS client."""

import copy
import unittest

from mlo_lifecycle.lease_contract import LeaseContract
from mlo_lifecycle.snapshot_contract import audit_export, check_partition


def exported():
    address = 'module.account["encore1"].module.hosts[0].aws_instance.host["control-a"]'
    binding = {"project": "mlo-p4", "environment": "dev", "generation": "synthetic-v1",
               "account": "111111111111", "region": "ap-northeast-2", "state_key": "dev/encore1/terraform.tfstate",
               "lineage": "sim-lineage-encore1", "serial": 7}
    resource = {**{k: binding[k] for k in ("account", "region", "state_key", "lineage", "serial")},
                "address": address, "module_address": 'module.account["encore1"].module.hosts[0]',
                "type": "aws_instance", "provider": "registry.terraform.io/hashicorp/aws",
                "id": "sim:control-a", "ownership": "managed", "dependencies": [], "protected": False}
    return {"schema_version": "offline-foundation-export-v1", "example_only": True,
            "binding": binding, "state": {"lineage": binding["lineage"], "serial": 7, "resources": [resource]},
            "configuration": {address: {"references": [], "prevent_destroy": False,
                                       "deletion_protection": False, "observed_protection": False}},
            "provider_schemas": {resource["provider"]: {"aws_instance": {"schema_version": 1}}},
            "expected_targets": {address: resource["id"]}}


class SnapshotContractTests(unittest.TestCase):
    def test_expanded_module_count_for_each_and_whole_sets(self):
        value = exported()
        result = audit_export(value)
        self.assertFalse(result["execution_enabled"])
        self.assertFalse(result["ready_for_aws"])
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["candidate_targets"], list(value["expected_targets"]))
        check_partition(["a", "b"], ["a"], ["b"], ["b"])
        for args in ((["a", "b"], ["a"], [], []), (["a"], ["a"], ["a"], ["a"]),
                     (["a"], [], ["a"], ["a", "foreign"])):
            with self.assertRaises(ValueError):
                check_partition(*args)

    def test_unknown_provider_or_config_never_enables_execution(self):
        for key in ("configuration", "provider_schemas"):
            value = exported()
            value[key] = {}
            result = audit_export(value)
            self.assertTrue(result["unknown"])
            self.assertFalse(result["execution_enabled"])

    def test_stale_state_binding_protection_references_and_target_drift_refused(self):
        for mutation in ("serial", "ownership", "region", "module_address", "protected", "references", "extra", "real"):
            value = exported()
            item = value["state"]["resources"][0]
            config = next(iter(value["configuration"].values()))
            if mutation == "serial":
                value["state"]["serial"] += 1
            elif mutation in ("ownership", "region", "module_address"):
                item[mutation] = "other"
            elif mutation == "protected":
                item["protected"] = True
            elif mutation == "references":
                config["references"] = ["aws_vpc.missing"]
            elif mutation == "extra":
                value["expected_targets"]["other"] = "sim:extra"
            else:
                value["example_only"] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                audit_export(value)


class LeaseContractTests(unittest.TestCase):
    def test_two_controls_create_deploy_destroy_exclusion_and_stale_fence(self):
        authority = LeaseContract()
        scope = {"resource_scope": "five-accounts", "environment": "dev", "generation": "g1"}
        first = authority.acquire(scope, "control-a", "create", 0, 10, expected_epoch=0)
        for operation in ("deploy", "destroy"):
            with self.assertRaises(ValueError):
                authority.acquire(scope, "control-b", operation, 5, 10, expected_epoch=1)
        with self.assertRaises(ValueError):
            authority.acquire({**scope, "generation": "g2"}, "control-b", "destroy", 5, 10, expected_epoch=1)
        second = authority.acquire(scope, "control-b", "destroy", 11, 10, expected_epoch=1)
        self.assertGreater(second["fencing_token"], first["fencing_token"])
        for operation in (lambda: authority.check(first, 11), lambda: authority.renew(first, 11, 10),
                          lambda: authority.acquire(scope, "control-a", "deploy", 30, 10, expected_epoch=1)):
            with self.assertRaises(ValueError):
                operation()
        authority.check(second, 12)

    def test_network_partition_or_dead_owner_cannot_renew_or_act_after_expiry(self):
        authority = LeaseContract()
        scope = {"resource_scope": "five-accounts", "environment": "dev", "generation": "g1"}
        lease = authority.acquire(scope, "control-a", "destroy", 0, 5, expected_epoch=0)
        for operation in (lambda: authority.renew(lease, 6, 10), lambda: authority.check(lease, 6)):
            with self.assertRaises(ValueError):
                operation()
        current = authority.acquire(scope, "control-b", "deploy", 6, 5, expected_epoch=1)
        renewed = authority.renew(current, 7, 5)
        with self.assertRaises(ValueError):
            authority.check(current, 8)
        authority.check(renewed, 8)
