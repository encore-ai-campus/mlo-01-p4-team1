"""Exercise the real archive verifier/installer before the non-root ELK lab.

Only synthetic lab certificates are projected into a test-owned named volume.
Production keeps the installer's individual read-only file binds.
"""
import json
from pathlib import Path

from mlo_runtime.contracts import RUNTIME
from mlo_runtime.packager import package


def install_observation_files(lab, config):
    host = lab.folder / "bundle-observation/hosts/control-b"
    host.mkdir(parents=True)
    names = ("logstash.yml", "logstash.conf", "kibana.yml")
    for name in names:
        (host / name).write_bytes((lab.folder / name).read_bytes())
    services = {}
    secrets = ["/run/mlo/secrets/" + name for name in
               ("ca.crt", "server.crt", "server.key", "server-pkcs8.key")]
    for service, image, files in (("log-ingest", "logstash", names[:2]),
                                   ("kibana", "kibana", names[2:])):
        services[service] = {"image": lab.image(image), "volumes": [
            f"/etc/mlo/current/config/{name}:/installed/{name}:ro" for name in files
        ] + [f"{name}:{name}:ro" for name in secrets]}
    (host / "compose.yml").write_text(json.dumps({"services": services}))
    (host / "details.json").write_text(json.dumps({"platform": "linux/amd64"}))
    result = package(host.parents[1], lab.folder / "observation.tar.gz",
                     artifact_version="observation-v1", environment="dev", generation="g1",
                     node_roles={"control-b": "control"},
                     image_digests={k: v["image"] for k, v in services.items()},
                     required_secrets={"control-b": secrets}, fixture=False)
    (lab.folder / "observation-result.json").write_text(json.dumps(result))
    config["volumes"].update({"installed": {}, "pq": {}, "kb": {}})
    config["services"]["install-config"] = {
        "image": "mlo-p4-b-host-lab:20261029", "user": "0:0",
        "mem_limit": "256m", "labels": {"mlo.run": lab.project, "mlo.role": "B"},
        "volumes": [f"{lab.folder}:/seed:ro", f"{RUNTIME.parents[1]}:/workspace:ro",
                    "installed:/export", "pq:/pq", "kb:/kb"],
        "entrypoint": ["/usr/bin/python3", "-c"],
        "command": [INSTALL],
    }
    lab.compose_path.write_text(json.dumps(config))
    output = lab.compose("run", "--rm", "-T", "install-config", timeout=120)
    assert b"verified archive -> installed 1000:0/0600" in output.stdout


INSTALL = r'''
import json, os, shutil, stat, sys
from pathlib import Path
sys.path.insert(0, "/workspace/aws-infra/runtime/src")
sys.path.insert(0, "/workspace/aws-infra/contracts")
from artifact_verifier import inspect_artifact
from mlo_runtime.install import prepare_release, verify_payload, InstallError
result=json.loads(Path("/seed/observation-result.json").read_text())
root=Path("/tmp/mlo-observation-install"); root.mkdir()
payload=root/"payload"
inspect_artifact("/seed/observation.tar.gz", result["sha256"], "dev", "g1", "control-b",
                 payload, result["manifest_sha256"])
names=("logstash.yml", "logstash.conf", "kibana.yml")
assert all(stat.S_IMODE((payload/"hosts/control-b"/n).stat().st_mode)==0o600 for n in names)
binding={"node":"control-b", "role":"control", "environment":"dev", "generation":"g1",
         "root":str(root), "lab":True, "accepted_manifests":[result["manifest_sha256"]],
         "image_digests":result["manifest"]["image_digests"],
         "required_secrets":result["manifest"]["required_secrets"]["control-b"],
         "persistent_owners":{"observations/logstash":{"uid":1000,"gid":0},
                              "observations/kibana":{"uid":1000,"gid":0}}}
for name in binding["required_secrets"]:
    target=root/name.lstrip("/"); target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile("/seed/"+target.name,target); target.chmod(0o600)
# A root-owned private key cannot be silently made public or auto-chowned.
try:
    prepare_release(payload, root/"rejected", binding, result["manifest"])
except InstallError:
    assert not (root/"rejected").exists()
else:
    raise AssertionError("root-owned secret was accepted for UID 1000")
for name in binding["required_secrets"]:
    os.chown(root/name.lstrip("/"),1000,0)
manifest=verify_payload(payload,binding,"install")
release=root/"release"
prepare_release(payload,release,binding,manifest)
installed=json.loads((release/"installed-compose.json").read_text())
assert all(s["user"]=="1000:0" for s in installed["services"].values())
for name in names:
    source=release/"hosts/control-b/config"/name
    assert source.stat().st_uid==1000 and stat.S_IMODE(source.stat().st_mode)==0o600
    target=Path("/export")/name
    shutil.copyfile(source,target); target.chmod(0o600); os.chown(target,1000,0)
    assert source.read_bytes()==target.read_bytes()
for name in binding["required_secrets"]:
    source=root/name.lstrip("/"); target=Path("/export")/source.name
    shutil.copyfile(source,target); target.chmod(0o600); os.chown(target,1000,0)
for name in ("/pq","/kb"):
    os.chown(name,1000,0); os.chmod(name,0o750)
os.chown("/export",1000,0); os.chmod("/export",0o750)
print("verified archive -> installed 1000:0/0600",flush=True)
'''
