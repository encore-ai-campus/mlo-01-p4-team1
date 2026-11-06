"""Independent evaluation of the IAM subset emitted by Track2 mock plans.

This is a local policy/configuration model, not the AWS IAM simulator.
"""
import copy
import fnmatch
import json
from model import ACCOUNTS, NODES, require, ContractError
from integration import resources

def items(value):
    return value if isinstance(value,list) else [value]

def decision(statements, action, resource, principal, **context):
    context = {'aws:PrincipalArn':principal,'aws:SecureTransport':'true', **context}
    result = 'implicit_deny'
    for s in statements:
        if not any(fnmatch.fnmatchcase(action.casefold(),a.casefold()) for a in items(s['Action'])): continue
        if not any(fnmatch.fnmatchcase(resource,v) for v in items(s['Resource'])): continue
        if 'Principal' in s and s['Principal'] != '*':
            p=s['Principal']
            if 'Service' in p:
                if principal not in items(p['Service']): continue
            else:
                if not any(principal==v or (v.endswith(':root') and principal.startswith(v[:-4])) for v in items(p['AWS'])): continue
        match=True
        for operator, keys in s.get('Condition',{}).items():
            require(operator in {'Bool','StringEquals','ArnEquals','StringLike','StringNotEquals'}, 'unsupported evaluator condition')
            for key, expected in keys.items():
                actual=context.get(key)
                if operator=='StringNotEquals': ok=actual not in items(expected)
                elif operator=='StringLike': ok=actual is not None and any(fnmatch.fnmatchcase(actual,v) for v in items(expected))
                else: ok=actual in items(expected)
                match=match and ok
        if match:
            if s['Effect']=='Deny': return 'explicit_deny'
            result='allow'
    return result

def policy(resource):
    return json.loads(resource['values']['policy'])['Statement']

