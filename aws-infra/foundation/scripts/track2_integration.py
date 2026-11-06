"""Exercise opt-in resources using the pinned provider mock, never a real plan."""
import copy
import json
import subprocess
from model import ACCOUNTS, FOUNDATION, fixture_binding, require, ContractError, validate_binding
from integration import run, state_from_test
from export_inventory import export, write_inventory
from export_track2 import export_track2
from track2 import fixture_track2
from track2_audit import audit_edge, audit_storage, edge_mutations

def exercise(work,env,artifacts,tool,original_parts,base_binding):
    binding=copy.deepcopy(base_binding); binding['track2']=fixture_track2()
    validate_binding(binding)
    originals={}; states={}; paths=[]
    def configure(account,value):
        root=work/('terraform/environments/dev/'+account)
        test=root/'tests/offline.tftest.hcl'
        if account not in originals: originals[account]=test.read_text()
        original=originals[account]; start=original.index('variables {'); end=original.index('run "offline"',start)
        overrides=''
        if account=='encore1':
            for target,name in [('module.account.module.observability_archive[0]',value['track2']['archive']['bucket']),('module.edge.module.media[0]',value['track2']['media']['bucket'])]:
                overrides+='override_resource {\n override_during = plan\n target = '+target+'.aws_s3_bucket.this\n values = '+json.dumps({'id':name,'arn':'arn:aws:s3:::'+name})+'\n}\n'
            overrides+='override_resource {\n override_during = plan\n target = module.edge.aws_cloudfront_distribution.this\n values = { arn = "arn:aws:cloudfront::111111111111:distribution/EXAMPLETRACK2" }\n}\n'
        test.write_text(original[:start]+overrides+'variables { binding = '+json.dumps(value)+' }\n'+original[end:])
        return root
    for account in ACCOUNTS:
        root=configure(account,binding)
        raw=run([str(tool),'-chdir='+str(root),'test','-json','-verbose'],env,artifacts/('track2-'+account+'.jsonl'))
        state=state_from_test(raw); states[account]=state
        outputs=state['values']['outputs']
        path=work/('track2-'+account+'.json')
        path.write_text(json.dumps({k:outputs[k] for k in ['foundation','foundation_track2']})); paths.append(path)
    paths.append(original_parts[-1]) # connectivity remains the common v1 producer.
    inventory=export(paths); sidecar=export_track2(paths,inventory)
    write_inventory(inventory,artifacts/'track2-inventory.mock.json')
    write_inventory(sidecar,artifacts/'track2-sidecar.mock.json')
    require(set(inventory['storage'])=={'raw_bucket_ref','lake_bucket_ref','backup_bucket_ref','state_bucket_ref','transfer_bucket_ref'},'Track2 leaked into common inventory v1')
    audit_storage(states,binding); audit_edge(states['encore1'],binding['track2']); edge_mutations(states['encore1'],binding['track2'])
    # Exporter refuses output drift and keeps the prior file intact on failure.
    original=paths[0].read_text(); before=(artifacts/'track2-sidecar.mock.json').read_bytes()
    mutations=[lambda v:v.update(generation='other-generation'),lambda v:v.update(example_only=False),
               lambda v:v.update(ready_for_aws=True),lambda v:v.update(unknown='field'),
               lambda v:v.update(endpoint_id=binding['track2']['endpoint_ids']['encore2']),
               lambda v:v.update(media_bucket_ref='real-looking-bucket'),lambda v:v.pop('driver_queries')]
    for mutate in mutations:
        raw=json.loads(original); mutate(raw['foundation_track2']['value']); paths[0].write_text(json.dumps(raw))
        try:write_inventory(export_track2(paths,inventory),artifacts/'track2-sidecar.mock.json')
        except (ContractError,KeyError):pass
        else:raise ContractError('Track2 exporter mutation accepted')
        require((artifacts/'track2-sidecar.mock.json').read_bytes()==before,'failed sidecar export overwrote previous output')
    paths[0].write_text(original)
    for invalid_paths in [paths[:-2]+paths[-1:],paths+[paths[0]]]:
        try:export_track2(invalid_paths,inventory)
        except ContractError:pass
        else:raise ContractError('missing/duplicate Track2 output accepted')
    require(export_track2(list(reversed(paths)),inventory)==sidecar,'sidecar order dependency')
    print('PASS: common inventory-v1 unchanged; Track2 sidecar missing/duplicate/metadata/fixture/endpoint/new-field drift refused')
    # TLS is a configuration candidate, never an AWS readiness claim.
    tls=copy.deepcopy(binding)
    tls['track2']['tls'].update(enabled=True,origin_name='origin.example.org',certificate_arn='arn:aws:acm:ap-northeast-2:111111111111:certificate/12345678-1234-1234-1234-123456789abc',control_tls_ready=True)
    validate_binding(tls)
    root=configure('encore1',tls)
    raw=run([str(tool),'-chdir='+str(root),'test','-json','-verbose'],env,artifacts/'track2-full-tls.jsonl')
    full=state_from_test(raw); audit_edge(full,tls['track2'])
    require(full['values']['outputs']['foundation_track2']['value']['tls_status']=='CONFIGURED_NOT_VERIFIED','TLS readiness overclaimed')
    bad_cases=[('alb-default-dns',lambda b:b['track2']['tls'].update(origin_name='mock.ap-northeast-2.elb.amazonaws.com')),
               ('missing-certificate',lambda b:b['track2']['tls'].update(certificate_arn='')),
               ('control-tls-unready',lambda b:b['track2']['tls'].update(control_tls_ready=False)),
               ('certificate-account',lambda b:b['track2']['tls'].update(certificate_arn=b['track2']['tls']['certificate_arn'].replace('111111111111','999999999999'))),
               ('ttl-unapproved',lambda b:b['track2']['media'].update(ttl_approved=False)),
               ('bucket-collision',lambda b:b['track2']['media'].update(bucket=b['state_bucket'])),
               ('foreign-endpoint',lambda b:b['track2']['endpoint_ids'].update(encore1='vpce-fffffffffffffffff')),
               ('oversized-policy',lambda b:b['track2']['driver_queries'].update({'control-a':['venues-'+str(i)+'-'+'x'*100 for i in range(24)]})),
               ('spark-checkpoint-owner',lambda b:b['track2']['driver_queries'].update({'spark-01':['venues-catalog']}))]
    for name,mutate in bad_cases:
        bad=copy.deepcopy(tls);mutate(bad);root=configure('encore1',bad)
        result=subprocess.run([str(tool),'-chdir='+str(root),'test','-json'],env=env,capture_output=True,text=True)
        (artifacts/('track2-refused-'+name+'.jsonl')).write_text(result.stdout+result.stderr)
        events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        diagnostics=[e.get('diagnostic',{}).get('summary') for e in events if e.get('type')=='diagnostic']
        require(result.returncode!=0 and any(v in {'Invalid value for variable','Resource postcondition failed','Resource precondition failed'} for v in diagnostics), 'invalid Track2 input not specifically refused: '+name)
    # Optional resources can be disabled while retaining venues access.
    minimal=copy.deepcopy(binding)
    from track2 import DEFAULT
    minimal['track2']['media']=copy.deepcopy(DEFAULT['media']);minimal['track2']['archive']=copy.deepcopy(DEFAULT['archive'])
    root=configure('encore1',minimal)
    raw=run([str(tool),'-chdir='+str(root),'test','-json','-verbose'],env,artifacts/'track2-optional-disabled.jsonl')
    state=state_from_test(raw);audit_edge(state,minimal['track2'])
    from integration import resources
    require(not any(r['type'] in {'aws_cloudfront_origin_access_control','aws_cloudfront_function'} or 'observability_archive' in r['address'] or 'module.media[' in r['address'] for r in resources(state)), 'disabled optional resources still created')
    for account,original in originals.items():
        (work/('terraform/environments/dev/'+account+'/tests/offline.tftest.hcl')).write_text(original)
    print(f'PASS: full TLS configuration candidate, {len(bad_cases)} native Terraform refusal cases, optional bucket/OAC absence')
