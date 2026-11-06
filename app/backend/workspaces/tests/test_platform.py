import hashlib
import json
import secrets
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import transaction, connection, IntegrityError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from jsonschema import Draft202012Validator, FormatChecker
from backend.accounts.tests.test_accounts import CONFIG, PASSWORD
from backend.accounts.models import MemberProfile, AccountMail, AccountToken
from backend.accounts import crypto, services
from backend.workspaces import service, worker
from backend.workspaces.authorization import dbnow
from backend.workspaces.models import *
from backend.workspaces.schema import CONTRACT

BASE='/api/platform/v1'
TEST_CONFIG={**CONFIG,'PLATFORM_ENABLED':True,'MEMBER_PUBLIC_LIMIT_HOUR':10000}


class Helpers:
    def response_matches(self,response,path,method):
        descriptor=CONTRACT['paths'][path][method]['responses'][str(response.status_code)]
        if response.status_code==204:
            self.assertEqual(response.content,b'')
            return
        schema={**descriptor['content']['application/json']['schema'],'components':CONTRACT['components']}
        errors=list(Draft202012Validator(schema,format_checker=FormatChecker()).iter_errors(response.json()))
        self.assertEqual([error.message for error in errors],[])

    def user(self,name,kind='BUSINESS'):
        user=get_user_model().objects.create_user(username=name,password=PASSWORD)
        MemberProfile.objects.create(user=user,nickname=name,nickname_key=name)
        ServiceMembership.objects.create(user=user,kind=kind)
        return user

    def client_for(self,user):
        client=Client(enforce_csrf_checks=True)
        client.force_login(user)
        return client

    def reviewer(self,name='reviewer'):
        user=get_user_model().objects.create_user(username=name,password=PASSWORD,is_staff=True)
        user.user_permissions.add(Permission.objects.get(content_type__app_label='workspaces',codename='review_workspace'))
        client=self.client_for(user)
        session=client.session
        session['admin_passkey_user']=str(user.pk)
        session['admin_passkey_verified_at']=dbnow().timestamp()
        session.save()
        return user,client

    def call(self,client,path,body=None,method='post',version=None,key=None,grant=None):
        url=path if path.startswith('/api/') else BASE+path
        if method=='get':
            return client.get(url)
        boot=client.get(BASE+'/sessions/current').json()['data']
        csrf=boot.get('csrf_token') or boot['session']['csrf_token']
        headers={'HTTP_X_CSRFTOKEN':csrf,'HTTP_IDEMPOTENCY_KEY':key or secrets.token_hex(16)}
        if version is not None: headers['HTTP_IF_MATCH']='"'+str(version)+'"'
        if grant is not None: headers['HTTP_X_REAUTHENTICATION_GRANT']=grant
        return getattr(client,method)(url,{} if body is None else body,content_type='application/json',**headers)

    def data(self,response,status=200):
        self.assertEqual(response.status_code,status,response.content)
        return response.json()['data']

    def claim(self,client,fixture='demo-active-001'):
        body={'display_name':'합성 매장','address':'합성 주소','applicant_relation':'DELEGATE','fixture_id':fixture}
        return self.data(self.call(client,'/workspace-claims',body),201)

    def submit(self,client,claim):
        self.data(self.call(client,f"/workspace-claims/{claim['id']}/business-checks",version=claim['revision']),202)
        self.assertTrue(worker.run_one())
        return self.data(self.call(client,f"/workspace-claims/{claim['id']}/submissions",version=claim['revision']))

    def approve(self,client,claim):
        return self.data(self.call(client,f"/admin/workspace-claims/{claim['id']}/decisions",
            {'decision':'APPROVE','reason_code':'DEMO_SCENARIO_CONFIRMED'},version=claim['revision']))['claim']

    def setup_store(self,user,location='one'):
        with transaction.atomic():
            business=BusinessEntity.objects.create(registration_lookup=secrets.token_hex(32),registration_ciphertext=crypto.seal('synthetic'),display_name='합성 사업자')
            workspace=Workspace.objects.create(business=business,location_key=location,display_name='합성 매장',address_ciphertext=crypto.seal('합성 주소'),status='ACTIVE')
            member=WorkspaceMembership.objects.create(workspace=workspace,user=user,role='OWNER',granted_by=user)
            WorkspaceMusicState.objects.create(workspace=workspace)
        return workspace,member

    def grant_for(self,client,path,body,version):
        request_intent={'method':'POST','path':BASE+path,'body_sha256':service.sha(service.canonical(body)),
                        'if_match':'"'+str(version)+'"'}
        return self.data(self.call(client,'/reauthentications',{'password':PASSWORD,'intent':request_intent}))['grant']

    def invite(self,owner_client,recipient,workspace,role='STAFF'):
        recipient_client=self.client_for(recipient)
        code=self.data(self.call(recipient_client,'/me/invitation-receivers'))['recipient_code']
        path=f'/workspaces/{workspace.pk}/invitations'
        body={'recipient_code':code,'role':role}
        grant=self.grant_for(owner_client,path,body,workspace.version)
        invitation=self.data(self.call(owner_client,path,body,version=workspace.version,grant=grant),201)
        return invitation,recipient_client


