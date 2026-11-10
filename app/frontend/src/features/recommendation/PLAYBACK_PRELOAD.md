# 다음 곡 사전 준비 기능과 프런트 연결 계약

2026-11-10 기능 구현본입니다. 사용자 결정에 따라 기존 `YoutubePlayer.tsx`와 `YoutubePlayer.css`는 작업 전 내용으로 복원했습니다. 새 화면·레이아웃은 적용하지 않았습니다. 기존 화면이 이 기능을 자동으로 사용하거나 배포된 상태가 아닙니다.

## 구현 파일

| 위치 | 역할 |
| --- | --- |
| `youtubePlayerController.ts` | 실제 현재 곡의 첫 PLAYING에서 다음 1곡 후보 조회·사전 준비, 곡 종료/다음 버튼에서 준비 결과 소비 |
| `youtubeMediaPool.ts` | 현재 곡과 다음 곡의 미디어를 분리해 보관하고, 준비한 미디어를 다시 로드하지 않고 승격 |
| `youtubeIframeApi.ts` | 공식 iframe API 로딩·타입 |
| 백엔드 `recommendation/youtube_audio_api.py` | 소유한 선곡표의 검수된 영상만 오디오 준비 |
| 백엔드 `recommendation/youtube_audio.py`·`youtube_audio_worker.py` | 비DRM 오디오 메타데이터 조회·한시적 URL·타임아웃·요청 제한 |

이 문서는 `app/frontend/src/features/recommendation/`에 둡니다. 백엔드는 `app/backend/` 기준입니다.

## 처리 순서

1. 현재 곡이 실제 재생되면 바로 다음 곡의 영상 후보를 조회합니다.
2. 별도 iframe에서 음소거로 실제 시간 진행을 확인합니다. 미디어 시간이 최소 2초 진행하고 실제 시간도 2.5초 이상 지나야 준비 완료로 표시합니다.
3. 오류 101·150이면 해당 준비용 iframe을 제거하고 `/youtube-audio`를 호출합니다. 오디오는 `preload=auto`, 버퍼 최소 2초와 `readyState >= 3`을 확인합니다.
4. 이미 별도 수동 검수로 native 재생이 확인된 영상은 `playback_hint: AUDIO`로 바로 오디오 준비가 가능합니다. iframe 검수 상태나 DB를 자동 승격하지 않습니다.
5. 현재 곡 ENDED 또는 명시적 다음 선택에서 준비한 플레이어를 그대로 사용합니다. 같은 곡의 후보/오디오 주소를 다시 조회하지 않습니다.
6. 모든 후보가 실제 불가로 확인된 다음 곡은 기한이 있는 NO_MATCH로 보관합니다. 전환 시 사유를 기록하고 재시도 대기 없이 건너뜁니다. 일시적인 네트워크·인증·자동재생 문제를 곡 불가로 확정하지 않습니다.

보관은 다음 한 곡으로 제한합니다. 기본 90초마다 다시 준비하고 120초 이상 지난 결과는 폐기합니다. 오디오 주소가 먼저 만료되면 실제 만료 30초 전부터 갱신하고 15초 전까지만 승격에 사용합니다. 예를 들어 70초 남은 주소는 40초 후 갱신·55초 후 사용 만료입니다. 주소 만료 임박은 곡 자체의 재생 불가로 확정하지 않습니다. 이전/재시도/목록 교체/차단/컴포넌트 종료에서는 준비 요청을 취소하고 늦은 응답을 무시합니다.

## 프런트에서 연결할 최소 예제

아래 코드는 화면 디자인이 아니라 연결 예제입니다. `host`, `playlistId`, `tracks`와 상태 콜백은 새 프런트가 제공합니다. React에서는 effect 안에서 생성하고 cleanup에서 해제합니다.

```ts
import {api, csrfHeaders, requestJson} from '../../shared/api';
import {loadYoutubeIframeApi} from './youtubeIframeApi';
import {YoutubeMediaPool, type AudioResolution} from './youtubeMediaPool';
import {YoutubePlaybackController, type PlaybackResolution} from './youtubePlayerController';

const YT = await loadYoutubeIframeApi();
let controller: YoutubePlaybackController;
const media = new YoutubeMediaPool({
  api: YT,
  host, // 프런트가 제공하는 HTMLDivElement
  current: () => {
    const state = controller.snapshot();
    const track = tracks[state.index];
    return state.candidate && track ? {candidate: state.candidate, track} : null;
  },
  resolveAudio: (track, candidate, signal) => requestJson<AudioResolution>(
    `/api/playlists/${encodeURIComponent(playlistId)}/youtube-audio`, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', signal,
      headers: {'Content-Type': 'application/json', ...csrfHeaders()},
      body: JSON.stringify({track_id: track.track_id, video_id: candidate.video_id}),
    }, 25000),
  onState: state => controller.onPlayerState(state),
  onError: code => controller.onPlayerError(code),
  onAutoplayBlocked: () => controller.onAutoplayBlocked(),
  onRoute: route => updateRoute(route),
  onPreparation: message => updatePreparation(message),
  onTransition: measurement => recordTransition(measurement), // 선택적 측정 콜백
});
controller = new YoutubePlaybackController(tracks, media,
  (trackId, excluded, signal) => api<PlaybackResolution>(
    `/api/playlists/${encodeURIComponent(playlistId)}/youtube-playback`, 'POST',
    {track_id: trackId, excluded_video_ids: excluded}, undefined, signal),
  state => renderPlaybackState(state),
);
await controller.start();

// 버튼: controller.play()/pause()/next()/previous()/retry()
// 제외 항목 검토 등 재생 보류: controller.setBlocked(reason)
// 화면 종료 또는 선곡표 교체:
const cleanup = () => {controller.destroy(); media.destroy();};
// React effect cleanup에서 cleanup()을 호출합니다.
```

