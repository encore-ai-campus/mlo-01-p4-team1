#!/usr/bin/env python3
"""Disposable PostgreSQL, SMTP and Chromium member acceptance. Never uses AWS or existing DBs."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'aws-infra/runtime/src'))
from mlo_runtime.lab import Lab
from build_app_images import source_manifest

MAILPIT = 'axllent/mailpit@sha256:9b3a463be15354c0f0eba91697d80e3e0d8a5623ef0e627cacc836bc17fd5231'
BROWSER = 'mcr.microsoft.com/playwright:v1.62.1-noble@sha256:dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-image',help='Current production web image; defaults to --images arm64 record')
    parser.add_argument('--images',type=Path,default=ROOT/'.artifacts/app-predeploy/images.json')
    parser.add_argument('--frontend-image',default='mlo-p4-frontend:dev')
    parser.add_argument('--output',type=Path,default=ROOT/'.artifacts/member-local/summary.json')
    args=parser.parse_args()
    for key in list(os.environ):
        if key.startswith('AWS_'):del os.environ[key]
    os.environ.update(AWS_CONFIG_FILE=os.devnull,AWS_SHARED_CREDENTIALS_FILE=os.devnull,AWS_EC2_METADATA_DISABLED='true')
    lab=Lab('members')
    private=ROOT/'.private'/lab.project
    private.mkdir(parents=True,mode=0o700)
    result={'status':'fail','project':lab.project,'evidence':str(lab.folder),'real_aws_calls':False,'ready_for_aws':False}
    try:
        password=secrets.token_urlsafe(32)
        key=lambda:base64.b64encode(secrets.token_bytes(32)).decode()
        values={'DJANGO_SETTINGS_MODULE':'backend.cafe.settings_docker_local','MLO_LOCAL_DOCKER':'1',
            'C_LOCAL_DEMO':'0','DJANGO_SECRET_KEY':secrets.token_urlsafe(48),'DJANGO_ALLOWED_HOSTS':'localhost,api,testserver',
            'DJANGO_CSRF_TRUSTED_ORIGINS':'http://localhost:5173','PGHOST':'postgres','PGDATABASE':'member_validation',
            'PGUSER':'member_validation','PGPASSWORD':password,'PGSSLMODE':'disable','INTEGRATION_ENABLED':'1',
            'GUEST_TTL_SECONDS':'600','ADMIN_SESSION_SECONDS':'600','ADMIN_LOCAL_LOGIN_ENABLED':'1',
            'MEMBER_EMAIL_KEYS':json.dumps({'local':key()}),'MEMBER_ACTIVE_EMAIL_KEY':'local','MEMBER_LOOKUP_KEY':key(),
            'MEMBER_PUBLIC_ORIGIN':'http://localhost:5173','MEMBER_MAIL_LIMIT_HOUR':'10','MEMBER_RECOVERY_IP_LIMIT_HOUR':'30',
            'EMAIL_HOST':'mailpit','EMAIL_PORT':'1025','EMAIL_USE_TLS':'0','DEFAULT_FROM_EMAIL':'accounts@example.org',
            'ADMIN_PASSKEY_RP_ID':'localhost','ADMIN_PASSKEY_ORIGIN':'http://localhost:5173','PYTHONPATH':'/workspace/app'}
        env=private/'api.env'
        env.write_text('\n'.join(k+'='+v for k,v in values.items())+'\n');env.chmod(0o600)
        fixture={'password':'Mbr'+secrets.token_hex(5)+'!@','admin':'passkey-admin','ticket':secrets.token_urlsafe(32),
                 'member':'ui'+secrets.token_hex(5),'member_email':'mail'+secrets.token_hex(5),'email':'qa@example.org'}
        fixture_path=private/'fixture.json';fixture_path.write_text(json.dumps(fixture));fixture_path.chmod(0o600)
        files, revision=source_manifest()
        if args.api_image is None:
            manifest=json.loads(args.images.read_text())
            if manifest['revision'] != revision:raise RuntimeError('run make app-images for the current source first')
            args.api_image=next(row['tag'] for row in manifest['results'] if row['kind']=='api' and row['platform']=='linux/arm64' and row['exit_code']==0)
        inspected=json.loads(lab.docker('image','inspect',args.api_image).stdout)[0]
        if 'APP_REVISION='+revision not in inspected['Config']['Env'] or inspected['Config'].get('Volumes'):
            raise RuntimeError('current production web image without anonymous volumes required')
        snapshot=lab.folder/'source'
        for name in files:
            target=snapshot/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(ROOT/name,target)
        source=str(snapshot/'app')
        dist=Path(source)/'frontend/dist'
        dist.mkdir(parents=True)
        lab.docker('run','--rm','--user','0','--entrypoint','sh','--mount','type=bind,src='+str(dist)+',dst=/out',
                   args.api_image,'-c','cp -a /workspace/app/frontend/dist/. /out/',timeout=60)
        result['revision']=revision
        (lab.folder/'source.json').write_text(json.dumps({'revision':revision,'files':files},indent=2)+'\n')
        # Existing tests need these writable but disposable local directories.
        for folder in (Path(source)/'.local',Path(source)/'.local-preview-logs'):folder.mkdir(exist_ok=True)
        services={
            'postgres':{'image':lab.image('postgresql'),'environment':{'POSTGRES_USER':'member_validation','POSTGRES_DB':'member_validation','POSTGRES_PASSWORD':password},
                'healthcheck':{'test':['CMD','pg_isready','-U','member_validation'],'interval':'2s','timeout':'3s','retries':30}},
            'mailpit':{'image':MAILPIT},
            'api':{'image':args.api_image,'env_file':[str(env)],'volumes':[source+':/workspace/app:ro',str(private)+':/test-private:ro'],
                'tmpfs':['/workspace/app/.local:uid=10001,gid=10001,mode=0700','/workspace/app/.local-preview-logs:uid=10001,gid=10001,mode=0700'],
                'command':['python','-m','backend.manage','runserver','0.0.0.0:5173','--noreload']},
            'mail-worker':{'image':args.api_image,'env_file':[str(env)],'volumes':[source+':/workspace/app:ro'],
                'command':['python','-m','backend.manage','deliver_account_mail']},
            'browser-deps':{'image':args.frontend_image,'user':'0','volumes':['browser_deps:/out'],
                'command':['sh','-c','cp -a /workspace/app/frontend/node_modules/. /out/']},
            'browser':{'image':BROWSER,'network_mode':'service:api','environment':{'NODE_PATH':'/deps'},
                'volumes':['browser_deps:/deps:ro',str(private)+':/test-private:ro',str(lab.folder)+':/evidence',
                    source+'/frontend/scripts/member-browser.cjs:/test/member-browser.cjs:ro'],
                'command':['node','/test/member-browser.cjs']},
        }
        services['tests']={**services['api'],'user':'0','volumes':[source+':/workspace/app',str(private)+':/test-private:ro']}
        # Compose itself needs the SQL bootstrap password; its run-only file is private.
        lab.compose_path=private/'compose.json'
        lab.compose_path.write_text(json.dumps({'services':services,'volumes':{'browser_deps':{}}}));lab.compose_path.chmod(0o600)
        for image in (MAILPIT,BROWSER):lab.docker('pull',image,timeout=300)
        lab.compose('up','-d','--wait','postgres','mailpit',timeout=120)
        print('RUN Django regression and PostgreSQL concurrent member checks',flush=True)
        tests=lab.compose('run','--rm','--no-deps','--workdir','/tmp','-e','C_LOCAL_DEMO=1','-e','MLO_DISPOSABLE_MEMBER_TEST=1','tests','python','-m','backend.manage','test',
            'backend.accounts.tests','backend.accounts.tests.postgres','backend.common','backend.recommendation','backend.music','backend.weather',
            '--settings=backend.cafe.settings_test_postgres','--noinput','--verbosity','1',timeout=300,check=False)
        (lab.folder/'django-tests.log').write_bytes(tests.stdout+tests.stderr)
        require=tests.returncode==0 and b'skipped=' not in tests.stderr and b'expected failures=' not in tests.stderr
        if not require:raise RuntimeError('Django regression/concurrency failed; inspect private local evidence')
        lab.compose('run','--rm','--no-deps','api','python','-m','backend.manage','migrate','--noinput',timeout=120)
        seed="""import os,json,hashlib;os.environ.setdefault('DJANGO_SETTINGS_MODULE','backend.cafe.settings_docker_local')
