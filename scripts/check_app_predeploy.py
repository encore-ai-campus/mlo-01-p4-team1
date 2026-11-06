#!/usr/bin/env python3
"""Actual production app images against disposable TLS databases; no AWS calls."""
import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import re
from pathlib import Path
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'aws-infra/runtime/src'))
from mlo_runtime.lab import Lab, certificates
from build_app_images import source_manifest


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def check_platform(record, architecture):
    selected = {r['kind']:r for r in record['results'] if r['platform']=='linux/'+architecture}
    assert set(selected)=={'api','ads'} and all(r['exit_code']==0 for r in selected.values())
    revision=record['revision']
    lab=Lab('production-app-'+architecture)
    checks=[]
    try:
        for item in selected.values():
            inspected=json.loads(lab.docker('image','inspect',item['tag']).stdout)[0]
            assert inspected['Id']==item['image_id'] and inspected['Architecture']==architecture
            assert 'APP_REVISION='+revision in inspected['Config']['Env']
        certificates(lab.folder, ['mongo','postgres','api','ads'])
        (lab.folder/'mongodb-keyfile').write_text(base64.b64encode(secrets.token_bytes(48)).decode())
        password=secrets.token_hex(24)
        port=free_port()
        origin='https://localhost:'+str(port)
        shared={'DJANGO_SECRET_KEY':secrets.token_urlsafe(40),'DJANGO_ALLOWED_HOSTS':'localhost,127.0.0.1,api,ads',
                'DJANGO_CSRF_TRUSTED_ORIGINS':origin,'ADS_INTERNAL_SECRET':secrets.token_urlsafe(40)}
        api_env={**shared,'PGHOST':'postgres','PGPORT':'5432','PGDATABASE':'common_dev','PGUSER':'common_app',
                 'PGPASSWORD':password,'PGSSLMODE':'verify-full','PGSSLROOTCERT':'/run/mlo/secrets/ca.crt'}
        ads_env={**shared,'POSTGRES_HOST':'postgres','POSTGRES_PORT':'5432','POSTGRES_DB':'ads_dev',
                 'POSTGRES_USER':'ads_app','POSTGRES_PASSWORD':password,'POSTGRES_SSLMODE':'verify-full',
                 'POSTGRES_SSLROOTCERT':'/run/mlo/secrets/ca.crt','ADS_MONGODB_DATABASE':'advertising_dev',
                 'ADS_MONGODB_REPLICA_SET':'ads-rs','ADS_COMMON_API_URL':'http://api:8000',
                 'ADS_DELIVERY_SIGNING_KEY':secrets.token_urlsafe(40),
                 'ADS_MONGODB_URI':f'mongodb://mlo:{password}@mongo:27017/?authSource=admin&replicaSet=ads-rs&tls=true&tlsCAFile=/run/mlo/secrets/ca.crt&tlsCertificateKeyFile=/run/mlo/secrets/client.pem'}
        for kind,values,listen in [('api',api_env,8000),('ads',ads_env,8001)]:
            (lab.folder/(kind+'-env.json')).write_text(json.dumps(values))
            (lab.folder/(kind+'-service.json')).write_text(json.dumps({'kind':'web' if kind=='api' else 'advertising',
                        'bind':'0.0.0.0:'+str(listen),'secret_env':'/run/mlo/secrets/app-env.json'}))
        (lab.folder/'haproxy.cfg').write_text('global\n  log stdout format raw local0\ndefaults\n  mode http\n  timeout connect 5s\n  timeout client 15s\n  timeout server 15s\nfrontend local_tls\n  bind *:8443 ssl crt /seed/server.pem\n  http-request set-header X-Forwarded-Proto https\n  use_backend advertising if { path_beg /ads-readyz }\n  default_backend common\nbackend common\n  server common api:8000\nbackend advertising\n  http-request set-path /readyz\n  server ads ads:8001\n')
        platform='linux/'+architecture
        # All database/secret volumes belong to this newly generated project.
        services={
          'postgres':{'image':lab.image('postgresql'),'environment':{'POSTGRES_USER':'bootstrap','POSTGRES_PASSWORD':password},
              'volumes':['postgres:/var/lib/postgresql/data',f'{lab.folder}:/seed:ro'],
              'entrypoint':['bash','-ec','cp /seed/server.crt /seed/server.key /tmp/; chown postgres:postgres /tmp/server.*; chmod 600 /tmp/server.key; exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key'],
              'mem_limit':'384m'},
          'mongo':{'image':lab.image('mongodb'),'environment':{'MONGO_INITDB_ROOT_USERNAME':'mlo','MONGO_INITDB_ROOT_PASSWORD':password},
              'volumes':['mongodb:/data/db','mongodb_config:/data/configdb',f'{lab.folder}:/seed:ro'],
              'entrypoint':['bash','-ec','cp /seed/server.pem /tmp/server.pem; cp /seed/mongodb-keyfile /tmp/mongodb-keyfile; chown mongodb:mongodb /tmp/server.pem /tmp/mongodb-keyfile; chmod 600 /tmp/server.pem /tmp/mongodb-keyfile; exec docker-entrypoint.sh mongod --replSet ads-rs --keyFile /tmp/mongodb-keyfile --bind_ip_all --tlsMode requireTLS --tlsCertificateKeyFile /tmp/server.pem --tlsCAFile /seed/ca.crt'],
              'mem_limit':'512m'},
        }
        init="from pathlib import Path;import os,shutil\nfor kind,uid in [('api',10001),('ads',10002)]:\n target=Path('/secrets')/kind\n for source,name,mode in [(kind+'-env.json','app-env.json',0o400),('ca.crt','ca.crt',0o444),('server.pem','client.pem',0o400)]:\n  path=target/name;shutil.copyfile(Path('/seed')/source,path);os.chown(path,uid,uid);path.chmod(mode)\n"
        services['prepare-secrets']={'image':selected['api']['tag'],'user':'0:0','volumes':[f'{lab.folder}:/seed:ro','api_secrets:/secrets/api','ads_secrets:/secrets/ads'],
                                    'entrypoint':['python','-c',init],'command':[],'mem_limit':'128m'}
        for kind in ('api','ads'):
            services[kind]={'image':selected[kind]['tag'],'volumes':[f'{lab.folder}/{kind}-service.json:/etc/mlo/service.json:ro',kind+'_secrets:/run/mlo/secrets:ro'],
                  'environment':{'ADMIN_SESSION_SECONDS':'28800','GUEST_TTL_SECONDS':'86400','ADMIN_LOCAL_LOGIN_ENABLED':'1','GEMINI_ENABLED':'0'},
                  'mem_limit':'768m','stop_grace_period':'220s'}
        services['proxy']={'image':lab.image('haproxy'),'user':'0:0','volumes':[f'{lab.folder}/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro',f'{lab.folder}:/seed:ro'],
                           'ports':[f'127.0.0.1:{port}:8443'],'mem_limit':'128m'}
        for service in services.values():
            service['platform']=platform
            service['labels']={'mlo.role':'B','mlo.run':lab.project}
        lab.compose_path.write_text(json.dumps({'services':services,'volumes':{v:{} for v in ['postgres','mongodb','mongodb_config','api_secrets','ads_secrets']}}))
        lab.compose('up','-d','postgres','mongo',timeout=600)
        def exec_service(service,*args,check=True,timeout=120):
            return lab.compose('exec','-T',service,*args,check=check,timeout=timeout)
        def wait(predicate,label,seconds=90):
            until=time.monotonic()+seconds
            while time.monotonic()<until:
                try:
                    if predicate():return
                except Exception:pass
                raw=lab.compose('ps','--all','--format','json').stdout.decode()
                states=json.loads(raw) if raw.lstrip().startswith('[') else [json.loads(line) for line in raw.splitlines() if line]
                if any(s.get('Service') in {'postgres','mongo','api','ads','proxy'} and s.get('State') in {'exited','dead'} for s in states):
                    raise RuntimeError(label+' required container exited; inspect '+str(lab.folder))
                time.sleep(1)
            raise RuntimeError(label+' timed out; inspect '+str(lab.folder))
        # The image's initdb bootstrap server accepts Unix sockets before it
        # shuts down and starts the final server. TCP avoids that false ready.
        wait(lambda:exec_service('postgres','pg_isready','-h','127.0.0.1','-d','postgres','-U','bootstrap',check=False).returncode==0,'PostgreSQL')
        for role,database in [('common_app','common_dev'),('ads_app','ads_dev')]:
            exec_service('postgres','psql','-U','bootstrap','-d','postgres','-v','ON_ERROR_STOP=1','-c',f"CREATE USER {role} PASSWORD '{password}'")
            exec_service('postgres','createdb','-U','bootstrap','-O',role,database)
        mongo=['mongosh','--host','mongo','--tls','--tlsCAFile','/seed/ca.crt','--tlsCertificateKeyFile','/seed/server.pem',
               '--username','mlo','--password',password,'--authenticationDatabase','admin','--quiet','--eval']
        wait(lambda:exec_service('mongo',*mongo,'quit(db.runCommand({ping:1}).ok?0:1)',check=False).returncode==0,'Mongo TLS/auth')
        exec_service('mongo',*mongo,"rs.initiate({_id:'ads-rs',members:[{_id:0,host:'mongo:27017'}]})")
        wait(lambda:exec_service('mongo',*mongo,'quit(db.hello().isWritablePrimary?0:1)',check=False).returncode==0,'Mongo replica-set primary')
        lab.compose('run','--rm','--no-deps','prepare-secrets',timeout=90)
        lab.compose('up','-d','api','ads',timeout=180)
        # HAProxy resolves static service names when it starts. Starting all
        # three at once can make it exit before Docker has attached the APIs.
        # Wait for real application listeners, including the required 503 for
        # an unmigrated DB, before starting the TLS proxy.
        for kind,listen in (('api',8000),('ads',8001)):
            probe=("import urllib.request,urllib.error; "
                   "opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
                   "request=urllib.request.Request('http://127.0.0.1:"+str(listen)+"/readyz')\n"
                   "try: response=opener.open(request,timeout=3); response.close(); raise SystemExit(1)\n"
                   "except urllib.error.HTTPError as error:\n"
                   " code=error.code; error.close(); raise SystemExit(0 if code==503 else 1)\n")
            wait(lambda kind=kind,probe=probe:exec_service(kind,'python','-c',probe,check=False,timeout=10).returncode==0,
                 kind+' unmigrated listener')
        lab.compose('up','-d','proxy',timeout=90)
        jar=http.cookiejar.CookieJar()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar),
                  urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(lab.folder/'ca.crt'))))
        def fetch(path):
            try:
                with opener.open(origin+path,timeout=8) as response:return response.status,json.loads(response.read(65536))
            except urllib.error.HTTPError as error:return error.code,json.loads(error.read(65536))
        wait(lambda:fetch('/readyz')[0]==503,'unmigrated common app refusal')
        wait(lambda:fetch('/ads-readyz')[0]==503,'unprepared advertising app refusal')
        checks.append('unmigrated_schema_not_ready')
        def management(kind,args,timeout=120):
            module='backend.manage' if kind=='api' else 'manage'
            code="import os,sys,runpy;sys.path.insert(0,'/opt/mlo/bin');from app_runtime import secret_environment;os.environ.update(secret_environment('/run/mlo/secrets/app-env.json'));sys.argv="+repr(['manage',*args])+";runpy.run_module("+repr(module)+",run_name='__main__')"
            return exec_service(kind,'python','-c',code,timeout=timeout)
        management('api',['migrate','--noinput'])
        management('ads',['migrate','--noinput'])
        management('ads',['prepare_ads_runtime','--apply'])
        for path in ('/readyz','/ads-readyz'):
            wait(lambda:fetch(path)==(200,{'status':'ready','revision':revision}),path)
        checks.append('real_tls_postgresql_and_mongo_schema_revision_ready')
        html = None
        for page in ('/signup', '/mypage', '/account/recovery', '/admin/passkey'):
            with opener.open(origin+page,timeout=8) as response:
                body=response.read(2_000_000)
                assert response.status==200 and b'id="root"' in body
                assert response.headers['Cache-Control']=='no-store'
                assert response.headers['Referrer-Policy']=='no-referrer'
                html=body
        asset=re.search(rb'src="(/assets/[^" ]+\.js)"',html).group(1).decode()
        with opener.open(origin+asset,timeout=8) as response:
            assert response.status==200 and response.read(2_000_000)
            assert 'javascript' in response.headers['Content-Type']
            assert 'immutable' in response.headers['Cache-Control']
            assert response.headers['X-Content-Type-Options']=='nosniff'
        checks.append('bundled_frontend_routes_and_static_assets')
        for kind in ('api','ads'):
            exec_service(kind,'/opt/mlo/bin/readiness')
            wrong=lab.compose('exec','-T','-e','APP_REVISION='+'f'*64,kind,'/opt/mlo/bin/readiness',check=False)
            assert wrong.returncode!=0
            exec_service(kind,'python','-c',"from pathlib import Path;assert not list(Path('/workspace/app').rglob('AGENTS.md'))")
        checks.append('wrong_revision_rejected_and_private_guides_absent')
        code,account=fetch('/api/auth/me')
        assert code==200 and account['authenticated'] is False and account['csrf_token']
        checks.append('actual_common_auth_and_csrf')
        rejected=lab.compose('run','--rm','--no-deps','-e','DJANGO_SECRET_KEY=conflicting-local-fixture','api',check=False,timeout=90)
        assert rejected.returncode!=0
        checks.append('conflicting_secret_sources_refused')
        lab.compose('stop','postgres',timeout=90)
        for path in ('/readyz','/ads-readyz'):assert fetch(path)[0]==503
        lab.compose('start','postgres',timeout=90)
        for path in ('/readyz','/ads-readyz'):wait(lambda:fetch(path)[0]==200,path+' recovery')
        checks.append('database_outage_refused_and_recovery_observed')
        # Run new readiness regressions in the real images. Only the disposable
        # bootstrap account receives test-DB creation; serving users remain scoped.
        for kind,label,key in [('api','backend.common.test_readiness','PGUSER'),('ads','tests.test_readiness','POSTGRES_USER')]:
            code="import os,sys,runpy;sys.path.insert(0,'/opt/mlo/bin');from app_runtime import secret_environment;os.environ.update(secret_environment('/run/mlo/secrets/app-env.json'));os.environ["+repr(key)+"]='bootstrap';sys.argv="+repr(['manage','test',label,'--noinput','--verbosity','1'])+";runpy.run_module("+repr('backend.manage' if kind=='api' else 'manage')+",run_name='__main__')"
            exec_service(kind,'python','-c',code,timeout=180)
        checks.append('readiness_regressions_in_real_images')
        lab.compose('stop','-t','220','api','ads',timeout=250)
        raw=lab.compose('ps','--all','--format','json').stdout.decode()
        state=json.loads(raw) if raw.lstrip().startswith('[') else [json.loads(line) for line in raw.splitlines() if line]
        # Compose versions can emit a JSON array or one JSON object per line.
        if isinstance(state,dict):state=[state]
        servers=[s for s in state if s.get('Service') in {'api','ads'}]
        assert {s.get('Service') for s in servers}=={'api','ads'} and all(s.get('ExitCode')==0 for s in servers)
        checks.append('real_servers_gracefully_stopped')
        return {'platform':platform,'status':'pass','checks':checks,'project':lab.project,'evidence':str(lab.folder),'revision':revision,'real_aws_calls':False}
    except Exception as error:
        error.app_smoke_evidence=str(lab.folder)
        raise
    finally:
        # Production servers use a 220-second graceful stop, longer than the
        # small default fixtures. Cleanup must wait through that same budget.
        lab.close(timeout=300)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    # This checker never inherits real cloud credentials, even through children.
    for key in list(os.environ):
        if key.startswith('AWS_'):del os.environ[key]
    os.environ.update(AWS_CONFIG_FILE=os.devnull,AWS_SHARED_CREDENTIALS_FILE=os.devnull,AWS_EC2_METADATA_DISABLED='true')
    record=json.loads(args.images.read_text())
    if record['revision'] != source_manifest()[1]:
        raise SystemExit('NOT READY: app source changed; run make app-images again.')
    results=[]
    for architecture in ('arm64','amd64'):
        print('RUN actual app production smoke linux/'+architecture,flush=True)
        try:
            result=check_platform(record,architecture)
        except Exception as error:
            result={'platform':'linux/'+architecture,'status':'fail','error_type':type(error).__name__,
                    'evidence':getattr(error,'app_smoke_evidence',None),'safe_reason':'inspect disposable lab evidence'}
        results.append(result)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps({'results':results,'real_aws_calls':False,'ready_for_aws':False},indent=2)+'\n')
        print(json.dumps(result),flush=True)
        if result['status']!='pass':return 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
