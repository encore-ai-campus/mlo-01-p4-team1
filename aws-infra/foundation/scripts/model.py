"""Foundation configuration and shared validator adapter; no AWS client imports."""
from pathlib import Path
import sys, json, ipaddress
ROOT = Path(__file__).resolve().parents[3]
FOUNDATION = ROOT / 'aws-infra/foundation'
sys.path.insert(0, str(ROOT / 'scripts'))
from check_preparation import (read_json, require, validate_inventory, validate_runtime_plan,
                               validate_services, ContractError)
TOPOLOGY = read_json(ROOT / 'aws-infra/contracts/topology.v1.json')
SERVICES = read_json(ROOT / 'aws-infra/contracts/services.v1.json')
ACCOUNTS = TOPOLOGY['accounts']
NODES = {n['name']: n for n in TOPOLOGY['nodes']}

def fixture_binding():
    # Deliberately derived from topology, never copies inventory.example.json.
    return {'environment':'dev', 'generation':'foundation-mock-v1', 'example_only':True,
            'region':'ap-northeast-2', 'account_ids':{a:str(111111111110+i) for i,a in enumerate(ACCOUNTS,1)},
            'cidrs':{a:f'10.{210+i}.0.0/16' for i,a in enumerate(ACCOUNTS)},
            'az_ids':{'a':'apne2-az1','b':'apne2-az2','c':'apne2-az3'},
            'ami_ids':{a:'ami-00000000000000001' for a in ACCOUNTS},
            'bucket_names':{k:f'example-only-foundation-{k}' for k in ['raw','lake','backup','transfer']},
            'state_bucket':'example-only-foundation-state',
            'origin_header':'example-only-never-use-in-aws-0123456789',
            'provisioner_arns':{a:f'arn:aws:iam::{111111111110+i}:role/UNBOUND-operator' for i,a in enumerate(ACCOUNTS,1)},
            'oidc_subjects':{}, 'ci_policies':{}, 'artifact':{}, 'controller_instance_ids':{}, 'existing_encore3_reviewed':False}

