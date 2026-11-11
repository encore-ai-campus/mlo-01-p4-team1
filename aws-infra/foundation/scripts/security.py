"""Independent audit of evaluated Terraform mock plans, including mutant rejection."""
from model import *
from integration import resources

def scalar_list(value):return value if isinstance(value,list) else [value]
def statements(value):return json.loads(value)['Statement']

def audit_s3_prefix_actions(statement):
    """AWS accepts s3:prefix only on these listing actions, not multipart lists."""
    has_prefix=any(key.casefold()=='s3:prefix'
                   for entries in statement.get('Condition',{}).values()
                   for key in entries)
    if has_prefix:
        actions={action.casefold() for action in scalar_list(statement.get('Action',[]))}
        require(bool(actions) and actions<={'s3:listbucket','s3:listbucketversions'},
                's3:prefix combined with an unsupported action')
def audit(states):
    all_resources=[r for s in states.values() for r in resources(s)]
    select=lambda t:[r['values'] for r in all_resources if r['type']==t]
    require(len(select('aws_instance'))==16,'sixteen EC2 required')
    require(not select('aws_nat_gateway'),'NAT forbidden')
    for v in select('aws_instance'):
        require(v['credit_specification'][0]['cpu_credits']=='standard','T3 Standard required')
        require(v['metadata_options'][0]['http_tokens']=='required','IMDSv2 required')
        require(v['root_block_device'][0]['encrypted'] is True,'root encryption required')
        name=v['tags']['Name'];require(v['instance_type']==NODES[name]['instance_type'],'instance spec mismatch')
        if NODES[name]['subnet_kind']=='public':require(v['associate_public_ip_address'] is True,'Gateway auto public IPv4 required')
    require(len(select('aws_ebs_volume'))==16,'separate persistent data volumes required')
    for i,a in enumerate(ACCOUNTS):
        rs=resources(states[a])
        # Resource indexes are available in addresses in Terraform's plan JSON.
        subnets={r['address'].split('["')[1].split('"]')[0]:r['values'] for r in rs if r['type']=='aws_subnet'}
        for r in rs:
            if r['type']!='aws_ebs_volume':continue
            name=r['address'].split('["')[1].split('"]')[0];n=NODES[name]
            subnet=subnets[f'{n["subnet_kind"]}-{n["az_slot"]}']
            require(subnet['availability_zone_id']==fixture_binding()['az_ids'][n['az_slot']],'subnet physical AZ mismatch')
            require(r['values']['availability_zone']==subnet['availability_zone'],'data volume crosses AZ')
    require(all(v['encrypted'] for v in select('aws_ebs_volume')),'data encryption required')
    require(all(v['force_detach'] is False for v in select('aws_volume_attachment')),'force detach forbidden')
    for v in select('aws_default_security_group'):require(v['ingress']==[] and v['egress']==[],'default SG must be closed')
    # Every required route in both directions for every route table.
    routes=[r for r in resources(states['connectivity']) if r['type']=='aws_route']
    require(len(routes)==120,'120 peering routes required')
    for a,b in TOPOLOGY['peering_pairs']:
        for direction in ['requester','acceptor']:
            matching=[r for r in routes if r['address'].startswith(f'module.{a}_{b}.aws_route.{direction}[')]
            require(len(matching)==6,'missing/one-way route')
            peer=b if direction=='requester' else a
            require(all(r['values']['destination_cidr_block']==fixture_binding()['cidrs'][peer] for r in matching),'wrong route CIDR')
    require(len(select('aws_vpc_peering_connection'))==10 and len(select('aws_vpc_peering_connection_accepter'))==10,'peering ownership missing')
    require(len(select('aws_route53_vpc_association_authorization'))==8 and len(select('aws_route53_zone_association'))==8,'cross-account DNS association missing')
    for rule in select('aws_security_group_rule'):
        require(not (rule['type']=='ingress' and '0.0.0.0/0' in (rule.get('cidr_blocks') or [])),'public ingress forbidden')
        require(rule['protocol']=='tcp','unexpected protocol')
        require(rule['from_port']>0 and rule['to_port']<65536,'unbounded SG ports')
    buckets=select('aws_s3_bucket')
    require(len(buckets)==5 and len({v['bucket'] for v in buckets})==5,'state/data buckets must be isolated')
    require(all(v['force_destroy'] is False for v in buckets),'force destroy forbidden')
    require(len(select('aws_s3_bucket_versioning'))==5,'versioning missing')
    require(all(v['versioning_configuration'][0]['status']=='Enabled' for v in select('aws_s3_bucket_versioning')),'versioning required')
    for v in select('aws_s3_bucket_public_access_block'):
        require(all(v[k] is True for k in ['block_public_acls','block_public_policy','ignore_public_acls','restrict_public_buckets']),'public bucket access forbidden')
    for v in select('aws_s3_bucket_policy'):
        ss=statements(v['policy'])
        require(any(s['Effect']=='Deny' and s.get('Condition',{}).get('Bool',{}).get('aws:SecureTransport')=='false' for s in ss),'TLS deny required')
        for s in ss:
            audit_s3_prefix_actions(s)
            if s['Effect']=='Allow':
                require(s['Principal']!='*' and '*' not in scalar_list(s['Principal']['AWS']),'public bucket grant forbidden')
                require('aws:PrincipalArn' in s.get('Condition',{}).get('ArnEquals',{}),'exact bucket role condition required')
                require(all('*' not in a and '?' not in a for a in scalar_list(s['Action'])),'broad bucket action')
    for v in select('aws_iam_role'):
        trust=statements(v['assume_role_policy'])
        for s in trust:
            require(s['Principal']!='*','wildcard trust forbidden')
            for principal in s['Principal'].values():require('*' not in scalar_list(principal),'wildcard principal forbidden')
    for v in select('aws_iam_role_policy'):
        for s in statements(v['policy']):
            audit_s3_prefix_actions(s)
            actions=[a.casefold() for a in scalar_list(s['Action'])];res=scalar_list(s['Resource'])
            require(all('*' not in a and '?' not in a for a in actions),'wildcard IAM action')
            if 'iam:passrole' in actions:
                runtime_arns={f'arn:aws:iam::{fixture_binding()["account_ids"][node["account"]]}:role/p4-runtime-{name}' for name,node in NODES.items()}
                require(all(arn in runtime_arns for arn in res) and s.get('Condition',{}).get('StringEquals',{}).get('iam:PassedToService')=='ec2.amazonaws.com','unbounded PassRole')
            if any(a.startswith('s3:') for a in actions):require('*' not in res,'unbounded S3 grant')
            if 's3:listbucket' in actions:require('s3:prefix' in s.get('Condition',{}).get('StringLike',s.get('Condition',{}).get('StringEquals',{})),'S3 prefix condition required')
            if any(a in actions for a in ['s3:getobject','s3:putobject','s3:getobjectversion']):
                for resource in res:
                    require(not resource.endswith('/*') or resource.count('/')>=2 or resource.endswith('.tflock'),'S3 object prefix widened to bucket root')
    for account in ACCOUNTS:
        for resource in resources(states[account]):
            if resource['type']!='aws_vpc_endpoint':continue
            v=resource['values']
            for s in statements(v['policy']):
                allowed=s.get('Condition',{}).get('ArnEquals',{}).get('aws:PrincipalArn')
                expected={f'arn:aws:iam::{fixture_binding()["account_ids"][n["account"]]}:role/p4-runtime-{name}' for name,n in NODES.items() if v['vpc_endpoint_type']=='Interface' or n['account']==account}
                actual=set(scalar_list(allowed))
                require(actual==expected if v['vpc_endpoint_type']=='Interface' else bool(actual) and actual<=expected,'endpoint role restriction missing/widened')
                require(all('*' not in a and '?' not in a for a in scalar_list(s['Action'])),'endpoint action wildcard')
                if v['vpc_endpoint_type']=='Gateway':require('*' not in scalar_list(s['Resource']),'unbounded endpoint resource')
    for v in select('aws_cloudfront_cache_policy'):
        require(all(v[k]==0 for k in ['min_ttl','default_ttl','max_ttl']),'API caching forbidden')
    for v in select('aws_cloudfront_origin_request_policy'):
        require(v['cookies_config'][0]['cookie_behavior']=='all' and v['headers_config'][0]=={'header_behavior':'allViewerAndWhitelistCloudFront','headers':[{'items':['CloudFront-Forwarded-Proto']}]} and v['query_strings_config'][0]['query_string_behavior']=='all','API forwarding incomplete')
    for v in select('aws_cloudfront_distribution'):
        require(v['default_cache_behavior'][0]['viewer_protocol_policy']=='redirect-to-https','viewer HTTPS required')
        require(v['origin'][0]['custom_origin_config'][0]['origin_protocol_policy']=='http-only','origin scope changed')
    for v in select('aws_lb_listener'):
        require(v['protocol']=='HTTP' and v['default_action'][0]['fixed_response'][0]['status_code']=='403','origin bypass allowed')
    require(all(v['condition'][0]['http_header'][0]['http_header_name']=='X-P4-Origin' for v in select('aws_lb_listener_rule')),'origin header missing')
    # Deployment input is unbound: no SSM payload or OIDC role may be guessed.
    require(not select('aws_ssm_document') and not select('aws_iam_openid_connect_provider'),'unbound execution resource created')
    return True

