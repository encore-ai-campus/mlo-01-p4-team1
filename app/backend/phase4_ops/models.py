import uuid
from typing import ClassVar

from django.db import models


class AccountLock(models.Model):
    """Application mutex; does not create or grant an authentication account."""
    key = models.CharField(max_length=200, primary_key=True)


class SongSubmission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_id = models.CharField(max_length=128, db_index=True)
    venue = models.ForeignKey('workspaces.Workspace', on_delete=models.PROTECT)
    policy = models.JSONField(default=dict)
    quota_key = models.CharField(max_length=200, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class SongItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(SongSubmission, on_delete=models.PROTECT, related_name='items')
    position = models.PositiveSmallIntegerField()
    track_name = models.CharField(max_length=300)
    artist_name = models.CharField(max_length=300)
    youtube_url = models.URLField(max_length=2048, blank=True)
    normalized_key = models.CharField(max_length=64, db_index=True)
    exact_key = models.CharField(max_length=64, db_index=True)
    review_status = models.CharField(max_length=16, default='PENDING', db_index=True)
    pipeline_status = models.CharField(max_length=24, default='NOT_QUEUED')
    revision = models.PositiveIntegerField(default=1)
    public_reason = models.CharField(max_length=1000, blank=True)
    internal_note = models.CharField(max_length=2000, blank=True)
    reviewer_id = models.CharField(max_length=128, blank=True)
    track_id = models.CharField(max_length=128, blank=True)
    catalog_version = models.CharField(max_length=128, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list] = [models.UniqueConstraint(fields=['submission', 'position'], name='p4_song_position')]
        permissions: ClassVar[list] = [('review_songs', '곡 신청 검수')]


class PipelineTask(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(SongItem, on_delete=models.PROTECT, related_name='tasks')
    is_current = models.BooleanField(default=True)
    status = models.CharField(max_length=24, default='INPUT_PENDING', db_index=True)
    source_ref = models.CharField(max_length=500, blank=True)
    input_payload = models.JSONField(default=dict)
    registration = models.JSONField(null=True)
    revision = models.PositiveIntegerField(default=1)
    lease_token_hash = models.CharField(max_length=64, blank=True)
    lease_expires_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list] = [
            models.UniqueConstraint(fields=['item'], condition=models.Q(is_current=True), name='p4_current_task'),
        ]


class InferenceReceipt(models.Model):
    result_id = models.CharField(max_length=128, primary_key=True)
    receipt_id = models.UUIDField(default=uuid.uuid4, unique=True)
    task = models.OneToOneField(PipelineTask, on_delete=models.PROTECT, related_name='receipt')
    payload_sha256 = models.CharField(max_length=64)
    candidate = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)


class ActionReceipt(models.Model):
    account_id = models.CharField(max_length=128)
    scope = models.CharField(max_length=80)
    key = models.CharField(max_length=200)
    fingerprint = models.CharField(max_length=64)
    response = models.JSONField()
    status = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints: ClassVar[list] = [models.UniqueConstraint(fields=['account_id', 'scope', 'key'], name='p4_idempotent_action')]


class History(models.Model):
    entity_type = models.CharField(max_length=24)
    entity_id = models.CharField(max_length=128, db_index=True)
    actor_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=40)
    revision = models.PositiveIntegerField()
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
