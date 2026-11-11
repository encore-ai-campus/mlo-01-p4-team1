"""Synthetic unit coverage for locked offline Python; no PyPI or AWS required."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from mlo_runtime.contracts import ROOT
from mlo_runtime.native_python import TARGET_DIRECT, compatible, hashed_requirements, verify


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fixture(base):
    root = Path(base)
    wheels = root / "wheels"
    wheels.mkdir()
    names = sorted(TARGET_DIRECT | {"typing-extensions"})
    backend = "".join(name + "==1.0\n" for name in names).encode()
    req = root / "requirements.lock"
    req.write_bytes(backend)
    app = root / "app.zip"
    with zipfile.ZipFile(app, "w") as z:
        z.writestr("backend/requirements.lock", backend)
    records = []
    for name in names:
        filename = name.replace("-", "_") + "-1.0-py3-none-any.whl"
        dist = name.replace("-", "_") + "-1.0.dist-info"
        with zipfile.ZipFile(wheels / filename, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(dist + "/METADATA", "Metadata-Version: 2.2\nName: " +
                       name + "\nVersion: 1.0\nSummary: synthetic test wheel\n")
            z.writestr(dist + "/WHEEL",
                       "Wheel-Version: 1.0\nGenerator: MLO unit fixture\n"
                       "Root-Is-Purelib: true\nTag: py3-none-any\n")
        file = wheels / filename
        records.append({"name": name, "version": "1.0", "filename": filename,
                        "sha256": sha(file.read_bytes()), "bytes": file.stat().st_size})
    data = {
        "schema_version": "native-feedback-python-lock-v1",
        "os": "ubuntu-24.04", "python": "CPython-3.12", "architecture": "x86_64",
        "source_requirements_sha256": sha(backend),
        "direct_requirements": [name + "==1.0" for name in sorted(TARGET_DIRECT)],
        "wheels": records,
        "unity_catalog_excluded": True,
        "ready_for_aws": False,
    }
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(data))
    return manifest, wheels, req, app, data


class NativeFeedbackPythonWheelLock(unittest.TestCase):
    def test_current_real_review_lock_declares_26_exact_pins(self):
        p = ROOT / "aws-infra/runtime/native-feedback-python.lock.json"
        data = json.loads(p.read_text())
        self.assertEqual(data["python"], "CPython-3.12")
        self.assertEqual(data["os"], "ubuntu-24.04")
        self.assertFalse(data["ready_for_aws"])
        self.assertEqual(len(data["wheels"]), 26)
        self.assertIn("typing-extensions", {i["name"] for i in data["wheels"]})
        self.assertEqual({i["name"] for i in data["wheels"]} & TARGET_DIRECT,
                         TARGET_DIRECT)

    def test_complete_synthetic_wheelhouse_is_deterministic_and_not_accepted(self):
        with tempfile.TemporaryDirectory(prefix="native-python-lock-") as path:
            manifest, wheels, req, app, _ = fixture(path)
            checked = verify(wheels, manifest, req, app)
            self.assertEqual(checked["wheel_count"], len(TARGET_DIRECT) + 1)
            self.assertFalse(checked["ready_for_aws"])
            lines = hashed_requirements(manifest).splitlines()
            self.assertEqual(len(lines), len(TARGET_DIRECT) + 1)
            self.assertTrue(all(" --hash=sha256:" in line for line in lines))

    def test_tampered_missing_extra_and_unapproved_abi_rejected(self):
        for condition in ("file-bytes", "missing", "extra", "source", "app",
                          "filename-abi", "metadata-abi", "manifest-os",
                          "manifest-direct", "symlink"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory(
                    prefix="native-python-reject-") as path:
                manifest, wheels, req, app, data = fixture(path)
                first = data["wheels"][0]
                target = wheels / first["filename"]
                if condition == "file-bytes":
                    target.write_bytes(b"corrupted")
                elif condition == "missing":
                    target.unlink()
                elif condition == "extra":
                    (wheels / "unknown-1.0-py3-none-any.whl").write_bytes(b"extra")
                elif condition == "source":
                    req.write_text(req.read_text() + "unapproved==1.0\n")
                elif condition == "app":
                    with zipfile.ZipFile(app, "w") as z:
                        z.writestr("backend/requirements.lock", "wrong")
                elif condition == "filename-abi":
                    filename = first["filename"].replace("-py3-none-any.whl",
                                                         "-cp314-cp314-win_amd64.whl")
                    target.rename(wheels / filename)
                    first["filename"] = filename
                elif condition == "metadata-abi":
                    with zipfile.ZipFile(target, "r") as source:
                        names = {k: source.read(k) for k in source.namelist()}
                    with zipfile.ZipFile(target, "w") as dest:
                        for name, raw in names.items():
                            if name.endswith("/WHEEL"):
                                raw = raw.replace(b"Tag: py3-none-any",
                                                  b"Tag: cp314-cp314-win_amd64")
                            dest.writestr(name, raw)
                    first["bytes"] = target.stat().st_size
                    first["sha256"] = sha(target.read_bytes())
                elif condition == "manifest-os":
                    data["os"] = "windows"
                elif condition == "manifest-direct":
                    data["direct_requirements"].pop()
                elif condition == "symlink":
                    replacement = Path(path) / "outside.whl"
                    target.rename(replacement)
                    target.symlink_to(replacement)
                manifest.write_text(json.dumps(data))
                with self.assertRaises((ValueError, zipfile.BadZipFile)):
                    verify(wheels, manifest, req, app)

    def test_compatibility_rejects_arm_windows_and_newer_interpreter(self):
        self.assertTrue(compatible(["cp312-cp312-manylinux_2_28_x86_64"]))
        self.assertTrue(compatible(["cp39-abi3-manylinux_2_26_x86_64"]))
        self.assertTrue(compatible(["py3-none-any"]))
        self.assertFalse(compatible(["cp314-cp314-manylinux_2_28_x86_64"]))
        self.assertFalse(compatible(["cp312-cp312-manylinux_2_40_x86_64"]))
        self.assertFalse(compatible(["cp312-cp312-manylinux_2_28_aarch64"]))
        self.assertFalse(compatible(["cp312-cp312-win_amd64"]))


if __name__ == "__main__":
    unittest.main()
