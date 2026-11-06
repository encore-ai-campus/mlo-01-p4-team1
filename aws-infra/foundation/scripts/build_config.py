#!/usr/bin/env python3
"""Generate reviewable Terraform JSON from the immutable common topology/flows."""
from model import *
import argparse
TF=FOUNDATION/'terraform'
V={'required_version':'= 1.11.4','required_providers':{'aws':{'source':'hashicorp/aws','version':'= 5.94.1'}}}
def ex(s): return '${'+s+'}'
def policy(statements): return json.dumps({'Version':'2012-10-17','Statement':statements})
def allow(actions,resources,**kw): return dict(Effect='Allow',Action=actions,Resource=resources,**kw)
def put(path,data):
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(data,indent=2)+'\n')

def operation_locals():
    # Exact physical scope is identical to infra_common.coordination.scope_key;
    # generation deliberately does not create a second lock for the same estate.
    scope='{project="mlo-p4",environment=var.binding.environment,account_ids=var.binding.account_ids,state_keys={for k in ["bootstrap","connectivity","encore1","encore2","encore3","encore4","encore5"]:k=>"dev/${k}/terraform.tfstate"}}'
    return {'operation_key':ex('"operations/${sha256(jsonencode('+scope+'))}.json"'),
            'operation_arn':ex('"arn:aws:s3:::${var.binding.state_bucket}/${local.operation_key}"'),
            'operation_controller_arn':ex('"arn:aws:iam::${var.binding.account_ids.encore1}:role/p4-operation-controller"')}

def operation_actions():
    # This lock is narrowed from the deterministic Autopilot baseline. No ACL,
    # KMS, tagging, retention or object-lambda candidate permission is adopted.
    locked=read_json(FOUNDATION/'operation-policy.lock.json')
    require(locked['actions']==['s3:GetObject','s3:PutObject'],'operation action lock changed')
    return locked['actions']

def bucket_module():
    r={
    'aws_s3_bucket':{'this':{'bucket':ex('var.name'),'force_destroy':False,'lifecycle':{'prevent_destroy':True}}},
    'aws_s3_bucket_public_access_block':{'this':{'bucket':ex('aws_s3_bucket.this.id'),'block_public_acls':True,'block_public_policy':True,'ignore_public_acls':True,'restrict_public_buckets':True}},
    'aws_s3_bucket_ownership_controls':{'this':{'bucket':ex('aws_s3_bucket.this.id'),'rule':[{'object_ownership':'BucketOwnerEnforced'}]}},
    'aws_s3_bucket_versioning':{'this':{'bucket':ex('aws_s3_bucket.this.id'),'versioning_configuration':[{'status':'Enabled'}]}},
    'aws_s3_bucket_server_side_encryption_configuration':{'this':{'bucket':ex('aws_s3_bucket.this.id'),'rule':[{'apply_server_side_encryption_by_default':[{'sse_algorithm':'AES256'}]}]}},
    'aws_s3_bucket_policy':{'this':{'bucket':ex('aws_s3_bucket.this.id'),'policy':ex('jsonencode({Version="2012-10-17",Statement=concat([{Effect="Deny",Principal="*",Action="s3:*",Resource=[aws_s3_bucket.this.arn,"${aws_s3_bucket.this.arn}/*"],Condition={Bool={"aws:SecureTransport"="false"}}}],var.statements)})')}}}
    return {'terraform':V,'variable':{'name':{'type':'string'},'statements':{'type':'any','default':[]}},'resource':r,'output':{'name':{'value':ex('aws_s3_bucket.this.id')}}}

