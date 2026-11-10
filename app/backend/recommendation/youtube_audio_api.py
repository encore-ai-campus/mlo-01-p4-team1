"""Owned playlist audio preparation; never mutates mappings or music state."""
from rest_framework.response import Response

from .api import Endpoint
from . import service
from .errors import require
from .models import Playlist
from .youtube_audio import validate_audio_request, resolve_audio


class YoutubeAudio(Endpoint):
    def post(self, request, pk):
        owner = self.owner(request)
        playlist = service.owned(Playlist, owner, pk)
        track_id, video_id = validate_audio_request(request.data)
        require(set(playlist.snapshot['context']['excluded_track_ids']) == set(playlist.family.excluded),
                'EXCLUSIONS_NOT_APPLIED', '제외 변경을 반영한 새 선곡표를 먼저 받아 주세요.', 409)
        track = next((item for item in playlist.snapshot['items'] if item['track_id'] == track_id), None)
        require(track is not None, 'NOT_FOUND', '선곡표에 있는 곡만 재생할 수 있어요.', 404)
        return Response(resolve_audio(track, video_id, owner))
