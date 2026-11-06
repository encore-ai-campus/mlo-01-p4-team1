"""Opt-in Foundation extensions. Common inventory/services v1 stay immutable."""
import copy
import json
import re
from model import ACCOUNTS, NODES, require

DEFAULT = {
    'enabled': False, 'dataset_version': '', 'driver_queries': {}, 'ingest_nodes': [],
    'endpoint_ids': {},
    'media': {'enabled': False, 'bucket': '', 'ttl_approved': False, 'browser_ttl': 0, 'edge_ttl': 0},
    'archive': {'enabled': False, 'bucket': ''},
    'tls': {'enabled': False, 'origin_name': '', 'certificate_arn': '', 'control_port': 8443, 'control_tls_ready': False},
}
PHOTO_PREFIX = 'media/venues/approved/'
PHOTO_FUNCTION = r'''function handler(event) {
  var request = event.request;
  if (!/^\/media\/venues\/approved\/[a-z0-9-]+\/[a-f0-9]{64}\/[a-zA-Z0-9_-]+\.(jpg|jpeg|png|webp|avif)$/.test(request.uri)) {
    return {statusCode: 403, statusDescription: 'Forbidden'};
  }
  return request;
}'''

def fixture_track2():
    value = copy.deepcopy(DEFAULT)
    value.update(enabled=True, dataset_version='fixture-v1',
                 driver_queries={'control-a': ['venues-catalog'], 'control-b': ['venues-catalog']},
                 ingest_nodes=['control-a'], endpoint_ids={a: f'vpce-{i:017x}' for i, a in enumerate(ACCOUNTS, 1)})
    value['media'].update(enabled=True, bucket='example-only-foundation-media', ttl_approved=True,
                          browser_ttl=60, edge_ttl=120)
    value['archive'].update(enabled=True, bucket='example-only-foundation-observability')
    return value

def validate_track2(value, binding):
    """Strict authoring input validation; Terraform independently validates its typed input."""
    require(isinstance(value, dict) and set(value) == set(DEFAULT), 'exact Track2 input keys required')
    for key in ['media', 'archive', 'tls']:
        require(isinstance(value[key], dict) and set(value[key]) == set(DEFAULT[key]), 'unknown/missing Track2 ' + key)
    for obj in [value, value['media'], value['archive'], value['tls']]:
        require(type(obj['enabled']) is bool, 'Track2 enabled must be boolean')
    if not value['enabled']:
        require(value == DEFAULT, 'disabled Track2 must contain no active inputs')
        return value
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', value['dataset_version']) is not None, 'invalid dataset version')
    require(set(value['endpoint_ids']) == set(ACCOUNTS) and len(set(value['endpoint_ids'].values())) == 5,
            'five distinct local S3 endpoints required')
    require(all(re.fullmatch(r'vpce-[a-f0-9]{17}', v) for v in value['endpoint_ids'].values()), 'invalid endpoint ID')
    require(set(value['driver_queries']) <= {'control-a', 'control-b'} and bool(value['driver_queries']), 'Control Drivers only')
    for queries in value['driver_queries'].values():
        require(isinstance(queries, list) and queries and len(set(queries)) == len(queries), 'nonempty distinct query IDs required')
        require(all(q == 'venues-catalog' for q in queries), 'unregistered Track2 query')
    require(isinstance(value['ingest_nodes'], list) and len(set(value['ingest_nodes'])) == len(value['ingest_nodes'])
            and set(value['ingest_nodes']) <= {'control-a', 'control-b'}, 'Control ingest allowlist required')
    buckets = [*binding['bucket_names'].values(), binding['state_bucket']]
    for key in ['media', 'archive']:
        item = value[key]
        if item['enabled']:
            require(re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', item['bucket']) is not None, 'invalid optional bucket')
            buckets.append(item['bucket'])
        else:
            require(item == DEFAULT[key], 'disabled bucket must contain no active inputs')
    require(len(set(buckets)) == len(buckets), 'optional buckets must be isolated from state/data/each other')
    media = value['media']
    require(type(media['ttl_approved']) is bool, 'explicit TTL approval flag required')
    if media['enabled']:
        require(media['ttl_approved'] and all(type(media[k]) is int for k in ['browser_ttl', 'edge_ttl'])
                and 0 <= media['browser_ttl'] <= media['edge_ttl'] <= 31536000 and media['edge_ttl'] > 0,
                'reviewed bounded photo TTL inputs required')
    tls = value['tls']
    require(type(tls['control_tls_ready']) is bool, 'Control TLS readiness must be boolean')
    if tls['enabled']:
        require(re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}', tls['origin_name']) is not None
                and not tls['origin_name'].endswith(('.amazonaws.com', '.cloudfront.net')), 'controlled origin hostname required')
        require(re.fullmatch(r'arn:aws:acm:ap-northeast-2:' + binding['account_ids']['encore1'] + r':certificate/[a-f0-9-]{36}', tls['certificate_arn']) is not None,
                'encore1 Seoul ACM certificate required')
        require(tls['control_tls_ready'] and tls['control_port'] == 8443, 'reviewed private Control TLS:8443 required')
    else:
        require(tls == DEFAULT['tls'], 'unresolved TLS inputs cannot enable partial TLS')
    return value

