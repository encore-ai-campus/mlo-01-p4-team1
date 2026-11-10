import copy, json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model import *
from export_inventory import export,write_inventory
from build_config import bucket_module,account_module,edge_module,pair_module

class BindingTests(unittest.TestCase):
    def test_valid_fixture(self):validate_binding(fixture_binding())
    def test_binding_mutations(self):
        mutations=[lambda b:b.update(region='us-east-1'),lambda b:b['account_ids'].pop('encore5'),
                   lambda b:b['account_ids'].update(encore5=b['account_ids']['encore1']),
                   lambda b:b['cidrs'].update(encore2='10.210.0.0/16'),lambda b:b['cidrs'].update(encore1='8.8.0.0/16'),
                   lambda b:b['az_ids'].update(c=b['az_ids']['a']),lambda b:b['az_ids'].update(c='invalid'),
                   lambda b:b['bucket_names'].update(raw=b['state_bucket']),lambda b:b['ami_ids'].update(encore1='latest'),
                   lambda b:b.update(generation='../x'),lambda b:b['provisioner_arns'].update(encore1='*')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                b=fixture_binding();mutate(b)
                with self.assertRaises((ValueError,KeyError)):validate_binding(b)
    def test_wildcard_oidc_rejected(self):
        b=fixture_binding();b['oidc_subjects']['apply']='repo:*/*:*'
        with self.assertRaises(ContractError):validate_binding(b)
    def test_passrole_requires_exact_runtime_and_service(self):
        b=fixture_binding();b['oidc_subjects']['apply']='repo:example/repo:environment:infra-apply'
        statement={'Effect':'Allow','Action':['iam:PassRole'],'Resource':['arn:aws:iam::111111111111:role/p4-runtime-control-a'],'Condition':{'StringEquals':{'iam:PassedToService':'ec2.amazonaws.com'}}}
        b['ci_policies']['apply']={'Version':'2012-10-17','Statement':[statement]};validate_binding(b)
        for key,value in [('Resource',['*']),('Action',['iam:*']),('Action',['iam:PassRol?']),('Condition',{})]:
            bad=copy.deepcopy(b);bad['ci_policies']['apply']['Statement'][0][key]=value
            with self.assertRaises(ContractError):validate_binding(bad)
    def test_passrole_binds_each_node_to_its_topology_account(self):
        b=fixture_binding();b['oidc_subjects']['apply']='repo:example/repo:environment:infra-apply'
        statement={'Effect':'Allow','Action':['iam:PassRole'],'Resource':[f'arn:aws:iam::{b["account_ids"][n["account"]]}:role/p4-runtime-{name}' for name,n in NODES.items()],'Condition':{'StringEquals':{'iam:PassedToService':'ec2.amazonaws.com'}}}
        b['ci_policies']['apply']={'Version':'2012-10-17','Statement':[statement]};validate_binding(b)
        for resource in ['arn:aws:iam::999999999999:role/p4-runtime-control-a',
                         'arn:aws:iam::111111111112:role/p4-runtime-control-a',
                         'arn:aws:iam::111111111111:role/p4-runtime-nonexistent',
                         'arn:aws:iam::111111111111:role/p4-runtime-control-?',
                         'arn:aws:iam::111111111111:role/p4-runtime-control-a/extra']:
            with self.subTest(resource=resource):
                bad=copy.deepcopy(b);bad['ci_policies']['apply']['Statement'][0]['Resource']=[resource]
                with self.assertRaises(ContractError):validate_binding(bad)
        for action in ['iam:passrole','IAM:PASSROLE','Iam:PaSsRoLe']:
            with self.subTest(action=action):
                valid=copy.deepcopy(b);valid['ci_policies']['apply']['Statement'][0]['Action']=[action];validate_binding(valid)
                valid['ci_policies']['apply']['Statement'][0]['Resource']=['arn:aws:iam::111111111111:role/p4-runtime-nonexistent']
                with self.assertRaises(ContractError):validate_binding(valid)
    def test_artifact_requires_immutable_hash_version_and_safe_key(self):
        b=fixture_binding();b['artifact']={'key':'artifacts/runtime-v1.tar.gz','version':'version-1','sha256':'a'*64,'manifest_sha256':'b'*64};validate_binding(b)
        valid=copy.deepcopy(b);valid['artifact']['version']='immutable+version/ID=';validate_binding(valid)
        for key,value in [('key','artifacts/../../escape.tar.gz'),('key','artifacts/runtime.zip'),('version','x; command'),('version','null'),('sha256','latest'),('manifest_sha256','')]:
            bad=copy.deepcopy(b);bad['artifact'][key]=value
            with self.assertRaises(ContractError):validate_binding(bad)
    def test_controller_ids_require_exact_counts_distinct_valid_ids(self):
        b=fixture_binding();b['controller_instance_ids']={a:[f'i-{i:017x}' for i,n in enumerate(NODES.values(),1) if n['account']==a] for a in ACCOUNTS};validate_binding(b)
        for mutate in [lambda v:v['controller_instance_ids'].pop('encore5'),lambda v:v['controller_instance_ids']['encore1'].append('i-fffffffffffffffff'),lambda v:v['controller_instance_ids']['encore1'].__setitem__(0,'i-short'),lambda v:v['controller_instance_ids']['encore1'].__setitem__(0,v['controller_instance_ids']['encore2'][0])]:
            bad=copy.deepcopy(b);mutate(bad)
            with self.assertRaises(ContractError):validate_binding(bad)

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.dir=Path(self.temp.name);self.paths=[]
        b=fixture_binding();meta={k:b[k] for k in ['environment','generation','example_only','region','az_ids']};meta['schema_version']='1.0.0'
        for i,a in enumerate(ACCOUNTS,1):
            nodes=[]
            for ni,n in enumerate(NODES.values(),1):
                if n['account']!=a:continue
                nodes.append(dict(n,instance_id=f'i-{ni:017x}',private_ip=f'10.{209+i}.{1+"abc".index(n["az_slot"])}.{20+ni}',az_id=b['az_ids'][n['az_slot']],runtime_role_ref='role-ref:'+n['name']))
            part=dict(meta,component=a,account={'vpc_id':f'vpc-{i:017x}','cidr':b['cidrs'][a],'s3_endpoint_id':f'vpce-{i:017x}'},nodes=nodes)
            if a=='encore1':part['storage']=dict({k+'_bucket_ref':v for k,v in b['bucket_names'].items()},state_bucket_ref=b['state_bucket'])
            self.add(a,part)
        self.add('connectivity',dict(meta,component='connectivity',peering_pairs=TOPOLOGY['peering_pairs']))
    def add(self,name,part):
        p=self.dir/(name+'.json');p.write_text(json.dumps({'foundation':{'value':part,'type':['object',{}],'sensitive':False}}));self.paths.append(p)
    def tearDown(self):self.temp.cleanup()
    def mutate(self,fn):
        raw=read_json(self.paths[0]);fn(raw['foundation']['value']);self.paths[0].write_text(json.dumps(raw))
    def test_round_trip_and_order(self):
        a=export(self.paths);self.assertEqual(export(self.paths[::-1]),a);self.assertEqual(len(a['nodes']),16)
    def test_missing_duplicate_and_invalid_json(self):
        for paths in [self.paths[:-1],self.paths+[self.paths[0]]]:
            with self.assertRaises(ContractError):export(paths)
        self.paths[0].write_text('{"x":1,"x":2}')
        with self.assertRaises(ContractError):export(self.paths)
    def test_provenance_mutations(self):
        self.mutate(lambda p:p.update(generation='different'))
        with self.assertRaises(ContractError):export(self.paths)
    def test_fixture_mixing_rejected(self):
        self.mutate(lambda p:p.update(example_only=False))
        with self.assertRaises(ContractError):export(self.paths)
    def test_real_shaped_value_inside_fixture_rejected(self):
        self.mutate(lambda p:p['nodes'][0].update(instance_id='i-1234567890abcdef0'))
        with self.assertRaises(ContractError):export(self.paths)
    def test_topology_mutations(self):
        original=self.paths[0].read_text()
        for key,val in [('instance_type','t3.small'),('az_slot','c'),('account','encore2'),('subnet_kind','public'),('private_ip','1.1.1.1')]:
            self.paths[0].write_text(original);self.mutate(lambda p:p['nodes'][0].update({key:val}))
            with self.assertRaises(ContractError):export(self.paths)
    def test_cidr_az_node_peer_mutations(self):
        original=self.paths[0].read_text()
        for fn in [lambda p:p['account'].update(cidr='10.211.0.0/16'),lambda p:p['az_ids'].update(c='apne2-az1'),lambda p:p['nodes'].pop(),lambda p:p['nodes'].append(p['nodes'][0])]:
            self.paths[0].write_text(original);self.mutate(fn)
            with self.assertRaises(ContractError):export(self.paths)
        self.paths[0].write_text(original)
        raw=read_json(self.paths[-1]);raw['foundation']['value']['peering_pairs'].pop();self.paths[-1].write_text(json.dumps(raw))
        with self.assertRaises(ContractError):export(self.paths)
    def test_real_output_cannot_use_public_path(self):
        inv=export(self.paths);inv['example_only']=False
        with self.assertRaises(ContractError):write_inventory(inv,self.dir/'public.json')
    def test_sensitive_envelope_rejected(self):
        raw=read_json(self.paths[0]);raw['foundation']['sensitive']=True;self.paths[0].write_text(json.dumps(raw))
        with self.assertRaises(ContractError):export(self.paths)

class SourceSafetyTests(unittest.TestCase):
    def test_s3_prefix_rejects_multipart_listing_without_widening(self):
        from security import audit_s3_prefix_actions
        statement={'Effect':'Allow','Action':['s3:ListBucket'],
                   'Resource':['arn:aws:s3:::example-only-bucket'],
                   'Condition':{'StringLike':{'s3:prefix':['raw/weather/*']}}}
        audit_s3_prefix_actions(statement)
        versions=copy.deepcopy(statement);versions['Action']=['S3:ListBucketVersions']
        audit_s3_prefix_actions(versions)
        for action in ['s3:ListBucketMultipartUploads','s3:GetBucketLocation','s3:GetObject','s3:List*']:
            with self.subTest(action=action):
                bad=copy.deepcopy(statement);bad['Action'].append(action)
                with self.assertRaises(ContractError):audit_s3_prefix_actions(bad)
        # The generated grant keeps prefix isolation and scoped object-part APIs.
        grant=account_module()['locals']['s3_grants']
        self.assertIn('s3:prefix',grant)
        self.assertIn('s3:ListMultipartUploadParts',grant)
        self.assertNotIn('s3:ListBucketMultipartUploads',grant)

    def test_native_target_is_review_metadata_not_bootstrap_execution(self):
        configuration=account_module()
        host=configuration['resource']['aws_instance']['host']
        self.assertIn('native-systemd-v1',host['tags']['DataExecutionTarget'])
        self.assertNotIn('user_data',host)
        design=configuration['output']['foundation']['value']['native_host_design']
        self.assertEqual(design['profile'],'native-data-systemd-v1')
        self.assertFalse(design['ready_for_aws'])
        self.assertIn('real native installer/start/protocol-health/config-rollback acceptance',design['deployment_blockers'])
        self.assertEqual(design['os']['version'],'24.04')

    def test_destroy_protection_and_no_formatting(self):
        a=account_module()['resource'];b=bucket_module()['resource']
        for kind in ['aws_vpc','aws_network_interface','aws_ebs_volume']:
            self.assertTrue(next(iter(a[kind].values()))['lifecycle']['prevent_destroy'])
        self.assertTrue(b['aws_s3_bucket']['this']['lifecycle']['prevent_destroy'])
        self.assertFalse(b['aws_s3_bucket']['this']['force_destroy'])
        self.assertNotIn('aws_s3_bucket_lifecycle_configuration',b)
        self.assertNotIn('user_data',a['aws_instance']['host'])
    def test_s3_lock_permissions_do_not_delete_state(self):
        boot=read_json(FOUNDATION/'terraform/bootstrap/main.tf.json')
        for a,p in boot['resource']['aws_iam_role_policy'].items():
            if a=='operation':
                self.assertIn('local.operation_arn',p['policy'])
                self.assertNotIn('DeleteObject',p['policy'])
                self.assertNotIn('terraform.tfstate',p['policy'])
                continue
            text=p['policy'];self.assertIn('.tflock',text);self.assertIn(f'dev/{a}/terraform.tfstate',text)
            self.assertEqual(text.count('s3:DeleteObject'),1)
    def test_mock_alias_coverage_and_no_real_provider_blocks(self):
        root=FOUNDATION/'terraform/environments/dev/connectivity'
        text=(root/'tests/offline.tftest.hcl').read_text()
        self.assertEqual(text.count('mock_provider "aws"'),5)
        self.assertNotIn('\nprovider "aws"',text)
        for a in ACCOUNTS:self.assertIn(f'alias           = "{a}"',text)
    def test_check_local_has_no_real_dispatch(self):
        text=(FOUNDATION/'scripts/check-local.sh').read_text();self.assertNotIn('run_foundation',text);self.assertNotIn('apply',text)

class BackendAndReproTests(unittest.TestCase):
    def test_backend_mutations_fail(self):
        b=fixture_binding()
        for key,value in [('key','dev/encore2/terraform.tfstate'),('bucket',b['bucket_names']['raw']),('use_lockfile',False),('encrypt',False),('region','us-east-1')]:
            config=backend_config('encore1',b);config[key]=value
            with self.assertRaises(ContractError):validate_backend(config,'encore1',b)
        keys=[backend_config(a,b)['key'] for a in [*ACCOUNTS,'connectivity']];self.assertEqual(len(set(keys)),6)
    def test_generated_terraform_is_reproducible(self):
        import build_config as builder
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)
            with patch.object(builder,'TF',target):builder.build()
            for generated in target.rglob('*.tf.json'):
                actual=FOUNDATION/'terraform'/generated.relative_to(target)
                self.assertEqual(read_json(generated),read_json(actual),str(actual))
    def test_all_roots_mocked_and_tests_plan_only(self):
        for root in [FOUNDATION/'terraform/bootstrap',*(FOUNDATION/'terraform/environments/dev').iterdir()]:
            if not root.is_dir():continue
            text=(root/'tests/offline.tftest.hcl').read_text()
            self.assertIn('command = plan',text)
            self.assertNotIn('command = apply',text)
            self.assertNotIn('\nprovider "aws"',text)
            self.assertEqual(text.count('mock_provider "aws"'),5 if root.name=='connectivity' else 1)
