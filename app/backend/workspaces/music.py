"""The synthetic playlist catalog shared by workspace and customer views."""

from .fixtures import FIXTURES
from .models import WorkspaceMusicState
from backend.accounts.models import MemberProfile
from backend.recommendation.models import Playlist
from django.db.models import F


DEMO_PLAYLISTS = {
    "demo-calm": "합성 차분한 선곡표",
    "demo-bright": "합성 밝은 선곡표",
}


def approved_owner_playlists(workspace):
    """Only the active store owner's currently approved personal playlists."""
    owner_ids = MemberProfile.objects.filter(
        user__is_active=True, user__workspacemembership__workspace=workspace,
        user__workspacemembership__role='OWNER', user__workspacemembership__status='ACTIVE',
        user__servicemembership__kind='BUSINESS', user__servicemembership__status='ACTIVE',
    ).values_list('owner_id', flat=True)
    return Playlist.objects.filter(
        family__owner_id__in=[str(value) for value in owner_ids],
        family__approved_playlist_id=F('id'),
    ).select_related('family')


def playlist_name(playlist):
    summary = playlist.snapshot.get('summary') if isinstance(playlist.snapshot, dict) else None
    return summary.strip()[:100] if isinstance(summary, str) and summary.strip() else '승인된 선곡표'


def public_applied_playlist(venue_id):
    links = [(fixture_id, fixture) for fixture_id, fixture in FIXTURES.items()
             if fixture.get("venue_id") == venue_id]
    if len(links) != 1:
        return None

    fixture_id, fixture = links[0]
    try:
        state = WorkspaceMusicState.objects.select_related('workspace').get(
            workspace__status="ACTIVE",
            workspace__mode="DEMO",
            workspace__location_key=fixture["location"],
            workspace__workspaceclaim__fixture_id=fixture_id,
            workspace__workspaceclaim__state="APPROVED",
        )
    except (WorkspaceMusicState.DoesNotExist, WorkspaceMusicState.MultipleObjectsReturned):
        return None

    playlist_id = state.applied_playlist
    name = DEMO_PLAYLISTS.get(playlist_id)
    if name is not None:
        return {"id": playlist_id, "name": name}
    playlist = approved_owner_playlists(state.workspace).filter(pk=playlist_id).first()
    return {"id": playlist_id, "name": playlist_name(playlist)} if playlist else None
