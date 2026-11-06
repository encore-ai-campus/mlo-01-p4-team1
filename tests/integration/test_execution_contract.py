"""Real credential consumers and A-dispatcher -> B-installer, with no AWS network."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import boto3
from botocore.config import Config
from botocore.stub import Stubber
from botocore.exceptions import CredentialRetrievalError
import yaml

from credential_bridge import create_isolated_config
from integration import resources, state_from_test
from mlo_runtime.contracts import ROOT, RUNTIME
from mlo_runtime.lab import Lab
from mlo_runtime.packager import package
from mlo_runtime.render import render


class CredentialConsumerTests(unittest.TestCase):
    def setup_bridge(self, folder):
        source = folder / "source-config"
        source.write_text("[profile encore1]\nregion=ap-northeast-2\n")
        credentials = folder / "credentials"
        credentials.write_text("")
        fake = folder / "aws"
        fake.write_text("#!" + sys.executable + "\n" + '''import json,os,sys
if sys.argv[1:4]!=['configure','export-credentials','--profile']:sys.exit(4)
if os.environ['AWS_CONFIG_FILE']!=os.environ['EXPECTED_SOURCE']:sys.exit(5)
if os.environ.get('FAIL_SOURCE'):sys.exit(7)
print(json.dumps({'Version':1,'AccessKeyId':'ASIA'+'0'*16,'SecretAccessKey':'fixture-'+'x'*32,'SessionToken':'fixture-token','Expiration':'2000-01-01T00:00:00Z' if os.environ.get('EXPIRE_SOURCE') else '2099-01-01T00:00:00Z'}))
''')
        fake.chmod(0o700)
        original = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        original.update(AWS_CONFIG_FILE=str(source), AWS_SHARED_CREDENTIALS_FILE=str(credentials), EXPECTED_SOURCE=str(source.resolve()))
        env, _, _ = create_isolated_config(folder / "isolated", {"encore1": "111111111111"}, fake, original=original)
        return env

    def test_sdk_process_and_controller_assume_role_use_isolated_source(self):
        with tempfile.TemporaryDirectory(prefix="mlo-sdk-") as name:
            folder = Path(name)
            env = self.setup_bridge(folder)
            with patch.dict(os.environ, env, clear=True):
                session = boto3.Session(profile_name="encore1", region_name="ap-northeast-2")
                credentials = session.get_credentials().get_frozen_credentials()
                self.assertEqual(credentials.token, "fixture-token")
                # STS is intercepted before any request is sent; source credentials are real process output.
                client = session.client("sts", config=Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1}))
                expected = {"RoleArn": "arn:aws:iam::111111111111:role/p4-controller-encore1", "RoleSessionName": "p4-runtime-controller", "DurationSeconds": 3600}
                response = {"Credentials": {"AccessKeyId": "ASIA" + "1" * 16, "SecretAccessKey": "x" * 40, "SessionToken": "controller-fixture", "Expiration": datetime(2099, 1, 1, tzinfo=timezone.utc)}, "AssumedRoleUser": {"AssumedRoleId": "fixture:controller", "Arn": "arn:aws:sts::111111111111:assumed-role/p4-controller-encore1/p4-runtime-controller"}}
                controller = boto3.Session(profile_name="p4-controller-encore1", region_name="ap-northeast-2")
                with Stubber(client) as stub:
                    stub.add_response("assume_role", response, expected)
                    with patch.object(controller._session, "create_client", return_value=client):
                        self.assertEqual(controller.get_credentials().get_frozen_credentials().token, "controller-fixture")
                    stub.assert_no_pending_responses()
                self.assertEqual((folder / "isolated").stat().st_mode & 0o777, 0o600)
                self.assertNotIn("SecretAccessKey", (folder / "isolated").read_text())

    def test_failed_or_expired_source_cannot_supply_sdk_credentials(self):
        with tempfile.TemporaryDirectory(prefix="mlo-sdk-negative-") as name:
            env = self.setup_bridge(Path(name))
            for condition in ["FAIL_SOURCE", "EXPIRE_SOURCE"]:
                with self.subTest(condition=condition), patch.dict(os.environ, {**env, condition: "1"}, clear=True):
                    with self.assertRaises(CredentialRetrievalError) as result:
                        boto3.Session(profile_name="encore1").get_credentials().get_frozen_credentials()
                    self.assertNotIn("fixture-token", str(result.exception))


LINUX_HARNESS = r'''
import base64,copy,http.server,json,os,pathlib,re,subprocess,threading
root=pathlib.Path('/tmp/mlo-x-runtime')
seed=pathlib.Path('/seed')
binding=json.loads((seed/'binding.json').read_text())
host=root/'binding.json';host.write_text(json.dumps(binding));host.chmod(0o600)
binary=root/'bin';binary.mkdir()
aws=binary/'aws'
aws.write_text('#!/usr/bin/python3\nimport os,pathlib,shutil,sys\nargs=sys.argv[1:]\nif args[:2]!=["s3api","get-object"] or args[args.index("--version-id")+1]!="fixture-s3-version":sys.exit(5)\nshutil.copyfile(os.environ["FAKE_S3_OBJECT"],args[-1])\n')
aws.chmod(0o700)
class Metadata(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):
        if self.path!='/latest/api/token':self.send_error(403);return
        self.send_response(200);self.end_headers();self.wfile.write(b'fixture-imds-token')
    def do_GET(self):
        if self.headers.get('X-aws-ec2-metadata-token')!='fixture-imds-token':self.send_error(403);return
        self.send_response(200);self.end_headers();self.wfile.write(binding['instance_id'].encode())
    def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Metadata)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
template=(seed/'dispatch.sh').read_text()
original=(seed/'dispatcher.py').read_bytes()
local=original.replace(b'http://169.254.169.254',('http://127.0.0.1:'+str(server.server_port)).encode()).replace(b'/var/lib/mlo/dispatch',str(root/'dispatch').encode()).replace(b'/etc/mlo/host-binding.json',str(host).encode())
template=template.replace(base64.b64encode(original).decode(),base64.b64encode(local).decode())
base=json.loads((seed/'config.json').read_text());artifacts=json.loads((seed/'artifacts.json').read_text())
config_line=next(line for line in template.splitlines() if line.startswith('config = json.loads('))
results=[]
def invoke(version,operation,mutation=None,success=True):
    config=copy.deepcopy(base);config['artifact']=dict(artifacts[version]['binding'])
    if mutation=='hash':config['artifact']['sha256']='0'*64
    if mutation=='manifest':config['artifact']['manifest_sha256']='0'*64
    if mutation=='generation':config['generation']='another-generation'
    if mutation=='node':config['instances'][binding['instance_id']]['name']='control-b'
    quote="'"*3
    line='config = json.loads(r'+quote+json.dumps(config)+quote+')'
    script=template.replace(config_line,line).replace('{{ Operation }}',operation)
    env={k:v for k,v in os.environ.items() if not k.startswith('AWS_')}
    env.update(PATH=str(binary)+os.pathsep+env['PATH'],FAKE_S3_OBJECT='/seed/'+artifacts[version]['file'])
    result=subprocess.run(['bash','-c',script],env=env,capture_output=True,text=True)
    receipt=json.loads((root/'dispatch/receipt.json').read_text())
    if (result.returncode==0)!=success or receipt['success']!=success:raise RuntimeError('X execution mismatch '+version+'/'+operation+'/'+str(mutation)+' '+result.stderr[-1200:])
    results.append({'version':version,'operation':operation,'mutation':mutation,'success':success})
try:
    invoke('v1','install');invoke('v1','install');invoke('v1','health')
    data=root/'srv/mlo/preserve';data.write_text('keep')
    invoke('v2','deploy');invoke('v2','rollback')
    for mutation in ['hash','manifest','generation','node']:invoke('v1','health',mutation,False)
    invoke('missing-secret','deploy',success=False)
    invoke('partial','deploy',success=False)
    invoke('v1','health')
    state=json.loads((root/'opt/mlo/state.json').read_text())
    if state['current']!='v1' or data.read_text()!='keep':raise RuntimeError('old revision or data changed')
    receipts=[json.loads(p.read_text()) for p in (root/'opt/mlo/receipts').glob('*.json')]
    if not any(r['status']=='failed' for r in receipts):raise RuntimeError('installer error receipt not retained')
    (root/'result.json').write_text(json.dumps({'status':'pass','operations':results,'current':state['current'],'data_preserved':True,'installer_receipts':len(receipts)}))
    print('X_ARTIFACT_LINUX_PASS')
finally:
    server.shutdown();server.server_close();thread.join()
'''


class ExecutionContractTests(unittest.TestCase):
    def test_evaluated_A_dispatcher_installs_real_B_artifact_on_disposable_linux(self):
        foundation = ROOT / "aws-infra/foundation"
        latest = json.loads((foundation / ".artifacts/latest-run.json").read_text())
        state = state_from_test((foundation / latest["logs"] / "bound-encore1-artifact-controller-test.jsonl").read_text())
        document = next(r["values"] for r in resources(state) if r["type"] == "aws_ssm_document")
        template = "\n".join(json.loads(document["content"])["mainSteps"][0]["inputs"]["runCommand"])
        line = next(line for line in template.splitlines() if line.startswith("config = json.loads("))
        quote = "'" * 3
        config = json.loads(line.split("r" + quote, 1)[1].rsplit(quote, 1)[0])
        lab = Lab("x-artifact")
        self.addCleanup(lab.close)
        render(foundation / ".artifacts/inventory.mock.json", lab.folder / "plan.json", bundle=lab.folder / "bundle")
        host = lab.folder / "bundle/hosts/control-a"
        # Explicit no-product-app fixture. Actual service/Compose readiness is tested in B's Linux suite.
        (host / "compose.yml").write_text(yaml.safe_dump({"services": {}}))
        for unit in [*host.glob("*.service"), *host.glob("*.timer")]:
            unit.unlink()
        artifacts = {}
        for version in ["v1", "v2", "missing-secret", "partial"]:
            if version == "partial":
                (host / "config").mkdir()
                (host / "config/conflict.json").write_text("{}")
            required = ["/run/mlo/secrets/missing-token"] if version == "missing-secret" else []
            result = package(lab.folder / "bundle", lab.folder / (version + ".tar.gz"), artifact_version=version, environment="dev", generation=config["generation"], node_roles={"control-a": "control"}, image_digests={}, required_secrets={"control-a": required}, fixture=True)
            artifacts[version] = {"file": version + ".tar.gz", "binding": {"key": "artifacts/runtime.tar.gz", "version": "fixture-s3-version", "sha256": result["sha256"], "manifest_sha256": result["manifest_sha256"]}}
        instance = next(key for key, value in config["instances"].items() if value["name"] == "control-a")
        binding = {"schema_version": "1.0.0", "instance_id": instance, "node": "control-a", "role": "control", "environment": "dev", "generation": config["generation"], "lab": True, "root": "/tmp/mlo-x-runtime", "mount_target": "/tmp/mlo-x-runtime/srv/mlo", "image_digests": {}, "required_secrets": [], "accepted_manifests": [a["binding"]["manifest_sha256"] for a in artifacts.values()]}
        for name, value in [("config.json", config), ("binding.json", binding), ("artifacts.json", artifacts)]:
            (lab.folder / name).write_text(json.dumps(value))
        (lab.folder / "dispatch.sh").write_text(template)
        (lab.folder / "dispatcher.py").write_bytes((foundation / "scripts/dispatcher.py").read_bytes())
        (lab.folder / "harness.py").write_text(LINUX_HARNESS)
        lab.configure({"image": "mlo-p4-b-host-lab:20261029", "volumes": ["data:/tmp/mlo-x-runtime/srv/mlo", f"{lab.folder}:/seed:ro"], "command": ["sleep", "infinity"]})
        lab.up()
        result = lab.exec("/usr/bin/python3", "/seed/harness.py", timeout=180)
        self.assertIn(b"X_ARTIFACT_LINUX_PASS", result.stdout)
        evidence = lab.exec("cat", "/tmp/mlo-x-runtime/result.json").stdout
        (lab.folder / "result.json").write_bytes(evidence)
        self.assertTrue(json.loads(evidence)["data_preserved"])


if __name__ == "__main__":
    unittest.main()
