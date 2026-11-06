import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
import artifact_verifier as verifier


class ArtifactContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.body = b"print('explicit local fixture')\n"
        self.manifest = {
            "schema_version": "1.0.0", "artifact_version": "v1", "environment": "dev",
            "generation": "fixture-v1", "platform": "linux/amd64", "example_only": False,
            "fixture": True, "target_nodes": ["control-a"], "node_roles": {"control-a": "control"},
            "operations": ["install", "deploy", "health", "rollback"],
            "files": {"bin/runtime-entrypoint.py": hashlib.sha256(self.body).hexdigest()},
            "image_digests": {}, "config_hashes": {}, "required_secrets": {"control-a": []},
        }

    def archive(self, manifest=None, extra=None, body=None):
        manifest = self.manifest if manifest is None else manifest
        raw = json.dumps(manifest, sort_keys=True).encode()
        path = self.folder / "payload.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in [("manifest.json", raw), ("bin/runtime-entrypoint.py", self.body if body is None else body)]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            if extra is not None:
                info, data = extra
                archive.addfile(info, io.BytesIO(data) if data is not None else None)
        return path, hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(raw).hexdigest()

    def inspect(self, values, destination=None, **changes):
        path, digest, manifest_digest = values
        parameters = dict(expected_sha256=digest, environment="dev", generation="fixture-v1", node="control-a", destination=destination or self.folder / "release", expected_manifest_sha256=manifest_digest)
        parameters.update(changes)
        return verifier.inspect_artifact(path, **parameters)

    def test_schema_and_standalone_verifier_accept_same_payload(self):
        Draft202012Validator(json.loads((ROOT / "aws-infra/contracts/runtime-artifact.schema.json").read_text())).validate(self.manifest)
        self.assertEqual(self.inspect(self.archive()), self.manifest)
        self.assertEqual((self.folder / "release/bin/runtime-entrypoint.py").read_bytes(), self.body)
        self.assertEqual((self.folder / "release/bin/runtime-entrypoint.py").stat().st_mode & 0o777, 0o600)

    def test_external_hashes_and_target_metadata_are_required(self):
        values = self.archive()
        for change in [dict(expected_sha256="0" * 64), dict(expected_manifest_sha256="0" * 64), dict(expected_manifest_sha256=None), dict(node="control-b"), dict(generation="other"), dict(environment="prod")]:
            with self.subTest(change=change), self.assertRaises(verifier.ContractError):
                self.inspect(values, **change)
            self.assertFalse((self.folder / "release").exists())

    def test_internal_content_change_is_rejected(self):
        with self.assertRaisesRegex(verifier.ContractError, "file checksum"):
            self.inspect(self.archive(body=b"unexpected content"))

    def test_manifest_semantic_mutations_are_rejected(self):
        mutations = [lambda m: m.update(platform="linux/arm64"), lambda m: m.update(example_only=True), lambda m: m["node_roles"].update({"control-a": "unknown"}), lambda m: m.update(operations=["shell"]), lambda m: m["image_digests"].update(web="example:latest"), lambda m: m["required_secrets"].update({"control-a": ["/run/mlo/secrets/../../password"]}), lambda m: m["config_hashes"].update({"missing": "0" * 64})]
        for mutation in mutations:
            manifest = copy.deepcopy(self.manifest)
            mutation(manifest)
            with self.subTest(mutation=mutation), self.assertRaises(verifier.ContractError):
                self.inspect(self.archive(manifest))

    def test_unsafe_and_duplicate_member_paths_are_rejected(self):
        for name in ["../escape", "/absolute", "bin/../escape", "bin//escape", "bin\\escape", "bin/runtime-entrypoint.py"]:
            member = tarfile.TarInfo(name)
            member.size = 1
            with self.subTest(name=name), self.assertRaises(verifier.ContractError):
                self.inspect(self.archive(extra=(member, b"x")))

    def test_links_directories_and_devices_are_rejected(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE]:
            member = tarfile.TarInfo("unsafe")
            member.type, member.linkname = kind, "/outside"
            with self.subTest(kind=kind), self.assertRaises(verifier.ContractError):
                self.inspect(self.archive(extra=(member, None)))

    def test_expansion_bound_and_file_bound_apply_before_publication(self):
        values = self.archive()
        for setting in [dict(MAX_TAR_BYTES=64), dict(MAX_FILE_BYTES=16), dict(MAX_MEMBERS=1)]:
            with self.subTest(setting=setting), patch.multiple(verifier, **setting), self.assertRaises(verifier.ContractError):
                self.inspect(values)
            self.assertFalse((self.folder / "release").exists())

    def test_existing_release_and_failed_publication_are_preserved(self):
        destination = self.folder / "existing"
        destination.mkdir()
        (destination / "keep").write_text("old")
        values = self.archive()
        with self.assertRaises(verifier.ContractError):
            self.inspect(values, destination)
        self.assertEqual((destination / "keep").read_text(), "old")
        with patch.object(verifier.os, "rename", side_effect=OSError("injected disk failure")), self.assertRaises(OSError):
            self.inspect(values)
        self.assertFalse((self.folder / "release").exists())
        self.assertEqual(list(self.folder.glob(".artifact-*")), [])
        self.assertFalse((self.folder / ".release.artifact.lock").exists())

    def test_security_checks_survive_python_optimization(self):
        member = tarfile.TarInfo("../escape")
        member.size = 1
        path, digest, manifest_digest = self.archive(extra=(member, b"x"))
        result = subprocess.run([sys.executable, "-O", str(ROOT / "aws-infra/contracts/artifact_verifier.py"), "--archive", str(path), "--sha256", digest, "--manifest-sha256", manifest_digest, "--environment", "dev", "--generation", "fixture-v1", "--node", "control-a", "--destination", str(self.folder / "release")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.folder / "release").exists())


if __name__ == "__main__":
    unittest.main()
