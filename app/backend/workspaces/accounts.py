"""Central account API adapters reuse the approved account security implementation."""
from django.contrib.auth import get_user_model, authenticate, login, logout
from django.db import transaction
from django.middleware.csrf import get_token
from backend.accounts import services, policy
from backend.accounts import api as legacy
from backend.accounts.models import MemberProfile, AccountToken
import uuid
import hashlib
from backend.common.identity import raw_request
from backend.recommendation.errors import require
from . import authorization as auth
from .models import ServiceMembership


def memberships(user):
    return [{'type':r.kind,'status':r.status} for r in ServiceMembership.objects.filter(user=user).order_by('kind')]


def next_action(kind):
    if kind == 'BUSINESS':
        return {'kind':'NAVIGATE','route_key':'MUTHER_HOME','path':'/platform/business'}
    if kind == 'CONSUMER':
        return {'kind':'SERVICE_PREPARING','route_key':'CONSUMER_PREPARING','path':'/platform/consumer'}
    return {'kind':'CHOOSE_SERVICE','route_key':'SERVICE_CHOOSER','path':'/platform'}


def account_data(user):
    profile = MemberProfile.objects.get(user=user)
    data = services.profile_data(profile)
    challenge=AccountToken.objects.filter(user=user,purpose='email',consumed_at=None,expires_at__gt=auth.dbnow()).first()
    return {'account_id':str(profile.owner_id),'username':user.username,'nickname':profile.nickname,
            'memberships':memberships(user),'email_masked':data['email_masked'],
            'email_status':'VERIFIED' if data['email_verified'] else 'PENDING' if data['email_pending'] else 'ABSENT',
            'email_pending_until':data['email_pending_until'],
            'email_challenge_id':str(challenge.pk) if challenge else None}


def result(request, user):
    session = raw_request(request).session
    kinds = [r['type'] for r in memberships(user) if r['status']=='ACTIVE']
    active = session.get('platform_active_membership')
    if active not in kinds:
        active = kinds[0] if len(kinds)==1 else None
        session['platform_active_membership'] = active
    return {'account':account_data(user),'session':{'active_membership':active,
            'expires_at':session.get_expiry_date().isoformat(),'csrf_token':get_token(raw_request(request))},
            'next_action':next_action(active)}


def acknowledgements(body, *, signup=False):
    ack = body['acknowledgements']
    mapped = {'policy_version':body['policy_version'],'terms_accepted':ack['terms'],
              'privacy_acknowledged':ack['privacy_notice'],'age_confirmed':ack.get('age14',True),'email_consent':False}
    policy.consent(mapped)
    return mapped


