"""Small manifest-v1 native configuration artifact; binaries travel separately."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile

from .contracts import ROOT, RUNTIME, load_inventory
from .native import plan
from .native_releases import need

ENTRYPOINT = '''from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aws-infra/runtime/src"))
from mlo_runtime.native_host import main
raise SystemExit(main())
'''


def package(inventory, output, revision, *, fixture=False):
    need(not inventory["example_only"] or fixture, "example inventory requires explicit fixture artifact")
    value = plan(inventory, revision)
    files = {"bin/runtime-entrypoint.py": ENTRYPOINT.encode()}
    secrets, roles, images = {}, {}, {}
    for name, node in value["nodes"].items():
        node = {**node, "node": name, "environment": inventory["environment"], "generation": inventory["generation"]}
        node["config_hashes"] = {p: hashlib.sha256(b.encode()).hexdigest() for p, b in node["configs"].items()}
        node["unit_hashes"] = {p: hashlib.sha256(b.encode()).hexdigest() for p, b in node["units"].items()}
        secrets[name] = sorted({line.split(":", 1)[1] for text in node["units"].values() for line in text.splitlines() if line.startswith("LoadCredential=")})
        # Protocol health checks use credentials independently of service-start credentials.
        secrets[name] = sorted(set(secrets[name]) | {"/run/mlo/secrets/ca.crt", "/run/mlo/secrets/client.crt", "/run/mlo/secrets/client.key"} | ({"/run/mlo/secrets/es-netrc"} if node["role"] == "elasticsearch" else set()))
        roles[name] = node["role"]
        for service, item in node["auxiliary_compose"]["services"].items():
            images[service] = item["image"]
        files[f"hosts/{name}/native.json"] = (json.dumps(node, indent=2, sort_keys=True) + "\n").encode()
    for path in (RUNTIME / "src/mlo_runtime").glob("*.py"):
        files["aws-infra/runtime/src/mlo_runtime/" + path.name] = path.read_bytes()
    for path in (ROOT / "aws-infra/contracts").glob("*.json"):
        files["aws-infra/contracts/" + path.name] = path.read_bytes()
    for path in (ROOT / "aws-infra/contracts/infra_common").glob("*.py"):
        files["aws-infra/contracts/infra_common/" + path.name] = path.read_bytes()
    files["scripts/check_preparation.py"] = (ROOT / "scripts/check_preparation.py").read_bytes()
    files["aws-infra/runtime/native-releases.lock.json"] = (RUNTIME / "native-releases.lock.json").read_bytes()
    files["aws-infra/runtime/native-connectors.lock.json"] = (RUNTIME / "native-connectors.lock.json").read_bytes()
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
    manifest = {"schema_version": "1.0.0", "artifact_version": revision,
        "environment": inventory["environment"], "generation": inventory["generation"],
        "platform": "linux/amd64", "example_only": False, "target_nodes": sorted(roles),
        "node_roles": roles, "operations": ["install", "deploy", "health", "rollback"],
        "files": hashes, "image_digests": images, "required_secrets": secrets,
        "config_hashes": {k: v for k, v in hashes.items() if k.startswith("hosts/")}, "fixture": fixture}
    sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
    from artifact_verifier import validate_manifest
    validate_manifest(manifest, inventory["environment"], inventory["generation"], sorted(roles)[0])
    files["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    output = Path(output)
    need(not output.exists(), "native artifact output exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=".native-package-")
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as tar:
                for name, body in sorted(files.items()):
                    member = tarfile.TarInfo(name)
                    member.mode, member.size, member.mtime = 0o600, len(body), 0
                    tar.addfile(member, io.BytesIO(body))
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"archive": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256(files["manifest.json"]).hexdigest(),
            "target_nodes": sorted(roles), "fixture": fixture, "ready_for_aws": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    inventory, _ = load_inventory(args.inventory)
    need(inventory["example_only"] or ".private" in args.output.resolve().parts, "real artifact must stay private")
    print(json.dumps(package(inventory, args.output, args.revision, fixture=args.fixture), sort_keys=True))


if __name__ == "__main__":
    main()