import django;django.setup()
from pathlib import Path
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from backend.common.admin_roles import assign_role
from backend.accounts.models import PasskeyEnrollment
f=json.loads(Path('/test-private/fixture.json').read_text())
u=get_user_model().objects.create_user(username=f['admin'],password=f['password'],is_staff=True)
assign_role(u,'OPS_ADMIN')
PasskeyEnrollment.objects.create(user=u,digest=hashlib.sha256(f['ticket'].encode()).hexdigest(),expires_at=timezone.now()+timedelta(minutes=10))
"""
        # Root reads the run-private fixture; no credential is printed or passed on the command line.
        lab.compose('run','--rm','--no-deps','--user','0','api','python','-c',seed)
        lab.compose('up','-d','api','mail-worker',timeout=90)
        lab.compose('run','--rm','--no-deps','browser-deps',timeout=60)
        print('RUN Chromium signup/profile/recovery/SMTP/passkey flows',flush=True)
        browser=lab.compose('run','--rm','--no-deps','browser',timeout=240,check=False)
        (lab.folder/'browser.log').write_bytes(browser.stdout+browser.stderr)
        if browser.returncode:raise RuntimeError('browser acceptance failed; inspect local evidence')
        result.update(status='pass',checks=['django_regression_without_skips','postgres_concurrent_constraints_tokens_limits',
            'actual_smtp_mailpit','chromium_member_signup_profile_recovery_erasure','chromium_virtual_passkey_registration_authentication'],
            browser=json.loads((lab.folder/'browser-summary.json').read_text()))
    except Exception as error:
        result['error_type']=type(error).__name__
    finally:
        try:
            if lab.compose_path.exists():
                lab.close()
        except Exception as error:
            result.update(status='fail', cleanup_error_type=type(error).__name__)
        # All cryptographic fixture/compose files remain ignored and owner-only for diagnosis.
        for path in private.iterdir():
            if path.is_file():path.chmod(0o600)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['status']=='pass' else 2


if __name__=='__main__':raise SystemExit(main())