def account_module():
    r={}; data={}; modules={}
    def add(t,n,v): r.setdefault(t,{})[n]=v
    nodes_expr='{for name,node in local.topology : name=>node if node.account==var.account}'
    loc={'topology':NODES,'flows':flows(),'nodes':ex(nodes_expr),'addresses':ex('{for name,n in local.topology : name=>cidrhost(cidrsubnet(var.binding.cidrs[n.account],8,index(["a","b","c"],n.az_slot)+(n.subnet_kind=="public" ? 100 : 1)),20+index(sort(keys(local.topology)),name))}'),
         'subnets':{f'{kind}-{slot}':{'slot':slot,'index':i+(100 if kind=='public' else 1),'kind':kind} for kind in ['private','public'] for i,slot in enumerate('abc')},
         'runtime_arns':ex('[for name,n in local.topology : "arn:aws:iam::${var.binding.account_ids[n.account]}:role/p4-runtime-${name}"]'),
         's3_resources':ex('flatten([for k,name in var.binding.bucket_names : ["arn:aws:s3:::${name}","arn:aws:s3:::${name}/*"] if k!="transfer"])'),
         'local_runtime_arns':ex('[for name,n in local.nodes : "arn:aws:iam::${var.binding.account_ids[n.account]}:role/p4-runtime-${name}"]'),
         'controller_ids':ex('try(var.binding.controller_instance_ids[var.account],[])'),
         'controller_enabled':ex('length(local.controller_ids)==length(local.nodes)'),
         'controller_arn':ex('"arn:aws:iam::${var.binding.account_ids[var.account]}:role/p4-controller-${var.account}"'),
         'controller_grants':ex('[for s in [{Effect="Allow",Action=["s3:GetBucketLocation"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}"]},{Effect="Allow",Action=["s3:ListBucket"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}"],Condition={StringLike={"s3:prefix"=[for id in local.controller_ids:"${id}/*"]}}},{Effect="Allow",Action=["s3:GetObject","s3:PutObject","s3:DeleteObject"],Resource=[for id in local.controller_ids:"arn:aws:s3:::${var.binding.bucket_names.transfer}/${id}/*"]}] : s if local.controller_enabled]')}
    loc.update(operation_locals())
    add('aws_vpc','this',{'cidr_block':ex('var.binding.cidrs[var.account]'),'enable_dns_support':True,'enable_dns_hostnames':True,'tags':{'Name':ex('"p4-${var.account}"')},'lifecycle':{'prevent_destroy':True}})
    add('aws_default_security_group','closed',{'vpc_id':ex('aws_vpc.this.id'),'ingress':[],'egress':[]})
    add('aws_internet_gateway','this',{'count':ex('contains(["encore1","encore5"],var.account) ? 1 : 0'),'vpc_id':ex('aws_vpc.this.id')})
    add('aws_subnet','this',{'for_each':ex('local.subnets'),'vpc_id':ex('aws_vpc.this.id'),'availability_zone_id':ex('var.binding.az_ids[each.value.slot]'),'cidr_block':ex('cidrsubnet(var.binding.cidrs[var.account],8,each.value.index)'),'map_public_ip_on_launch':ex('each.value.kind=="public" && var.account=="encore5"'),'tags':{'Name':ex('each.key')}})
    add('aws_route_table','this',{'for_each':ex('local.subnets'),'vpc_id':ex('aws_vpc.this.id')})
    add('aws_route_table_association','this',{'for_each':ex('local.subnets'),'subnet_id':ex('aws_subnet.this[each.key].id'),'route_table_id':ex('aws_route_table.this[each.key].id')})
    add('aws_route','internet',{'for_each':ex('{for k,s in local.subnets:k=>s if s.kind=="public" && contains(["encore1","encore5"],var.account)}'),'route_table_id':ex('aws_route_table.this[each.key].id'),'destination_cidr_block':'0.0.0.0/0','gateway_id':ex('aws_internet_gateway.this[0].id')})
    endpoint_policy=ex('jsonencode({Version="2012-10-17",Statement=concat([{Effect="Allow",Principal="*",Action=["s3:GetObject","s3:GetObjectVersion","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts","s3:ListBucket","s3:ListBucketMultipartUploads","s3:GetBucketLocation"],Resource=local.s3_resources,Condition={ArnEquals={"aws:PrincipalArn"=local.local_runtime_arns}}}],[for s in local.controller_grants:merge(s,{Principal="*",Condition=merge(try(s.Condition,{}),{ArnEquals={"aws:PrincipalArn"=local.controller_arn}})})],local.artifact_enabled ? [{Effect="Allow",Principal="*",Action=["s3:GetObjectVersion"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}/${local.artifact_key}"],Condition={StringEquals={"s3:VersionId"=local.artifact_version},ArnEquals={"aws:PrincipalArn"=local.local_runtime_arns}}}] : [])})')
    add('aws_vpc_endpoint','s3',{'vpc_id':ex('aws_vpc.this.id'),'service_name':'com.amazonaws.ap-northeast-2.s3','vpc_endpoint_type':'Gateway','route_table_ids':ex('[for r in aws_route_table.this:r.id]'),'policy':endpoint_policy})
    # Operation traffic has a separate exact key, not a Terraform state or data
    # prefix. Identity, central bucket and local endpoint must all permit it.
    endpoint=r['aws_vpc_endpoint']['s3']['policy'][2:-1]
    endpoint=endpoint.replace('Statement=concat([','Statement=concat([{Effect="Allow",Principal="*",Action='+json.dumps(operation_actions())+',Resource=[local.operation_arn],Condition={ArnEquals={"aws:PrincipalArn"=local.local_runtime_arns}}}],[' ,1)
    r['aws_vpc_endpoint']['s3']['policy']=ex(endpoint)
    add('aws_security_group','host',{'for_each':ex('local.nodes'),'name':ex('"p4-${each.key}"'),'vpc_id':ex('aws_vpc.this.id'),'ingress':[],'egress':[]})
    add('aws_security_group_rule','flow_in',{'for_each':ex('{for k,f in local.flows:k=>f if contains(keys(local.nodes),f.target)}'),'type':'ingress','security_group_id':ex('aws_security_group.host[each.value.target].id'),'protocol':'tcp','from_port':ex('each.value.low'),'to_port':ex('each.value.high'),'cidr_blocks':ex('["${local.addresses[each.value.source]}/32"]')})
    add('aws_security_group_rule','flow_out',{'for_each':ex('{for k,f in local.flows:k=>f if contains(keys(local.nodes),f.source)}'),'type':'egress','security_group_id':ex('aws_security_group.host[each.value.source].id'),'protocol':'tcp','from_port':ex('each.value.low'),'to_port':ex('each.value.high'),'cidr_blocks':ex('["${local.addresses[each.value.target]}/32"]')})
    add('aws_security_group_rule','ssm_out',{'for_each':ex('local.nodes'),'type':'egress','security_group_id':ex('aws_security_group.host[each.key].id'),'protocol':'tcp','from_port':443,'to_port':443,'cidr_blocks':ex('[cidrsubnet(var.binding.cidrs.encore1,8,1)]')})
    add('aws_security_group_rule','s3_out',{'for_each':ex('local.nodes'),'type':'egress','security_group_id':ex('aws_security_group.host[each.key].id'),'protocol':'tcp','from_port':443,'to_port':443,'prefix_list_ids':ex('[aws_vpc_endpoint.s3.prefix_list_id]')})
    add('aws_security_group_rule','gateway_web',{'for_each':ex('{for k,n in local.nodes:k=>n if n.role=="gateway"}'),'type':'egress','security_group_id':ex('aws_security_group.host[each.key].id'),'protocol':'tcp','from_port':80,'to_port':80,'cidr_blocks':['0.0.0.0/0']})
    add('aws_security_group_rule','gateway_tls',{'for_each':ex('{for k,n in local.nodes:k=>n if n.role=="gateway"}'),'type':'egress','security_group_id':ex('aws_security_group.host[each.key].id'),'protocol':'tcp','from_port':443,'to_port':443,'cidr_blocks':['0.0.0.0/0']})
    trust=policy([{'Effect':'Allow','Principal':{'Service':'ec2.amazonaws.com'},'Action':'sts:AssumeRole'}])
    add('aws_iam_role','runtime',{'for_each':ex('local.nodes'),'name':ex('"p4-runtime-${each.key}"'),'assume_role_policy':trust})
    add('aws_iam_role_policy','operation',{'for_each':ex('local.nodes'),'role':ex('aws_iam_role.runtime[each.key].id'),'policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action='+json.dumps(operation_actions())+',Resource=[local.operation_arn]}]})')})
    add('aws_iam_instance_profile','runtime',{'for_each':ex('local.nodes'),'name':ex('"p4-runtime-${each.key}"'),'role':ex('aws_iam_role.runtime[each.key].name')})
    add('aws_iam_role','controller',{'name':ex('"p4-controller-${var.account}"'),'assume_role_policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action="sts:AssumeRole",Principal={AWS="arn:aws:iam::${var.binding.account_ids[var.account]}:root"},Condition={ArnEquals={"aws:PrincipalArn"=concat([var.binding.provisioner_arns[var.account]],contains(keys(var.binding.oidc_subjects),"runtime") ? ["arn:aws:iam::${var.binding.account_ids[var.account]}:role/p4-ci-runtime"] : [])}}}]})')})
    controller_ssm=[{'Effect':'Allow','Action':['ssm:StartSession'],'Resource':ex('concat([for id in local.controller_ids:"arn:aws:ec2:ap-northeast-2:${var.binding.account_ids[var.account]}:instance/${id}"],["arn:aws:ssm:ap-northeast-2:${var.binding.account_ids[var.account]}:document/SSM-SessionManagerRunShell"])')},
                    {'Effect':'Allow','Action':['ssm:TerminateSession','ssmmessages:OpenDataChannel'],'Resource':ex('["arn:aws:ssm:ap-northeast-2:${var.binding.account_ids[var.account]}:session/$${aws:userid}-*"]')}]
    # jsonencode handles the expression-valued Resource inside this Terraform structure.
    loc['controller_ssm']=controller_ssm
    loc['controller_deployment']=ex('jsondecode(local.controller_enabled && local.artifact_enabled ? jsonencode([{Effect="Allow",Action=["ssm:SendCommand"],Resource=concat([for id in local.controller_ids:"arn:aws:ec2:ap-northeast-2:${var.binding.account_ids[var.account]}:instance/${id}"],["arn:aws:ssm:ap-northeast-2:${var.binding.account_ids[var.account]}:document/p4-runtime-artifact"])},{Effect="Allow",Action=["ssm:GetCommandInvocation"],Resource=["*"]},{Effect="Allow",Action=["ssm:GetDocument","ssm:DescribeDocument"],Resource=["arn:aws:ssm:ap-northeast-2:${var.binding.account_ids[var.account]}:document/p4-runtime-artifact"]},{Effect="Allow",Action=["s3:GetObjectVersion"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}/${local.artifact_key}"],Condition={StringEquals={"s3:VersionId"=local.artifact_version}}}]) : "[]")')
    add('aws_iam_role_policy','controller',{'count':ex('local.controller_enabled ? 1 : 0'),'role':ex('aws_iam_role.controller.id'),'policy':ex('jsonencode({Version="2012-10-17",Statement=concat(local.controller_grants,local.controller_ssm,local.controller_deployment)})'),
        'lifecycle':{'precondition':[{'condition':ex('toset(local.controller_ids)==toset([for n in aws_instance.host:n.id])'),'error_message':'Controller prefixes must equal this account actual EC2 instance set; export then bind the IDs.'}]}})
    # AWS SSM agent actions without resource-level authorization must use *.
    agent_actions=['ssm:UpdateInstanceInformation','ssm:ListAssociations','ssm:ListInstanceAssociations','ssm:PutInventory','ssm:PutComplianceItems','ssm:UpdateAssociationStatus','ssm:UpdateInstanceAssociationStatus']
    channel_actions=['ssmmessages:CreateControlChannel','ssmmessages:CreateDataChannel','ssmmessages:OpenControlChannel','ssmmessages:OpenDataChannel']
    ssm=allow(agent_actions+channel_actions,['*'])
    loc['storage_grants']={
        'control':{'raw':['raw/weather/*'],'lake':['lake/feedback/*','aws/${var.binding.environment}/${var.binding.generation}/*/checkpoint/*']},
        'spark':{'raw':['raw/weather/*'],'lake':['lake/feedback/*']},
        'gateway':{'raw':['raw/weather/*']},
        'postgresql':{'backup':['backups/${var.binding.environment}/${var.binding.generation}/postgresql/*']},
        'mongodb':{'backup':['backups/${var.binding.environment}/${var.binding.generation}/mongodb/*']}}
    # Same source produces identity and resource policies for cross-account grants.
    loc['s3_grants']=ex('{for name,n in local.topology:name=>flatten([for purpose,prefixes in lookup(local.storage_grants,n.role,{}) : [{Effect="Allow",Action=["s3:ListBucket","s3:ListBucketMultipartUploads"],Resource=["arn:aws:s3:::${var.binding.bucket_names[purpose]}"],Condition={StringLike={"s3:prefix"=prefixes}}},{Effect="Allow",Action=["s3:GetObject","s3:GetObjectVersion","s3:PutObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],Resource=[for p in prefixes:"arn:aws:s3:::${var.binding.bucket_names[purpose]}/${p}"]}]] )}')
    # Checkpoint and Delta writers need scoped cleanup; no bucket lifecycle deletes data.
    loc['lake_cleanup']=ex('{for name,n in local.topology:name=>(contains(["control","spark"],n.role) ? [{Effect="Allow",Action=["s3:DeleteObject"],Resource=concat(["arn:aws:s3:::${var.binding.bucket_names.lake}/lake/feedback/*"],n.role=="control" ? ["arn:aws:s3:::${var.binding.bucket_names.lake}/aws/${var.binding.environment}/${var.binding.generation}/*/checkpoint/*"] : [])}] : [])}')
    add('aws_iam_role_policy','runtime',{'for_each':ex('local.nodes'),'role':ex('aws_iam_role.runtime[each.key].id'),'policy':ex('jsonencode({Version="2012-10-17",Statement=concat('+json.dumps([ssm])+',local.s3_grants[each.key],local.lake_cleanup[each.key],[{Effect="Allow",Action=["ssm:GetDocument","ssm:DescribeDocument"],Resource=["arn:aws:ssm:ap-northeast-2:${var.binding.account_ids[var.account]}:document/p4-runtime-artifact"]}])})')})
    add('aws_network_interface','host',{'for_each':ex('local.nodes'),'subnet_id':ex('aws_subnet.this["${each.value.subnet_kind}-${each.value.az_slot}"].id'),'private_ips':ex('[local.addresses[each.key]]'),'security_groups':ex('[aws_security_group.host[each.key].id]'),'lifecycle':{'prevent_destroy':True}})
    add('aws_instance','host',{'for_each':ex('local.nodes'),'ami':ex('var.binding.ami_ids[var.account]'),'instance_type':ex('each.value.instance_type'),'iam_instance_profile':ex('aws_iam_instance_profile.runtime[each.key].name'),'network_interface':[{'network_interface_id':ex('aws_network_interface.host[each.key].id'),'device_index':0,'delete_on_termination':False}],'root_block_device':[{'encrypted':True,'volume_type':'gp3','volume_size':24,'delete_on_termination':True}],'credit_specification':[{'cpu_credits':'standard'}],'metadata_options':[{'http_endpoint':'enabled','http_tokens':'required','http_put_response_hop_limit':2}],'tags':{'Name':ex('each.key'),'Generation':ex('var.binding.generation'),'Project':'mlo-p4'},'lifecycle':{'precondition':[{'condition':ex('data.aws_caller_identity.this.account_id==var.binding.account_ids[var.account]'),'error_message':'Account identity mismatch.'}]}})
    add('aws_ebs_volume','data',{'for_each':ex('local.nodes'),'availability_zone':ex('aws_subnet.this["${each.value.subnet_kind}-${each.value.az_slot}"].availability_zone'),'encrypted':True,'type':'gp3','size':ex('var.data_sizes[each.key]'),'tags':{'Name':ex('"p4-data-${each.key}"')},'lifecycle':{'prevent_destroy':True}})
    add('aws_volume_attachment','data',{'for_each':ex('local.nodes'),'device_name':'/dev/sdf','volume_id':ex('aws_ebs_volume.data[each.key].id'),'instance_id':ex('aws_instance.host[each.key].id'),'force_detach':False,'stop_instance_before_detaching':True})
    add('aws_security_group','ssm',{'count':ex('var.account=="encore1" ? 1 : 0'),'vpc_id':ex('aws_vpc.this.id'),'ingress':[],'egress':[]})
    add('aws_security_group_rule','ssm_in',{'for_each':ex('var.account=="encore1" ? local.addresses : {}'),'type':'ingress','security_group_id':ex('aws_security_group.ssm[0].id'),'protocol':'tcp','from_port':443,'to_port':443,'cidr_blocks':ex('["${each.value}/32"]')})
    add('aws_vpc_endpoint','ssm',{'for_each':ex('var.account=="encore1" ? toset(["ssm","ssmmessages"]) : toset([])'),'vpc_id':ex('aws_vpc.this.id'),'service_name':ex('"com.amazonaws.ap-northeast-2.${each.key}"'),'vpc_endpoint_type':'Interface','private_dns_enabled':False,'subnet_ids':ex('[aws_subnet.this["private-a"].id]'),'security_group_ids':ex('[aws_security_group.ssm[0].id]'),'policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Principal="*",Action=each.key=="ssm" ? '+json.dumps(agent_actions+['ssm:GetDocument','ssm:DescribeDocument'])+' : '+json.dumps(channel_actions)+',Resource="*",Condition={ArnEquals={"aws:PrincipalArn"=local.runtime_arns}}}]})')})
    add('aws_route53_zone','ssm',{'for_each':ex('var.account=="encore1" ? toset(["ssm","ssmmessages"]) : toset([])'),'name':ex('"${each.key}.ap-northeast-2.amazonaws.com"'),'vpc':[{'vpc_id':ex('aws_vpc.this.id'),'vpc_region':'ap-northeast-2'}],'lifecycle':{'ignore_changes':['vpc']}})
    add('aws_route53_record','ssm',{'for_each':ex('aws_route53_zone.ssm'),'zone_id':ex('each.value.zone_id'),'name':ex('each.value.name'),'type':'A','alias':[{'name':ex('aws_vpc_endpoint.ssm[each.key].dns_entry[0].dns_name'),'zone_id':ex('aws_vpc_endpoint.ssm[each.key].dns_entry[0].hosted_zone_id'),'evaluate_target_health':False}]})
    for purpose in ['raw','lake','backup','transfer']:
        # Resource policy each statement gains exact logical runtime principal.
        statements=ex('flatten([for name,n in local.topology:[for s in concat(local.s3_grants[name],local.lake_cleanup[name]):merge(s,{Principal={AWS="arn:aws:iam::${var.binding.account_ids[n.account]}:root"},Condition=merge(try(s.Condition,{}),{ArnEquals={"aws:PrincipalArn"="arn:aws:iam::${var.binding.account_ids[n.account]}:role/p4-runtime-${name}"}})}) if anytrue([for resource in s.Resource:startswith(resource,"arn:aws:s3:::${var.binding.bucket_names.'+purpose+'}")])]])')
        if purpose=='transfer':
            statements=ex('concat(flatten([for account,ids in try(var.binding.controller_instance_ids,{}) : [for s in [{Effect="Allow",Principal={AWS="arn:aws:iam::${var.binding.account_ids[account]}:root"},Action=["s3:GetBucketLocation"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}"],Condition={ArnEquals={"aws:PrincipalArn"="arn:aws:iam::${var.binding.account_ids[account]}:role/p4-controller-${account}"}}},{Effect="Allow",Principal={AWS="arn:aws:iam::${var.binding.account_ids[account]}:root"},Action=["s3:ListBucket"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}"],Condition={StringLike={"s3:prefix"=[for id in ids:"${id}/*"]},ArnEquals={"aws:PrincipalArn"="arn:aws:iam::${var.binding.account_ids[account]}:role/p4-controller-${account}"}}},{Effect="Allow",Principal={AWS="arn:aws:iam::${var.binding.account_ids[account]}:root"},Action=["s3:GetObject","s3:PutObject","s3:DeleteObject"],Resource=[for id in ids:"arn:aws:s3:::${var.binding.bucket_names.transfer}/${id}/*"],Condition={ArnEquals={"aws:PrincipalArn"="arn:aws:iam::${var.binding.account_ids[account]}:role/p4-controller-${account}"}}}] : s if length(ids)>0]]),local.artifact_enabled ? [{Effect="Allow",Principal={AWS=[for id in values(var.binding.account_ids):"arn:aws:iam::${id}:root"]},Action=["s3:GetObjectVersion"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}/${local.artifact_key}"],Condition={StringEquals={"s3:VersionId"=local.artifact_version},ArnEquals={"aws:PrincipalArn"=local.runtime_arns}}}] : [])')
        modules[purpose]={'count':ex('var.account=="encore1" ? 1 : 0'),'source':'../bucket','name':ex('var.binding.bucket_names.'+purpose),'statements':statements}
    data['aws_caller_identity']={'this':{}}
    out={'component':ex('var.account'),'schema_version':'1.0.0','environment':ex('var.binding.environment'),'generation':ex('var.binding.generation'),'example_only':ex('var.binding.example_only'),'region':'ap-northeast-2','az_ids':ex('var.binding.az_ids'),
         'account':{'vpc_id':ex('aws_vpc.this.id'),'cidr':ex('var.binding.cidrs[var.account]'),'s3_endpoint_id':ex('aws_vpc_endpoint.s3.id')},
         'nodes':ex('[for name,n in local.nodes:merge(n,{instance_id=aws_instance.host[name].id,private_ip=local.addresses[name],az_id=var.binding.az_ids[n.az_slot],runtime_role_ref="role-ref:${name}"})]'),
         'route_tables':ex('{for k,r in aws_route_table.this:k=>r.id}'),'zones':ex('{for k,z in aws_route53_zone.ssm:k=>z.zone_id}'),
         'storage':ex('merge({for k,v in var.binding.bucket_names:"${k}_bucket_ref"=>v},{state_bucket_ref=var.binding.state_bucket})')}
    # Auto-assigned public IPv4 requires EC2 to create the primary interface.
    r['aws_network_interface']['host']['for_each']=ex('{for k,n in local.nodes:k=>n if n.subnet_kind=="private"}')
    host=r['aws_instance']['host']
    interface=host.pop('network_interface')
    host['dynamic']={'network_interface':{'for_each':ex('each.value.subnet_kind=="private" ? [1] : []'),'content':interface[0]}}
    host.update(subnet_id=ex('each.value.subnet_kind=="public" ? aws_subnet.this["public-${each.value.az_slot}"].id : null'),
                private_ip=ex('each.value.subnet_kind=="public" ? local.addresses[each.key] : null'),
                vpc_security_group_ids=ex('each.value.subnet_kind=="public" ? [aws_security_group.host[each.key].id] : null'),
                associate_public_ip_address=ex('each.value.subnet_kind=="public" ? true : null'))
    # Immutable gzip TAR plus externally bound archive/manifest hashes.
    loc['artifact_enabled']=ex('length(var.binding.artifact)>0')
    for key in ['key','version','sha256','manifest_sha256']:loc['artifact_'+key]=ex('try(var.binding.artifact.'+key+',"UNBOUND")')
    # Separate immutable artifact reads for deployment controllers. Temporary
    # Ansible transfer grants remain unchanged and cannot read another prefix.
    extra=ex('local.artifact_enabled && local.controller_enabled ? [{Effect="Allow",Principal="*",Action=["s3:GetObjectVersion"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}/${local.artifact_key}"],Condition={StringEquals={"s3:VersionId"=local.artifact_version},ArnEquals={"aws:PrincipalArn"=local.controller_arn}}}] : []')
    previous=r['aws_vpc_endpoint']['s3']['policy'][2:-1]
    r['aws_vpc_endpoint']['s3']['policy']=ex('jsonencode({Version="2012-10-17",Statement=concat(jsondecode('+previous+').Statement,'+extra[2:-1]+')})')
    transfer=modules['transfer']['statements'][2:-1]
    controllers=ex('local.artifact_enabled ? [for account in keys(try(var.binding.controller_instance_ids,{})):{Effect="Allow",Principal={AWS="arn:aws:iam::${var.binding.account_ids[account]}:root"},Action=["s3:GetObjectVersion"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}/${local.artifact_key}"],Condition={StringEquals={"s3:VersionId"=local.artifact_version},ArnEquals={"aws:PrincipalArn"="arn:aws:iam::${var.binding.account_ids[account]}:role/p4-controller-${account}"}}}] : []')
    modules['transfer']['statements']=ex('concat('+transfer+','+controllers[2:-1]+')')
    add('aws_iam_role_policy','artifact',{'for_each':ex('local.artifact_enabled ? local.nodes : {}'),'role':ex('aws_iam_role.runtime[each.key].id'),'policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action=["s3:GetObjectVersion"],Resource=["arn:aws:s3:::${var.binding.bucket_names.transfer}/${var.binding.artifact.key}"],Condition={StringEquals={"s3:VersionId"=var.binding.artifact.version}}}]})')})
    from ssm_dispatch import dispatcher_script
    script=dispatcher_script()
    document={'schemaVersion':'2.2','description':'Versioned runtime artifact dispatcher; payload owned by runtime.','parameters':{'Operation':{'type':'String','allowedValues':['install','deploy','health','rollback'],'default':'health'}},'mainSteps':[{'action':'aws:runShellScript','name':'versionedArtifact','inputs':{'timeoutSeconds':'1800','runCommand':script}}]}
    # Encode after evaluating embedded instance-map expressions, so JSON quoting remains valid.
    add('aws_ssm_document','runtime',{'count':ex('local.artifact_enabled ? 1 : 0'),'name':'p4-runtime-artifact','document_type':'Command','document_format':'JSON','content':ex('jsonencode('+json.dumps(document)+')')})
    return {'terraform':V,'variable':{'account':{'type':'string','validation':[{'condition':ex('contains(["encore1","encore2","encore3","encore4","encore5"],var.account)'),'error_message':'Unknown account alias.'}]},'binding':{'type':'any'},'data_sizes':{'type':'map(number)'}},'locals':loc,'resource':r,'data':data,'module':modules,'output':{'foundation':{'value':out},'runtime_artifact_boundary':{'value':ex('[for d in aws_ssm_document.runtime:{name=d.name,version=d.document_version,hash=d.hash,hash_type=d.hash_type}]')},'edge_inputs':{'value':{'vpc_id':ex('aws_vpc.this.id'),'subnets':ex('[for k,s in aws_subnet.this:s.id if startswith(k,"public-")]'),'control_sgs':ex('[for k,s in aws_security_group.host:s.id if startswith(k,"control-")]'),'targets':ex('[for k,n in local.nodes:aws_instance.host[k].id if n.role=="control"]')}}}}