def audit_edge(state, config):
    rs=resources(state)
    def select(kind,name=None):
        return [r['values'] for r in rs if r['type']==kind and (name is None or r['address'].split('.')[-1]==name)]
    caches={v['name']:v for v in select('aws_cloudfront_cache_policy')}
    require(all(caches['p4-api-no-cache'][k]==0 for k in ['min_ttl','default_ttl','max_ttl']), 'API TTL must be zero')
    forwarding=select('aws_cloudfront_origin_request_policy')[0]
    require(forwarding['cookies_config'][0]['cookie_behavior']=='all' and forwarding['query_strings_config'][0]['query_string_behavior']=='all', 'cookie/query forwarding lost')
    require(forwarding['headers_config']==[{'header_behavior':'allViewerAndWhitelistCloudFront','headers':[{'items':['CloudFront-Forwarded-Proto']}]}], 'auth/CSRF/proof/protocol forwarding lost')
    distribution=select('aws_cloudfront_distribution')[0]
    api=distribution['default_cache_behavior'][0]
    require(set(api['allowed_methods'])=={'GET','HEAD','OPTIONS','PUT','POST','PATCH','DELETE'} and api['cached_methods']==['GET','HEAD'], 'API method forwarding lost')
    require(api['cache_policy_id']==caches['p4-api-no-cache']['id'] and api['origin_request_policy_id']==forwarding['id'], 'API policy detached')
    response=next(v for v in select('aws_cloudfront_response_headers_policy') if v['name']=='p4-private-no-store')
    require(response['custom_headers_config'][0]['items']==[{'header':'Cache-Control','override':True,'value':'no-store, private'}]
            and api['response_headers_policy_id']==response['id'], 'private browser/proxy cache prohibition lost')
    ordered=distribution['ordered_cache_behavior']
    explicit=next(v for v in ordered if v['path_pattern']=='/api/*')
    require(all(explicit[k]==api[k] for k in ['allowed_methods','cache_policy_id','origin_request_policy_id','response_headers_policy_id','target_origin_id']), 'explicit API behavior weakened')
    require(set(v['path_pattern'] for v in ordered)==({'/api/*','/media/venues/approved/*'} if config['media']['enabled'] else {'/api/*'}), 'private/ads cache behavior introduced')
    require(api['viewer_protocol_policy']=='redirect-to-https','viewer HTTPS required')
    origin=next(o for o in distribution['origin'] if o['origin_id']=='control')
    tls=config['tls']['enabled']
    require(origin['custom_origin_config'][0]['origin_protocol_policy']==('https-only' if tls else 'http-only'), 'origin TLS downgrade')
    require(origin['custom_header'][0]['name']=='X-P4-Origin', 'origin secret header lost')
    listener=select('aws_lb_listener')[0]
    require(listener['protocol']==('HTTPS' if tls else 'HTTP') and listener['port']==(443 if tls else 80), 'listener TLS mismatch')
    if tls:
        require(listener['certificate_arn']==config['tls']['certificate_arn'] and origin['domain_name']==config['tls']['origin_name'],'TLS certificate/hostname mismatch')
    require(listener['default_action'][0]['fixed_response'][0]['status_code']=='403','default origin bypass')
    target=select('aws_lb_target_group')[0]
    require(target['protocol']==('HTTPS' if tls else 'HTTP') and target['port']==(8443 if tls else 8000), 'Control TLS mismatch')
    require(target['health_check'][0]['protocol']==target['protocol'], 'health TLS mismatch')
    for resource in rs:
        if resource['type']=='aws_security_group_rule' and '.target_' in resource['address']:
            require(resource['values']['from_port']==target['port']==resource['values']['to_port'], 'Control TLS security group port mismatch')
    alb=select('aws_security_group')[0]
    alb=next(v for v in select('aws_security_group') if v.get('name')=='p4-control-alb' or any(x.get('description')=='CloudFront origin only' for x in v.get('ingress',[])))
    rule=alb['ingress'][0]
    require(rule['from_port']==listener['port'] and rule['to_port']==listener['port'] and rule['prefix_list_ids']==['pl-00000002'] and not rule['cidr_blocks'], 'ALB ingress widened')
    if config['media']['enabled']:
        photos=next(v for v in ordered if v['path_pattern']=='/media/venues/approved/*')
        require(photos['target_origin_id']=='approved-photos' and photos['allowed_methods']==['GET','HEAD'], 'photos origin/method widened')
        cache=caches['p4-approved-photo-cache']
        require(cache['min_ttl']==0 and cache['default_ttl']==cache['max_ttl']==config['media']['edge_ttl'], 'photo TTL input ignored')
        oac=select('aws_cloudfront_origin_access_control')[0]
        require(oac['signing_behavior']=='always' and oac['signing_protocol']=='sigv4', 'OAC unsigned request')
        photo_origin=next(o for o in distribution['origin'] if o['origin_id']=='approved-photos')
        require(photo_origin['origin_access_control_id']==oac['id'] and 's3.ap-northeast-2.amazonaws.com' in photo_origin['domain_name'], 'private S3 origin missing')
        from track2 import PHOTO_FUNCTION
        require(select('aws_cloudfront_function')[0]['code']==PHOTO_FUNCTION and photos['function_association'][0]['event_type']=='viewer-request','photo version guard lost')
    require(state['values']['outputs']['foundation_track2']['value']['ready_for_aws'] is False,'unverified readiness')