def variable():
    """Typed defaults are deliberately complete; validation also covers direct Terraform use."""
    from build_config import ex
    shape = ('object({enabled=bool,dataset_version=string,driver_queries=map(set(string)),ingest_nodes=set(string),'
             'endpoint_ids=map(string),media=object({enabled=bool,bucket=string,ttl_approved=bool,browser_ttl=number,edge_ttl=number}),'
             'archive=object({enabled=bool,bucket=string}),tls=object({enabled=bool,origin_name=string,certificate_arn=string,control_port=number,control_tls_ready=bool})})')
    conditions = [
        ('!var.track2.enabled ? jsonencode(var.track2) == jsonencode(jsondecode('+json.dumps(json.dumps(DEFAULT))+')) : true', 'Disabled Track2 cannot contain active inputs.'),
        ('!var.track2.enabled || (can(regex("^[a-z0-9][a-z0-9-]{0,62}$",var.track2.dataset_version)) && length(var.track2.endpoint_ids)==5 && length(distinct(values(var.track2.endpoint_ids)))==5 && alltrue([for a in ["encore1","encore2","encore3","encore4","encore5"]:can(regex("^vpce-[a-f0-9]{17}$",var.track2.endpoint_ids[a]))]))', 'Track2 dataset and five distinct local endpoint IDs required.'),
        ('!var.track2.enabled || (length(var.track2.driver_queries)>0 && alltrue([for n,qs in var.track2.driver_queries:contains(["control-a","control-b"],n) && length(qs)>0 && alltrue([for q in qs:q=="venues-catalog"])]) && alltrue([for n in var.track2.ingest_nodes:contains(["control-a","control-b"],n)]))', 'Only Control may ingest venues or own query checkpoints.'),
        ('!var.track2.media.enabled || (var.track2.enabled && var.track2.media.ttl_approved && var.track2.media.browser_ttl>=0 && floor(var.track2.media.browser_ttl)==var.track2.media.browser_ttl && var.track2.media.edge_ttl>0 && floor(var.track2.media.edge_ttl)==var.track2.media.edge_ttl && var.track2.media.edge_ttl>=var.track2.media.browser_ttl && var.track2.media.edge_ttl<=31536000)', 'Approved bounded photo TTL inputs required.'),
        ('!var.track2.tls.enabled || (var.track2.enabled && var.track2.tls.control_tls_ready && var.track2.tls.control_port==8443 && can(regex("^([a-z0-9][a-z0-9-]*[.])+[a-z]{2,}$",var.track2.tls.origin_name)) && !endswith(var.track2.tls.origin_name,".amazonaws.com") && !endswith(var.track2.tls.origin_name,".cloudfront.net") && can(regex("^arn:aws:acm:ap-northeast-2:[0-9]{12}:certificate/[a-f0-9-]{36}$",var.track2.tls.certificate_arn)))', 'Full TLS requires a controlled hostname, ACM certificate and Control TLS readiness.'),
    ]
    return {'type': shape, 'default': copy.deepcopy(DEFAULT), 'validation': [
        {'condition': ex(c), 'error_message': m} for c, m in conditions]}

