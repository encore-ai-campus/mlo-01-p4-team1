"""Seed only the explicitly isolated local platform database. No published passwords."""
import hashlib
import json
from pathlib import Path
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from backend.accounts import services
from backend.accounts.models import AdminPasskey, PasskeyEnrollment
from backend.common.admin_roles import assign_role
from backend.workspaces.authorization import enabled, dbnow
from backend.workspaces.models import ServiceMembership


class Command(BaseCommand):
    help = 'Seed synthetic local MVP accounts from a private owner-only input file.'

    def add_arguments(self,parser):
        parser.add_argument('--inputs',default='/mvp-private/local-inputs.json')

    @transaction.atomic
    def handle(self,*args,**options):
        enabled()
        if settings.DATABASES['default']['NAME']!='platform_mvp':
            raise CommandError('Only isolated platform_mvp database is allowed')
        data=json.loads(Path(options['inputs']).read_text())
        for username,kind,nickname in [('mvpowner','BUSINESS','시연소유자'),('mvpstaff','BUSINESS','시연직원'),('mvpconsumer','CONSUMER','시연사용자')]:
            user=get_user_model().objects.filter(username=username).first()
            if user is None:
                profile=services.signup({'username':username,'password':data['password'],'password_confirmation':data['password'],
                    'nickname':nickname,'policy_version':settings.MEMBER_POLICY_VERSION,'terms_accepted':True,
                    'privacy_acknowledged':True,'age_confirmed':True,'email_consent':False})
                user=profile.user
                ServiceMembership.objects.create(user=user,kind=kind)
        for username,ticket in [('mvpreviewer',data['enrollment_ticket']),('mvpqareviewer',data['qa_enrollment_ticket'])]:
            user,created=get_user_model().objects.get_or_create(username=username,defaults={'is_staff':True})
            if created:
                user.set_password(data['password']);user.save()
                assign_role(user,'OPS_ADMIN')
                user.user_permissions.add(Permission.objects.get(content_type__app_label='workspaces',codename='review_workspace'))
            if not AdminPasskey.objects.filter(user=user).exists():
                PasskeyEnrollment.objects.update_or_create(user=user,defaults={'digest':hashlib.sha256(ticket.encode()).hexdigest(),
                    'expires_at':dbnow()+timedelta(minutes=10)})
        self.stdout.write('Synthetic accounts ready: mvpowner / mvpstaff / mvpconsumer / mvpreviewer. Credentials remain in the private input file.')