def execute(operation, request, body):
    raw = raw_request(request)
    if operation == 'policies':
        value=policy.documents()
        return {**value,'documents':[{'kind':kind,'scope':'COMMON','version':value['version'],
            'sha256':hashlib.sha256(text.encode()).hexdigest(),'text':text}
            for kind,text in value['documents'].items()]},200
    if operation == 'availability':
        field = 'nickname' if 'nickname' in body else 'username'
        require(field in ('username','nickname'), 'INVALID_REQUEST', '확인할 항목을 확인해 주세요.', 400)
        value = body.get(field)
        if field=='username':
            normalized=policy.username(value)
            taken=get_user_model().objects.filter(username__iexact=normalized).exists()
        else:
            _, normalized=policy.nickname(value)
            taken=MemberProfile.objects.filter(nickname_key=normalized).exists()
        return {'field':field,'available':not taken},200
    if operation == 'signup':
        mapped = acknowledgements(body, signup=True)
        mapped.update({k:v for k,v in body.items() if k in ('username','password','password_confirmation','nickname','email_consent')})
        if body['email_consent']:
            mapped['email'] = body['email']
        with transaction.atomic():
            profile=services.signup(mapped)
            ServiceMembership.objects.create(user=profile.user,kind=body['initial_membership'])
        login(raw,profile.user)
        raw.session.set_expiry(8*3600)
        raw.session['platform_active_membership']=body['initial_membership']
        return result(request,profile.user),201
    if operation == 'login':
        require(services.limit('platform-login:'+body['username'].casefold(),maximum=100), 'RATE_LIMITED','로그인 요청이 많습니다.',429)
        candidate=get_user_model().objects.filter(username__iexact=body['username']).first()
        user=authenticate(raw,username=candidate.username if candidate else body['username'].lower(),password=body['password'])
        require(user and user.is_active and not user.is_staff and MemberProfile.objects.filter(user=user).exists(),
                'AUTH_FAILED','아이디 또는 비밀번호를 확인해 주세요.',401)
        login(raw,user)
        raw.session.set_expiry(7*86400 if body.get('remember') else 8*3600)
        return result(request,user),200
    if operation == 'current':
        from django.contrib.auth import get_user
        user=get_user(raw)
        if user.is_authenticated and user.is_active and not user.is_staff and MemberProfile.objects.filter(user=user).exists():
            value=result(request,user)
            return {'authenticated':True,'account':value['account'],'session':value['session']},200
        return {'authenticated':False,'csrf_token':get_token(raw)},200
    if operation == 'logout':
        logout(raw)
        return None,204
    if operation == 'logout_all':
        from backend.common.admin_roles import revoke_sessions
        user=auth.account(request)
        legacy.current_password(request,user)
        revoke_sessions(user)
        logout(raw)
        return None,204
    if operation == 'recovery_request':
        services.recovery_request('username' if body['purpose']=='ID_HINT' else 'password',body['email'],request.META.get('REMOTE_ADDR',''))
        return {'message':'등록·확인된 복구 이메일이 있다면 안내 메일을 발송합니다.'},202
    if operation == 'recovery_complete':
        purpose='username' if body['purpose']=='ID_HINT' else 'password'
        parts=body['token'].split('.',1)
        require(len(parts)==2,'INVALID_TOKEN','복구 링크를 확인해 주세요.',400)
        try:
            uuid.UUID(parts[0])
        except ValueError:
            require(False,'INVALID_TOKEN','복구 링크를 확인해 주세요.',400)
        value=services.consume_recovery(purpose,parts[0],parts[1],body.get('new_password'),body.get('new_password_confirmation'))
        return ({'purpose':'ID_HINT','username':value} if purpose=='username' else {'purpose':'PASSWORD_RESET','login_required':True}),200
    user=auth.account(request)
    if operation=='me':
        return account_data(user),200
    if operation=='profile':
        response=legacy.Profile().patch(request)
        return account_data(user),response.status_code
    if operation=='add_membership':
        acknowledgements(body)
        with transaction.atomic():
            auth.lock_accounts([user.pk])
            row,created=ServiceMembership.objects.get_or_create(user=user,kind=body['membership_type'])
            require(row.status=='ACTIVE','FORBIDDEN','이용 유형 상태를 확인해 주세요.',403)
            if created:
                services.receipt(user,'terms',body['policy_version'])
                services.receipt(user,'privacy_notice',body['policy_version'])
        return {'memberships':memberships(user)},200
    if operation=='context':
        require(ServiceMembership.objects.filter(user=user,kind=body['membership_type'],status='ACTIVE').exists(),
                'FORBIDDEN','이용 유형을 먼저 추가해 주세요.',403)
        raw.session['platform_active_membership']=body['membership_type']
        return {'active_membership':body['membership_type'],'next_action':next_action(body['membership_type'])},200
    if operation=='entry':
        kind='BUSINESS' if body['service']=='MUTHER' else 'CONSUMER'
        require(ServiceMembership.objects.filter(user=user,kind=kind,status='ACTIVE').exists(),'FORBIDDEN','이용 유형을 먼저 추가해 주세요.',403)
        from .models import WorkspaceMembership
        access=('CHECK_PER_RESOURCE' if WorkspaceMembership.objects.filter(user=user,status='ACTIVE',workspace__status='ACTIVE').exists() else 'NOT_LINKED') if kind=='BUSINESS' else 'NOT_APPLICABLE'
        return {'availability':'AVAILABLE' if kind=='BUSINESS' else 'PREPARING',
                'next_action':next_action(kind),'workspace_access':access},200
    if operation=='email_add':
        response=legacy.Email().post(request)
        token=AccountToken.objects.filter(user=user,purpose='email',consumed_at=None).latest('created_at')
        return {'challenge_id':str(token.pk),'expires_at':token.expires_at.isoformat(),'resend_after':60,'message':'복구 이메일 확인 코드를 발송했습니다.'},202
    if operation=='email_remove':
        legacy.Email().delete(request)
        return None,204
    if operation=='email_verify':
        require(AccountToken.objects.filter(pk=body['challenge_id'],user=user,purpose='email',consumed_at=None).exists(),
                'INVALID_TOKEN','확인 요청을 다시 시작해 주세요.',400)
        legacy.EmailVerify().post(request)
        return account_data(user),200
    if operation=='password':
        request._full_data={**body,'password_confirmation':body['new_password_confirmation']}
        legacy.Password().post(request)
        return None,204
    if operation=='withdraw':
        from .models import WorkspaceMembership
        require(not WorkspaceMembership.objects.filter(user=user,status='ACTIVE',role='OWNER',workspace__status__in=['ACTIVE','SUSPENDED']).exists(),
                'OWNERSHIP_RESOLUTION_REQUIRED','소유 매장을 닫거나 관리책임을 먼저 이전해 주세요.',409)
        request._full_data={**body,'confirmation':'회원탈퇴'}
        legacy.Withdraw().post(request)
        return {'status':'ACCEPTED','scope':'ALL_MEMBERSHIPS','message':'전체 계정 탈퇴를 처리했습니다. 별도 보유 자료·백업은 해당 정책에 따라 정리합니다.'},202
    raise AssertionError('unregistered_account_operation')
