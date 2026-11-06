"""Execute the evaluated dispatcher shell with fake versioned S3 and local IMDSv2."""
from model import require, ContractError, NODES
from pathlib import Path
import base64, gzip, hashlib, http.server, io, json, os, subprocess, sys, tarfile, threading

ENTRYPOINT = b'import json,os,sys\nopen(os.environ["PAYLOAD_RESULT"],"w").write(json.dumps(sys.argv[1:]))\n'

def artifact_parts(*, unsafe=False, symlink=False, duplicate=False, missing_manifest=False,
                   missing_file=False, wrong_target=False, wrong_role=False, oversized=False):
    files={'bin/runtime-entrypoint.py':ENTRYPOINT,'config/accepted.json':b'{"accepted":true}\n'}
    if oversized:files['config/large.bin']=b'0'*(33*1024*1024)
    nodes=[n for n in NODES if not (wrong_target and n=='control-a')]
    manifest={'schema_version':'1.0.0','artifact_version':'runtime-v1','environment':'dev',
              'generation':'foundation-mock-v1','platform':'linux/amd64','example_only':False,
              'target_nodes':nodes,'node_roles':{n:('gateway' if wrong_role and n=='control-a' else NODES[n]['role']) for n in nodes},
              'operations':['install','deploy','health','rollback'],
              'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()},
              'image_digests':{'accepted':'ghcr.io/example/runtime@sha256:'+'a'*64},
              'config_hashes':{'config/accepted.json':hashlib.sha256(files['config/accepted.json']).hexdigest()},
              'required_secrets':{n:[] for n in nodes}}
    raw=json.dumps(manifest,sort_keys=True).encode()
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w') as archive:
        def add(name,value,kind=None):
            entry=tarfile.TarInfo(name);entry.size=len(value)
            if kind:entry.type=kind;entry.linkname='/etc/passwd';entry.size=0
            archive.addfile(entry,io.BytesIO(value))
        for name,value in files.items():
            if missing_file and name=='config/accepted.json':continue
            add(name,value)
        if not missing_manifest:add('manifest.json',raw)
        if unsafe:add('../escape',b'bad')
        if symlink:add('bad-link',b'',tarfile.SYMTYPE)
        if duplicate:add('config/accepted.json',b'duplicate')
    return gzip.compress(buf.getvalue(),mtime=0),raw

def artifact_bytes(**kwargs):return artifact_parts(**kwargs)[0]

