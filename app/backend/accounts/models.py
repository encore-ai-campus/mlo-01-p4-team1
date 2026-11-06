import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


class MemberProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    owner_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    nickname = models.CharField(max_length=20)
    nickname_key = models.CharField(max_length=80, unique=True)
    store_type = models.CharField(max_length=24, default='CAFE')
    email_ciphertext = models.TextField(default='')
    email_lookup = models.CharField(max_length=64, unique=True, null=True)
    email_confirmed_at = models.DateTimeField(null=True)
    email_consent_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(store_type='CAFE'), name='member_store_cafe')]


class PolicyReceipt(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    version = models.CharField(max_length=80)
    kind = models.CharField(max_length=32)
    accepted = models.BooleanField()
    document_sha256 = models.CharField(max_length=64, default='')
    created_at = models.DateTimeField(auto_now_add=True)


class AccountToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    purpose = models.CharField(max_length=24)
    digest = models.CharField(max_length=64)
    email_ciphertext = models.TextField(default='')
    email_lookup = models.CharField(max_length=64, default='')
    expires_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AccountMail(models.Model):
    token = models.ForeignKey(AccountToken, on_delete=models.CASCADE, null=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # Address AND body (including the raw code/link) are encrypted, never logged.
    payload = models.TextField()
    kind = models.CharField(max_length=24)
    state = models.CharField(max_length=12, default='PENDING', db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    expires_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_until = models.DateTimeField(null=True)
    lease = models.UUIDField(null=True)
    last_error = models.CharField(max_length=64, default='')
    created_at = models.DateTimeField(auto_now_add=True)


class AccountLimit(models.Model):
    # HMAC subject; no raw email or IP retained here.
    key = models.CharField(primary_key=True, max_length=64)
    window_at = models.DateTimeField()
    last_at = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)


class SavedPlaylist(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    source_id = models.CharField(max_length=36)
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'source_id'], name='member_saved_source')]


class AdminPasskey(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    credential_id = models.TextField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveBigIntegerField(default=0)
    label = models.CharField(max_length=60, default='패스키')
    created_at = models.DateTimeField(auto_now_add=True)


class PasskeyChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    session_digest = models.CharField(max_length=64)
    purpose = models.CharField(max_length=16)
    challenge = models.BinaryField()
    expires_at = models.DateTimeField()


class PasskeyEnrollment(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
