import hashlib
import json
import secrets
from datetime import timedelta
from django.db import transaction
from backend.accounts import crypto, services as account_services
from backend.common.identity import raw_request
from backend.recommendation.errors import require
from . import authorization as auth
from .fixtures import FIXTURES
from .music import DEMO_PLAYLISTS, approved_owner_playlists, playlist_name
from .models import (Workspace, WorkspaceClaim, BusinessCheckJob, ClaimDecision, BusinessEntity,
    WorkspaceMembership, InvitationReceiver, WorkspaceInvitation, ReauthenticationGrant,
    IdempotencyRecord, AuthorizationAudit, InternalOutbox, WorkspaceMusicState)

PREFIX='/api/platform/v1'


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def audit(request, user, action, resource, workspace=None):
    row=AuthorizationAudit.objects.create(actor=user,workspace=workspace,action=action,
        resource_id=str(resource.pk),resource_version=getattr(resource,'version',1),request_id=request.request_id)
    InternalOutbox.objects.create(audit=row,payload={'event_id':str(row.pk),'action':action,
        'workspace_id':str(workspace.pk) if workspace else None,'actor_account_id':str(user.pk),
        'resource_id':str(resource.pk),'resource_version':row.resource_version})


def claim_payload(body):
    fixture = FIXTURES.get(body['fixture_id'])
    require(fixture is not None, 'INVALID_REQUEST', '합성 시연 fixture를 선택해 주세요.', 400)
    venue_id = fixture.get('venue_id')
    if not venue_id:
        return body

    from backend.venues.repositories.csv_lazy_repo import get_venue_repo

    venue = get_venue_repo().get_venue_by_id(venue_id)
    require(venue is not None and venue['venue_id'] == venue_id, 'VENUE_UNAVAILABLE', '연결할 매장을 찾을 수 없습니다.', 409)
    return {**body, 'display_name': venue['venue_name'], 'address': venue['road_address']}


def claim_data(row):
    body=crypto.unseal(row.payload_ciphertext)
    check=BusinessCheckJob.objects.filter(claim=row,content_revision=row.content_revision).first()
    return {'id':str(row.pk),'revision':row.version,'content_revision':row.content_revision,'state':row.state,
        'mode':'DEMO','display_name':body['display_name'],'address':body['address'],'applicant_relation':body['applicant_relation'],
        'review_context':{'applicant_label':row.applicant.memberprofile.nickname,'fixture_id':row.fixture_id,
                          'authority_evidence_kind':'DEMO_SCENARIO' if row.state=='APPROVED' else 'PENDING','policy_version':'platform-local-v0.6'},
        'match_result':check.match_result if check else 'UNCHECKED',
        'business_status':check.business_status if check else 'UNKNOWN',
        'approved_workspace_id':str(row.approved_workspace_id) if row.approved_workspace_id else None,
        'notice':'합성 사업자정보 시연 · 실제 국세청 확인이 아닙니다.'}


def check_data(row):
    return {'id':str(row.pk),'claim_id':str(row.claim_id),'claim_content_revision':row.content_revision,
            'state':row.state,'mode':'DEMO','notice':'합성 정보 확인 작업'}


def workspace_data(row, member):
    return {'id':str(row.pk),'revision':row.version,'display_name':row.display_name,'mode':'DEMO',
            'status':row.status,'my_role':member.role}


def member_data(row):
    return {'id':str(row.pk),'revision':row.version,'nickname':row.user.memberprofile.nickname,
            'role':row.role,'status':row.status}


def invitation_data(row):
    state=row.state
    if state=='PENDING' and row.expires_at<=auth.dbnow():
        state='EXPIRED'
    return {'id':str(row.pk),'revision':row.version,'workspace_id':str(row.workspace_id),
            'workspace_display_name':row.workspace.display_name,'role':row.role,'state':state,
            'expires_at':row.expires_at.isoformat()}


def music_data(row):
    actual = [{'id': p.pk, 'name': playlist_name(p)} for p in approved_owner_playlists(row.workspace)]
    return {'workspace_id':str(row.workspace_id),'revision':row.version,'preferences':row.preferences,
            'exclusions':row.exclusions,'applied_playlist':row.applied_playlist,
            'playlists':[{'id': identifier, 'name': name} for identifier, name in DEMO_PLAYLISTS.items()] + actual,
            'notice':'시연 선곡표와 점주의 승인된 선곡표를 표시합니다.'}