def extend_account(module):
    from build_config import ex
    module['variable']['track2'] = variable()
    loc, r = module['locals'], module['resource']
    loc['track2_buckets'] = ex('merge(var.binding.bucket_names,{media=var.track2.media.bucket,archive=var.track2.archive.bucket})')
    # Independent table I/O and Driver state. Instance profiles do not fence processes.
    read = '["s3:GetObject","s3:GetObjectVersion"]'
    write = '["s3:PutObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"]'
    rw = '["s3:GetObject","s3:GetObjectVersion","s3:PutObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts","s3:DeleteObject"]'
    loc['track2_scopes'] = ex('{for name,n in local.topology:name=>concat('
        'contains(["control","spark"],n.role) ? [{purpose="raw",prefixes=["raw/venues/${var.track2.dataset_version}/*"],actions='+read+'},{purpose="lake",prefixes=[for layer in ["bronze","silver","gold"]:"lake/venues/${var.track2.dataset_version}/v1/${layer}/*"],actions='+rw+'}] : [],'
        'contains(var.track2.ingest_nodes,name) ? [{purpose="raw",prefixes=["raw/venues/${var.track2.dataset_version}/*"],actions='+write+'}] : [],'
        '[for q in lookup(var.track2.driver_queries,name,toset([])):{purpose="lake",prefixes=["aws/${var.binding.environment}/${var.binding.generation}/${q}/checkpoint/*"],actions='+rw+'}],'
        'var.track2.media.enabled && n.role=="control" ? [{purpose="media",prefixes=["media/venues/approved/*"],actions='+rw+'}] : [],'
        'var.track2.archive.enabled && name=="control-b" ? [{purpose="archive",prefixes=["observability/${var.binding.environment}/${var.binding.generation}/*"],actions='+write+'}] : [])}')
    loc['track2_grants'] = ex('{for name,n in local.topology:name=>flatten([for s in local.track2_scopes[name]:['
        '{Effect="Allow",Action=["s3:ListBucket"],Resource=["arn:aws:s3:::${local.track2_buckets[s.purpose]}"],Condition={StringLike={"s3:prefix"=s.prefixes},StringEquals={"aws:SourceVpce"=try(var.track2.endpoint_ids[n.account],"UNBOUND")}}},'
        '{Effect="Allow",Action=s.actions,Resource=[for p in s.prefixes:"arn:aws:s3:::${local.track2_buckets[s.purpose]}/${p}"],Condition={StringEquals={"aws:SourceVpce"=try(var.track2.endpoint_ids[n.account],"UNBOUND")}}}]] )}')
    r['aws_iam_role_policy']['track2'] = {'for_each': ex('var.track2.enabled ? {for name,n in local.nodes:name=>n if length(local.track2_scopes[name])>0} : {}'),
        'role': ex('aws_iam_role.runtime[each.key].id'), 'policy': ex('jsonencode({Version="2012-10-17",Statement=local.track2_grants[each.key]})')}
    runtime_sizes = '+'.join('length('+r['aws_iam_role_policy'][name]['policy'][2:-1]+')' for name in ['runtime','operation','track2'])
    artifact_size = 'local.artifact_enabled ? length('+r['aws_iam_role_policy']['artifact']['policy'][2:-1]+') : 0'
    r['aws_iam_role_policy']['track2']['lifecycle'] = {'precondition':[{
        'condition':ex(runtime_sizes+'+('+artifact_size+')<=10240'),
        'error_message':'Combined runtime inline policies exceed 10240 characters; review the query registry.'}]}
    # Replace the historical endpoint-wide data allowance with the same exact
    # per-node grants used by identity/bucket policies. Artifact/operation and
    # controller statements retain their independent boundaries.
    policy = r['aws_vpc_endpoint']['s3']['policy'][2:-1]
    broad = '[{Effect="Allow",Principal="*",Action=["s3:GetObject","s3:GetObjectVersion","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts","s3:ListBucket","s3:ListBucketMultipartUploads","s3:GetBucketLocation"],Resource=local.s3_resources,Condition={ArnEquals={"aws:PrincipalArn"=local.local_runtime_arns}}}]'
    scoped = 'flatten([for name,n in local.nodes:[for s in concat(local.s3_grants[name],local.lake_cleanup[name]):merge(s,{Principal="*",Condition=merge(try(s.Condition,{}),{ArnEquals={"aws:PrincipalArn"="arn:aws:iam::${var.binding.account_ids[n.account]}:role/p4-runtime-${name}"}})})]])'
    require(broad in policy, 'legacy endpoint generator changed; review Track2 composition')
    policy = policy.replace(broad, scoped)
    r['aws_vpc_endpoint']['s3']['policy'] = ex('jsonencode({Version="2012-10-17",Statement=concat(jsondecode('+policy+').Statement,flatten([for name,n in local.nodes:[for s in local.track2_grants[name]:merge(s,{Principal="*",Condition=merge(s.Condition,{ArnEquals={"aws:PrincipalArn"="arn:aws:iam::${var.binding.account_ids[n.account]}:role/p4-runtime-${name}"}})}) if var.track2.enabled]]))})')
    r['aws_vpc_endpoint']['s3']['lifecycle'] = {'precondition': [{
        'condition': ex('!var.track2.enabled || try(var.track2.endpoint_ids[var.account]==aws_vpc_endpoint.s3.id,false)'),
        'error_message': 'Track2 must use this account own S3 endpoint; review first-stage outputs.'}]}
    # A resource cannot refer to itself in preconditions: check after the computed ID exists.
    r['aws_vpc_endpoint']['s3']['lifecycle']['postcondition'] = r['aws_vpc_endpoint']['s3']['lifecycle'].pop('precondition')
    r['aws_vpc_endpoint']['s3']['lifecycle']['postcondition'][0]['condition'] = ex('!var.track2.enabled || try(var.track2.endpoint_ids[var.account]==self.id,false)')
    r['aws_vpc_endpoint']['s3']['lifecycle']['precondition'] = [{
        'condition':ex('length('+r['aws_vpc_endpoint']['s3']['policy'][2:-1]+')<=20480'),
        'error_message':'S3 endpoint policy exceeds 20 KiB; reduce/review the query registry.'}]
    module['variable']['track2']['validation'].append({
        'condition':ex('!var.track2.enabled || (length(distinct(concat(values(var.binding.bucket_names),[var.binding.state_bucket],var.track2.media.enabled ? [var.track2.media.bucket] : [],var.track2.archive.enabled ? [var.track2.archive.bucket] : [])))==length(var.binding.bucket_names)+1+(var.track2.media.enabled ? 1 : 0)+(var.track2.archive.enabled ? 1 : 0) && alltrue([for name in concat(var.track2.media.enabled ? [var.track2.media.bucket] : [],var.track2.archive.enabled ? [var.track2.archive.bucket] : []):can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$",name))]))'),
        'error_message':'Optional buckets must have valid names and be distinct from all data/state buckets.'})
    loc['track2_bucket_statements'] = ex('{for purpose in ["raw","lake","media","archive"]:purpose=>flatten([for name,n in local.topology:[for s in local.track2_grants[name]:merge(s,{Principal={AWS="arn:aws:iam::${var.binding.account_ids[n.account]}:root"},Condition=merge(s.Condition,{ArnEquals={"aws:PrincipalArn"="arn:aws:iam::${var.binding.account_ids[n.account]}:role/p4-runtime-${name}"}})}) if contains(s.Resource,"arn:aws:s3:::${local.track2_buckets[purpose]}") || anytrue([for p in s.Resource:startswith(p,"arn:aws:s3:::${local.track2_buckets[purpose]}/")])]])}')
    for purpose in ['raw', 'lake']:
        old = module['module'][purpose]['statements'][2:-1]
        # Deny scoped new object paths outside all five reviewed endpoints; individual
        # allow conditions additionally bind each principal to its own VPC endpoint.
        prefixes = '["raw/venues/*"]' if purpose == 'raw' else 'concat(["lake/venues/*"],[for q in distinct(flatten(values(var.track2.driver_queries))):"aws/${var.binding.environment}/${var.binding.generation}/${q}/checkpoint/*"])'
        deny = '[{Effect="Deny",Principal="*",Action="s3:*",Resource=[for p in '+prefixes+':"arn:aws:s3:::${var.binding.bucket_names.'+purpose+'}/${p}"],Condition={StringNotEquals={"aws:SourceVpce"=values(var.track2.endpoint_ids)}}}]'
        module['module'][purpose]['statements'] = ex('concat('+old+',[for s in concat(local.track2_bucket_statements.'+purpose+','+deny+'):s if var.track2.enabled])')
    module['module']['observability_archive'] = {'source':'../bucket', 'count':ex('var.account=="encore1" && var.track2.enabled && var.track2.archive.enabled ? 1 : 0'),
        'name':ex('var.track2.archive.bucket'), 'statements':ex('concat(local.track2_bucket_statements.archive,[{Effect="Deny",Principal="*",Action="s3:*",Resource=["arn:aws:s3:::${var.track2.archive.bucket}","arn:aws:s3:::${var.track2.archive.bucket}/*"],Condition={StringNotEquals={"aws:SourceVpce"=try(var.track2.endpoint_ids.encore1,"UNBOUND")}}}])')}
    module['output']['edge_inputs']['value']['media_statements'] = ex('[for s in local.track2_bucket_statements.media:s if var.track2.enabled && var.track2.media.enabled]')
    module['output']['foundation_track2_account'] = {'value': {
        'component': ex('var.account'), 'enabled': ex('var.track2.enabled'),
        'schema_version':'foundation-track2/1', 'environment':ex('var.binding.environment'), 'generation':ex('var.binding.generation'),
        'example_only':ex('var.binding.example_only'), 'region':'ap-northeast-2',
        'dataset_version':ex('var.track2.dataset_version'), 'driver_queries':ex('var.track2.driver_queries'),
        'ingest_nodes':ex('sort(tolist(var.track2.ingest_nodes))'),
        'endpoint_id':ex('aws_vpc_endpoint.s3.id'), 'archive_bucket_ref':ex('var.track2.archive.enabled ? var.track2.archive.bucket : null'),
        'ready_for_aws':False}}
    return module

