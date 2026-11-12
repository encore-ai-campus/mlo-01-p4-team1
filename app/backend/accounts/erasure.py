"""Delete live SQL account data. External logs/backups require the operator erasure procedure."""
from django.contrib.auth import get_user_model
from django.db import transaction
from backend.recommendation.errors import require
from backend.common.admin_roles import revoke_sessions
from backend.common import models as common
from backend.recommendation import models as rec
from .models import MemberProfile


@transaction.atomic
def erase(profile):
    profile = MemberProfile.objects.select_for_update().get(pk=profile.pk)
    user = get_user_model().objects.select_for_update().get(pk=profile.user_id)
    from backend.workspaces.models import WorkspaceMembership, WorkspaceInvitation
    require(not WorkspaceMembership.objects.filter(user=user,status='ACTIVE',role='OWNER',
        workspace__status__in=['ACTIVE','SUSPENDED']).exists(),
        'OWNERSHIP_RESOLUTION_REQUIRED','소유 매장을 닫거나 관리책임을 먼저 이전해 주세요.',409)
    WorkspaceInvitation.objects.filter(invitee=user,state='PENDING').update(state='REVOKED')
    owner = str(profile.owner_id)
    revoke_sessions(user)
    events = common.FeedbackEvent.objects.filter(owner_id=owner)
    event_ids = list(events.values_list('pk', flat=True))
    common.StreamRecord.objects.filter(event_id__in=event_ids).delete()
    common.ProjectedState.objects.filter(event_id__in=event_ids).delete()
    common.Projection.objects.filter(event_id__in=event_ids).delete()
    common.Outbox.objects.filter(event_id__in=event_ids).delete()
    families = rec.Family.objects.filter(owner_id=owner)
    playlists = rec.Playlist.objects.filter(family__in=families)
    common.FeedbackState.objects.filter(playlist__in=playlists).delete()
    events.delete()
    rec.DemoFeedback.objects.filter(playlist__in=playlists).delete()
    playlists.delete()
    rec.Job.objects.filter(family__in=families).delete()
    families.delete()
    rec.Idempotency.objects.filter(owner_id=owner).delete()
    rec.PendingEvent.objects.filter(payload__owner_id=owner).delete()
    rec.OwnerLock.objects.filter(owner_id=owner).delete()
    # Member data never uses protected administrator audit relations.
    user.delete()