def revision(request, row):
    value=request.headers.get('If-Match')
    require(value is not None,'PRECONDITION_REQUIRED','최신 revision이 필요합니다.',428)
    require(value=='"'+str(row.version)+'"','REVISION_CHANGED','다른 작업으로 변경되었습니다. 새로 확인해 주세요.',412)


def intent(request, body):
    return {'method':request.method,'path':request.path,'body_sha256':sha(canonical(body)),
            'if_match':request.headers.get('If-Match')}


def grant(request, user, body):
    secret=request.headers.get('X-Reauthentication-Grant','')
    require(20<=len(secret)<=200,'REAUTH_REQUIRED','현재 비밀번호로 다시 확인해 주세요.',403)
    row=ReauthenticationGrant.objects.select_for_update().filter(user=user,digest=sha(secret),
        session_digest=auth.session_digest(request),expires_at__gt=auth.dbnow(),consumed_at=None).first()
    require(row is not None and row.intent==intent(request,body),'REAUTH_REQUIRED','재인증 요청이 만료되었거나 작업이 변경되었습니다.',403)
    return row


def page(request, query, serialize):
    raw_limit=request.query_params.get('limit','20')
    cursor=request.query_params.get('cursor','0')
    require(raw_limit.isdigit() and cursor.isdigit() and len(cursor)<=8,'INVALID_REQUEST','페이지 값을 확인해 주세요.',400)
    limit=int(raw_limit); offset=int(cursor)
    require(1<=limit<=100,'INVALID_REQUEST','페이지 크기를 확인해 주세요.',400)
    items=list(query.order_by('created_at','pk')[offset:offset+limit+1])
    return {'items':[serialize(row) for row in items[:limit]],'next_cursor':str(offset+limit) if len(items)>limit else None}


def contextual(request, operation, values, *, lock=False):
    """Re-run for every request, including idempotent response retrieval."""
    if operation in ('listWorkspaceClaimsForReview','reviewWorkspaceClaim','decideWorkspaceClaim'):
        user=auth.reviewer(request)
    else:
        user=auth.account(request)
    context={'user':user}
    if 'workspace_id' in values:
        roles=['OWNER'] if operation in ('changeWorkspaceMemberRole','revokeWorkspaceMember','createWorkspaceInvitation',
            'listWorkspaceInvitations','revokeWorkspaceInvitation','closeWorkspace') else ['OWNER','MANAGER'] if operation=='listWorkspaceMembers' else None
        workspace,member=auth.workspace(user,values['workspace_id'],lock=lock,roles=roles,closing=operation=='closeWorkspace')
        context.update(workspace=workspace,member=member)
    if 'claim_id' in values:
        query=WorkspaceClaim.objects.select_for_update() if lock else WorkspaceClaim.objects
        if not operation in ('reviewWorkspaceClaim','decideWorkspaceClaim'):
            auth.business(user)
            query=query.filter(applicant=user)
        claim=query.filter(pk=values['claim_id']).first()
        require(claim,'NOT_FOUND','신청을 찾을 수 없습니다.',404)
        context['claim']=claim
    if 'membership_id' in values:
        query=WorkspaceMembership.objects.select_for_update() if lock else WorkspaceMembership.objects
        target=query.filter(pk=values['membership_id'],workspace=context['workspace']).first()
        require(target,'NOT_FOUND','구성원을 찾을 수 없습니다.',404)
        context['target']=target
    if 'invitation_id' in values:
        query=WorkspaceInvitation.objects.select_for_update() if lock else WorkspaceInvitation.objects
        query=query.filter(workspace=context['workspace']) if 'workspace' in context else query.filter(invitee=user)
        invitation=query.filter(pk=values['invitation_id']).first()
        require(invitation,'NOT_FOUND','초대를 찾을 수 없습니다.',404)
        context['invitation']=invitation
    return context


