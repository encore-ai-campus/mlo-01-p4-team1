#!/usr/bin/env python3
"""Build the two real app images locally; never publish images or contact AWS."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[1]


def source_manifest():
    paths=subprocess.check_output(['git','ls-files','-c','-o','--exclude-standard','-z','--','app'],cwd=ROOT).split(b'\0')
    files={p.decode():hashlib.sha256((ROOT/p.decode()).read_bytes()).hexdigest()
           for p in sorted(set(paths)) if p and (ROOT/p.decode()).is_file()}
    return files, hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'.artifacts/app-predeploy/images.json')
    args=parser.parse_args()
    files,revision=source_manifest()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    folder=args.output.parent/('build-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6])
    folder.mkdir()
    (folder/'source.json').write_text(json.dumps({'revision':revision,'files':files},indent=2)+'\n')
    record={'revision':revision,'source':str(folder/'source.json'),'results':[],'published':False}
    for architecture in ('arm64','amd64'):
        for kind in ('api','ads'):
            tag=f'mlo-p4-{kind}:predeploy-{revision[:12]}-{architecture}'
            log=folder/f'{kind}-{architecture}.log'
            command=['docker','build','--platform','linux/'+architecture,'--target','production',
                     '--build-arg','APP_REVISION='+revision,'--tag',tag,
                     '--file',str(ROOT/f'app/docker/{kind}.Dockerfile'),str(ROOT/'app')]
            print('BUILD',kind,architecture,flush=True)
            with log.open('wb') as stream:
                result=subprocess.run(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT)
            entry={'kind':kind,'platform':'linux/'+architecture,'tag':tag,'exit_code':result.returncode,'log':str(log)}
            if result.returncode==0:
                inspected=json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]
                entry.update(image_id=inspected['Id'],architecture=inspected['Architecture'])
                if inspected['Architecture']!=architecture:
                    raise RuntimeError('built image architecture mismatch')
            record['results'].append(entry)
            args.output.write_text(json.dumps(record,indent=2)+'\n')
            if result.returncode:
                print('FAIL: inspect '+str(log),flush=True)
                return result.returncode
    print(json.dumps({'revision':revision,'builds':len(record['results']),'published':False}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
