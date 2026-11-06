#!/usr/bin/env python3
"""No credentials, no backend, all AWS providers mocked; real exporter consumes test states."""
import argparse, os, shutil, subprocess, sys, tempfile, uuid
from datetime import datetime,timezone
from pathlib import Path
from model import *
from export_inventory import export,write_inventory
TF=FOUNDATION/'terraform'

def environment(work):
    env={k:v for k,v in os.environ.items() if not k.startswith(('AWS_','TF_')) and k not in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','http_proxy','https_proxy','all_proxy','no_proxy']}
    env.update(AWS_CONFIG_FILE=os.devnull,AWS_SHARED_CREDENTIALS_FILE=os.devnull,AWS_EC2_METADATA_DISABLED='true',
               CHECKPOINT_DISABLE='1',TF_IN_AUTOMATION='1',TF_INPUT='0',TF_CLI_CONFIG_FILE=str(work/'terraform.rc'),
               HTTPS_PROXY='http://127.0.0.1:9',HTTP_PROXY='http://127.0.0.1:9',ALL_PROXY='http://127.0.0.1:9',NO_PROXY='')
    (work/'terraform.rc').write_text('disable_checkpoint = true\n')
    return env

def run(command,env,log):
    p=subprocess.run(command,env=env,capture_output=True,text=True)
    log.write_text(p.stdout+p.stderr)
    if p.returncode:
        print((p.stdout+p.stderr)[-6500:],file=sys.stderr)
        raise ContractError(f'command failed ({p.returncode}); log: {log}')
    return p.stdout

def state_from_test(text):
    events=[json.loads(s) for s in text.splitlines() if s.strip()]
    states=[e['test_plan'] for e in events if e.get('type')=='test_plan']
    require(len(states)==1,'test state required; no fabricated output fallback')
    summary=[e['test_summary'] for e in events if e.get('type')=='test_summary']
    require(len(summary)==1 and summary[0]['passed']==1 and not summary[0]['failed'] and not summary[0]['errored'],'complete mock test required')
    plan=states[0]
    outputs={}
    def tf_type(value):
        if isinstance(value,bool):return 'bool'
        if isinstance(value,str):return 'string'
        if isinstance(value,(int,float)):return 'number'
        if isinstance(value,list):return ['tuple',[tf_type(v) for v in value]]
        if isinstance(value,dict):return ['object',{k:tf_type(v) for k,v in value.items()}]
        return 'dynamic'
    def unknown(v):
        if isinstance(v,dict):return any(unknown(x) for x in v.values())
        if isinstance(v,list):return any(unknown(x) for x in v)
        return v is True
    for name,change in plan['output_changes'].items():
        require(not unknown(change.get('after_unknown')),'unknown output cannot be exported')
        outputs[name]={'value':change['after'],'sensitive':change.get('after_sensitive',False),'type':tf_type(change['after'])}
    # Terraform test verbose JSON exposes planned resources as changes, not a state file.
    # Adapt that public representation to the audit/export envelope without fabricated values.
    managed=[{'address':r['address'],'type':r['type'],'mode':r['mode'],'values':r['change']['after']} for r in plan['resource_changes'] if r['change']['after'] is not None]
    return {'values':{'outputs':outputs,'root_module':{'resources':managed}}}

def resources(state):
    def walk(m):
        return m.get('resources',[])+[r for c in m.get('child_modules',[]) for r in walk(c)]
    return walk(state['values']['root_module'])

def render(output):
    tool=FOUNDATION/'.cache/toolchain/terraform'
    mirror=FOUNDATION/'.cache/provider-mirror'
    require(tool.is_file() and mirror.is_dir(),'pinned toolchain/provider mirror missing; run setup-local.py')
    require(json.loads(subprocess.check_output([str(tool),'version','-json'],text=True))['terraform_version']=='1.11.4','wrong Terraform version')
    artifact_root=FOUNDATION/'.artifacts';artifact_root.mkdir(exist_ok=True)
    artifacts=artifact_root/'runs'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]);artifacts.mkdir(parents=True)
    (artifact_root/'latest-run.json').write_text(json.dumps({'logs':str(artifacts.relative_to(FOUNDATION))})+'\n')
    print('검증 로그: '+str(artifacts),flush=True)
    with tempfile.TemporaryDirectory(prefix='foundation-mock-',dir=FOUNDATION/'.cache') as temp:
        work=Path(temp);shutil.copytree(TF,work/'terraform',ignore=shutil.ignore_patterns('.terraform','*.tfstate*','*.tfplan'))
        env=environment(work)
        run([str(tool),'fmt','-check','-recursive',str(work/'terraform')],env,artifacts/'fmt.log')
        parts=[];accounts={};states={}
        for a in [*ACCOUNTS,'connectivity','bootstrap']:
            root=work/'terraform'/('bootstrap' if a=='bootstrap' else f'environments/dev/{a}')
            require((root/'tests/offline.tftest.hcl').is_file(),'missing mock suite')
            if a=='connectivity':
                # Replace just the test binding of accounts with outputs from the five previous runs.
                # Dependency values come exclusively from the five actual mock plans above.
                private=work/'accounts.tfvars.json';private.write_text(json.dumps({'accounts':accounts}))
            run([str(tool),'-chdir='+str(root),'init','-backend=false','-input=false','-lockfile=readonly','-plugin-dir='+str(mirror)],env,artifacts/(a+'-init.log'))
            run([str(tool),'-chdir='+str(root),'validate','-no-color'],env,artifacts/(a+'-validate.log'))
            args=[str(tool),'-chdir='+str(root),'test','-json','-verbose']
            if a=='connectivity':args+=['-var-file='+str(private)]
            raw=run(args,env,artifacts/(a+'-test.jsonl'));state=state_from_test(raw);states[a]=state
            if a=='bootstrap':continue
            wrapped=state['values']['outputs']['foundation']
            path=work/(a+'.json');path.write_text(json.dumps({'foundation':wrapped}));parts.append(path)
            if a in ACCOUNTS:accounts[a]=wrapped['value']
        inv=export(parts);write_inventory(inv,output)
        # Actual producer output compared with the independent common consumer example.
        example=read_json(ROOT/'aws-infra/contracts/runtime-plan.example.json')
        for k in ['environment','generation','example_only']:example[k]=inv[k]
        validate_runtime_plan(example,inv,validate_services(SERVICES,inv))
        from security import audit,mutation_checks
        audit(states)
        mutation_checks(states)
        from security import audit_operation
        audit_operation(states,fixture_binding())
        import copy
        for layer in ['identity','endpoint','bucket']:
            changed=copy.deepcopy(states)
            if layer=='identity':
                target=next(r for r in resources(changed['encore1']) if r['type']=='aws_iam_role_policy' and '.operation[' in r['address'])
                target['values']['policy']=json.dumps({'Statement':[]})
            elif layer=='endpoint':
                target=next(r for r in resources(changed['encore1']) if r['type']=='aws_vpc_endpoint' and r['values']['vpc_endpoint_type']=='Gateway')
                value=json.loads(target['values']['policy']);value['Statement']=[s for s in value['Statement'] if 'operations/' not in json.dumps(s)];target['values']['policy']=json.dumps(value)
            else:
                target=next(r for r in resources(changed['bootstrap']) if r['type']=='aws_s3_bucket_policy')
                value=json.loads(target['values']['policy']);next(s for s in value['Statement'] if s['Effect']=='Allow')['Condition']['ArnEquals']['aws:PrincipalArn'].append('arn:aws:iam::111111111111:role/unapproved');target['values']['policy']=json.dumps(value)
            try:audit_operation(changed,fixture_binding())
            except ContractError:pass
            else:raise ContractError('operation '+layer+' policy mutation accepted')
        # Exercise the normally disabled SSM/OIDC branches with an explicit fixture binding.
        bound=fixture_binding()
        from dispatcher_test import artifact_parts,test_dispatcher
        import hashlib
        artifact,manifest=artifact_parts()
        bound['artifact']={'key':'artifacts/runtime-v1.tar.gz','version':'mock-version-1','sha256':hashlib.sha256(artifact).hexdigest(),'manifest_sha256':hashlib.sha256(manifest).hexdigest()}
        bound['controller_instance_ids']={a:[node['instance_id'] for node in accounts[a]['nodes']] for a in ACCOUNTS}
        for purpose in ['plan','apply','runtime']:
            bound['oidc_subjects'][purpose]=f'repo:example@123/foundation@456:environment:infra-{purpose}'
            bound['ci_policies'][purpose]={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:GetObject'],'Resource':['arn:aws:s3:::example-only-foundation-state/dev/encore1/terraform.tfstate']}]}
        validate_binding(bound)
        bound_states={}
        for account in ACCOUNTS:
            root=work/('terraform/environments/dev/'+account)
            test=root/'tests/offline.tftest.hcl'
            original=test.read_text();start=original.index('variables {');end=original.index('run "offline"',start)
            test.write_text(original[:start]+'variables { binding = '+json.dumps(bound)+' }\n'+original[end:])
            raw=run([str(tool),'-chdir='+str(root),'test','-json','-verbose'],env,artifacts/('bound-'+account+'-artifact-controller-test.jsonl'))
            bound_states[account]=state_from_test(raw)
        from security import audit_bound,audit_transfer,bound_mutation_checks
        audit_bound(bound_states['encore1'],bound)
        bound_mutation_checks(bound_states['encore1'],bound)
        audit_transfer(bound_states,bound)
        import copy
        wrong_ids=copy.deepcopy(bound)
        wrong_ids['controller_instance_ids']['encore1'][0],wrong_ids['controller_instance_ids']['encore2'][0]=wrong_ids['controller_instance_ids']['encore2'][0],wrong_ids['controller_instance_ids']['encore1'][0]
        validate_binding(wrong_ids)  # Shape alone cannot prove ownership; evaluated resources must reject it.
        root=work/'terraform/environments/dev/encore1';test=root/'tests/offline.tftest.hcl'
        original=test.read_text();start=original.index('variables {');end=original.index('run "offline"',start)
        test.write_text(original[:start]+'variables { binding = '+json.dumps(wrong_ids)+' }\n'+original[end:])
        refused=subprocess.run([str(tool),'-chdir='+str(root),'test','-json','-verbose'],env=env,capture_output=True,text=True)
        (artifacts/'controller-wrong-instance-set.jsonl').write_text(refused.stdout+refused.stderr)
        require(refused.returncode!=0 and 'Controller prefixes must equal this account actual EC2 instance set' in refused.stdout,'stale/cross-account controller instance binding accepted')
        print('PASS: evaluated Terraform precondition rejects swapped cross-account controller IDs')
        test_dispatcher(bound_states['encore1'],bound,work)
        from track2_integration import exercise
        exercise(work,env,artifacts,tool,parts,bound)
        require(export(list(reversed(parts)))==inv,'order-dependent exporter')
        # Exporter must preserve a previous valid inventory on failed replacement.
        before=Path(output).read_bytes()
        broken=read_json(parts[0]);broken['foundation']['value']['generation']='mixed-generation';parts[0].write_text(json.dumps(broken))
        try:write_inventory(export(parts),output)
        except ContractError:pass
        else:raise ContractError('mixed generation accepted')
        require(Path(output).read_bytes()==before,'failed export overwrote valid inventory')
    print('PASS: seven mocked roots, 16 EC2, 10 peering pairs, exporter, policy audit, common runtime-plan stand-in')
    return inv

def main():
    p=argparse.ArgumentParser();p.add_argument('--render',action='store_true');p.add_argument('--output',type=Path);a=p.parse_args()
    try:
        output=a.output or FOUNDATION/'.artifacts/inventory.mock.json'
        render(output)
        if not a.render:
            latest=read_json(FOUNDATION/'.artifacts/latest-run.json')
            run([sys.executable,str(ROOT/'scripts/required_suite.py'),'--start',str(FOUNDATION/'tests'),'--pattern','test_wrapper.py'],os.environ.copy(),FOUNDATION/latest['logs']/'wrapper.log')
            print('PASS: fake CLI wrapper failure/restart checks')
    except (ValueError,KeyError,OSError,subprocess.SubprocessError) as exc:print(f'FAIL: {exc}',file=sys.stderr);return 2
    return 0
if __name__=='__main__':sys.exit(main())
