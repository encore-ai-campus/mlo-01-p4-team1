"""Platform authorization ledger. Private account data stays in accounts."""
import uuid
from django.conf import settings
from django.db import models


class Record(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class ServiceMembership(Record):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(max_length=12)
    status = models.CharField(max_length=12, default='ACTIVE')
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'kind'], name='platform_service_user_kind'),
            models.CheckConstraint(condition=models.Q(kind__in=['CONSUMER', 'BUSINESS']), name='platform_service_kind'),
            models.CheckConstraint(condition=models.Q(status__in=['ACTIVE', 'SUSPENDED', 'REMOVED']), name='platform_service_status'),
        ]


class BusinessEntity(Record):
    mode = models.CharField(max_length=8, default='DEMO')
    registration_lookup = models.CharField(max_length=64)
    registration_ciphertext = models.TextField()
    display_name = models.CharField(max_length=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['mode', 'registration_lookup'], name='platform_business_identity')]


class Workspace(Record):
    business = models.ForeignKey(BusinessEntity, on_delete=models.PROTECT)
    mode = models.CharField(max_length=8, default='DEMO')
    location_key = models.CharField(max_length=100)
    display_name = models.CharField(max_length=100)
    address_ciphertext = models.TextField()
    status = models.CharField(max_length=12, default='PENDING')
    version = models.PositiveBigIntegerField(default=1)
    authz_version = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['business', 'location_key'], name='platform_business_location'),
            models.CheckConstraint(condition=models.Q(status__in=['PENDING', 'ACTIVE', 'SUSPENDED', 'CLOSED']), name='platform_workspace_status'),
        ]


class WorkspaceClaim(Record):
    applicant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    payload_ciphertext = models.TextField()
    fixture_id = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default='DRAFT')
    version = models.PositiveBigIntegerField(default=1)
    content_revision = models.PositiveBigIntegerField(default=1)
    approved_workspace = models.OneToOneField(Workspace, on_delete=models.PROTECT, null=True)

    class Meta:
        permissions = [('review_workspace', 'Review workspace authority claims')]


class BusinessCheckJob(Record):
    claim = models.ForeignKey(WorkspaceClaim, on_delete=models.CASCADE)
    content_revision = models.PositiveBigIntegerField()
    state = models.CharField(max_length=16, default='QUEUED', db_index=True)
    match_result = models.CharField(max_length=16, default='UNCHECKED')
    business_status = models.CharField(max_length=16, default='UNKNOWN')
    checked_at = models.DateTimeField(null=True)
    lease = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    fencing_token = models.PositiveBigIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['claim', 'content_revision'], name='platform_check_content')]


class ClaimDecision(Record):
    claim = models.ForeignKey(WorkspaceClaim, on_delete=models.CASCADE)
    claim_revision = models.PositiveBigIntegerField()
    content_revision = models.PositiveBigIntegerField()
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    decision = models.CharField(max_length=16)
    reason_code = models.CharField(max_length=48)
    evidence_kind = models.CharField(max_length=32, default='DEMO_SCENARIO')
    policy_version = models.CharField(max_length=80)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['claim', 'claim_revision'], name='platform_claim_decision_revision')]


class WorkspaceMembership(Record):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=12)
    status = models.CharField(max_length=12, default='ACTIVE')
    version = models.PositiveBigIntegerField(default=1)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'user'], name='platform_workspace_user'),
            models.UniqueConstraint(fields=['workspace'], condition=models.Q(status='ACTIVE', role='OWNER'), name='platform_one_active_owner'),
            models.CheckConstraint(condition=models.Q(role__in=['OWNER', 'MANAGER', 'STAFF']), name='platform_workspace_role'),
            models.CheckConstraint(condition=models.Q(status__in=['ACTIVE', 'REVOKED']), name='platform_membership_status'),
        ]


class InvitationReceiver(Record):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    state = models.CharField(max_length=12, default='OPEN')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user'], condition=models.Q(state='OPEN'), name='platform_receiver_open')]


class WorkspaceInvitation(Record):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    invitee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=12)
    inviter_membership = models.ForeignKey(WorkspaceMembership, on_delete=models.PROTECT, related_name='+')
    inviter_version = models.PositiveBigIntegerField()
    state = models.CharField(max_length=12, default='PENDING')
    expires_at = models.DateTimeField()
    version = models.PositiveBigIntegerField(default=1)
    accepted_membership = models.ForeignKey(WorkspaceMembership, on_delete=models.SET_NULL, null=True, related_name='+')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'invitee'], condition=models.Q(state='PENDING'), name='platform_invitation_pending'),
            models.CheckConstraint(condition=models.Q(role__in=['STAFF', 'MANAGER']), name='platform_invitation_role'),
        ]


class IdempotencyRecord(Record):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    path = models.CharField(max_length=500)
    method = models.CharField(max_length=8)
    key_digest = models.CharField(max_length=64)
    request_digest = models.CharField(max_length=64)
    response = models.JSONField()
    status = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'path', 'method', 'key_digest'], name='platform_idempotency_scope')]


class ReauthenticationGrant(Record):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    session_digest = models.CharField(max_length=64)
    digest = models.CharField(max_length=64, unique=True)
    intent = models.JSONField()
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True)
    consumed_by = models.OneToOneField(IdempotencyRecord, on_delete=models.SET_NULL, null=True)


class AuthorizationAudit(Record):
    workspace = models.ForeignKey(Workspace, on_delete=models.SET_NULL, null=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=64)
    resource_id = models.CharField(max_length=64)
    resource_version = models.PositiveBigIntegerField(default=1)
    request_id = models.CharField(max_length=64)


class InternalOutbox(Record):
    audit = models.OneToOneField(AuthorizationAudit, on_delete=models.PROTECT)
    state = models.CharField(max_length=12, default='PENDING')
    # Minimal identifier event, never business inputs, tokens, email or credentials.
    payload = models.JSONField()


class WorkspaceMusicState(Record):
    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE)
    preferences = models.JSONField(default=dict)
    exclusions = models.JSONField(default=list)
    applied_playlist = models.CharField(max_length=64, default='')
    version = models.PositiveBigIntegerField(default=1)
