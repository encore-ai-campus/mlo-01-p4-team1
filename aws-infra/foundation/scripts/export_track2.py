#!/usr/bin/env python3
"""Separate Foundation output; never merges optional purposes into inventory-v1."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from model import ACCOUNTS, FOUNDATION, read_json, require, validate_inventory
from export_inventory import export, write_inventory

def inventory_digest(inventory):
    return hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def export_track2(paths, inventory):
    validate_inventory(inventory)
    parts = {}
    base = {'schema_version','component','enabled','environment','generation','example_only','region',
            'dataset_version','driver_queries','ingest_nodes','endpoint_id','archive_bucket_ref','ready_for_aws'}
    edge = {'media_bucket_ref','distribution_arn','tls_status'}
    for path in paths:
        raw = read_json(path)
        if raw['foundation']['value']['component'] == 'connectivity':
            require('foundation_track2' not in raw, 'connectivity must not produce a Track2 account')
            continue
        require('foundation_track2' in raw, 'five Track2 outputs required')
        wrapped = raw['foundation_track2']
        require(wrapped.get('sensitive') is False and 'type' in wrapped, 'invalid Track2 envelope')
        part = wrapped['value']; account = part['component']
        require(account in ACCOUNTS and account not in parts, 'duplicate/unknown Track2 account')
        require(account == raw['foundation']['value']['component'], 'Track2 component/envelope mismatch')
        require(set(part) == base | (edge if account == 'encore1' else set()), 'unknown/missing Track2 fields')
        require(part['schema_version'] == 'foundation-track2/1' and part['ready_for_aws'] is False, 'invalid Track2 version/readiness')
        require(type(part['enabled']) is bool, 'invalid Track2 enabled flag')
        for key in ['environment','generation','example_only','region']:
            require(type(part[key]) is type(inventory[key]) and part[key] == inventory[key], 'Track2 metadata/generation/fixture mismatch')
        require(part['endpoint_id'] == inventory['accounts'][account]['s3_endpoint_id'], 'Track2 endpoint/account mismatch')
        parts[account] = part
    require(set(parts) == set(ACCOUNTS), 'five Track2 accounts required')
    first = parts['encore1']
    for part in parts.values():
        require(all(part[k] == first[k] for k in ['enabled','dataset_version','driver_queries','ingest_nodes','archive_bucket_ref']), 'mixed Track2 configuration')
    optional = [first[k] for k in ['media_bucket_ref','archive_bucket_ref'] if first[k] is not None]
    import re
    require(all(isinstance(v,str) and re.fullmatch('[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',v) for v in optional), 'invalid optional bucket ref')
    require(isinstance(first['distribution_arn'],str) and re.fullmatch(r'arn:aws:cloudfront::[0-9]{12}:distribution/[A-Z0-9]+',first['distribution_arn']), 'invalid distribution ARN')
    require(isinstance(first['ingest_nodes'],list) and len(first['ingest_nodes'])==len(set(first['ingest_nodes'])) and set(first['ingest_nodes'])<={'control-a','control-b'}, 'invalid ingest owner')
    require(len(set(optional + list(inventory['storage'].values()))) == len(optional) + len(inventory['storage']), 'optional bucket collision')
    if first['example_only']:
        require(all(v.startswith('example-only-') for v in optional), 'fixture optional storage mixed with real values')
    else:
        require(not any('example-only-' in v for v in optional), 'real optional storage mixed with fixture')
    require(first['tls_status'] in {'CONFIGURED_NOT_VERIFIED','UNRESOLVED_ORIGIN_NAME_CERTIFICATE','VIEWER_HTTPS_ORIGIN_HTTP'}, 'unknown TLS status')
    if first['enabled']:
        require(isinstance(first['dataset_version'], str) and re.fullmatch('[a-z0-9][a-z0-9-]{0,62}', first['dataset_version']), 'invalid output dataset')
        require(set(first['driver_queries']) <= {'control-a','control-b'} and bool(first['driver_queries']), 'invalid output Driver owners')
        require(all(isinstance(qs,list) and qs and len(qs)==len(set(qs)) and all(re.fullmatch('venues-[a-z0-9-]+',q) for q in qs) for qs in first['driver_queries'].values()), 'invalid output queries')
    else:
        require(not first['dataset_version'] and not first['driver_queries'] and not first['ingest_nodes'] and not optional, 'disabled output contains active configuration')
    result = {'schema_version':'foundation-track2/1', **{k:inventory[k] for k in ['environment','generation','example_only','region']},
            'inventory_sha256':inventory_digest(inventory), 'enabled':first['enabled'],
            'dataset_version':first['dataset_version'], 'driver_queries':first['driver_queries'], 'ingest_nodes':first['ingest_nodes'],
            'media_bucket_ref':first['media_bucket_ref'], 'archive_bucket_ref':first['archive_bucket_ref'],
            'distribution_arn':first['distribution_arn'], 'tls_status':first['tls_status'], 'ready_for_aws':False,
            'paths':{'raw_venues':f'raw/venues/{first["dataset_version"]}/' if first['enabled'] else None,
                     'lake_venues':f'lake/venues/{first["dataset_version"]}/v1/' if first['enabled'] else None,
                     'checkpoints':{node:[f'aws/{inventory["environment"]}/{inventory["generation"]}/{q}/checkpoint/' for q in queries] for node,queries in first['driver_queries'].items()},
                     'approved_photos':'media/venues/approved/' if first['media_bucket_ref'] else None,
                     'archive':f'observability/{inventory["environment"]}/{inventory["generation"]}/' if first['archive_bucket_ref'] else None}}
    from jsonschema import Draft202012Validator
    errors=list(Draft202012Validator(read_json(FOUNDATION/'schemas/foundation-track2.schema.json')).iter_errors(result))
    require(not errors,'invalid Foundation Track2 sidecar schema')
    return result

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--input',type=Path,action='append',required=True); parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        inventory=export(args.input)
        write_inventory(export_track2(args.input,inventory),args.output)
    except (ValueError,KeyError,TypeError,OSError):
        print('FAIL: invalid/incomplete Foundation Track2 outputs',file=sys.stderr); return 2
    return 0

if __name__=='__main__': sys.exit(main())
