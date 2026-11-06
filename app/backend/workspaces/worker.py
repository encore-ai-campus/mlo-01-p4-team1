"""Leased synthetic checks. No key or external network client exists in this adapter."""
import uuid
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from .authorization import dbnow
from .fixtures import FIXTURES
from .models import BusinessCheckJob, WorkspaceClaim


def obtain(fixture_id):
    return FIXTURES.get(fixture_id)


def run_one():
    with transaction.atomic():
        now = dbnow()
        job = BusinessCheckJob.objects.select_for_update(skip_locked=True).filter(
            Q(state='QUEUED') | Q(state='RUNNING', lease_until__lt=now)).order_by('created_at').first()
        if not job:
            return False
        job.state, job.lease, job.lease_until = 'RUNNING', uuid.uuid4(), now+timedelta(seconds=30)
        job.fencing_token += 1
        job.attempts += 1
        job.save()
        identifier, lease, generation = job.pk, job.lease, job.fencing_token
        claim_id, revision = job.claim_id, job.content_revision
    # Provider I/O would happen outside locks; this adapter reads synthetic fixtures only.
    claim = WorkspaceClaim.objects.get(pk=claim_id)
    result = obtain(claim.fixture_id)
    with transaction.atomic():
        claim = WorkspaceClaim.objects.select_for_update().get(pk=claim_id)
        job = BusinessCheckJob.objects.select_for_update().get(pk=identifier)
        if job.lease != lease or job.fencing_token != generation or job.lease_until <= dbnow():
            return True
        if claim.content_revision != revision or claim.state not in ('DRAFT','NEEDS_MORE','SUBMITTED'):
            job.state = 'STALE'
        else:
            job.state = 'SUCCEEDED'
            job.match_result = result['match'] if result else 'UNAVAILABLE'
            job.business_status = result['status'] if result else 'UNKNOWN'
            job.checked_at = dbnow()
        job.lease = job.lease_until = None
        job.save()
    return True
