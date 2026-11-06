"""Write test-only overrides, including all provider aliases and AWS data sources."""
from model import *

def write_tests():
    b=fixture_binding()
    roots=[*ACCOUNTS,'connectivity','bootstrap']
    for index,a in enumerate(roots,1):
        root=FOUNDATION/'terraform'/('bootstrap' if a=='bootstrap' else f'environments/dev/{a}')
        p=root/'tests'; p.mkdir(exist_ok=True)
        content=[]
        aliases=ACCOUNTS if a=='connectivity' else [None]
        for alias in aliases:
            defaults={'aws_iam_role':{'arn':'arn:aws:iam::111111111111:role/p4-mock'},
                      'aws_iam_openid_connect_provider':{'arn':'arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com'},
                      'aws_ssm_document':{'arn':'arn:aws:ssm:ap-northeast-2:111111111111:document/p4-runtime-artifact','document_version':'1','hash':'b'*64,'hash_type':'Sha256'},
                      'aws_lb':{'arn':'arn:aws:elasticloadbalancing:ap-northeast-2:111111111111:loadbalancer/app/mock/1234567890123456','dns_name':'mock.ap-northeast-2.elb.amazonaws.com'},
                      'aws_lb_target_group':{'arn':'arn:aws:elasticloadbalancing:ap-northeast-2:111111111111:targetgroup/mock/1234567890123456'},
                      'aws_lb_listener':{'arn':'arn:aws:elasticloadbalancing:ap-northeast-2:111111111111:listener/app/mock/1234567890123456/1234567890123456'},
                      'aws_subnet':{'availability_zone':'ap-northeast-2a'},
                      'aws_s3_bucket':{'arn':'arn:aws:s3:::example-only-mock'}}
            content.append('mock_provider "aws" {\n override_during = plan\n'+(f'  alias = "{alias}"\n' if alias else '')+'\n'.join(' mock_resource "'+t+'" {\n defaults = '+json.dumps(v)+'\n }' for t,v in defaults.items())+'\n}\n')
        variables={'binding':b}
        if a in ACCOUNTS:
            content.append('override_data {\n override_during = plan\n target = module.account.data.aws_caller_identity.this\n values = { account_id = "'+b['account_ids'][a]+'" }\n}\n')
            overrides={'module.account.aws_vpc.this':{'id':f'vpc-{index:017x}'},'module.account.aws_vpc_endpoint.s3':{'id':f'vpce-{index:017x}','prefix_list_id':'pl-00000001'}}
            for ni,(name,node) in enumerate(NODES.items(),1):
                if node['account']==a:overrides[f'module.account.aws_instance.host["{name}"]']={'id':f'i-{ni:017x}'}
            for ri,k in enumerate([f'{kind}-{s}' for kind in ['private','public'] for s in 'abc'],1):
                overrides[f'module.account.aws_route_table.this["{k}"]']={'id':f'rtb-{index*10+ri:017x}'}
                # Deliberately rotate AZ names by account: only physical AZ IDs are shared.
                slot=k[-1]
                overrides[f'module.account.aws_subnet.this["{k}"]']={'id':f'subnet-{index*10+ri:017x}','availability_zone':'ap-northeast-2'+'abc'[(index-1+'abc'.index(slot))%3]}
            if a=='encore1':
                content.append('override_data {\n override_during = plan\n target = module.edge.data.aws_ec2_managed_prefix_list.cloudfront\n values = { id = "pl-00000002" }\n}\n')
                for purpose,name in b['bucket_names'].items():overrides[f'module.account.module.{purpose}[0].aws_s3_bucket.this']={'id':name,'arn':'arn:aws:s3:::'+name}
                for ei,s in enumerate(['ssm','ssmmessages'],1):
                    overrides[f'module.account.aws_route53_zone.ssm["{s}"]']={'zone_id':f'ZMOCK{ei}','name':f'{s}.ap-northeast-2.amazonaws.com'}
                    overrides[f'module.account.aws_vpc_endpoint.ssm["{s}"]']={'dns_entry':[{'dns_name':f'{s}.mock.invalid','hosted_zone_id':f'ZMOCK{ei}'}]}
            for target,values in overrides.items():content.append('override_resource {\n override_during = plan\n target = '+target+'\n values = '+json.dumps(values)+'\n}\n')
        if a=='bootstrap':content.append('override_resource {\n override_during = plan\n target = module.state.aws_s3_bucket.this\n values = '+json.dumps({'id':b['state_bucket'],'arn':'arn:aws:s3:::'+b['state_bucket']})+'\n}\n')
        content.append('variables {\n'+'\n'.join(k+' = '+json.dumps(v,indent=2) for k,v in variables.items())+'\n}\n')
        assertions=[]
        if a in ACCOUNTS:
            assertions.append(('length(output.foundation.nodes)=='+str(sum(n['account']==a for n in NODES.values())),'Approved node count required.'))
            assertions.append(('output.foundation.example_only && output.foundation.generation=="foundation-mock-v1"','Fixture metadata required.'))
        elif a=='connectivity':assertions.append(('length(output.foundation.peering_pairs)==10','Ten direct pairs required.'))
        else:assertions.append(('output.state_bucket=="example-only-foundation-state"','State bucket must be isolated.'))
        content.append('run "offline" {\n command = plan\n'+'\n'.join(' assert {\n condition = '+c+'\n error_message = "'+m+'"\n }' for c,m in assertions)+'\n}\n')
        (p/'offline.tftest.hcl').write_text('\n'.join(content))
if __name__=='__main__':write_tests()