def extend_edge(module):
    from build_config import ex
    module['variable']['track2'] = variable()
    module['variable']['account_id'] = {'type':'string'}
    r = module['resource']
    tls = 'var.track2.enabled && var.track2.tls.enabled'
    port = ex(tls+' ? 8443 : 8000')
    r['aws_security_group']['alb']['ingress'][0].update(from_port=ex(tls+' ? 443 : 80'), to_port=ex(tls+' ? 443 : 80'))
    for name in ['target_in', 'target_out']:
        r['aws_security_group_rule'][name].update(from_port=port, to_port=port)
    r['aws_lb_target_group']['web'].update(port=port, protocol=ex(tls+' ? "HTTPS" : "HTTP"'))
    r['aws_lb_target_group']['web']['health_check'][0]['protocol'] = ex(tls+' ? "HTTPS" : "HTTP"')
    r['aws_lb_target_group_attachment']['web']['port'] = port
    listener = r['aws_lb_listener']['http']
    listener.update(port=ex(tls+' ? 443 : 80'), protocol=ex(tls+' ? "HTTPS" : "HTTP"'),
                    certificate_arn=ex(tls+' ? var.track2.tls.certificate_arn : null'),
                    ssl_policy=ex(tls+' ? "ELBSecurityPolicy-TLS13-1-2-2021-06" : null'))
    listener['lifecycle'] = {'precondition':[{'condition':ex('!('+tls+') || startswith(var.track2.tls.certificate_arn,"arn:aws:acm:ap-northeast-2:${var.account_id}:certificate/")'), 'error_message':'Origin certificate must belong to encore1 in Seoul.'}]}
    # Forward a CloudFront-generated protocol header with all viewer auth/proof data.
    r['aws_cloudfront_origin_request_policy']['api']['headers_config'] = [
        {'header_behavior':'allViewerAndWhitelistCloudFront','headers':[{'items':['CloudFront-Forwarded-Proto']}]}]
    distribution = r['aws_cloudfront_distribution']['this']
    r['aws_cloudfront_response_headers_policy'] = {'private_api':{'name':'p4-private-no-store',
        'custom_headers_config':[{'items':[{'header':'Cache-Control','override':True,'value':'no-store, private'}]}]}}
    distribution['default_cache_behavior'][0]['response_headers_policy_id'] = ex('aws_cloudfront_response_headers_policy.private_api.id')
    origin = distribution['origin'][0]
    origin['domain_name'] = ex(tls+' ? var.track2.tls.origin_name : aws_lb.this.dns_name')
    origin['custom_origin_config'][0]['origin_protocol_policy'] = ex(tls+' ? "https-only" : "http-only"')
    # Explicit no-cache API behavior + safe default for account/admin/ads/HTML.
    api = copy.deepcopy(distribution['default_cache_behavior'][0]); api['path_pattern']='/api/*'
    distribution['ordered_cache_behavior'] = [api]
    media_on='var.track2.enabled && var.track2.media.enabled'
    r['aws_cloudfront_origin_access_control'] = {'media':{'count':ex(media_on+' ? 1 : 0'), 'name':'p4-approved-venue-photos',
        'origin_access_control_origin_type':'s3','signing_behavior':'always','signing_protocol':'sigv4'}}
    # The bucket policy is owned here to bind its exact distribution ARN without an
    # account/edge dependency cycle. This never copies the data SourceVpce Deny.
    module['module'] = {'media':{'source':'../bucket','count':ex(media_on+' ? 1 : 0'),'name':ex('var.track2.media.bucket'),
        'statements':ex('concat(var.inputs.media_statements,[{Effect="Allow",Principal={Service="cloudfront.amazonaws.com"},Action=["s3:GetObject"],Resource=["arn:aws:s3:::${var.track2.media.bucket}/media/venues/approved/*"],Condition={StringEquals={"AWS:SourceArn"=aws_cloudfront_distribution.this.arn}}}])')}}
    distribution['dynamic'] = {'origin':{'for_each':ex(media_on+' ? [1] : []'),'content':{
        'domain_name':ex('"${module.media[0].name}.s3.ap-northeast-2.amazonaws.com"'),'origin_id':'approved-photos',
        'origin_access_control_id':ex('aws_cloudfront_origin_access_control.media[0].id'),'s3_origin_config':[{'origin_access_identity':''}]}},
        'ordered_cache_behavior':{'for_each':ex(media_on+' ? [1] : []'),'content':{
            'path_pattern':'/media/venues/approved/*','target_origin_id':'approved-photos','viewer_protocol_policy':'https-only',
            'allowed_methods':['GET','HEAD'],'cached_methods':['GET','HEAD'],'cache_policy_id':ex('aws_cloudfront_cache_policy.photos[0].id'),
            'response_headers_policy_id':ex('aws_cloudfront_response_headers_policy.photos[0].id'),
            'function_association':[{'event_type':'viewer-request','function_arn':ex('aws_cloudfront_function.photo_version[0].arn')}]}}}
    r['aws_cloudfront_cache_policy']['photos'] = {'count':ex(media_on+' ? 1 : 0'),'name':'p4-approved-photo-cache','min_ttl':0,
        'default_ttl':ex('var.track2.media.edge_ttl'),'max_ttl':ex('var.track2.media.edge_ttl'),
        'parameters_in_cache_key_and_forwarded_to_origin':copy.deepcopy(r['aws_cloudfront_cache_policy']['disabled']['parameters_in_cache_key_and_forwarded_to_origin'])}
    r['aws_cloudfront_response_headers_policy']['photos'] = {'count':ex(media_on+' ? 1 : 0'),'name':'p4-approved-photo-response',
        'custom_headers_config':[{'items':[{'header':'Cache-Control','override':True,'value':ex('"public,max-age=${var.track2.media.browser_ttl},s-maxage=${var.track2.media.edge_ttl}"')}]}]}
    r['aws_cloudfront_function'] = {'photo_version':{'count':ex(media_on+' ? 1 : 0'),'name':'p4-approved-photo-version','runtime':'cloudfront-js-2.0','publish':True,'code':PHOTO_FUNCTION}}
    module['output'] = {'foundation_track2_edge':{'value':{
        'media_bucket_ref':ex(media_on+' ? var.track2.media.bucket : null'),
        'distribution_arn':ex('aws_cloudfront_distribution.this.arn'),
        'tls_status':ex(tls+' ? "CONFIGURED_NOT_VERIFIED" : "UNRESOLVED_ORIGIN_NAME_CERTIFICATE"'),
        'ready_for_aws':False}}}
    return module