def lock_related_accounts(context, body):
    ids={context['user'].pk}
    if 'workspace' in context:
        ids.update(WorkspaceMembership.objects.filter(workspace=context['workspace']).values_list('user_id',flat=True))
    if 'claim' in context:
        ids.add(context['claim'].applicant_id)
    if 'invitation' in context:
        invite=context['invitation']
        ids.add(invite.inviter_membership.user_id)
    if 'recipient_code' in body:
        receiver=InvitationReceiver.objects.filter(digest=sha(body['recipient_code'])).first()
        if receiver:
            ids.add(receiver.user_id)
    auth.lock_accounts(ids)


def read(operation, request, values):
    context=contextual(request,operation,values)
    user=context['user']
    if operation=='listOwnWorkspaceClaims':
        auth.business(user)
        return page(request,WorkspaceClaim.objects.filter(applicant=user),claim_data)
    if operation in ('getOwnWorkspaceClaim','reviewWorkspaceClaim'):
        return claim_data(context['claim'])
    if operation=='getBusinessCheck':
        row=BusinessCheckJob.objects.filter(pk=values['check_id'],claim=context['claim']).first()
        require(row,'NOT_FOUND','확인 작업을 찾을 수 없습니다.',404)
        return check_data(row)
    if operation=='listWorkspaceClaimsForReview':
        return page(request,WorkspaceClaim.objects.filter(state='SUBMITTED'),claim_data)
    if operation=='listMyWorkspaces':
        auth.business(user)
        return page(request,WorkspaceMembership.objects.filter(user=user,status='ACTIVE',workspace__status__in=['ACTIVE','SUSPENDED']),
                    lambda member:workspace_data(member.workspace,member))
    if operation=='getWorkspace':
        return workspace_data(context['workspace'],context['member'])
    if operation=='getWorkspaceCapabilities':
        role=context['member'].role
        allowed=['workspace.read','playlist.read_shared','playlist.create_draft','playlist.approve_for_workspace',
                 'workspace.exclusions.write','workspace.music_preferences.write']
        if role in ('MANAGER','OWNER'):
            allowed+=['workspace.roster.read','workspace.analytics.read_aggregates','playlist.edit_any_shared_draft']
        if role=='OWNER':
            allowed+=['workspace.invite_staff','workspace.invite_manager','workspace.roles.write','workspace.close_request']
        return {'workspace_id':str(context['workspace'].pk),'policy_version':'platform-local-v0.6',
                'membership_revision':context['member'].version,'permissions':allowed}
    if operation=='listWorkspaceMembers':
        return page(request,WorkspaceMembership.objects.filter(workspace=context['workspace'],status='ACTIVE'),member_data)
    if operation=='listWorkspaceInvitations':
        return page(request,WorkspaceInvitation.objects.filter(workspace=context['workspace']),invitation_data)
    if operation=='listMyWorkspaceInvitations':
        return page(request,WorkspaceInvitation.objects.filter(invitee=user),invitation_data)
    if operation=='getMyWorkspaceInvitation':
        return invitation_data(context['invitation'])
    if operation=='getWorkspaceMusic':
        row=WorkspaceMusicState.objects.filter(workspace=context['workspace']).first()
        require(row,'NOT_FOUND','매장 음악 상태를 찾을 수 없습니다.',404)
        return music_data(row)
    raise AssertionError('unregistered_read_operation')


def secret(operation,request,body):
    user=auth.account(request)
    require(account_services.limit('platform-secret:'+str(user.pk),maximum=100), 'RATE_LIMITED','요청이 많습니다.',429)
    if operation=='reauthenticateIntent':
        require(body['intent']['path'].startswith(PREFIX+'/workspaces/') and '?' not in body['intent']['path'],
                'INVALID_REQUEST','재인증 작업 경로를 확인해 주세요.',400)
        with transaction.atomic():
            auth.lock_accounts([user.pk])
            user=auth.account(request)
            require(user.check_password(body['password']),'AUTH_FAILED','비밀번호를 확인해 주세요.',403)
            token=secrets.token_urlsafe(32)
            ReauthenticationGrant.objects.create(user=user,session_digest=auth.session_digest(request),
                digest=sha(token),intent=body['intent'],expires_at=auth.dbnow()+timedelta(minutes=5))
        return {'grant':token,'expires_in':300},200
    with transaction.atomic():
        auth.lock_accounts([user.pk])
        InvitationReceiver.objects.filter(user=user,state='OPEN').update(state='REVOKED')
        code=secrets.token_urlsafe(24)
        InvitationReceiver.objects.create(user=user,digest=sha(code),expires_at=auth.dbnow()+timedelta(minutes=15))
    return {'recipient_code':code,'expires_in':900},200