def pair_module():
    vp=lambda a: ex(f'var.accounts[var.{a}].account.vpc_id')
    route=lambda a,b: {'for_each':ex(f'var.accounts[var.{a}].route_tables'),'provider':f'aws.{a}','route_table_id':ex('each.value'),'destination_cidr_block':ex(f'var.accounts[var.{b}].account.cidr'),'vpc_peering_connection_id':ex('aws_vpc_peering_connection_accepter.this.id')}
    return {'terraform':dict(V,required_providers={'aws':dict(V['required_providers']['aws'],configuration_aliases=['aws.requester','aws.acceptor'])}),
      'variable':{k:{'type':t} for k,t in {'requester':'string','acceptor':'string','accounts':'any','account_ids':'map(string)'}.items()},
      'resource':{'aws_vpc_peering_connection':{'this':{'provider':'aws.requester','vpc_id':vp('requester'),'peer_vpc_id':vp('acceptor'),'peer_owner_id':ex('var.account_ids[var.acceptor]'),'peer_region':'ap-northeast-2','auto_accept':False}},
      'aws_vpc_peering_connection_accepter':{'this':{'provider':'aws.acceptor','vpc_peering_connection_id':ex('aws_vpc_peering_connection.this.id'),'auto_accept':True}},
      'aws_route':{'requester':route('requester','acceptor'),'acceptor':route('acceptor','requester')}}}

