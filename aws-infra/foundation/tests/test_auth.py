import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from credential_bridge import create_isolated_config, export_credentials, CredentialError
from model import fixture_binding

class AuthTests(unittest.TestCase):
    def test_isolated_config_sources_controller_roles_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'source config';credentials=folder/'source credentials'
            source.write_text('[profile encore1]\nlogin_session=original\n');credentials.write_text('')
            original={'AWS_CONFIG_FILE':str(source),'AWS_SHARED_CREDENTIALS_FILE':str(credentials),'AWS_ACCESS_KEY_ID':'unwanted','AWS_SECRET_ACCESS_KEY':'must-not-copy','PATH':os.environ['PATH']}
            destination=folder/'isolated config'
            env,config,shared=create_isolated_config(destination,fixture_binding()['account_ids'],'aws',original=original)
            self.assertEqual(config,source.resolve());self.assertEqual(shared,credentials.resolve())
            self.assertEqual(source.read_text(),'[profile encore1]\nlogin_session=original\n')
            self.assertNotIn('AWS_ACCESS_KEY_ID',env);self.assertNotIn('must-not-copy',destination.read_text())
            parser=configparser.RawConfigParser();parser.read(destination)
            self.assertEqual(len(parser.sections()),10)
            for account,account_id in fixture_binding()['account_ids'].items():
                argv=shlex.split(parser['profile '+account]['credential_process'])
                self.assertEqual(argv[argv.index('--config')+1],str(source.resolve()))
                controller=parser['profile p4-controller-'+account]
                self.assertEqual(controller['source_profile'],account)
                self.assertEqual(controller['role_arn'],f'arn:aws:iam::{account_id}:role/p4-controller-{account}')
            self.assertEqual(destination.stat().st_mode&0o777,0o600)
    def test_source_config_restored_and_expiry_verified_in_memory(self):
        result=subprocess.CompletedProcess([],0,json.dumps({'Version':1,'AccessKeyId':'fake-id','SecretAccessKey':'fake-secret','SessionToken':'fake-token','Expiration':'2099-01-01T00:00:00Z'}),'')
        with patch('credential_bridge.subprocess.run',return_value=result) as process:
            value=export_credentials('aws','encore2','/original/config','/original/credentials',original={'AWS_CONFIG_FILE':'/isolated/config','AWS_PROFILE':'other','AWS_SECRET_ACCESS_KEY':'bad'})
        self.assertEqual(value['AccessKeyId'],'fake-id')
        env=process.call_args.kwargs['env']
        self.assertEqual(env['AWS_CONFIG_FILE'],'/original/config');self.assertEqual(env['AWS_SHARED_CREDENTIALS_FILE'],'/original/credentials')
        self.assertNotIn('AWS_PROFILE',env);self.assertNotIn('AWS_SECRET_ACCESS_KEY',env)
    def test_missing_expired_malformed_and_failure_are_sanitized(self):
        cases=[subprocess.CompletedProcess([],5,'secret-value','secret-value'),
               subprocess.CompletedProcess([],0,'not-json secret-value',''),
               subprocess.CompletedProcess([],0,json.dumps({'Version':1,'AccessKeyId':'secret-value','Expiration':'2000-01-01T00:00:00Z'}),'')]
        for result in cases:
            with self.subTest(case=result.returncode):
                with patch('credential_bridge.subprocess.run',return_value=result):
                    with self.assertRaises(CredentialError) as caught:export_credentials('aws','encore1','/source/config','/source/credentials')
                self.assertNotIn('secret-value',str(caught.exception))
    def test_refuse_to_replace_original_file(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'config';source.write_text('original')
            with self.assertRaises(CredentialError):create_isolated_config(source,fixture_binding()['account_ids'],'aws',original={'AWS_CONFIG_FILE':str(source)})
            self.assertEqual(source.read_text(),'original')
