import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mlo_runtime.contracts import ROOT, ContractError
from mlo_runtime.render import render
import mlo_runtime.render as module


class AtomicRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mlo-render-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.output, self.bundle = self.base / "plan.json", self.base / "bundle"
        self.fixture = ROOT / "aws-infra/contracts/inventory.example.json"
        render(self.fixture, self.output, self.bundle)
        self.previous = self.output.read_bytes()
        (self.bundle / "prior-marker").write_text("keep")

    def assert_preserved(self):
        self.assertEqual(self.output.read_bytes(), self.previous)
        self.assertEqual((self.bundle / "prior-marker").read_text(), "keep")
        self.assertEqual(list(self.base.glob(".runtime-*")), [])

    def test_eighth_host_failure_keeps_old_output_and_bundle(self):
        original = module.service_configs
        count = 0
        def fail(node, inventory):
            nonlocal count
            count += 1
            if count == 8:
                raise ValueError("injected eighth host rejection")
            return original(node, inventory)
        with patch.object(module, "service_configs", fail), self.assertRaises(ValueError):
            render(self.fixture, self.output, self.bundle)
        self.assert_preserved()

    def test_disk_write_failure_keeps_previous_output(self):
        original = Path.write_text
        def fail(path, *args, **kwargs):
            if path.name == "docker-daemon.required.json":
                raise OSError("ENOSPC injection")
            return original(path, *args, **kwargs)
        with patch.object(Path, "write_text", fail), self.assertRaises(OSError):
            render(self.fixture, self.output, self.bundle)
        self.assert_preserved()

    def test_publish_plan_failure_restores_old_bundle(self):
        original = module.os.replace
        def fail(source, target):
            if Path(target) == self.output:
                raise OSError("rename injection")
            return original(source, target)
        with patch.object(module.os, "replace", fail), self.assertRaises(OSError):
            render(self.fixture, self.output, self.bundle)
        self.assert_preserved()

    def test_concurrent_bundle_render_refused_even_with_other_output(self):
        with open(str(self.bundle) + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ContractError):
                render(self.fixture, self.base / "other.json", self.bundle)
        self.assert_preserved()

    def test_success_replaces_full_bundle_with_canonical_plan(self):
        render(self.fixture, self.output, self.bundle)
        self.assertFalse((self.bundle / "prior-marker").exists())
        self.assertEqual(self.output.read_bytes(), (self.bundle / "runtime-plan.json").read_bytes())