def edge_module():
    r={
    'aws_security_group':{'alb':{'vpc_id':ex('var.inputs.vpc_id'),'ingress':[{'protocol':'tcp','from_port':80,'to_port':80,'prefix_list_ids':ex('[data.aws_ec2_managed_prefix_list.cloudfront.id]'),'cidr_blocks':[],'ipv6_cidr_blocks':[],'security_groups':[],'self':False,'description':'CloudFront origin only'}],'egress':[]}},
    'aws_security_group_rule':{'target_in':{'count':ex('length(var.inputs.control_sgs)'),'type':'ingress','security_group_id':ex('var.inputs.control_sgs[count.index]'),'source_security_group_id':ex('aws_security_group.alb.id'),'protocol':'tcp','from_port':8000,'to_port':8000},'target_out':{'count':ex('length(var.inputs.control_sgs)'),'type':'egress','security_group_id':ex('aws_security_group.alb.id'),'source_security_group_id':ex('var.inputs.control_sgs[count.index]'),'protocol':'tcp','from_port':8000,'to_port':8000}},
    'aws_lb':{'this':{'name':'p4-control','internal':False,'load_balancer_type':'application','security_groups':ex('[aws_security_group.alb.id]'),'subnets':ex('var.inputs.subnets'),'enable_deletion_protection':True}},
    'aws_lb_target_group':{'web':{'name':'p4-control','port':8000,'protocol':'HTTP','vpc_id':ex('var.inputs.vpc_id'),'health_check':[{'path':'/health','port':'traffic-port','protocol':'HTTP','matcher':'200'}]}},
    'aws_lb_target_group_attachment':{'web':{'count':ex('length(var.inputs.targets)'),'target_group_arn':ex('aws_lb_target_group.web.arn'),'target_id':ex('var.inputs.targets[count.index]'),'port':8000}},
    'aws_lb_listener':{'http':{'load_balancer_arn':ex('aws_lb.this.arn'),'port':80,'protocol':'HTTP','default_action':[{'type':'fixed-response','fixed_response':[{'content_type':'text/plain','message_body':'Forbidden','status_code':'403'}]}]}},
    'aws_lb_listener_rule':{'origin':{'listener_arn':ex('aws_lb_listener.http.arn'),'priority':10,'action':[{'type':'forward','target_group_arn':ex('aws_lb_target_group.web.arn')}],'condition':[{'http_header':[{'http_header_name':'X-P4-Origin','values':ex('[var.origin_header]')}]}]}},
    'aws_cloudfront_cache_policy':{'disabled':{'name':'p4-api-no-cache','min_ttl':0,'default_ttl':0,'max_ttl':0,'parameters_in_cache_key_and_forwarded_to_origin':[{'cookies_config':[{'cookie_behavior':'none'}],'headers_config':[{'header_behavior':'none'}],'query_strings_config':[{'query_string_behavior':'none'}]}]}},
    'aws_cloudfront_origin_request_policy':{'api':{'name':'p4-api-all-viewer','cookies_config':[{'cookie_behavior':'all'}],'headers_config':[{'header_behavior':'allViewer'}],'query_strings_config':[{'query_string_behavior':'all'}]}},
    'aws_cloudfront_distribution':{'this':{'enabled':True,'is_ipv6_enabled':True,'origin':[{'domain_name':ex('aws_lb.this.dns_name'),'origin_id':'control','custom_header':[{'name':'X-P4-Origin','value':ex('var.origin_header')}],'custom_origin_config':[{'http_port':80,'https_port':443,'origin_protocol_policy':'http-only','origin_ssl_protocols':['TLSv1.2']}]}],'default_cache_behavior':[{'target_origin_id':'control','viewer_protocol_policy':'redirect-to-https','allowed_methods':['GET','HEAD','OPTIONS','PUT','POST','PATCH','DELETE'],'cached_methods':['GET','HEAD'],'cache_policy_id':ex('aws_cloudfront_cache_policy.disabled.id'),'origin_request_policy_id':ex('aws_cloudfront_origin_request_policy.api.id')}],'restrictions':[{'geo_restriction':[{'restriction_type':'none'}]}],'viewer_certificate':[{'cloudfront_default_certificate':True}],'price_class':'PriceClass_200'}}}
    return {'terraform':V,'variable':{'inputs':{'type':'any'},'origin_header':{'type':'string','sensitive':True,'validation':[{'condition':ex('length(var.origin_header)>=32'),'error_message':'Origin header minimum 32 characters.'}]}},'data':{'aws_ec2_managed_prefix_list':{'cloudfront':{'name':'com.amazonaws.global.cloudfront.origin-facing'}}},'resource':r}

