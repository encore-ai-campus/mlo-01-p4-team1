"""Trusted SSM dispatcher; embedded with common verifier in the Terraform document."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from artifact_verifier import inspect_artifact

IMDS_ENDPOINT = 'http://169.254.169.254'
DISPATCH_ROOT = '/var/lib/mlo/dispatch'
HOST_BINDING = '/etc/mlo/host-binding.json'

def imds_instance_id():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(IMDS_ENDPOINT+'/latest/api/token', method='PUT',
                                 headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'})
    with opener.open(req, timeout=3) as response:
        token = response.read(4097).decode('ascii')
    if not token or len(token)>4096 or '\n' in token:
        raise ValueError('invalid IMDSv2 token')
    req = urllib.request.Request(IMDS_ENDPOINT+'/latest/meta-data/instance-id',
                                 headers={'X-aws-ec2-metadata-token': token})
    with opener.open(req, timeout=3) as response:
        instance_id = response.read(64).decode('ascii').strip()
    if not re.fullmatch(r'i-[0-9a-f]{17}', instance_id):
        raise ValueError('invalid IMDSv2 instance identity')
    return instance_id

def protected_binding(path):
    path = Path(path)
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ValueError('host binding must be protected and owned by executor')
    return json.loads(path.read_text())

def dispatch(config, operation):
    root = Path(DISPATCH_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    history = root/'receipts'
    history.mkdir(exist_ok=True, mode=0o700)
    receipt = {'schema_version': '1.0.0', 'operation': operation, 'generation': config['generation'],
               'environment': config['environment'], 'success': False, 'stage': 'identity',
               'dispatch_id': uuid.uuid4().hex, 'artifact_key': config['artifact']['key'],
               's3_version': config['artifact']['version']}
    receipt_path = root/'receipt.json'
    def save():
        fd, name = tempfile.mkstemp(prefix='.receipt-', dir=root)
        with os.fdopen(fd, 'w') as out:
            json.dump(receipt, out, sort_keys=True)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        # Keep every attempt, including refused/failed retries, plus a latest receipt.
        os.link(name, history/(receipt['dispatch_id']+'.json'))
        os.replace(name, receipt_path)
        for directory in (history, root):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    try:
        if operation not in ('install', 'deploy', 'health', 'rollback'):
            raise ValueError('operation refused')
        instance_id = imds_instance_id()
        target = config['instances'][instance_id]
        binding = protected_binding(HOST_BINDING)
        expected = {'instance_id': instance_id, 'node': target['name'], 'role': target['role'],
                    'environment': config['environment'], 'generation': config['generation']}
        if any(binding.get(k) != v for k,v in expected.items()):
            raise ValueError('actual instance and protected host binding mismatch')
        receipt.update(node=target['name'], stage='download')
        with tempfile.TemporaryDirectory(prefix='stage-', dir=root) as work:
            archive = Path(work)/'artifact.tar.gz'
            # IAM credentials resolve from the EC2 instance role. CLI output is never logged.
            result = subprocess.run(['aws', 's3api', 'get-object', '--region', 'ap-northeast-2',
                                     '--bucket', config['bucket'], '--key', config['artifact']['key'],
                                     '--version-id', config['artifact']['version'], str(archive)],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise ValueError('versioned download failed')
            receipt['stage'] = 'verify'
            payload = Path(work)/'payload'
            manifest = inspect_artifact(archive, config['artifact']['sha256'], config['environment'],
                                        config['generation'], target['name'], payload,
                                        expected_manifest_sha256=config['artifact']['manifest_sha256'])
            if (manifest['node_roles'].get(target['name']) != target['role'] or
                operation not in manifest['operations']):
                raise ValueError('artifact role or operation mismatch')
            entrypoint = payload/'bin/runtime-entrypoint.py'
            if not entrypoint.is_file() or entrypoint.is_symlink():
                raise ValueError('fixed runtime entrypoint missing')
            receipt.update(stage='runtime', artifact_version=manifest['artifact_version'],
                           sha256=config['artifact']['sha256'], manifest_sha256=config['artifact']['manifest_sha256'])
            env = {k:v for k,v in os.environ.items() if not k.startswith(('PYTHON', 'AWS_'))}
            # The runtime entrypoint resolves protected host credentials; no profile/login files are copied.
            result = subprocess.run([sys.executable, '-I', str(entrypoint), '--operation', operation,
                                     '--environment', config['environment'], '--generation', config['generation'],
                                     '--node', target['name'], '--binding', HOST_BINDING],
                                    cwd=payload, env=env, capture_output=True, text=True, timeout=1500)
            if result.returncode:
                raise ValueError('runtime operation failed')
        receipt.update(success=True, stage='complete')
        save()
        return receipt
    except Exception:
        receipt['failure'] = 'dispatch refused or failed; reconcile protected receipt before retry'
        save()
        raise ValueError('runtime dispatch failed; inspect protected receipt without secrets') from None