def validate_binding(b, *, real=False):
    require(b['region']=='ap-northeast-2','region mismatch')
    require(b['environment']=='dev','environment mismatch')
    import re
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',b['generation']),'invalid generation')
    for key in ['account_ids','cidrs','ami_ids','provisioner_arns']:
        require(set(b[key])==set(ACCOUNTS),f'{key}: five exact account aliases required')
    require(len(set(b['account_ids'].values()))==5,'account IDs must be distinct')
    require(all(re.fullmatch(r'\d{12}',v) for v in b['account_ids'].values()),'invalid account IDs')
    require(set(b['az_ids'])=={'a','b','c'} and len(set(b['az_ids'].values()))==3,'three physical AZs required')
    require(all(re.fullmatch(r'apne2-az\d+',v) for v in b['az_ids'].values()),'invalid AZ ID')
    nets=[ipaddress.ip_network(b['cidrs'][a],strict=True) for a in ACCOUNTS]
    blocks=[ipaddress.ip_network(s) for s in ['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16']]
    require(all(n.version==4 and n.prefixlen==16 and any(n.subnet_of(p) for p in blocks) for n in nets),'RFC1918 /16 required')
    require(not any(n.overlaps(m) for i,n in enumerate(nets) for m in nets[i+1:]),'overlapping CIDRs')
    require(all(re.fullmatch(r'ami-[0-9a-f]{17}',v) for v in b['ami_ids'].values()),'invalid AMI')
    require(set(b['bucket_names'])=={'raw','lake','backup','transfer'},'storage purpose mismatch')
    buckets=[*b['bucket_names'].values(),b['state_bucket']]
    require(len(set(buckets))==5,'state and data buckets must be distinct')
    require(all(re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',v) for v in buckets),'invalid bucket names')
    for a,arn in b['provisioner_arns'].items():
        require(re.fullmatch(r'arn:aws:iam::'+b['account_ids'][a]+r':role/[A-Za-z0-9_+=,.@/-]+',arn) is not None,'invalid provisioner binding')
    for purpose,subject in b['oidc_subjects'].items():
        require(purpose in {'plan','apply','runtime'} and subject.endswith(':environment:infra-'+purpose) and re.fullmatch(r'repo:[\w.-]+(?:@[0-9]+)?/[\w.-]+(?:@[0-9]+)?:environment:[\w.-]+',subject),'exact GitHub environment subject required')
        require(subject.count('@') in {0,2},'both immutable repository identifiers required')
        require(purpose in b['ci_policies'],'CI permission policy required')
        policy=b['ci_policies'][purpose]
        require(policy.get('Version')=='2012-10-17' and bool(policy.get('Statement')),'empty CI policy')
        for s in policy['Statement']:
            actions=s.get('Action',[]); actions=[actions] if isinstance(actions,str) else actions
            resources=s.get('Resource',[]); resources=[resources] if isinstance(resources,str) else resources
            require(s.get('Effect')=='Allow' and actions and resources and 'NotAction' not in s and 'NotResource' not in s,'CI policy must be explicit')
            require(all('*' not in v and '?' not in v for v in actions),'wildcard CI action')
            if '*' in resources:
                # EC2 Describe APIs and these non-resource-scoped read APIs
                # cannot be granted with invented resource ARNs. Writes and
                # secret-reading services never inherit this exception.
                readonly=all((a.startswith('ec2:Describe') and len(a)>len('ec2:Describe')) or a in {'sts:GetCallerIdentity','ssm:GetCommandInvocation'} for a in actions)
                require(readonly,'unbounded CI resource')
                if any(a.startswith('ec2:') for a in actions):
                    require(s.get('Condition',{}).get('StringEquals',{}).get('aws:RequestedRegion')=='ap-northeast-2','EC2 read region condition required')
            if 'iam:passrole' in {action.casefold() for action in actions}:
                require(s.get('Condition',{}).get('StringEquals',{}).get('iam:PassedToService')=='ec2.amazonaws.com','PassRole service condition required')
                runtime_arns={f'arn:aws:iam::{b["account_ids"][node["account"]]}:role/p4-runtime-{name}' for name,node in NODES.items()}
                require(all(v in runtime_arns for v in resources),'PassRole exact topology runtime roles only')
    if b['artifact']:
        artifact=b['artifact']
        require(set(artifact)=={'key','version','sha256','manifest_sha256'},'artifact key/version/archive+manifest hashes required')
        require(re.fullmatch(r'artifacts/[A-Za-z0-9_./-]+\.tar\.gz',artifact['key']) and '..' not in artifact['key'],'invalid artifact path')
        require(all(re.fullmatch(r'[a-f0-9]{64}',artifact[k]) for k in ['sha256','manifest_sha256']) and re.fullmatch(r'[A-Za-z0-9_./+=-]{1,1024}',artifact['version']) and artifact['version']!='null','immutable artifact required')
    controller_ids=b.get('controller_instance_ids',{})
    if controller_ids:
        require(set(controller_ids)==set(ACCOUNTS),'controller node binding requires five exact accounts')
        seen=[]
        for account,ids in controller_ids.items():
            require(isinstance(ids,list) and len(ids)==sum(n['account']==account for n in NODES.values()),'controller instance set count mismatch')
            require(all(isinstance(i,str) and re.fullmatch(r'i-[0-9a-f]{17}',i) for i in ids),'invalid controller instance ID')
            seen.extend(ids)
        require(len(set(seen))==len(seen),'duplicate/cross-account controller instance IDs')
    if real:
        require(b['example_only'] is False,'fixture cannot target AWS')
        require(b['existing_encore3_reviewed'] is True,'encore3 ownership review required')
        require(not any('example-only' in s or 'UNBOUND' in s for s in json.dumps(b).split('"')),'unbound/example value')
        require(len(b['origin_header'])>=32,'origin header length')
    return b

def flows():
    out=[]
    for flow in SERVICES['flows']:
        spec=SERVICES['services'][flow['destination_service']]
        low,high=spec.get('port_range',[spec.get('port'),spec.get('port')])
        for target,t in NODES.items():
            if t['role'] not in spec['roles'] or ('nodes' in spec and target not in spec['nodes']): continue
            for source,s in NODES.items():
                if s['role'] in flow['source_roles']:
                    out.append({'source':source,'target':target,'low':low,'high':high,'service':flow['destination_service']})
    return {f'{f["source"]}-{f["target"]}-{f["service"]}':f for f in out}

def backend_config(account,binding):
    require(account in [*ACCOUNTS,'connectivity'],'unknown state owner')
    config={'bucket':binding['state_bucket'],'key':f'dev/{account}/terraform.tfstate',
            'region':'ap-northeast-2','encrypt':True,'use_lockfile':True,
            'profile':'encore1' if account=='connectivity' else account,
            'assume_role':{'role_arn':f'arn:aws:iam::{binding["account_ids"]["encore1"]}:role/p4-state-{account}'}}
    validate_backend(config,account,binding)
    return config

def validate_backend(config,account,binding):
    require(config['bucket']==binding['state_bucket'] and config['key']==f'dev/{account}/terraform.tfstate','wrong state ownership')
    require(config['region']=='ap-northeast-2' and config['encrypt'] is True and config['use_lockfile'] is True,'S3 encrypted lockfile backend required')
    require(config['assume_role']['role_arn']==f'arn:aws:iam::{binding["account_ids"]["encore1"]}:role/p4-state-{account}','wrong state access role')