def identity_module():
    # Disabled by empty subjects; no guessed repo trust or default admin policy.
    loc={'subjects':ex('var.binding.oidc_subjects')}
    r={'aws_iam_openid_connect_provider':{'github':{'count':ex('length(local.subjects)>0 ? 1 : 0'),'url':'https://token.actions.githubusercontent.com','client_id_list':['sts.amazonaws.com']}},
       'aws_iam_role':{'ci':{'for_each':ex('local.subjects'),'name':ex('"p4-ci-${each.key}"'),'assume_role_policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action="sts:AssumeRoleWithWebIdentity",Principal={Federated=aws_iam_openid_connect_provider.github[0].arn},Condition={StringEquals={"token.actions.githubusercontent.com:aud"="sts.amazonaws.com","token.actions.githubusercontent.com:sub"=each.value}}}]})')}},
       'aws_iam_role_policy':{'ci':{'for_each':ex('local.subjects'),'role':ex('aws_iam_role.ci[each.key].id'),'policy':ex('jsonencode(var.binding.ci_policies[each.key])')}},
       'aws_iam_role':{}}
    r['aws_iam_role']={'ci':{'for_each':ex('local.subjects'),'name':ex('"p4-ci-${each.key}"'),'assume_role_policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action="sts:AssumeRoleWithWebIdentity",Principal={Federated=aws_iam_openid_connect_provider.github[0].arn},Condition={StringEquals={"token.actions.githubusercontent.com:aud"="sts.amazonaws.com","token.actions.githubusercontent.com:sub"=each.value}}}]})')},
       'foundation':{'name':'p4-foundation','assume_role_policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action="sts:AssumeRole",Principal={AWS=var.binding.provisioner_arns[var.account]}}]})')}}
    # Creation alone grants no infrastructure privileges; reviewed policy binding is required later.
    return {'terraform':V,'variable':{'binding':{'type':'any'},'account':{'type':'string'}},'locals':loc,'resource':r}