def write(operation,request,body,values):
    context=contextual(request,operation,values)
    user=context['user']
    key=request.headers.get('Idempotency-Key','')
    require(16<=len(key)<=200,'INVALID_REQUEST','Idempotency-Key가 필요합니다.',400)
    fingerprint=sha(canonical({'body':body,'if_match':request.headers.get('If-Match')}))
    with transaction.atomic():
        lock_related_accounts(context,body)
        # Account locks precede workspace locks, even for first-use idempotency rows.
        if 'invitation' in context and 'workspace' not in context:
            workspace=Workspace.objects.select_for_update().get(pk=context['invitation'].workspace_id)
            require(workspace.status=='ACTIVE','WORKSPACE_UNAVAILABLE','매장이 활성 상태가 아닙니다.',409)
        context=contextual(request,operation,values,lock=True)
        cached=IdempotencyRecord.objects.filter(user=user,path=request.path,method=request.method,key_digest=sha(key)).first()
        if cached:
            require(cached.request_digest==fingerprint,'IDEMPOTENCY_CONFLICT','같은 키로 다른 작업을 요청할 수 없습니다.',409)
            if operation=='acceptWorkspaceInvitation':
                require(WorkspaceMembership.objects.filter(pk=cached.response.get('id'),user=user,status='ACTIVE').exists(),
                        'INVITATION_UNAVAILABLE','이 초대로 부여된 매장 권한이 변경되었습니다.',409)
            return cached.response,cached.status
        fresh=None
        if operation in ('changeWorkspaceMemberRole','revokeWorkspaceMember','createWorkspaceInvitation','revokeWorkspaceInvitation','closeWorkspace'):
            fresh=grant(request,user,body)
        data,status=mutate(operation,request,body,values,context)
        record=IdempotencyRecord.objects.create(user=user,path=request.path,method=request.method,key_digest=sha(key),
            request_digest=fingerprint,response=data,status=status)
        if fresh:
            fresh.consumed_at=auth.dbnow();fresh.consumed_by=record;fresh.save()
        return data,status


