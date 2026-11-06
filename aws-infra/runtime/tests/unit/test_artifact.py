import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from mlo_runtime.artifact import inspect_artifact
from mlo_runtime.contracts import ContractError


class ArtifactTests(unittest.TestCase):
    def payload(self, directory, name="config/web.json", link=False, mutation=None):
        archive = Path(directory) / "runtime.tar.gz"
        body = b'{"image":"supplied-separately"}'
        entrypoint = b'print("fixed fixture")\n'
        manifest = {
            "schema_version": "1.0.0", "artifact_version": "v1", "environment": "dev",
            "generation": "g1", "example_only": False, "platform": "linux/amd64",
            "target_nodes": ["control-a"], "node_roles": {"control-a": "control"},
            "operations": ["install", "deploy", "health", "rollback"],
            "files": {name: hashlib.sha256(body).hexdigest(), "bin/runtime-entrypoint.py": hashlib.sha256(entrypoint).hexdigest()},
            "image_digests": {}, "config_hashes": {}, "required_secrets": {"control-a": []}, "fixture": True,
        }
        if mutation:
            mutation(manifest)
        raw = json.dumps(manifest).encode()
        with tarfile.open(archive, "w:gz") as target:
            for key, data in [("manifest.json", raw), (name, body), ("bin/runtime-entrypoint.py", entrypoint)]:
                info = tarfile.TarInfo(key)
                info.size = len(data)
                if link and key == name:
                    info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                target.addfile(info, io.BytesIO(data))
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest(), hashlib.sha256(raw).hexdigest()

    def test_checksum_target_and_safe_extract(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, digest, manifest = self.payload(directory)
            destination = Path(directory) / "version"
            for actual_digest, gen, node, actual_manifest in [
                ("0" * 64, "g1", "control-a", manifest),
                (digest, "g2", "control-a", manifest),
                (digest, "g1", "control-b", manifest),
                (digest, "g1", "control-a", "0" * 64),
                (digest, "g1", "control-a", None),
            ]:
                with self.assertRaises(ContractError):
                    inspect_artifact(archive, actual_digest, "dev", gen, node, destination, actual_manifest)
                self.assertFalse(destination.exists())
            inspect_artifact(archive, digest, "dev", "g1", "control-a", destination, manifest)
            self.assertEqual((destination / "config/web.json").stat().st_mode & 0o777, 0o600)

    def test_path_traversal_and_symlink_rejected(self):
        for name, link in [("../escape", False), ("/absolute", False), ("config/link", True)]:
            with tempfile.TemporaryDirectory() as directory:
                archive, digest, manifest = self.payload(directory, name, link)
                with self.assertRaises(ContractError):
                    inspect_artifact(archive, digest, "dev", "g1", "control-a", Path(directory) / "version", manifest)

    def test_malformed_manifest_rejected(self):
        for mutation in [lambda m: m.update(node_roles={"control-a": "unknown"}),
                         lambda m: m.update(platform="linux/arm64"),
                         lambda m: m.update(operations=["shell"]),
                         lambda m: m.update(image_digests={"app": "repo:latest"})]:
            with tempfile.TemporaryDirectory() as directory:
                archive, digest, manifest = self.payload(directory, mutation=mutation)
                with self.assertRaises(ContractError):
                    inspect_artifact(archive, digest, "dev", "g1", "control-a", Path(directory) / "version", manifest)