def audit_storage(states,binding):
    config=binding['track2']
    central=resources(states['encore1'])
    for state in states.values():
        rs=resources(state); sizes={}
        for resource in rs:
            value=resource['values']
            if resource['type'] in {'aws_s3_bucket_policy','aws_vpc_endpoint'}:
                require(len(value['policy'].encode())<=20480,'S3 bucket/endpoint policy exceeds limit')
            if resource['type']=='aws_iam_role_policy' and any('.'+kind+'[' in resource['address'] for kind in ['runtime','operation','artifact','track2']):
                node=resource['address'].split('["')[-1].split('"]')[0]
                sizes[node]=sizes.get(node,0)+len(value['policy'])
        require(all(v<=10240 for v in sizes.values()),'aggregate runtime inline policy exceeds limit')
    buckets={r['values']['bucket']:policy(r) for r in central if r['type']=='aws_s3_bucket_policy'}
    bucket_names={**binding['bucket_names'],'media':config['media']['bucket'],'archive':config['archive']['bucket']}
    def arn(purpose,path):return 'arn:aws:s3:::'+bucket_names[purpose]+('/'+path if path is not None else '')
    for name,node in NODES.items():
        account=node['account']; rs=resources(states[account]); principal=f'arn:aws:iam::{binding["account_ids"][account]}:role/p4-runtime-{name}'
        identities=[s for r in rs if r['type']=='aws_iam_role_policy' and r['address'].endswith('["'+name+'"]') for s in policy(r)]
        endpoint=policy(next(r for r in rs if r['type']=='aws_vpc_endpoint' and r['values']['vpc_endpoint_type']=='Gateway'))
        def check(purpose,path,action,expected,*,prefix=None,source=None):
            context={'aws:SourceVpce':source or config['endpoint_ids'][account],'s3:prefix':prefix}
            layers=[decision(layer,action,arn(purpose,path),principal,**context) for layer in [identities,buckets[bucket_names[purpose]],endpoint]]
            require(all(v=='allow' for v in layers) if expected else not all(v=='allow' for v in layers), f'{name} {purpose}/{path} {action}: {layers}')
            return layers
        path=f'raw/venues/{config["dataset_version"]}/part.csv'
        for action in ['s3:GetObject','s3:GetObjectVersion']:
            check('raw',path,action,node['role'] in {'control','spark'})
        check('raw',path,'s3:PutObject',name in config['ingest_nodes'])
        check('raw','raw/venues/other-dataset/part.csv','s3:GetObject',False)
        check('raw',None,'s3:ListBucket',node['role'] in {'control','spark'},prefix=f'raw/venues/{config["dataset_version"]}/')
        check('raw',None,'s3:ListBucket',False,prefix='')
        for layer in ('bronze', 'silver', 'gold'):
            valid = f'lake/venues/{config["dataset_version"]}/v1/{layer}/_delta_log/0001.json'
            for action in ('s3:GetObject', 's3:PutObject', 's3:DeleteObject'):
                check('lake', valid, action, node['role'] in {'control', 'spark'})
        check('lake', f'lake/venues/{config["dataset_version"]}/v1/unknown/part.parquet', 's3:PutObject', False)
        check('lake', 'lake/venues/other-dataset/v1/bronze/_delta_log/0001.json', 's3:GetObject', False)
        for query in ['venues-catalog','venues-refresh','venues-unassigned']:
            cp=f'aws/{binding["environment"]}/{binding["generation"]}/{query}/checkpoint/offsets/1'
            owns=query in config['driver_queries'].get(name,[])
            for action in ['s3:GetObject','s3:PutObject','s3:DeleteObject']:
                check('lake',cp,action,owns)
            check('lake',cp.replace(binding['generation'],'other-generation'),'s3:GetObject',False)
        if node['role'] in {'control','spark'}:
            for source in [None, 'vpce-fffffffffffffffff']:
                context={'aws:SourceVpce':source}
                require(decision(buckets[bucket_names['raw']],'s3:GetObject',arn('raw',path),principal,**context)=='explicit_deny','data endpoint deny absent')
            other=next(v for a,v in config['endpoint_ids'].items() if a!=account)
            check('raw',path,'s3:GetObject',False,source=other)
            require(all(v!='allow' for v in check('raw',path,'s3:GetObject',False,source=other)), 'another account endpoint accepted by a layer')
            check('raw',path,'s3:DeleteObjectVersion',False)
        if config['media']['enabled']:
            check('media','media/venues/approved/v/abc/photo.jpg','s3:GetObject',node['role']=='control')
            check('media','evidence/proof.bin','s3:GetObject',False)
        if config['archive']['enabled']:
            path=f'observability/{binding["environment"]}/{binding["generation"]}/run/part.jsonl'
            check('archive',path,'s3:PutObject',name=='control-b')
            for action in ['s3:GetObject','s3:DeleteObject','s3:DeleteObjectVersion']:
                check('archive',path,action,False)
        if node['role']=='gateway':
            for action in ['s3:GetObject','s3:PutObject']:
                check('raw','raw/weather/retained.json',action,True)
        for layer in [identities,endpoint]:
            foreign=principal.replace(binding['account_ids'][account],'999999999999')
            if layer is endpoint:
                require(decision(layer,'s3:GetObject',arn('raw',f'raw/venues/{config["dataset_version"]}/part.csv'),foreign,**{'aws:SourceVpce':config['endpoint_ids'][account]})!='allow','foreign principal accepted')
    if config['media']['enabled']:
        spec=buckets[bucket_names['media']]
        distribution=states['encore1']['values']['outputs']['foundation_track2']['value']['distribution_arn']
        for source,expected in [(distribution,'allow'),(distribution+'other','implicit_deny'),(None,'implicit_deny')]:
            require(decision(spec,'s3:GetObject',arn('media','media/venues/approved/v/hash/photo.jpg'),'cloudfront.amazonaws.com',**{'AWS:SourceArn':source})==expected,'OAC distribution restriction')
        require(decision(spec,'s3:GetObject',arn('media','evidence/proof'),'cloudfront.amazonaws.com',**{'AWS:SourceArn':distribution})!='allow','OAC evidence leak')
        require(not any('aws:SourceVpce' in s.get('Condition',{}).get('StringNotEquals',{}) for s in spec),'data endpoint deny copied to media')
    optional=[r for r in central if r['type']=='aws_s3_bucket' and any(x in r['address'] for x in ['observability_archive','module.media'])]
    require(len(optional)==int(config['media']['enabled'])+int(config['archive']['enabled']), 'optional bucket count mismatch')
    for r in optional:
        prefix=r['address'].rsplit('aws_s3_bucket.',1)[0]
        require(r['values']['force_destroy'] is False,'optional bucket force destroy')
        for kind in ['aws_s3_bucket_public_access_block','aws_s3_bucket_server_side_encryption_configuration','aws_s3_bucket_versioning','aws_s3_bucket_ownership_controls']:
            require(any(x['type']==kind and x['address'].startswith(prefix) for x in central),'optional bucket protection missing')
    require(not any(r['type'] in {'aws_kms_key','aws_s3_bucket_lifecycle_configuration'} for r in central),'unapproved KMS/retention resource')
    print('PASS: Track2 16-role identity/bucket/own-endpoint intersections; prefix/query/generation/account/OAC/archive/weather refusal checks')