@override_settings(**TEST_CONFIG)
class PlatformTests(Helpers,TestCase):
    def setUp(self):
        self.owner=self.user('storeowner')
        self.owner_client=self.client_for(self.owner)
        self.store,self.owner_member=self.setup_store(self.owner)

    def test_central_signup_atomic_initial_type_auto_login_and_optional_email_discard(self):
        client=Client(enforce_csrf_checks=True)
        body={'username':'platformnew','password':PASSWORD,'password_confirmation':PASSWORD,'nickname':'',
              'initial_membership':'CONSUMER','policy_version':'test-v1',
              'acknowledgements':{'terms':True,'privacy_notice':True,'age14':True},'email_consent':False,'email':{'discard':'before validation'}}
        data=self.data(self.call(client,'/signups',body),201)
        self.assertEqual(data['session']['active_membership'],'CONSUMER')
        self.assertEqual(data['next_action']['route_key'],'CONSUMER_PREPARING')
        profile=MemberProfile.objects.get(user__username='platformnew')
        self.assertEqual(profile.email_ciphertext,'')
        self.assertRegex(profile.nickname,r'^회원\d{4}$')
        self.assertEqual(self.data(self.call(client,'/me',method='get'))['account_id'],str(profile.owner_id))

    def test_new_signup_and_profile_policy_cannot_be_bypassed_through_platform(self):
        client=Client(enforce_csrf_checks=True)
        body={'username':'policynew','password':PASSWORD,'password_confirmation':PASSWORD,'nickname':'한글Ab12',
              'initial_membership':'CONSUMER','policy_version':'test-v1',
              'acknowledgements':{'terms':True,'privacy_notice':True,'age14':True},'email_consent':False}
        for invalid in ['abcd12!!','Abcd123!','Abc d1!!','Abcd12!!\n','A'+'b'*18+'!!']:
            response=self.call(client,'/signups',{**body,'password':invalid,'password_confirmation':invalid})
            self.assertGreaterEqual(response.status_code,400)
        for invalid in ['a','abcdefghijk','공 백','漢字']:
            self.assertGreaterEqual(self.call(client,'/signups',{**body,'nickname':invalid}).status_code,400)
        self.assertFalse(get_user_model().objects.filter(username='policynew').exists())
        self.data(self.call(client,'/signups',body),201)
        self.assertGreaterEqual(self.call(client,'/me',{'nickname':'새 별명'},method='patch').status_code,400)
        self.assertEqual(self.data(self.call(client,'/me',{'nickname':'새별명123'},method='patch'))['nickname'],'새별명123')

    def test_other_service_type_added_without_new_account_or_implicit_invite_consent(self):
        user=self.user('consumerone','CONSUMER');client=self.client_for(user)
        body={'membership_type':'BUSINESS','policy_version':'test-v1','acknowledgements':{'terms':True,'privacy_notice':True}}
        self.data(self.call(client,'/me/memberships',body))
        self.data(self.call(client,'/me/memberships',body))
        self.assertEqual(ServiceMembership.objects.filter(user=user).count(),2)
        self.assertEqual(get_user_model().objects.filter(username='consumerone').count(),1)

    def test_default_gate_blocks_non_local_platform(self):
        with override_settings(PLATFORM_ENABLED=False):
            self.assertEqual(self.owner_client.get(BASE+'/me').status_code,503)

    def test_authentication_required_for_protected_routes(self):
        client=Client(enforce_csrf_checks=True)
        for path in ['/me','/me/workspaces',f'/workspaces/{self.store.pk}']:
            self.assertEqual(client.get(BASE+path).status_code,401)

    def test_csrf_unknown_fields_and_owner_role_injection(self):
        self.assertEqual(self.owner_client.post(BASE+'/me/invitation-receivers',{},content_type='application/json').status_code,403)
        body={'role':'OWNER'}
        self.assertEqual(self.call(self.owner_client,f'/workspaces/{self.store.pk}/memberships/{self.owner_member.pk}',body,method='patch').status_code,400)
        self.assertEqual(self.call(self.owner_client,'/workspace-claims',{'account_id':'spoof'}).status_code,400)

    def test_foreign_workspace_and_child_ids_never_leak(self):
        other=self.user('otherowner');store,member=self.setup_store(other,'two')
        for path in [f'/workspaces/{store.pk}',f'/workspaces/{store.pk}/music']:
            self.assertEqual(self.owner_client.get(BASE+path).status_code,404)
        body={'role':'STAFF'}
        self.assertEqual(self.call(self.owner_client,f'/workspaces/{self.store.pk}/memberships/{member.pk}',body,method='patch',version=1).status_code,404)

    def test_business_type_alone_gives_no_workspace_permission(self):
        client=self.client_for(self.user('unlinkeduser'))
        self.assertEqual(client.get(BASE+f'/workspaces/{self.store.pk}').status_code,404)
        self.assertEqual(self.data(self.call(client,'/me/workspaces',method='get'))['items'],[])

    def test_staff_can_apply_playlist_exclusions_and_music_preferences(self):
        user=self.user('musicstaff');client=self.client_for(user)
        WorkspaceMembership.objects.create(workspace=self.store,user=user,role='STAFF',granted_by=self.owner)
        version=1
        for body in [{'action':'APPLY','playlist_id':'demo-calm'},{'action':'EXCLUSIONS','exclusions':['demo-track-1']},
                     {'action':'PREFERENCES','preferences':{'mood':'BRIGHT','volume':30}}]:
            data=self.data(self.call(client,f'/workspaces/{self.store.pk}/music',body,method='patch',version=version))
            version=data['revision']
        self.assertEqual(data['applied_playlist'],'demo-calm')
        self.assertEqual(AuthorizationAudit.objects.filter(workspace=self.store,action__startswith='music.').count(),3)
        self.assertEqual(InternalOutbox.objects.count(),3)

    def test_manager_cannot_invite_revoke_or_change_roles(self):
        user=self.user('managerone');client=self.client_for(user)
        WorkspaceMembership.objects.create(workspace=self.store,user=user,role='MANAGER',granted_by=self.owner)
        self.assertEqual(self.call(client,f'/workspaces/{self.store.pk}/invitations',{'recipient_code':'a'*32,'role':'STAFF'},version=1).status_code,403)
        self.assertEqual(self.call(client,f'/workspaces/{self.store.pk}/memberships/{self.owner_member.pk}',{'role':'STAFF'},method='patch',version=1).status_code,403)

    def test_owner_cannot_use_workspace_role_to_read_another_account(self):
        other=self.user('privateuser')
        response=self.owner_client.get(BASE+f'/me/{other.pk}')
        self.assertEqual(response.status_code,404)
        self.assertNotIn('email',json.dumps(self.data(self.call(self.owner_client,f'/workspaces/{self.store.pk}/memberships',method='get'))))

    def test_current_database_state_overrules_session_and_capabilities(self):
        user=self.user('revokedstaff');client=self.client_for(user)
        member=WorkspaceMembership.objects.create(workspace=self.store,user=user,role='STAFF',granted_by=self.owner)
        self.data(self.call(client,f'/workspaces/{self.store.pk}/capabilities',method='get'))
        member.status='REVOKED';member.save()
        self.assertEqual(client.get(BASE+f'/workspaces/{self.store.pk}/music').status_code,404)

    def test_admin_has_no_implicit_workspace_access(self):
        _,client=self.reviewer()
        self.assertEqual(client.get(BASE+f'/workspaces/{self.store.pk}').status_code,403)

    def test_synthetic_claim_check_submit_and_independent_approval(self):
        client=self.client_for(self.user('claimowner'))
        claim=self.submit(client,self.claim(client,'demo-branch-002'))
        _,review=self.reviewer()
        approved=self.approve(review,claim)
        self.assertEqual(approved['state'],'APPROVED')
        self.assertEqual(WorkspaceMembership.objects.filter(workspace_id=approved['approved_workspace_id'],role='OWNER',status='ACTIVE').count(),1)

    def test_mismatch_closed_and_unavailable_never_auto_approve(self):
        for fixture in ['demo-mismatch','demo-closed','demo-unavailable']:
            claim=self.claim(self.owner_client,fixture)
            self.data(self.call(self.owner_client,f"/workspace-claims/{claim['id']}/business-checks",version=1),202)
            worker.run_one()
            self.assertEqual(self.call(self.owner_client,f"/workspace-claims/{claim['id']}/submissions",version=1).status_code,409)

    def test_reviewer_requires_capability_and_fresh_passkey(self):
        user,client=self.reviewer()
        self.assertEqual(client.get(BASE+'/admin/workspace-claims').status_code,200)
        session=client.session;session['admin_passkey_verified_at']=dbnow().timestamp()-301;session.save()
        self.assertEqual(client.get(BASE+'/admin/workspace-claims').status_code,403)
        session=client.session;session['admin_passkey_verified_at']=dbnow().timestamp();session.save()
        user.user_permissions.clear()
        self.assertEqual(client.get(BASE+'/admin/workspace-claims').status_code,403)

    def test_claim_content_and_row_revisions_are_separate(self):
        claim=self.claim(self.owner_client)
        submitted=self.submit(self.owner_client,claim)
        self.assertEqual(submitted['content_revision'],1)
        self.assertEqual(submitted['revision'],2)
        self.assertEqual(submitted['match_result'],'MATCHED')

    def test_edit_invalidates_old_check_and_late_completion(self):
        claim=self.claim(self.owner_client)
        self.data(self.call(self.owner_client,f"/workspace-claims/{claim['id']}/business-checks",version=1),202)
        body={'display_name':'변경 매장','address':'합성 새 주소','applicant_relation':'DELEGATE','fixture_id':'demo-branch-002'}
        edited=self.data(self.call(self.owner_client,f"/workspace-claims/{claim['id']}",body,method='patch',version=1))
        worker.run_one()
        self.assertEqual(BusinessCheckJob.objects.get(claim_id=claim['id']).state,'STALE')
        self.assertEqual(self.call(self.owner_client,f"/workspace-claims/{claim['id']}/submissions",version=edited['revision']).status_code,409)

    def test_live_fields_and_provider_are_rejected(self):
        body={'display_name':'합성','address':'합성','applicant_relation':'DELEGATE','fixture_id':'demo-active-001','provider':'LIVE'}
        self.assertEqual(self.call(self.owner_client,'/workspace-claims',body).status_code,400)

    def test_recipient_code_bound_invitation_and_target_acceptance(self):
        target=self.user('targetstaff')
        invitation,client=self.invite(self.owner_client,target,self.store)
        self.assertEqual(self.owner_client.get(BASE+f"/workspace-invitations/{invitation['id']}").status_code,404)
        member=self.data(self.call(client,f"/workspace-invitations/{invitation['id']}/acceptances",version=1))
        self.assertEqual(member['role'],'STAFF')
        self.assertEqual(WorkspaceMembership.objects.filter(user=target,workspace=self.store).count(),1)

    def test_receiver_expiry_and_reuse(self):
        target=self.user('receivertarget')
        invitation,client=self.invite(self.owner_client,target,self.store)
        self.assertEqual(InvitationReceiver.objects.get(user=target).state,'CONSUMED')
        code=self.data(self.call(client,'/me/invitation-receivers'))['recipient_code']
        InvitationReceiver.objects.filter(user=target,state='OPEN').update(expires_at=dbnow()-timedelta(seconds=1))
        path=f'/workspaces/{self.store.pk}/invitations';body={'recipient_code':code,'role':'STAFF'}
        grant=self.grant_for(self.owner_client,path,body,1)
        self.assertEqual(self.call(self.owner_client,path,body,version=1,grant=grant).status_code,400)

    def test_invitation_expiry_decline_and_get_has_no_side_effect(self):
        invitation,client=self.invite(self.owner_client,self.user('inviteexpiry'),self.store)
        row=WorkspaceInvitation.objects.get(pk=invitation['id'])
        self.data(self.call(client,f"/workspace-invitations/{row.pk}",method='get'))
        row.refresh_from_db();self.assertEqual(row.state,'PENDING')
        row.expires_at=dbnow()-timedelta(seconds=1);row.save()
        self.assertEqual(self.call(client,f'/workspace-invitations/{row.pk}/acceptances',version=1).status_code,409)

    def test_inviter_version_change_invalidates_old_invitation(self):
        invitation,client=self.invite(self.owner_client,self.user('oldinvite'),self.store)
        self.owner_member.version+=1;self.owner_member.save()
        self.assertEqual(self.call(client,f"/workspace-invitations/{invitation['id']}/acceptances",version=1).status_code,409)

    def test_consumer_must_explicitly_add_business_before_acceptance(self):
        invitation,client=self.invite(self.owner_client,self.user('consumertarget','CONSUMER'),self.store)
        response=self.call(client,f"/workspace-invitations/{invitation['id']}/acceptances",version=1)
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['error']['code'],'BUSINESS_MEMBERSHIP_REQUIRED')

    def test_idempotent_acceptance_and_current_permission_check(self):
        user=self.user('retryinvite');invitation,client=self.invite(self.owner_client,user,self.store)
        path=f"/workspace-invitations/{invitation['id']}/acceptances";key=secrets.token_hex(16)
        first=self.data(self.call(client,path,version=1,key=key))
        second=self.data(self.call(client,path,version=1,key=key))
        self.assertEqual(first,second)
        self.assertEqual(WorkspaceMembership.objects.filter(user=user,workspace=self.store).count(),1)
        WorkspaceMembership.objects.filter(user=user,workspace=self.store).update(status='REVOKED')
        self.assertEqual(self.call(client,path,version=1,key=key).status_code,409)

    def test_reauthentication_intent_grant_is_one_use_and_exact(self):
        target=self.user('granttarget');client=self.client_for(target)
        code=self.data(self.call(client,'/me/invitation-receivers'))['recipient_code']
        path=f'/workspaces/{self.store.pk}/invitations';body={'recipient_code':code,'role':'STAFF'}
        grant=self.grant_for(self.owner_client,path,body,1)
        self.assertEqual(self.call(self.owner_client,path,{**body,'role':'MANAGER'},version=1,grant=grant).status_code,403)
        key=secrets.token_hex(16)
        self.data(self.call(self.owner_client,path,body,version=1,grant=grant,key=key),201)
        self.data(self.call(self.owner_client,path,body,version=1,grant=grant,key=key),201)
        self.assertEqual(self.call(self.owner_client,path,body,version=1,grant=grant).status_code,403)

    def test_missing_and_stale_revision_do_not_mutate(self):
        body={'action':'APPLY','playlist_id':'demo-calm'};path=f'/workspaces/{self.store.pk}/music'
        self.assertEqual(self.call(self.owner_client,path,body,method='patch').status_code,428)
        self.assertEqual(self.call(self.owner_client,path,body,method='patch',version=2).status_code,412)
        self.assertEqual(WorkspaceMusicState.objects.get(workspace=self.store).version,1)

    def test_owner_cannot_leave_and_account_withdrawal_requires_resolution(self):
        self.assertEqual(self.call(self.owner_client,f'/workspaces/{self.store.pk}/membership-exits',{'reason_code':'SELF_EXIT'},version=1).status_code,409)
        response=self.call(self.owner_client,'/me/withdrawals',{'current_password':PASSWORD,'confirmation':'전체 계정 탈퇴'})
        self.assertEqual(response.status_code,409)

    def test_sanitized_audit_and_outbox_exclude_input_secrets(self):
        self.claim(self.owner_client)
        text=json.dumps(list(InternalOutbox.objects.values_list('payload',flat=True)),ensure_ascii=False)
        for secret in [PASSWORD,'합성 주소','recipient_code','representative_name']:
            self.assertNotIn(secret,text)

    def test_request_dto_and_key_response_schemas_match_contract(self):
        for path,name in [('/me','Account'),(f'/workspaces/{self.store.pk}','Workspace'),(f'/workspaces/{self.store.pk}/capabilities','Capabilities')]:
            data=self.data(self.call(self.owner_client,path,method='get'))
            schema={'$ref':'#/components/schemas/'+name,'components':CONTRACT['components']}
            errors=list(Draft202012Validator(schema,format_checker=FormatChecker()).iter_errors(data))
            self.assertEqual(errors,[])

    def test_current_session_availability_service_entry_and_logout_match_contract(self):
        for path in ['/sessions/current','/policies']:
            self.response_matches(self.call(self.owner_client,path,method='get'),path,'get')
        for body in [{'username':'availableuser'},{'nickname':'새로운별명'}]:
            self.response_matches(self.call(self.owner_client,'/availability',body),'/availability','post')
        self.response_matches(self.call(self.owner_client,'/service-entries',{'service':'MUTHER'}),'/service-entries','post')
        self.response_matches(self.call(self.owner_client,'/me/context',{'membership_type':'BUSINESS'},method='put'),'/me/context','put')
        self.response_matches(self.call(self.owner_client,'/sessions/current',method='delete'),'/sessions/current','delete')

    def test_common_login_resolves_current_database_memberships_and_isolates_context(self):
        user=self.user('routingconsumer','CONSUMER')
        first=Client(enforce_csrf_checks=True)
        body={'username':user.username,'password':PASSWORD,'remember':False}
        self.assertEqual(self.data(self.call(first,'/sessions',body))['next_action']['path'],'/platform/consumer')
        ServiceMembership.objects.create(user=user,kind='BUSINESS')
        second=Client(enforce_csrf_checks=True)
        self.assertEqual(self.data(self.call(second,'/sessions',body))['next_action']['path'],'/platform')
        self.assertEqual(self.data(self.call(second,'/me/context',{'membership_type':'BUSINESS'},method='put'))['next_action']['path'],'/platform/business')
        self.assertEqual(self.data(self.call(first,'/sessions/current',method='get'))['session']['active_membership'],'CONSUMER')
        ServiceMembership.objects.filter(user=user,kind='BUSINESS').update(status='SUSPENDED')
        third=Client(enforce_csrf_checks=True)
        self.assertEqual(self.data(self.call(third,'/sessions',body))['next_action']['path'],'/platform/consumer')
        self.assertEqual(self.call(second,'/me/context',{'membership_type':'BUSINESS'},method='put').status_code,403)

    def test_platform_mail_links_and_pending_email_challenge_stay_in_common_account(self):
        user=self.user('centralrecovery','CONSUMER');client=self.client_for(user)
        services.start_email(user,'central@example.org')
        data=self.data(self.call(client,'/me',method='get'))
        token=AccountToken.objects.get(user=user,purpose='email',consumed_at=None)
        self.assertEqual(data['email_challenge_id'],str(token.pk))
        other=self.client_for(self.user('differentmail'))
        self.assertIsNone(self.data(self.call(other,'/me',method='get'))['email_challenge_id'])
        mail=AccountMail.objects.get(user=user,kind='email')
        code=re.search(r'\d{6}',crypto.unseal(mail.payload)['text']).group()
        self.data(self.call(client,'/me/email/verify',{'challenge_id':str(token.pk),'code':code}))
        self.assertIsNone(self.data(self.call(client,'/me',method='get'))['email_challenge_id'])
        services.recovery_request('username','central@example.org','127.0.0.1')
        text=crypto.unseal(AccountMail.objects.get(user=user,kind='username').payload)['text']
        self.assertIn('/platform/account/recovery#username/',text)
        identifier,secret=text.split('#username/')[1].split('/')
        result=self.data(self.call(Client(enforce_csrf_checks=True),'/recovery/completions',{'purpose':'ID_HINT','token':identifier+'.'+secret}))
        self.assertEqual(result['username'],user.username)

    @override_settings(ADMIN_PASSKEY_REQUIRED=True,ADMIN_LOCAL_LOGIN_ENABLED=True,ADMIN_SESSION_SECONDS=900)
    def test_platform_administrator_primary_login_does_not_grant_review_before_step_up(self):
        from backend.common.admin_roles import assign_role
        user=get_user_model().objects.create_user('centraladmin',password=PASSWORD,is_staff=True)
        assign_role(user,'OPS_ADMIN')
        user.user_permissions.add(Permission.objects.get(content_type__app_label='workspaces',codename='review_workspace'))
        client=Client(enforce_csrf_checks=True)
        csrf=client.get('/api/auth/me').json()['csrf_token']
        response=client.post('/api/auth/login',{'username':user.username,'password':PASSWORD},content_type='application/json',HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code,200,response.content)
        self.assertTrue(response.json()['passkey_required'])
        self.assertTrue(response.json()['platform_enabled'])
        self.assertFalse(response.json()['authenticated'])
        self.assertEqual(response.json()['allowed_menus'],[])
        self.assertNotIn('admin_passkey_user',client.session)
        self.assertEqual(client.get(BASE+'/admin/workspace-claims').status_code,403)

    def test_email_recovery_password_and_withdrawal_adapters_match_contract(self):
        user=self.user('contractemail');client=self.client_for(user)
        body={'email':'platform@example.org','email_consent':True,'policy_version':'test-v1','current_password':PASSWORD}
        response=self.call(client,'/me/email',body);self.assertEqual(response.status_code,202,response.content)
        self.response_matches(response,'/me/email','post')
        challenge=response.json()['data']['challenge_id']
        mail=AccountMail.objects.get(user=user,kind='email')
        code=re.search(r'\d{6}',crypto.unseal(mail.payload)['text']).group()
        response=self.call(client,'/me/email/verify',{'challenge_id':challenge,'code':code})
        self.response_matches(response,'/me/email/verify','post')
        response=self.call(client,'/recovery/requests',{'purpose':'ID_HINT','email':'platform@example.org'})
        self.response_matches(response,'/recovery/requests','post')
        text=crypto.unseal(AccountMail.objects.get(user=user,kind='username').payload)['text']
        _,identifier,secret=text.split('#')[1].split('/')
        response=self.call(client,'/recovery/completions',{'purpose':'ID_HINT','token':identifier+'.'+secret})
        self.response_matches(response,'/recovery/completions','post')
        self.response_matches(self.call(client,'/me/email',{'current_password':PASSWORD},method='delete'),'/me/email','delete')
        response=self.call(client,'/me/password',{'current_password':PASSWORD,'new_password':'Replacement!@2026',
            'new_password_confirmation':'Replacement!@2026'})
        self.response_matches(response,'/me/password','post')
        response=self.call(client,'/me/withdrawals',{'current_password':'Replacement!@2026','confirmation':'전체 계정 탈퇴'})
        self.response_matches(response,'/me/withdrawals','post')

    def test_old_session_and_cached_role_do_not_survive_password_reset(self):
        user=get_user_model().objects.get(pk=self.owner.pk)
        user.set_password('Changed account passphrase!');user.save(update_fields=['password'])
        self.assertEqual(self.owner_client.get(BASE+f'/workspaces/{self.store.pk}').status_code,401)

    def test_reviewer_cannot_approve_own_preexisting_claim(self):
        claim=self.submit(self.owner_client,self.claim(self.owner_client,'demo-branch-002'))
        self.owner.is_staff=True;self.owner.save(update_fields=['is_staff'])
        self.owner.user_permissions.add(Permission.objects.get(content_type__app_label='workspaces',codename='review_workspace'))
        client=self.client_for(self.owner);session=client.session
        session['admin_passkey_user']=str(self.owner.pk);session['admin_passkey_verified_at']=dbnow().timestamp();session.save()
        response=self.call(client,f"/admin/workspace-claims/{claim['id']}/decisions",{'decision':'APPROVE','reason_code':'DEMO_SCENARIO_CONFIRMED'},version=claim['revision'])
        self.assertEqual(response.status_code,403)

    def test_old_provider_lease_cannot_overwrite_new_worker(self):
        claim=self.claim(self.owner_client)
        self.call(self.owner_client,f"/workspace-claims/{claim['id']}/business-checks",version=1)
        newer_lease=uuid.uuid4()
        def takeover(fixture_id):
            from backend.workspaces.fixtures import FIXTURES
            BusinessCheckJob.objects.filter(claim_id=claim['id']).update(lease=newer_lease,fencing_token=2)
            return FIXTURES[fixture_id]
        with patch('backend.workspaces.worker.obtain',side_effect=takeover):
            self.assertTrue(worker.run_one())
        row=BusinessCheckJob.objects.get(claim_id=claim['id'])
        self.assertEqual((row.state,row.lease,row.match_result),('RUNNING',newer_lease,'UNCHECKED'))

    def test_invitation_decline_and_replay_are_terminal(self):
        invitation,client=self.invite(self.owner_client,self.user('declineuser'),self.store)
        self.data(self.call(client,f"/workspace-invitations/{invitation['id']}/declines",version=1))
        self.assertEqual(self.call(client,f"/workspace-invitations/{invitation['id']}/acceptances",version=2).status_code,409)


