import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model import fixture_binding, validate_binding, ContractError
from track2 import DEFAULT, fixture_track2, validate_track2, extend_account, extend_edge
from build_config import account_module, edge_module
from track2_audit import decision

class Track2InputTests(unittest.TestCase):
    def setUp(self):
        self.binding=fixture_binding();self.binding['track2']=fixture_track2()

    def test_explicit_opt_in_and_optional_absence(self):
        validate_binding(self.binding)
        self.binding['track2']=copy.deepcopy(DEFAULT);validate_binding(self.binding)
        self.binding['track2']['media']['enabled']=True
        with self.assertRaises(ContractError):validate_binding(self.binding)

    def test_scope_input_mutations_refused(self):
        cases=[lambda c:c.update(dataset_version='../escape'),
               lambda c:c['endpoint_ids'].pop('encore5'),
               lambda c:c['endpoint_ids'].update(encore2=c['endpoint_ids']['encore1']),
               lambda c:c['driver_queries'].update({'spark-01':['venues-catalog']}),
               lambda c:c['driver_queries'].update({'control-a':['*']}),
               lambda c:c['driver_queries'].update({'control-a':['venues-catalog','venues-catalog']}),
               lambda c:c['driver_queries'].update({'control-a':['feedback-postgresql']}),
               lambda c:c.update(ingest_nodes=['gateway-a']),
               lambda c:c['media'].update(bucket=self.binding['state_bucket']),
               lambda c:c['archive'].update(bucket=c['media']['bucket']),
               lambda c:c['media'].update(ttl_approved=False),
               lambda c:c['media'].update(edge_ttl=-1),
               lambda c:c['media'].update(browser_ttl=121),
               lambda c:c['media'].update(edge_ttl=1.5),
               lambda c:c['media'].update(edge_ttl=True),
               lambda c:c['media'].update(bucket='proof/path'),
               lambda c:c.update(kms_key='unreviewed')]
        for mutate in cases:
            candidate=copy.deepcopy(self.binding);mutate(candidate['track2'])
            with self.subTest(candidate=candidate['track2']):
                with self.assertRaises(ContractError):validate_binding(candidate)

    def test_tls_gate_rejects_default_dns_partial_and_wrong_account(self):
        tls=self.binding['track2']['tls']
        tls.update(enabled=True,origin_name='origin.example.org',certificate_arn='arn:aws:acm:ap-northeast-2:111111111111:certificate/12345678-1234-1234-1234-123456789abc',control_tls_ready=True)
        validate_binding(self.binding)
        for key,value in [('origin_name','mock.elb.amazonaws.com'),('origin_name','x.cloudfront.net'),
                          ('origin_name','https://origin.example.org'),('certificate_arn','self-signed'),
                          ('certificate_arn',tls['certificate_arn'].replace('111111111111','999999999999')),
                          ('control_tls_ready',False),('control_port',8000)]:
            candidate=copy.deepcopy(self.binding);candidate['track2']['tls'][key]=value
            with self.subTest(key=key,value=value):
                with self.assertRaises(ContractError):validate_binding(candidate)

    def test_generated_security_boundaries(self):
        module=extend_account(account_module())
        endpoint=module['resource']['aws_vpc_endpoint']['s3']
        self.assertNotIn('Resource=local.s3_resources',endpoint['policy'])
        self.assertIn('local.s3_grants[name]',endpoint['policy'])
        self.assertIn('self.id',endpoint['lifecycle']['postcondition'][0]['condition'])
        self.assertNotIn('aws/${var.binding.environment}/${var.binding.generation}/*/checkpoint/*',json.dumps(module))
        edge=extend_edge(edge_module())
        self.assertNotIn('StringNotEquals',edge['module']['media']['statements'])
        self.assertIn('AWS:SourceArn',edge['module']['media']['statements'])
        self.assertIn('media/venues/approved/*',edge['module']['media']['statements'])

class LocalPolicyModelTests(unittest.TestCase):
    def test_explicit_deny_overrides_allow_and_missing_endpoint(self):
        statements=[{'Effect':'Allow','Action':['s3:GetObject'],'Resource':['arn:aws:s3:::test/raw/*']},
                    {'Effect':'Deny','Principal':'*','Action':'s3:*','Resource':['arn:aws:s3:::test/raw/*'],
                     'Condition':{'StringNotEquals':{'aws:SourceVpce':['vpce-approved']}}}]
        for endpoint,expected in [(None,'explicit_deny'),('vpce-other','explicit_deny'),('vpce-approved','allow')]:
            self.assertEqual(decision(statements,'s3:GetObject','arn:aws:s3:::test/raw/file','role',**{'aws:SourceVpce':endpoint}),expected)

    def test_unknown_condition_cannot_silently_pass(self):
        statement={'Effect':'Allow','Action':['s3:GetObject'],'Resource':['*'],'Condition':{'Unknown':{'key':'value'}}}
        with self.assertRaises(ContractError):decision([statement],'s3:GetObject','file','role')

if __name__=='__main__':unittest.main()
