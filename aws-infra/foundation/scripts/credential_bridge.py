#!/usr/bin/env python3
"""In-memory CLI login -> SDK credential_process adapter; never logs child output."""
import argparse
import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

class CredentialError(ValueError):
    pass

def source_environment(config, credentials, original=None):
    env = dict(os.environ if original is None else original)
    # Never let the isolated config recursively resolve this credential_process.
    for key in list(env):
        if key.startswith(('AWS_', 'TF_LOG')):
            env.pop(key)
    env.update(AWS_CONFIG_FILE=str(config), AWS_SHARED_CREDENTIALS_FILE=str(credentials),
               AWS_EC2_METADATA_DISABLED='true', AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off')
    return env

def export_credentials(aws, profile, config, credentials, *, original=None, now=None):
    if not re.fullmatch(r'encore[1-5]', profile):
        raise CredentialError('source profile refused')
    child = subprocess.run([str(aws), 'configure', 'export-credentials', '--profile', profile,
                            '--format', 'process'], env=source_environment(config, credentials, original),
                           capture_output=True, text=True, timeout=60)
    if child.returncode:
        raise CredentialError('credential source failed; reauthenticate source profile separately')
    try:
        value = json.loads(child.stdout)
        expiration = datetime.fromisoformat(value['Expiration'].replace('Z', '+00:00'))
        if (value['Version'] != 1 or expiration.tzinfo is None or
            expiration <= (now or datetime.now(timezone.utc)) or
            not all(isinstance(value[k], str) and value[k] for k in
                    ('AccessKeyId', 'SecretAccessKey', 'SessionToken'))):
            raise ValueError()
    except (ValueError, TypeError, KeyError) as exc:
        raise CredentialError('credential process output missing, expired or invalid') from exc
    # Reject extra fields rather than accidentally printing CLI diagnostics/secrets.
    return {k: value[k] for k in ('Version', 'AccessKeyId', 'SecretAccessKey', 'SessionToken', 'Expiration')}

def create_isolated_config(destination, account_ids, aws, *, original=None):
    original = dict(os.environ if original is None else original)
    config = Path(original.get('AWS_CONFIG_FILE', str(Path.home()/'.aws/config'))).expanduser().resolve()
    credentials = Path(original.get('AWS_SHARED_CREDENTIALS_FILE', str(Path.home()/'.aws/credentials'))).expanduser().resolve()
    parser = configparser.RawConfigParser()
    bridge = Path(__file__).resolve()
    for account in account_ids:
        if not re.fullmatch(r'encore[1-5]', account):
            raise CredentialError('unknown account source')
        parser['profile '+account] = {
            'region': 'ap-northeast-2',
            'credential_process': shlex.join([sys.executable, str(bridge), '--aws', str(Path(aws).resolve()) if '/' in str(aws) else str(aws),
                                             '--profile', account, '--config', str(config), '--credentials', str(credentials)])}
        parser['profile p4-controller-'+account] = {
            'region': 'ap-northeast-2', 'source_profile': account,
            'role_arn': 'arn:aws:iam::'+account_ids[account]+':role/p4-controller-'+account,
            'role_session_name': 'p4-runtime-controller', 'duration_seconds': '3600'}
    destination = Path(destination)
    if destination.resolve() in (config, credentials):
        raise CredentialError('isolated config cannot replace source files')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        parser.write(out)
    os.chmod(destination, 0o600)
    env = source_environment(destination, os.devnull, original)
    return env, config, credentials

def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ('aws', 'profile', 'config', 'credentials'):
        p.add_argument('--'+key, required=True)
    a = p.parse_args()
    try:
        value = export_credentials(a.aws, a.profile, a.config, a.credentials)
    except (ValueError, OSError, subprocess.SubprocessError):
        print('credential bridge refused credentials; inspect source profile without logging secrets', file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(value)+'\n')
    return 0

if __name__ == '__main__':
    sys.exit(main())