def mutation_checks(states):
    import copy
    cases=[('aws_instance',lambda v:v['credit_specification'][0].update(cpu_credits='unlimited')),
           ('aws_ebs_volume',lambda v:v.update(encrypted=False)),
           ('aws_s3_bucket',lambda v:v.update(force_destroy=True)),
           ('aws_s3_bucket_versioning',lambda v:v['versioning_configuration'][0].update(status='Suspended')),
           ('aws_cloudfront_cache_policy',lambda v:v.update(min_ttl=1)),
           ('aws_cloudfront_origin_request_policy',lambda v:v['cookies_config'][0].update(cookie_behavior='none')),
           ('aws_lb_listener',lambda v:v['default_action'][0]['fixed_response'][0].update(status_code='200')),
           ('aws_iam_role',lambda v:v.update(assume_role_policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'sts:AssumeRole','Principal':'*'}]}))),
           ('aws_iam_role_policy',lambda v:v.update(policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'iam:PassRole','Resource':'*'}]}))),
           ('aws_iam_role_policy',lambda v:v.update(policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'IAM:passrole','Resource':'*'}]}))),
           ('aws_iam_role_policy',lambda v:v.update(policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'iam:PassRol?','Resource':'arn:aws:iam::111111111111:role/p4-runtime-control-a'}]}))),
           ('aws_iam_role_policy',lambda v:v.update(policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'iam:PassRole','Resource':'arn:aws:iam::111111111111:role/p4-runtime-nonexistent','Condition':{'StringEquals':{'iam:PassedToService':'ec2.amazonaws.com'}}}]}))),
           ('aws_iam_role_policy',lambda v:v.update(policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:GetObject','Resource':'arn:aws:s3:::example-only-foundation-raw/*'}]}))),
           ('aws_iam_role_policy',lambda v:v.update(policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:ListBucket','Resource':'arn:aws:s3:::example-only-foundation-raw'}]}))),
           ('aws_vpc_endpoint',lambda v:v.update(policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':'*','Action':'s3:*','Resource':'*'}]}))),
           ('aws_s3_bucket_policy',lambda v:v.update(policy=json.dumps({'Version':'2012-10-17','Statement':[]}))),
           ('aws_route',lambda v:v.update(destination_cidr_block='0.0.0.0/0'))]
    for kind,mutate in cases:
        changed=copy.deepcopy(states)
        target=next(r for s in changed.values() for r in resources(s) if r['type']==kind and (kind!='aws_route' or r['address'].startswith('module.encore')))
        mutate(target['values'])
        try:audit(changed)
        except ContractError:continue
        raise ContractError('policy/topology mutation was accepted: '+kind)
    missing=copy.deepcopy(states)
    rs=missing['connectivity']['values']['root_module']['resources']
    rs.remove(next(r for r in rs if r['type']=='aws_route'))
    try:audit(missing)
    except ContractError:pass
    else:raise ContractError('one-way route mutation accepted')
    print(f'PASS: {len(cases)+1} evaluated resource/policy mutation rejection checks')

def audit_bound(state,binding):
    rs=resources(state)
    docs=[r['values'] for r in rs if r['type']=='aws_ssm_document']
    require(len(docs)==1,'bound SSM document missing')
    content=json.loads(docs[0]['content'])
    require(set(content['parameters'])=={'Operation'},'arbitrary SSM command parameter')
    require(content['parameters']['Operation']['allowedValues']==['install','deploy','health','rollback'],'SSM operation restriction')
    script='\n'.join(content['mainSteps'][0]['inputs']['runCommand'])
    require(binding['artifact']['version'] in script and binding['artifact']['sha256'] in script and binding['artifact']['manifest_sha256'] in script,'version/archive/manifest binding missing')
    import base64,re
    sources=[base64.b64decode(value).decode() for value in re.findall(r"base64.b64decode\('([A-Za-z0-9+/=]+)'\)",script)]
    require(len(sources)==2 and 'tarfile' in sources[0] and 'inspect_artifact' in sources[1] and 'imds_instance_id' in sources[1] and 'bin/runtime-entrypoint.py' in sources[1],'trusted artifact/target boundary missing')
    require('assert ' not in '\n'.join(sources),'optimized Python must retain security decisions')
    oidc=[r['values'] for r in rs if r['type']=='aws_iam_openid_connect_provider']
    require(len(oidc)==1,'OIDC binding missing')
    require(oidc[0]['url']=='https://token.actions.githubusercontent.com' and oidc[0]['client_id_list']==['sts.amazonaws.com'],'OIDC provider/audience widening')
    ci=[r['values'] for r in rs if r['type']=='aws_iam_role' and '.ci[' in r['address']]
    expected_roles={'p4-ci-'+purpose:subject for purpose,subject in binding['oidc_subjects'].items()}
    require(len(ci)==len(expected_roles) and {role['name'] for role in ci}==set(expected_roles),'separate plan/apply/runtime roles missing')
    for role in ci:
        trust=statements(role['assume_role_policy'])
        require(len(trust)==1,'extra OIDC trust statement')
        trust=trust[0]
        require(trust.get('Effect')=='Allow' and trust.get('Action')=='sts:AssumeRoleWithWebIdentity','OIDC action/effect mismatch')
        require(trust.get('Principal')=={'Federated':oidc[0]['arn']},'OIDC provider principal mismatch')
        require(trust.get('Condition')=={'StringEquals':{'token.actions.githubusercontent.com:aud':'sts.amazonaws.com','token.actions.githubusercontent.com:sub':expected_roles[role['name']]}},'OIDC role subject/audience widening')
    for r in rs:
        if r['type']=='aws_iam_role_policy' and '.artifact[' in r['address']:
            s=statements(r['values']['policy'])[0]
            require(s['Action']==['s3:GetObjectVersion'] and s['Condition']['StringEquals']['s3:VersionId']==binding['artifact']['version'],'artifact version policy missing')
        if r['type']=='aws_s3_bucket_policy' and '.transfer[' in r['address']:
            s=next(s for s in statements(r['values']['policy']) if s['Effect']=='Allow' and s['Action']==['s3:GetObjectVersion'])
            require(s['Condition']['StringEquals']['s3:VersionId']==binding['artifact']['version'],'cross-account artifact version policy missing')
            require(len(s['Condition']['ArnEquals']['aws:PrincipalArn'])==16,'artifact roles missing')
    print('PASS: enabled artifact SSM Document/version grants and exact three-role OIDC binding')

def bound_mutation_checks(state,binding):
    """Reject widened OIDC trust using resources from the enabled mock plan."""
    import copy
    def alter_trust(values,change):
        policy=json.loads(values['assume_role_policy']);change(policy['Statement']);values['assume_role_policy']=json.dumps(policy)
    cases=[('aws_iam_openid_connect_provider',lambda v:v.update(url='https://unapproved.invalid')),
           ('aws_iam_openid_connect_provider',lambda v:v['client_id_list'].append('unapproved')),
           ('aws_iam_role',lambda v:alter_trust(v,lambda ss:ss[0].update(Principal={'Federated':'arn:aws:iam::999999999999:oidc-provider/unapproved.invalid'}))),
           ('aws_iam_role',lambda v:alter_trust(v,lambda ss:ss.append(copy.deepcopy(ss[0])))),
           ('aws_iam_role',lambda v:alter_trust(v,lambda ss:ss[0].update(Action='sts:AssumeRole'))),
           ('aws_iam_role',lambda v:alter_trust(v,lambda ss:ss[0]['Condition']['StringEquals'].update({'token.actions.githubusercontent.com:aud':'unapproved'}))),
           ('aws_iam_role',lambda v:alter_trust(v,lambda ss:ss[0]['Condition']['StringEquals'].update({'token.actions.githubusercontent.com:sub':'repo:*/*:*'}))),
           ('aws_iam_role',lambda v:alter_trust(v,lambda ss:ss[0].update(Condition={'StringLike':ss[0]['Condition']['StringEquals']})))]
    for kind,mutate in cases:
        changed=copy.deepcopy(state)
        target=next(r for r in resources(changed) if r['type']==kind and (kind!='aws_iam_role' or '.ci[' in r['address']))
        mutate(target['values'])
        try:audit_bound(changed,binding)
        except ContractError:continue
        raise ContractError('bound OIDC mutation accepted: '+kind)
    changed=copy.deepcopy(state)
    roles=[r['values'] for r in resources(changed) if r['type']=='aws_iam_role' and '.ci[' in r['address']]
    policies=[json.loads(role['assume_role_policy']) for role in roles[:2]]
    key='token.actions.githubusercontent.com:sub'
    conditions=[p['Statement'][0]['Condition']['StringEquals'] for p in policies]
    conditions[0][key],conditions[1][key]=conditions[1][key],conditions[0][key]
    for role,policy in zip(roles,policies):role['assume_role_policy']=json.dumps(policy)
    try:audit_bound(changed,binding)
    except ContractError:pass
    else:raise ContractError('swapped OIDC plan/apply role subjects accepted')
    print('PASS: nine evaluated OIDC provider/principal/action/audience/subject/statement mutation rejection checks')

def audit_transfer(states,binding):
    """Evaluate the used IAM subset independently across identity + bucket + endpoint."""
    import fnmatch
    def allowed(statements_, action, resource, principal, prefix=None, version=None):
        permit=False
        for statement in statements_:
            if not any(fnmatch.fnmatchcase(action,v) for v in scalar_list(statement['Action'])):continue
            if not any(fnmatch.fnmatchcase(resource,v) for v in scalar_list(statement['Resource'])):continue
            matches=True
            for kind,keys in statement.get('Condition',{}).items():
                for key,expected in keys.items():
                    actual={'aws:PrincipalArn':principal,'s3:prefix':prefix,'aws:SecureTransport':'true','s3:VersionId':version}.get(key)
                    predicate=fnmatch.fnmatchcase if kind in ['StringLike','ArnLike'] else lambda a,b:a==b
                    matches=matches and actual is not None and any(predicate(actual,v) for v in scalar_list(expected))
            if not matches:continue
            if statement['Effect']=='Deny':return False
            permit=True
        return permit
    central=resources(states['encore1'])
    bucket=statements(next(r['values']['policy'] for r in central if r['type']=='aws_s3_bucket_policy' and '.transfer[' in r['address']))
    bucket_arn='arn:aws:s3:::'+binding['bucket_names']['transfer']
    for account in ACCOUNTS:
        rs=resources(states[account]);principal=f'arn:aws:iam::{binding["account_ids"][account]}:role/p4-controller-{account}'
        identity=statements(next(r['values']['policy'] for r in rs if r['type']=='aws_iam_role_policy' and '.controller[' in r['address']))
        endpoint=statements(next(r['values']['policy'] for r in rs if r['type']=='aws_vpc_endpoint' and r['values']['vpc_endpoint_type']=='Gateway'))
        def layers(action,resource,p=principal,prefix=None,version=None):
            # An identity policy is attached only to this exact controller principal.
            return [p==principal and allowed(identity,action,resource,p,prefix,version),allowed(bucket,action,resource,p,prefix,version),allowed(endpoint,action,resource,p,prefix,version)]
        if binding['artifact']:
            artifact_arn=bucket_arn+'/'+binding['artifact']['key']
            require(all(layers('s3:GetObjectVersion',artifact_arn,version=binding['artifact']['version'])),'controller immutable artifact read missing')
            require(not any(layers('s3:GetObjectVersion',artifact_arn,version='wrong-version')),'controller artifact version widened')
            require(not any(layers('s3:GetObject',artifact_arn)),'controller mutable artifact read allowed')
            require(not any(layers('s3:PutObject',artifact_arn)),'controller artifact overwrite allowed')
        for instance_id in binding['controller_instance_ids'][account]:
            for action in ['s3:GetObject','s3:PutObject','s3:DeleteObject']:
                require(all(layers(action,bucket_arn+'/'+instance_id+'/ansible-module.py')),'controller transfer grant mismatch')
            require(all(layers('s3:ListBucket',bucket_arn,prefix=instance_id+'/')),'controller list prefix mismatch')
        require(all(layers('s3:GetBucketLocation',bucket_arn)),'controller bucket location missing')
        require(not any(layers('s3:ListBucket',bucket_arn,prefix='')),'controller may list whole bucket')
        wrong=next(i for a,ids in binding['controller_instance_ids'].items() if a!=account for i in ids)
        for action in ['s3:GetObject','s3:PutObject','s3:DeleteObject']:
            require(not any(layers(action,bucket_arn+'/'+wrong+'/file.py')),'cross-account prefix widened')
            require(not any(layers(action,bucket_arn+'/secrets/other-node/password')),'secret prefix widened')
            require(not any(layers(action,bucket_arn+'/'+binding['controller_instance_ids'][account][0]+'/file.py',p=principal+'-unapproved')),'unapproved signer allowed')
        for node,n in NODES.items():
            if n['account']!=account:continue
            runtime=f'arn:aws:iam::{binding["account_ids"][account]}:role/p4-runtime-{node}'
            policy=statements(next(r['values']['policy'] for r in rs if r['type']=='aws_iam_role_policy' and r['address'].endswith('runtime["'+node+'"]')))
            for action in ['s3:PutObject','s3:DeleteObject']:
                resource=bucket_arn+'/'+binding['controller_instance_ids'][account][0]+'/file.py'
                require(not allowed(policy,action,resource,runtime) and not allowed(bucket,action,resource,runtime) and not allowed(endpoint,action,resource,runtime),'runtime temporary transfer writes allowed')
    print('PASS: five controller identity/bucket/endpoint policy intersections and unauthorized/account/secret/runtime negative checks')


def audit_operation(states, binding):
    """Independent evaluation of identity, bucket and endpoint operation grants."""
    import fnmatch, hashlib
    scope={'project':'mlo-p4','environment':binding['environment'],'account_ids':binding['account_ids'],
           'state_keys':{k:f'dev/{k}/terraform.tfstate' for k in ['bootstrap','connectivity',*ACCOUNTS]}}
    key='operations/'+hashlib.sha256(json.dumps(scope,sort_keys=True,separators=(',',':')).encode()).hexdigest()+'.json'
    arn='arn:aws:s3:::'+binding['state_bucket']+'/'+key
    boot=resources(states['bootstrap'])
    bucket=statements(next(r['values']['policy'] for r in boot if r['type']=='aws_s3_bucket_policy'))
    controller=f'arn:aws:iam::{binding["account_ids"]["encore1"]}:role/p4-operation-controller'
    principals={f'arn:aws:iam::{binding["account_ids"][n["account"]]}:role/p4-runtime-{name}' for name,n in NODES.items()} | {controller}
    grants=[s for s in bucket if s['Effect']=='Allow']
    require(len(grants)==1 and set(grants[0]['Condition']['ArnEquals']['aws:PrincipalArn'])==principals,'operation bucket principal set mismatch')

    def allowed(spec, action, resource, principal, conditional=True):
        permit=False
        for s in spec:
            if action not in scalar_list(s['Action']) and 's3:*' not in scalar_list(s['Action']):continue
            if not any(fnmatch.fnmatchcase(resource,r) for r in scalar_list(s['Resource'])):continue
            match=True
            for operator,values in s.get('Condition',{}).items():
                for name,expected in values.items():
                    actual={'aws:PrincipalArn':principal,'aws:SecureTransport':'true','s3:if-match':'"accepted-etag"' if conditional else None,'s3:if-none-match':None}.get(name)
                    if operator=='Null':match=match and (actual is None)==(str(expected).lower()=='true')
                    else:match=match and actual in scalar_list(expected)
            if match:
                if s['Effect']=='Deny':return False
                permit=True
        return permit

    for account in ACCOUNTS:
        rs=resources(states[account])
        endpoint=statements(next(r['values']['policy'] for r in rs if r['type']=='aws_vpc_endpoint' and r['values']['vpc_endpoint_type']=='Gateway'))
        for name,n in NODES.items():
            if n['account']!=account:continue
            p=f'arn:aws:iam::{binding["account_ids"][account]}:role/p4-runtime-{name}'
            identity=statements(next(r['values']['policy'] for r in rs if r['type']=='aws_iam_role_policy' and r['address'].endswith('operation["'+name+'"]')))
            for action in ['s3:GetObject','s3:PutObject']:
                require(all(allowed(layer,action,arn,p) for layer in [identity,bucket,endpoint]),'operation identity/bucket/endpoint mismatch')
            require(not allowed(bucket,'s3:PutObject',arn,p,False),'unconditional operation overwrite permitted')
            for wrong in [arn+'-other', 'arn:aws:s3:::'+binding['state_bucket']+'/dev/encore1/terraform.tfstate']:
                require(not any(allowed(layer,'s3:GetObject',wrong,p) for layer in [identity,bucket,endpoint]),'operation policy accesses other/state key')
            for action in ['s3:DeleteObject','s3:DeleteObjectVersion','s3:GetObjectVersion','s3:PutObjectAcl','s3:ListBucket']:
                require(not any(allowed(layer,action,arn,p) for layer in [identity,bucket,endpoint]),'unapproved operation action permitted')
    identity=statements(next(r['values']['policy'] for r in boot if r['type']=='aws_iam_role_policy' and r['address']=='aws_iam_role_policy.operation'))
    require(all(allowed(layer,'s3:PutObject',arn,controller) for layer in [identity,bucket]),'dedicated operation controller permission missing')
    print('PASS: 16 operation identities, five S3 endpoints, exact versioned bucket key and conditional/no-delete policy')