@override_settings(**TEST_CONFIG)
class PostgreSQLGuards(Helpers,TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor,'postgresql','This required suite must run on disposable PostgreSQL')

    def test_database_requires_owner_at_commit(self):
        business=BusinessEntity.objects.create(registration_lookup=secrets.token_hex(32),registration_ciphertext=crypto.seal('synthetic'),display_name='synthetic')
        with self.assertRaises(IntegrityError),transaction.atomic():
            Workspace.objects.create(business=business,location_key='one',display_name='synthetic',address_ciphertext=crypto.seal('synthetic'),status='ACTIVE')

    def test_database_prevents_last_owner_removal_and_duplicate_owner(self):
        owner=self.user('sqlowner');store,member=self.setup_store(owner)
        with self.assertRaises(IntegrityError),transaction.atomic():
            WorkspaceMembership.objects.filter(pk=member.pk).update(status='REVOKED')
        other=self.user('sqlduplicate')
        with self.assertRaises(IntegrityError),transaction.atomic():
            WorkspaceMembership.objects.create(workspace=store,user=other,role='OWNER')

    def test_database_inviter_must_belong_to_same_workspace(self):
        owner=self.user('sqlinviteowner');store,member=self.setup_store(owner)
        other=self.user('sqlother');second,_=self.setup_store(other,'two')
        with self.assertRaises(IntegrityError),transaction.atomic():
            WorkspaceInvitation.objects.create(workspace=second,invitee=owner,role='STAFF',inviter_membership=member,inviter_version=1,expires_at=dbnow()+timedelta(hours=1))

    def test_database_audit_is_immutable_except_actor_anonymization(self):
        owner=self.user('sqlaudit');store,_=self.setup_store(owner)
        row=AuthorizationAudit.objects.create(workspace=store,actor=owner,action='test',resource_id=str(store.pk),request_id='test')
        with self.assertRaises(IntegrityError),transaction.atomic():
            AuthorizationAudit.objects.filter(pk=row.pk).update(action='tampered')
        AuthorizationAudit.objects.filter(pk=row.pk).update(actor=None)

    def parallel(self,operations):
        barrier=Barrier(len(operations))
        def invoke(operation):
            from django.db import close_old_connections, connections
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            return list(pool.map(invoke,operations))

    def test_concurrent_invitation_acceptance_is_single_effect(self):
        owner=self.user('parallelowner');store,_=self.setup_store(owner)
        target=self.user('paralleltarget');invitation,_=self.invite(self.client_for(owner),target,store)
        clients=[self.client_for(target),self.client_for(target)]
        path=f"/workspace-invitations/{invitation['id']}/acceptances"
        responses=self.parallel([lambda c=c:self.call(c,path,version=1) for c in clients])
        self.assertEqual(sorted(r.status_code for r in responses),[200,412])
        self.assertEqual(WorkspaceMembership.objects.filter(user=target,workspace=store).count(),1)
        self.assertEqual(AuthorizationAudit.objects.filter(action='invitation.accepted').count(),1)

    def test_concurrent_approval_is_single_workspace_and_owner(self):
        applicant=self.user('parallelapplicant');client=self.client_for(applicant)
        claim=self.submit(client,self.claim(client))
        _,first=self.reviewer('parallelreviewone');_,second=self.reviewer('parallelreviewtwo')
        path=f"/admin/workspace-claims/{claim['id']}/decisions"
        body={'decision':'APPROVE','reason_code':'DEMO_SCENARIO_CONFIRMED'}
        responses=self.parallel([lambda c=c:self.call(c,path,body,version=claim['revision']) for c in [first,second]])
        self.assertEqual(sorted(r.status_code for r in responses),[200,412])
        self.assertEqual(Workspace.objects.count(),1)
        self.assertEqual(WorkspaceMembership.objects.filter(role='OWNER').count(),1)

    def test_revocation_serializes_with_music_application(self):
        owner=self.user('raceowner');store,_=self.setup_store(owner)
        target=self.user('racestaff');member=WorkspaceMembership.objects.create(workspace=store,user=target,role='STAFF',granted_by=owner)
        owner_client=self.client_for(owner);staff_client=self.client_for(target)
        path=f'/workspaces/{store.pk}/memberships/{member.pk}/revocations';body={'reason_code':'STAFF_DEPARTURE'}
        grant=self.grant_for(owner_client,path,body,1)
        music=f'/workspaces/{store.pk}/music'
        responses=self.parallel([lambda:self.call(owner_client,path,body,version=1,grant=grant),
            lambda:self.call(staff_client,music,{'action':'APPLY','playlist_id':'demo-calm'},method='patch',version=1)])
        self.assertEqual(responses[0].status_code,200,responses[0].content)
        self.assertIn(responses[1].status_code,[200,404])
        self.assertEqual(self.call(staff_client,music,{'action':'APPLY','playlist_id':'demo-bright'},method='patch',version=1).status_code,404)
        member.refresh_from_db();self.assertEqual(member.status,'REVOKED')
