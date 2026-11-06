#!/usr/bin/env python3
"""Combine six Terraform `output -json` documents; fail closed before writing."""
import argparse, json, os, sys, tempfile
from pathlib import Path
from model import *

def export(paths):
    parts={}
    for path in paths:
        raw=read_json(path)
        require('foundation' in raw and set(raw)<={'foundation','runtime_artifact_boundary','foundation_track2'},'foundation output required; unknown outputs rejected')
        wrapped=raw['foundation']
        require(wrapped.get('sensitive') is False and 'type' in wrapped and 'value' in wrapped,'invalid terraform output envelope')
        part=wrapped['value']; key=part['component']
        require(key in [*ACCOUNTS,'connectivity'] and key not in parts,'duplicate/unknown component')
        parts[key]=part
    require(set(parts)==set([*ACCOUNTS,'connectivity']),'six components required')
    common=['schema_version','environment','generation','example_only','region','az_ids']
    first=parts['connectivity']
    for p in parts.values():
        require(all(p[k]==first[k] for k in common),'generation/fixture/metadata mismatch')
    inv={k:first[k] for k in common}
    inv['accounts']={a:parts[a]['account'] for a in ACCOUNTS}
    inv['nodes']=sorted([n for a in ACCOUNTS for n in parts[a]['nodes']],key=lambda n:n['name'])
    for a in ACCOUNTS:
        require(all(n['account']==a for n in parts[a]['nodes']),'foreign account output')
    inv['peering_pairs']=sorted([sorted(p) for p in first['peering_pairs']])
    inv['storage']=parts['encore1']['storage']
    inv['secret_refs']={k:f'runtime-secret:{k}' for k in ['postgresql','mongodb','kafka','elasticsearch','zookeeper','llm_service','ghcr_read']}
    validate_inventory(inv)
    # Prevent individually schema-valid fixture IDs from leaking into real inventories.
    serialized=json.dumps(inv)
    fixture_markers=['example-only-','i-000000000000000','vpc-000000000000000','vpce-000000000000000']
    if inv['example_only']:
        require(all(v.startswith('example-only-') for v in inv['storage'].values()),'fixture storage mixed with real values')
        require(all(n['instance_id'].startswith('i-000000000000000') for n in inv['nodes']),'fixture instance values mixed')
        for details in inv['accounts'].values():
            require(details['vpc_id'].startswith('vpc-000000000000000') and details['s3_endpoint_id'].startswith('vpce-000000000000000'),'fixture account values mixed')
    else:
        require(not any(s in serialized for s in fixture_markers),'real/fixture values mixed')
    return inv

def write_inventory(inv,output):
    output=Path(output).resolve()
    if not inv['example_only']:
        require('.private' in output.parts,'real inventory must be under .private')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',dir=output.parent,delete=False) as f:
        os.chmod(f.name,0o600); json.dump(inv,f,indent=2,sort_keys=True); f.write('\n'); temp=f.name
    os.replace(temp,output)

def main():
    p=argparse.ArgumentParser(); p.add_argument('--input',type=Path,action='append',required=True); p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    try: write_inventory(export(a.input),a.output)
    except (ValueError,KeyError,TypeError,OSError):
        print('FAIL: invalid/incomplete Terraform output or unsafe inventory destination',file=sys.stderr); return 2
    return 0
if __name__=='__main__': sys.exit(main())
