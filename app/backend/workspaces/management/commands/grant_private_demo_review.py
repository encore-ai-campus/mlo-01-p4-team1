"""Grant only the existing workspace-review permission to the reviewed administrator."""
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from backend.accounts.models import AdminPasskey
from backend.accounts.passkeys import configuration
from backend.common.admin_roles import permitted
from backend.recommendation.errors import DomainError
from backend.workspaces import private_demo


PROJECT_ADMIN = private_demo.REVIEWER_USERNAME
REVIEW_PERMISSION = "workspaces.review_workspace"


class Command(BaseCommand):
    help = "Grant the exact synthetic-demo review permission to the existing projectadmin."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Verify eligibility without changing permissions.")
        parser.add_argument("--expect-user-id", type=int, required=True, help="Pin the previously reviewed account's database ID.")

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("postgresql_required")
        if settings.PLATFORM_ENABLED or settings.MEMBER_LOCAL_PREVIEW or not private_demo.demo_window_open():
            raise CommandError("private_demo_grant_unavailable")
        if settings.MEMBER_POLICY_VERSION not in ("", private_demo.POLICY_VERSION):
            raise CommandError("private_demo_policy_mismatch")
        try:
            configuration()
        except DomainError as error:
            raise CommandError(error.code) from None

        User = get_user_model()
        with transaction.atomic():
            users = User.objects
            if options["check"]:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            else:
                users = users.select_for_update()
            reviewer = users.filter(username=PROJECT_ADMIN, is_staff=True, is_active=True).first()
            if reviewer is None or not permitted(reviewer, "manage_music"):
                raise CommandError("active_projectadmin_required")
            if reviewer.pk != options["expect_user_id"]:
                raise CommandError("projectadmin_identity_mismatch")
            if not AdminPasskey.objects.filter(user=reviewer).exists():
                raise CommandError("projectadmin_passkey_required")
            permission = Permission.objects.filter(
                content_type__app_label="workspaces", content_type__model="workspaceclaim", codename="review_workspace",
            ).first()
            if permission is None:
                raise CommandError("workspace_review_permission_missing")

            granted = reviewer.has_perm(REVIEW_PERMISSION)
            changed = not granted and not options["check"]
            if changed:
                reviewer.user_permissions.add(permission)
                granted = User.objects.get(pk=reviewer.pk).has_perm(REVIEW_PERMISSION)
                if not granted:
                    raise CommandError("workspace_review_grant_unverified")

        self.stdout.write(json.dumps({
            "username": PROJECT_ADMIN, "permission": REVIEW_PERMISSION,
            "policy_version": private_demo.POLICY_VERSION, "check_only": options["check"],
            "granted": granted, "changed": changed,
        }, sort_keys=True))