`YoutubeMediaPool`이 만드는 DOM 클래스는 `.youtube-media-slot`, `.is-active`, `.is-preview`, `.youtube-media-label`, `.youtube-media-audio`입니다. 새 프런트는 현재·다음 미디어 영역을 구분해 스타일링하며, iframe은 최소 200×200을 확보합니다. 준비용 iframe은 화면에 표시되는 음소거 미리보기입니다. 준비 이벤트를 현재 곡 PLAYING/ENDED로 전달하면 안 됩니다. 기본 새 YouTube 탭 기능(`YoutubeNativePlayback`)과 이 삽입 플레이어 모듈은 별도입니다.

## 오디오 API

`POST /api/playlists/{playlistId}/youtube-audio`

```json
{"track_id": "소유한 선곡표의 곡 ID", "video_id": "검수된 영상 ID"}
```

성공 응답은 `status=READY`, `video_id`, `audio_url`, `thumbnail_url`, `expires_at`(UTC ISO8601)입니다. URL은 메모리에서만 사용하며 DB·파일·분석 로그에 저장하지 않습니다. 응답은 `Cache-Control: no-store`입니다. 원본 watch URL을 `<audio src>`에 넣는 방식이 아닙니다. 이미지는 영상 썸네일이며 앨범아트 동일성은 별도 확인합니다.

실패 응답의 `status=UNAVAILABLE`와 `reason`을 구분합니다. `BUSY`, `RATE_LIMITED`, `TIMEOUT`, `EXTRACTOR_UNAVAILABLE`, `NETWORK_ERROR`는 사전 준비의 `unknown`으로 처리해 현재 곡을 방해하지 않습니다. 권한·검수 미완료는 4xx입니다. 다른 사람의 선곡표·선곡표 밖 곡·임의 영상 URL은 받지 않습니다.

추출은 프로세스별 동시 2개, 작업 20초, 소켓 5초, 재시도 0회입니다. 서버 URL 캐시는 프로세스 메모리에 최대 64개·60초이며, 실제 URL 만료가 90초 미만 남으면 캐시 TTL이 남아 있어도 폐기하고 다시 조회합니다. 공유 캐시에는 요청 제한 카운터만 둡니다. 다중 웹 워커 전체의 동시 실행 상한은 이번 구현 범위에 포함되지 않습니다.

Vite의 해당 오디오 API 프록시 제한은 27초입니다. 일반 `api()`의 기본 제한(15초)을 그대로 사용하지 말고 위 `requestJson(...,25000)` 예제를 사용합니다. 백엔드 런타임에는 잠금된 `yt-dlp==2026.10.26`이 필요합니다. 새 프런트 연결과 백엔드 이미지 재빌드·배포는 별도 작업입니다.

## 검증 범위

- 프런트 전체 256개 테스트와 TypeScript·Vite 빌드 통과. 별도 백엔드 27개 단위/API 검사 통과.
- 임시 연결 컴포넌트에서 실제 공개 영상 3개로 iframe → 오디오 → iframe 전환 확인. 최종 시험의 `load` 호출부터 실제 `playing` 이벤트까지 각각 1ms와 281ms였습니다. 첫 곡 준비 시간 822ms는 곡 사이 전환 시간과 구분합니다.
- 브라우저 시험의 선곡표·소유권 API는 고정 시험 데이터이며, 오디오 조회는 적용된 실제 추출 worker를 사용했습니다. 실제 서비스 인증·DB·배포망을 포함한 브라우저 종단 검증은 미수행입니다.
- 음원 파일을 MP3/WAV로 저장하지 않았습니다. 브라우저의 스트리밍·버퍼링은 발생합니다. 순수 무음 구간의 청음 측정·모든 곡의 무지연·모든 브라우저의 자동재생을 보장하는 수치가 아닙니다.
- YouTube 공식 API 정책의 오디오/영상 분리 제한은 기술적 동작과 별개의 운영 검토 사항입니다: https://developers.google.com/youtube/terms/developer-policies#i.-additional-prohibitions