def test_dispatcher(state,binding,work):
    from integration import resources
    from ssm_dispatch import dispatcher_script
    doc=next(r['values'] for r in resources(state) if r['type']=='aws_ssm_document')
    content=json.loads(doc['content']);script='\n'.join(content['mainSteps'][0]['inputs']['runCommand']).replace('{{ Operation }}','health')
    folder=work/'dispatcher';folder.mkdir();binary=folder/'bin';binary.mkdir()
    fake=binary/'aws'
    fake.write_text('#!'+sys.executable+'\n'+'''import os,pathlib,shutil,sys
args=sys.argv[1:]
if args[:2]!=['s3api','get-object'] or args[args.index('--version-id')+1]!='mock-version-1' or args[args.index('--key')+1]!='artifacts/runtime-v1.tar.gz':sys.exit(20)
shutil.copyfile(os.environ['FAKE_S3_OBJECT'],args[-1])
''');fake.chmod(0o755)
    obj=folder/'object.tar.gz';result=folder/'result.json';host=folder/'host-binding.json';dispatch_root=folder/'dispatch'
    actual=next(n for n in state['values']['outputs']['foundation']['value']['nodes'] if n['name']=='control-a')
    host.write_text(json.dumps({'instance_id':actual['instance_id'],'node':'control-a','role':'control','environment':'dev','generation':binding['generation']}));host.chmod(0o600)
    class Metadata(http.server.BaseHTTPRequestHandler):
        bad=False
        def do_PUT(self):
            if self.path!='/latest/api/token' or self.headers.get('X-aws-ec2-metadata-token-ttl-seconds')!='60':self.send_error(403);return
            self.send_response(200);self.end_headers();self.wfile.write(b'local-imds-token')
        def do_GET(self):
            if self.headers.get('X-aws-ec2-metadata-token')!='local-imds-token':self.send_error(403);return
            self.send_response(200);self.end_headers();self.wfile.write(('i-fffffffffffffffff' if self.bad else actual['instance_id']).encode())
        def log_message(self,*args):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Metadata)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    # Only the test harness rewrites the trusted code's constant metadata/root paths.
    original=(Path(__file__).parent/'dispatcher.py').read_bytes()
    local=original.replace(b'http://169.254.169.254',f'http://127.0.0.1:{server.server_port}'.encode()).replace(b'/var/lib/mlo/dispatch',str(dispatch_root).encode()).replace(b'/etc/mlo/host-binding.json',str(host).encode())
    script=script.replace(base64.b64encode(original).decode(),base64.b64encode(local).decode())
    env={k:v for k,v in os.environ.items() if not k.startswith('AWS_')}
    env.update(PATH=str(binary)+os.pathsep+env['PATH'],FAKE_S3_OBJECT=str(obj),PAYLOAD_RESULT=str(result),PYTHONOPTIMIZE='1',AWS_CONFIG_FILE=os.devnull,AWS_SHARED_CREDENTIALS_FILE=os.devnull,AWS_EC2_METADATA_DISABLED='true')
    good,manifest=artifact_parts();obj.write_bytes(good)
    require(hashlib.sha256(good).hexdigest()==binding['artifact']['sha256'],'fixture artifact binding mismatch')
    def invoke(variant=script):return subprocess.run(['bash','-c',variant],env=env,capture_output=True,text=True)
    try:
        p=invoke();require(p.returncode==0,'actual dispatcher shell failed: '+p.stderr[-800:])
        require(json.loads(result.read_text())==['--operation','health','--environment','dev','--generation',binding['generation'],'--node','control-a','--binding',str(host)],'fixed payload argument boundary mismatch')
        require(json.loads((dispatch_root/'receipt.json').read_text())['success'] is True,'dispatcher success receipt missing')
        preserved={p.name:p.read_bytes() for p in (dispatch_root/'receipts').glob('*.json')}
        result.unlink()
        variants=[(b'corrupt-object',manifest,False)]
        for kw in [{'unsafe':True},{'symlink':True},{'duplicate':True},{'missing_manifest':True},{'missing_file':True},{'wrong_target':True},{'wrong_role':True},{'oversized':True}]:
            data,raw=artifact_parts(**kw);variants.append((data,raw,True))
        for bad,raw,retarget in variants:
            obj.write_bytes(bad)
            variant=script
            if retarget:
                variant=variant.replace(binding['artifact']['sha256'],hashlib.sha256(bad).hexdigest()).replace(binding['artifact']['manifest_sha256'],hashlib.sha256(raw).hexdigest())
            p=invoke(variant)
            require(p.returncode!=0 and not result.exists(),'invalid artifact reached runtime payload')
            require(not json.loads((dispatch_root/'receipt.json').read_text())['success'],'failure receipt missing')
            require(not (folder/'escape').exists(),'archive escaped extraction directory')
            require(not list(dispatch_root.glob('stage-*')),'dispatcher leaked temporary payload')
            for name,value in preserved.items():require((dispatch_root/'receipts'/name).read_bytes()==value,'earlier attempt receipt changed')
            preserved={p.name:p.read_bytes() for p in (dispatch_root/'receipts').glob('*.json')}
        obj.write_bytes(good)
        for mutation in ['identity','host-binding','manifest-hash']:
            variant=script
            if mutation=='identity':Metadata.bad=True
            if mutation=='host-binding':host.chmod(0o644)
            if mutation=='manifest-hash':variant=variant.replace(binding['artifact']['manifest_sha256'],'0'*64)
            p=invoke(variant);require(p.returncode!=0 and not result.exists(),'identity/binding/manifest failure reached runtime')
            Metadata.bad=False;host.chmod(0o600)
    finally:
        server.shutdown();server.server_close();thread.join()
    print('PASS: actual optimized dispatcher shell, fake versioned S3, IMDSv2 binding, archive/hash/role/node/permission failures and receipts')
