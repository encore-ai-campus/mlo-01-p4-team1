"""The synthetic playlist catalog shared by workspace and customer views."""

from .fixtures import FIXTURES
from .models import WorkspaceMusicState


DEMO_PLAYLISTS = {
    "demo-calm": "합성 차분한 선곡표",
    "demo-bright": "합성 밝은 선곡표",
}


def public_applied_playlist(venue_id):
    links = [(fixture_id, fixture) for fixture_id, fixture in FIXTURES.items()
             if fixture.get("venue_id") == venue_id]
    if len(links) != 1:
        return None

    fixture_id, fixture = links[0]
    try:
        playlist_id = WorkspaceMusicState.objects.values_list("applied_playlist", flat=True).get(
            workspace__status="ACTIVE",
            workspace__mode="DEMO",
            workspace__location_key=fixture["location"],
            workspace__workspaceclaim__fixture_id=fixture_id,
            workspace__workspaceclaim__state="APPROVED",
        )
    except (WorkspaceMusicState.DoesNotExist, WorkspaceMusicState.MultipleObjectsReturned):
        return None

    name = DEMO_PLAYLISTS.get(playlist_id)
    return {"id": playlist_id, "name": name} if name is not None else None