def edge_mutations(state,config):
    cases=[('aws_cloudfront_cache_policy',lambda v:v.update(min_ttl=1),'p4-api-no-cache'),
           ('aws_cloudfront_origin_request_policy',lambda v:v['cookies_config'][0].update(cookie_behavior='none'),None),
           ('aws_cloudfront_origin_request_policy',lambda v:v['headers_config'][0].update(header_behavior='whitelist'),None),
           ('aws_cloudfront_origin_request_policy',lambda v:v['query_strings_config'][0].update(query_string_behavior='none'),None),
           ('aws_cloudfront_distribution',lambda v:v['default_cache_behavior'][0]['allowed_methods'].remove('POST'),None),
           ('aws_cloudfront_distribution',lambda v:v['ordered_cache_behavior'].append(dict(v['default_cache_behavior'][0],path_pattern='/api/ad-deliveries/*/image')),None),
           ('aws_cloudfront_origin_access_control',lambda v:v.update(signing_behavior='never'),None),
           ('aws_cloudfront_response_headers_policy',lambda v:v['custom_headers_config'][0]['items'][0].update(value='public,max-age=60'),'p4-private-no-store')]
    for kind,mutate,name in cases:
        changed=copy.deepcopy(state)
        value=next(r['values'] for r in resources(changed) if r['type']==kind and (name is None or r['values'].get('name')==name))
        mutate(value)
        try:audit_edge(changed,config)
        except (ContractError,KeyError):continue
        raise ContractError('Track2 edge mutation accepted: '+kind)
    print(f'PASS: {len(cases)} evaluated cache/cookie/header/proof/query/method/private-image/OAC mutations rejected')