def roots():
    binding_var={'type':'any','validation':[{'condition':ex('var.binding.region=="ap-northeast-2" && length(distinct(values(var.binding.account_ids)))==5 && length(distinct(values(var.binding.az_ids)))==3'),'error_message':'Seoul, five accounts and three physical AZs required.'}]}
    common={'binding':binding_var}
    sizes={name:({'kafka':40,'spark':40,'elasticsearch':40,'postgresql':30,'mongodb':30}.get(n['role'],16)) for name,n in NODES.items()}
    for a in ACCOUNTS:
        root={'terraform':dict(V,backend={'s3':{}}),'variable':common,'provider':{'aws':{'region':'ap-northeast-2','allowed_account_ids':ex('[var.binding.account_ids.'+a+']')}},'module':{'account':{'source':'../../../modules/account','account':a,'binding':ex('var.binding'),'data_sizes':sizes},'identity':{'source':'../../../modules/identity','account':a,'binding':ex('var.binding')}},'output':{'foundation':{'value':ex('module.account.foundation')}}}
        if a=='encore1':root['module']['edge']={'source':'../../../modules/edge','inputs':ex('module.account.edge_inputs'),'origin_header':ex('var.binding.origin_header')}
        root['output']['runtime_artifact_boundary']={'value':ex('module.account.runtime_artifact_boundary')}
        put(TF/f'environments/dev/{a}/main.tf.json',root)
    providers=[{'alias':a,'region':'ap-northeast-2','allowed_account_ids':ex('[var.binding.account_ids.'+a+']')} for a in ACCOUNTS]
    con={'terraform':dict(V,backend={'s3':{}}),'variable':dict(common,accounts={'type':'any'}),'provider':{'aws':providers},'module':{},'resource':{},'output':{}}
    for a,b in TOPOLOGY['peering_pairs']:
        con['module'][a+'_'+b]={'source':'../../../modules/pair','providers':{'aws.requester':'aws.'+a,'aws.acceptor':'aws.'+b},'requester':a,'acceptor':b,'accounts':ex('var.accounts'),'account_ids':ex('var.binding.account_ids')}
    for a in ACCOUNTS[1:]:
        con['resource'].setdefault('aws_route53_vpc_association_authorization',{})[a]={'provider':'aws.encore1','for_each':ex('var.accounts.encore1.zones'),'zone_id':ex('each.value'),'vpc_id':ex('var.accounts.'+a+'.account.vpc_id'),'vpc_region':'ap-northeast-2'}
        con['resource'].setdefault('aws_route53_zone_association',{})[a]={'provider':'aws.'+a,'for_each':ex('var.accounts.encore1.zones'),'zone_id':ex('each.value'),'vpc_id':ex('var.accounts.'+a+'.account.vpc_id'),'vpc_region':'ap-northeast-2','depends_on':['aws_route53_vpc_association_authorization.'+a]}
    con['output']['foundation']={'value':dict({k:ex('var.binding.'+k) for k in ['environment','generation','example_only','az_ids']},schema_version='1.0.0',region='ap-northeast-2',component='connectivity',peering_pairs=TOPOLOGY['peering_pairs'])}
    put(TF/'environments/dev/connectivity/main.tf.json',con)
    boot={'terraform':dict(V,backend={'local':{'path':'../../.private/bootstrap.tfstate'}}),'variable':common,'provider':{'aws':{'region':'ap-northeast-2','allowed_account_ids':ex('[var.binding.account_ids.encore1]')}},'module':{'state':{'source':'../modules/bucket','name':ex('var.binding.state_bucket')}},'resource':{'aws_iam_role':{},'aws_iam_role_policy':{}},'output':{'state_bucket':{'value':ex('module.state.name')}}}
    boot['locals']=operation_locals()
    boot['locals']['operation_runtime_arns']=ex('[for name,n in '+json.dumps(NODES)+':"arn:aws:iam::${var.binding.account_ids[n.account]}:role/p4-runtime-${name}"]')
    boot['module']['state']['statements']=ex('[{Effect="Allow",Principal={AWS=[for id in values(var.binding.account_ids):"arn:aws:iam::${id}:root"]},Action='+json.dumps(operation_actions())+',Resource=[local.operation_arn],Condition={ArnEquals={"aws:PrincipalArn"=concat(local.operation_runtime_arns,[local.operation_controller_arn])}}},{Effect="Deny",Principal="*",Action=["s3:DeleteObject","s3:DeleteObjectVersion"],Resource=[local.operation_arn]},{Effect="Deny",Principal="*",Action=["s3:PutObject"],Resource=[local.operation_arn],Condition={Null={"s3:if-match"="true","s3:if-none-match"="true"}}}]')
    boot['resource']['aws_iam_role']['operation']={'name':'p4-operation-controller','assume_role_policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action="sts:AssumeRole",Principal={AWS="arn:aws:iam::${var.binding.account_ids.encore1}:root"},Condition={ArnEquals={"aws:PrincipalArn"=concat([var.binding.provisioner_arns.encore1],[for purpose in keys(var.binding.oidc_subjects):"arn:aws:iam::${var.binding.account_ids.encore1}:role/p4-ci-${purpose}"])}}}]})')}
    boot['resource']['aws_iam_role_policy']['operation']={'role':ex('aws_iam_role.operation.id'),'policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action='+json.dumps(operation_actions())+',Resource=[local.operation_arn]}]})')}
    boot['output']['operation_boundary']={'value':{'bucket':ex('var.binding.state_bucket'),'key':ex('local.operation_key'),'controller_role_arn':ex('local.operation_controller_arn')}}
    for a in [*ACCOUNTS,'connectivity']:
        key=f'dev/{a}/terraform.tfstate'
        source='encore1' if a=='connectivity' else a
        boot['resource']['aws_iam_role'][a]={'name':'p4-state-'+a,'assume_role_policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action="sts:AssumeRole",Principal={AWS="arn:aws:iam::${var.binding.account_ids.'+source+'}:root"},Condition={ArnEquals={"aws:PrincipalArn"=concat([var.binding.provisioner_arns.'+source+'],[for purpose in keys(var.binding.oidc_subjects):"arn:aws:iam::${var.binding.account_ids.'+source+'}:role/p4-ci-${purpose}" if contains(["plan","apply"],purpose)])}}}]})')}
        boot['resource']['aws_iam_role_policy'][a]={'role':ex('aws_iam_role.'+a+'.id'),'policy':ex('jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action=["s3:ListBucket"],Resource=["arn:aws:s3:::${var.binding.state_bucket}"],Condition={StringEquals={"s3:prefix"=["'+key+'","'+key+'.tflock"]}}},{Effect="Allow",Action=["s3:GetObject","s3:PutObject"],Resource=["arn:aws:s3:::${var.binding.state_bucket}/'+key+'"]},{Effect="Allow",Action=["s3:GetObject","s3:PutObject","s3:DeleteObject"],Resource=["arn:aws:s3:::${var.binding.state_bucket}/'+key+'.tflock"]}]})')}
    put(TF/'bootstrap/main.tf.json',boot)

def build():
    for name,f in [('bucket',bucket_module),('account',account_module),('pair',pair_module),('edge',edge_module),('identity',identity_module)]: put(TF/f'modules/{name}/main.tf.json',f())
    roots()
    # Explicit per-account profiles are used only by the separately enabled real runner.
    for path in [TF/'bootstrap/main.tf.json',*(TF/'environments/dev').glob('*/main.tf.json')]:
        conf=json.loads(path.read_text());providers=conf['provider']['aws']
        if isinstance(providers,list):
            for provider in providers:provider['profile']=provider['alias']
        else:providers['profile']='encore1' if path.parent.name=='bootstrap' else path.parent.name
        put(path,conf)
if __name__=='__main__':build()
