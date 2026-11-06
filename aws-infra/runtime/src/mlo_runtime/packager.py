"""Build the shared manifest-v1 tar.gz; never publish or run an artifact."""

from __future__ import annotations
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
import sys

from .contracts import ROOT, RUNTIME, require

ENTRYPOINT = '''"""Fixed runtime entrypoint. No shell command is accepted from deployment inputs."""
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aws-infra/runtime/src"))
from mlo_runtime.install import main
if __name__ == "__main__":
    raise SystemExit(main())
'''


def package(
    bundle,
    output,
    *,
    artifact_version,
    environment,
    generation,
    node_roles,
    image_digests,
    required_secrets,
    fixture=False,
):
    bundle, output = Path(bundle), Path(output)
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", artifact_version),
        "invalid artifact version",
    )
    require(bool(node_roles), "target node bindings required")
    files = {"bin/runtime-entrypoint.py": ENTRYPOINT.encode()}
    for node, role in sorted(node_roles.items()):
        require(re.fullmatch(r"[a-z0-9][a-z0-9-]+", node), "invalid target node")
        folder = bundle / "hosts" / node
        require(folder.is_dir() and not folder.is_symlink(), "missing node bundle")
        details = json.loads((folder / "details.json").read_text())
        compose = __import__("yaml").safe_load((folder / "compose.yml").read_text())
        services = compose.get("services", {})
        files["hosts/" + node + "/compose.json"] = (
            json.dumps(compose, sort_keys=True) + "\n"
        ).encode()
        require(fixture or bool(services), "operational node cannot have no services")
        for service in services.values():
            image = service.get("image", "")
            require(
                re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image),
                "unresolved or mutable image",
            )
            require(
                image in image_digests.values(),
                "compose image missing from manifest bindings",
            )
        require(node in required_secrets, "node secret acceptance missing")
        require(
            isinstance(required_secrets[node], list)
            and all(
                re.fullmatch(r"/run/mlo/secrets/[A-Za-z0-9_.-]+", name)
                for name in required_secrets[node]
            ),
            "protected absolute secret paths required",
        )
        if not fixture:
            require(
                bool(required_secrets[node]), "operational secrets must be accepted"
            )
            require(details.get("platform") == "linux/amd64", "node platform mismatch")
        for path in sorted(folder.rglob("*")):
            require(not path.is_symlink(), "bundle links refused")
            if path.is_file():
                if path == folder / "compose.json":
                    require(
                        json.loads(path.read_text()) == compose,
                        "YAML/JSON Compose bundle mismatch",
                    )
                    continue
                files["hosts/" + node + "/" + str(path.relative_to(folder))] = (
                    path.read_bytes()
                )
                if not fixture:
                    text = path.read_text()
                    references = set(
                        re.findall(r"/run/mlo/secrets/[A-Za-z0-9_.-]+", text)
                    )
                    references.update(
                        "/run/mlo/" + name
                        for name in re.findall(
                            r"(?<!/)secrets/(?:server\.key|server\.crt|ca\.crt)", text
                        )
                    )
                    require(
                        references <= set(required_secrets[node]),
                        "rendered secret dependency missing from accepted manifest",
                    )
    for path in sorted((RUNTIME / "src/mlo_runtime").glob("*.py")):
        files["aws-infra/runtime/src/mlo_runtime/" + path.name] = path.read_bytes()
    # Preserve relative ROOT/RUNTIME paths for shared runtime contract consumers.
    files["scripts/check_preparation.py"] = (
        ROOT / "scripts/check_preparation.py"
    ).read_bytes()
    for path in sorted((ROOT / "aws-infra/contracts").glob("*.json")):
        files["aws-infra/contracts/" + path.name] = path.read_bytes()
    for path in sorted((ROOT / "aws-infra/contracts/infra_common").glob("*.py")):
        files["aws-infra/contracts/infra_common/" + path.name] = path.read_bytes()
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
    manifest = {
        "schema_version": "1.0.0",
        "artifact_version": artifact_version,
        "environment": environment,
        "generation": generation,
        "platform": "linux/amd64",
        "example_only": False,
        "target_nodes": sorted(node_roles),
        "node_roles": node_roles,
        "operations": ["install", "deploy", "health", "rollback"],
        "files": hashes,
        "image_digests": image_digests,
        "config_hashes": {
            name: value for name, value in hashes.items() if name.startswith("hosts/")
        },
        "required_secrets": required_secrets,
        "fixture": fixture,
    }
    body = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
    from artifact_verifier import validate_manifest

    validate_manifest(manifest, environment, generation, sorted(node_roles)[0])
    files["manifest.json"] = body
    require(not output.exists(), "artifact output exists; preserve previous artifact")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".runtime-package-", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(
            fileobj=raw, mode="wb", mtime=0
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, data in sorted(files.items()):
                    member = tarfile.TarInfo(name)
                    member.mode, member.mtime, member.size = 0o600, 0, len(data)
                    archive.addfile(member, io.BytesIO(data))
        # Hard-link publication refuses races without overwriting a prior output.
        os.link(temporary, output)
    finally:
        os.unlink(temporary)
    return {
        "archive": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(body).hexdigest(),
        "manifest": manifest,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in [
        "bundle",
        "output",
        "artifact-version",
        "environment",
        "generation",
        "binding",
    ]:
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    binding = json.loads(Path(args.binding).read_text())
    result = package(
        args.bundle,
        args.output,
        artifact_version=args.artifact_version,
        environment=args.environment,
        generation=args.generation,
        node_roles=binding["node_roles"],
        image_digests=binding["image_digests"],
        required_secrets=binding["required_secrets"],
        fixture=binding.get("fixture") is True,
    )
    print(
        json.dumps({k: v for k, v in result.items() if k != "manifest"}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