def mutate(operation,request,body,values,context):
    user=context['user']; row=None
    if operation=='createWorkspaceClaim':
        auth.business(user)
        payload = claim_payload(body)
        row=WorkspaceClaim.objects.create(applicant=user,payload_ciphertext=crypto.seal(payload),fixture_id=body['fixture_id'])
        audit(request,user,'claim.created',row)
        return claim_data(row),201
    if 'claim' in context:
        row=context['claim'];revision(request,row)
        if operation=='editWorkspaceClaim':
            require(row.state in ('DRAFT','NEEDS_MORE'),'STATE_CONFLICT','초안·보완 상태에서만 수정할 수 있습니다.',409)
            payload = claim_payload(body)
            row.payload_ciphertext=crypto.seal(payload);row.fixture_id=body['fixture_id'];row.content_revision+=1;row.version+=1;row.save()
            audit(request,user,'claim.edited',row)
            return claim_data(row),200
        if operation=='requestBusinessCheck':
            require(row.state in ('DRAFT','NEEDS_MORE'),'STATE_CONFLICT','확인 가능한 신청 상태가 아닙니다.',409)
            job,_=BusinessCheckJob.objects.get_or_create(claim=row,content_revision=row.content_revision)
            return check_data(job),202
        if operation=='cancelWorkspaceClaim':
            require(row.state in ('DRAFT','SUBMITTED','NEEDS_MORE'),'STATE_CONFLICT','신청을 취소할 수 없습니다.',409)
            row.state='CANCELLED'
        if operation=='submitWorkspaceClaim':
            require(row.state in ('DRAFT','NEEDS_MORE'),'STATE_CONFLICT','제출 가능한 상태가 아닙니다.',409)
            check=BusinessCheckJob.objects.filter(claim=row,content_revision=row.content_revision,state='SUCCEEDED',match_result='MATCHED',business_status='ACTIVE').first()
            require(check,'BUSINESS_CHECK_REQUIRED','현재 신청 정보의 합성 사업자 확인을 먼저 완료해 주세요.',409)
            row.state='SUBMITTED'
        if operation=='decideWorkspaceClaim':
            require(row.applicant_id!=user.pk,'SELF_REVIEW_FORBIDDEN','본인의 신청을 심사할 수 없습니다.',403)
            require(row.state=='SUBMITTED','STATE_CONFLICT','제출 상태의 신청만 심사할 수 있습니다.',409)
            decision=body['decision']
            if decision=='APPROVE':
                auth.business(row.applicant)
                require(BusinessCheckJob.objects.filter(claim=row,content_revision=row.content_revision,state='SUCCEEDED',match_result='MATCHED',business_status='ACTIVE').exists(),
                        'BUSINESS_CHECK_REQUIRED','현재 내용의 확인 근거가 필요합니다.',409)
                fixture=FIXTURES[row.fixture_id]
                business,_=BusinessEntity.objects.get_or_create(mode='DEMO',registration_lookup=crypto.digest('platform-business',fixture['business']),
                    defaults={'registration_ciphertext':crypto.seal({'fixture':fixture['business']}),'display_name':'합성 사업자'})
                require(not Workspace.objects.filter(business=business,location_key=fixture['location']).exists(),
                        'WORKSPACE_ALREADY_LINKED','이미 연결되었거나 확인이 필요한 매장입니다.',409)
                payload=crypto.unseal(row.payload_ciphertext)
                store=Workspace.objects.create(business=business,location_key=fixture['location'],display_name=payload['display_name'],
                    address_ciphertext=crypto.seal(payload['address']),status='ACTIVE')
                WorkspaceMembership.objects.create(workspace=store,user=row.applicant,role='OWNER',granted_by=user)
                WorkspaceMusicState.objects.create(workspace=store)
                row.approved_workspace=store
                row.state='APPROVED'
                audit(request,user,'workspace.approved',store,store)
            else:
                row.state='NEEDS_MORE' if decision=='REQUEST_MORE' else 'REJECTED'
            decision_row=ClaimDecision.objects.create(claim=row,claim_revision=row.version,content_revision=row.content_revision,
                reviewer=user,decision=decision,reason_code=body['reason_code'],policy_version='platform-local-v0.6')
            row.version+=1;row.save()
            audit(request,user,'claim.decided',row,row.approved_workspace)
            return {'id':str(decision_row.pk),'claim':claim_data(row)},200
        row.version+=1;row.save();audit(request,user,'claim.'+row.state.lower(),row)
        return claim_data(row),200
    if operation in ('acceptWorkspaceInvitation','declineWorkspaceInvitation'):
        row=context['invitation'];revision(request,row)
        require(row.state=='PENDING' and row.expires_at>auth.dbnow(),'INVITATION_UNAVAILABLE','초대가 만료되었거나 처리되었습니다.',409)
        if operation=='declineWorkspaceInvitation':
            row.state='DECLINED';row.version+=1;row.save();audit(request,user,'invitation.declined',row,row.workspace)
            return invitation_data(row),200
        auth.business(user)
        issuer=WorkspaceMembership.objects.select_for_update().get(pk=row.inviter_membership_id)
        require(issuer.workspace_id==row.workspace_id and issuer.status=='ACTIVE' and issuer.role=='OWNER' and
                issuer.version==row.inviter_version and issuer.user.is_active,
                'INVITATION_UNAVAILABLE','초대자의 권한이 변경되었습니다.',409)
        auth.business(issuer.user)
        member=WorkspaceMembership.objects.filter(workspace=row.workspace,user=user).first()
        require(not member or member.status=='REVOKED','ALREADY_MEMBER','이미 소속된 매장입니다.',409)
        if member:
            member.role=row.role;member.status='ACTIVE';member.version+=1;member.granted_by=issuer.user;member.revoked_at=None;member.save()
        else:
            member=WorkspaceMembership.objects.create(workspace=row.workspace,user=user,role=row.role,granted_by=issuer.user)
        row.state='ACCEPTED';row.version+=1;row.accepted_membership=member;row.save()
        audit(request,user,'invitation.accepted',row,row.workspace)
        return member_data(member),200
    workspace=context['workspace']; own=context['member']
    if operation=='createWorkspaceInvitation':
        revision(request,workspace)
        receiver=InvitationReceiver.objects.select_for_update().filter(digest=sha(body['recipient_code']),state='OPEN',expires_at__gt=auth.dbnow()).first()
        require(receiver and receiver.user_id!=user.pk and receiver.user.is_active,'INVALID_RECIPIENT_CODE','유효한 초대 수신 코드를 확인해 주세요.',400)
        require(not WorkspaceMembership.objects.filter(workspace=workspace,user=receiver.user,status='ACTIVE').exists(),'ALREADY_MEMBER','이미 소속된 매장입니다.',409)
        WorkspaceInvitation.objects.filter(workspace=workspace,invitee=receiver.user,state='PENDING',expires_at__lte=auth.dbnow()).update(state='EXPIRED')
        require(not WorkspaceInvitation.objects.filter(workspace=workspace,invitee=receiver.user,state='PENDING').exists(),'INVITATION_PENDING','이미 대기 중인 초대가 있습니다.',409)
        row=WorkspaceInvitation.objects.create(workspace=workspace,invitee=receiver.user,role=body['role'],inviter_membership=own,
            inviter_version=own.version,expires_at=auth.dbnow()+timedelta(hours=24))
        receiver.state='CONSUMED';receiver.save()
        audit(request,user,'invitation.created',row,workspace)
        return invitation_data(row),201
    if operation=='revokeWorkspaceInvitation':
        row=context['invitation'];revision(request,row)
        require(row.state=='PENDING','STATE_CONFLICT','미사용 초대만 취소할 수 있습니다.',409)
        row.state='REVOKED';row.version+=1;row.save();audit(request,user,'invitation.revoked',row,workspace)
        return invitation_data(row),200
    if operation in ('changeWorkspaceMemberRole','revokeWorkspaceMember','leaveWorkspace'):
        row=context.get('target',own);revision(request,row)
        require(row.role!='OWNER','OWNERSHIP_RESOLUTION_REQUIRED','OWNER는 일반 변경·회수·탈퇴할 수 없습니다.',409)
        require(row.status=='ACTIVE','STATE_CONFLICT','현재 활성 소속이 아닙니다.',409)
        if operation=='changeWorkspaceMemberRole':
            row.role=body['role']
        else:
            row.status='REVOKED';row.revoked_at=auth.dbnow()
        row.version+=1;row.save()
        audit(request,user,'membership.changed',row,workspace)
        return (member_data(row),200) if operation=='changeWorkspaceMemberRole' else ({'status':'LEFT' if operation=='leaveWorkspace' else 'REVOKED','resource_id':str(row.pk)},200)
    if operation=='closeWorkspace':
        revision(request,workspace)
        workspace.status='CLOSED';workspace.version+=1;workspace.save()
        WorkspaceMembership.objects.filter(workspace=workspace,status='ACTIVE').update(status='REVOKED',revoked_at=auth.dbnow())
        WorkspaceInvitation.objects.filter(workspace=workspace,state='PENDING').update(state='REVOKED')
        audit(request,user,'workspace.closed',workspace,workspace)
        return {'status':'CLOSED','resource_id':str(workspace.pk)},200
    if operation=='changeWorkspaceMusic':
        row=WorkspaceMusicState.objects.select_for_update().get(workspace=workspace);revision(request,row)
        if body['action']=='APPLY':
            playlist_id = body['playlist_id']
            if playlist_id not in DEMO_PLAYLISTS:
                from backend.recommendation.models import Family
                playlist = approved_owner_playlists(workspace).filter(pk=playlist_id).first()
                require(playlist is not None, 'PLAYLIST_NOT_AVAILABLE', '점주의 승인된 선곡표를 확인해 주세요.', 404)
                family = Family.objects.select_for_update().get(pk=playlist.family_id)
                require(family.approved_playlist_id == playlist_id, 'PLAYLIST_NOT_AVAILABLE', '점주의 승인된 선곡표를 확인해 주세요.', 404)
            row.applied_playlist=playlist_id
        elif body['action']=='PREFERENCES': row.preferences=body['preferences']
        else: row.exclusions=body['exclusions']
        row.version+=1;row.save();audit(request,user,'music.'+body['action'].lower(),row,workspace)
        return music_data(row),200
    raise AssertionError('unregistered_mutation_operation')
