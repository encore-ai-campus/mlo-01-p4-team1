import {useEffect, useRef, useState} from 'react';
import {api, csrfHeaders, requestJson} from '../../shared/api';
import {loadYoutubeIframeApi} from './youtubeIframeApi';
import {YoutubeMediaPool, type AudioResolution} from './youtubeMediaPool';
import {YoutubePlaybackController, type PlaybackCandidate, type PlaybackResolution, type PlaybackSnapshot, type PlaybackTrack} from './youtubePlayerController';
import './YoutubePlayer.css';

export type YoutubePlayerProps = {playlistId: string; tracks: PlaybackTrack[]; blockReason?: string | null;
  mappingRevision?: number; pauseRevision?: number; showSourceThumbnail?: boolean; allowTaggedPreview?: boolean;
  onPlaybackActivityChange?: (playing: boolean) => void};

const initial: PlaybackSnapshot = {index: 0, phase: 'idle', message: '', candidate: null, skipped: [], playedTrackIds: []};
const defaultCover = '/default-cover.svg';
const videoId = /^[A-Za-z0-9_-]{11}$/;

export function sourceThumbnail(track: PlaybackTrack | undefined, enabled: boolean) {
  const id = track?.youtube_video_id;
  return enabled && typeof id === 'string' && videoId.test(id)
    ? `https://i.ytimg.com/vi/${id}/hqdefault.jpg` : null;
}

export function allowedPlaybackCandidate(track: PlaybackTrack, candidate: PlaybackCandidate, allowTaggedPreview: boolean) {
  if (typeof candidate.video_id !== 'string' || !videoId.test(candidate.video_id)) return false;
  if (candidate.source === 'TAGGED_JSON_PREVIEW') {
    return allowTaggedPreview && candidate.match_status === 'CANDIDATE' && candidate.video_id === track.youtube_video_id;
  }
  return candidate.match_status === 'VERIFIED';
}

function PlaybackCover({track, showSourceThumbnail}: {track?: PlaybackTrack; showSourceThumbnail: boolean}) {
  const cover = track?.cover_image_url || defaultCover;
  const thumbnail = sourceThumbnail(track, showSourceThumbnail);
  return <img className="youtube-player-cover" src={thumbnail || cover} alt={thumbnail ? '수집 영상 썸네일' : '곡 표지'}
    onError={event => {
      const image = event.currentTarget;
      if (image.getAttribute('src') === defaultCover) return;
      image.src = image.getAttribute('src') === thumbnail ? cover : defaultCover;
      image.alt = '곡 표지';
    }}/>;
}

export function YoutubePlayer({playlistId, tracks, blockReason, mappingRevision = 0, pauseRevision = 0,
  showSourceThumbnail = false, allowTaggedPreview = false, onPlaybackActivityChange}: YoutubePlayerProps) {
  const host = useRef<HTMLDivElement>(null), engine = useRef<YoutubePlaybackController | null>(null);
  const userStarted = useRef(false), latestBlock = useRef(blockReason);
  latestBlock.current = blockReason;
  const [state, setState] = useState<PlaybackSnapshot>(initial);
  const queueKey = tracks.map(track => track.track_id).join('|');

  useEffect(() => {
    if (!host.current) return;
    setState(initial); userStarted.current = false;
    const currentTracks = [...tracks];
    const tracksById = new Map(currentTracks.map(track => [track.track_id, track]));
    const allowCandidate = (track: PlaybackTrack, candidate: PlaybackCandidate) => allowedPlaybackCandidate(track, candidate, allowTaggedPreview);
    const pool = new YoutubeMediaPool({api: loadYoutubeIframeApi, host: host.current,
      shouldAutoplay: () => userStarted.current,
      allowCandidate,
      current: () => {
        const {candidate, index} = controller.snapshot();
        return candidate && currentTracks[index] ? {candidate, track: currentTracks[index]} : null;
      },
      resolveAudio: (track, candidate, signal) => requestJson<AudioResolution>(
        `/api/playlists/${encodeURIComponent(playlistId)}/youtube-audio`, {
          method: 'POST', credentials: 'same-origin', cache: 'no-store', signal,
          headers: {'Content-Type': 'application/json', ...csrfHeaders()},
          body: JSON.stringify({track_id: track.track_id, video_id: candidate.video_id}),
        }, 25000),
      onState: value => {
        if (value === 1) userStarted.current = true;
        controller.onPlayerState(value);
      },
      onError: code => controller.onPlayerError(code),
      onAutoplayBlocked: () => controller.onAutoplayBlocked(),
      onRoute: () => {},
    });
    const controller: YoutubePlaybackController = new YoutubePlaybackController(currentTracks, pool, async (trackId, excludedVideoIds, signal) => {
      const result = await api<PlaybackResolution>(`/api/playlists/${encodeURIComponent(playlistId)}/youtube-playback`,
        'POST', {track_id: trackId, excluded_video_ids: excludedVideoIds}, undefined, signal);
      const track = tracksById.get(trackId);
      return {...result, candidates: track ? result.candidates.filter(candidate => allowCandidate(track, candidate)) : []};
    }, setState, () => allowTaggedPreview || userStarted.current);
    engine.current = controller;
    if (latestBlock.current) controller.setBlocked(latestBlock.current);
    else if (currentTracks.length) void controller.start();
    return () => {controller.destroy(); pool.destroy(); engine.current = null;};
    // Refetching the same immutable playlist after approval or feedback keeps its media active.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playlistId, queueKey, mappingRevision, allowTaggedPreview]);

  const wasBlocked = useRef(false);
  useEffect(() => {
    const controller = engine.current;
    if (blockReason) {wasBlocked.current = true; userStarted.current = false; controller?.setBlocked(blockReason); return;}
    if (!wasBlocked.current) return;
    wasBlocked.current = false;
    controller?.setBlocked(null);
    if (controller && !controller.snapshot().candidate) void controller.start(controller.snapshot().index);
  }, [blockReason]);

  const lastPause = useRef(pauseRevision);
  useEffect(() => {
    if (lastPause.current === pauseRevision) return;
    lastPause.current = pauseRevision; userStarted.current = false;
    engine.current?.pause();
  }, [pauseRevision]);

  const playing = !blockReason && state.phase === 'playing';
  useEffect(() => {
    onPlaybackActivityChange?.(playing);
    return () => {if (playing) onPlaybackActivityChange?.(false);};
  }, [playing, onPlaybackActivityChange]);

  const track = tracks[state.index];
  const busy = state.phase === 'idle' || state.phase === 'resolving';
  const failed = state.phase === 'error' || state.phase === 'unavailable';
  const message = blockReason || (failed ? '재생 연결을 확인해 주세요.' : busy || state.phase === 'loading' ? '재생 준비 중…'
    : state.phase === 'blocked' ? '재생을 눌러 주세요.' : state.phase === 'complete' ? '선곡표 재생 완료' : '');
  return <section className="youtube-player" aria-label="선곡표 연속 재생">
    <div className="youtube-player-frame">
      <PlaybackCover key={`${track?.track_id}:${track?.youtube_video_id}`} track={track} showSourceThumbnail={showSourceThumbnail}/>
      <div ref={host} className="youtube-player-media" hidden={!!blockReason || !state.candidate || state.phase === 'error'}/>
    </div>
    {track && <div className="youtube-player-track"><strong>{track.track_name}</strong><span>{track.artists_name}</span></div>}
    {message && <p className="youtube-player-status" role="status">{message}</p>}
    <div className="youtube-player-controls">
      <button type="button" disabled={!!blockReason || busy || !state.index} onClick={() => engine.current?.previous()} aria-label="이전 곡">이전</button>
      {playing ? <button type="button" onClick={() => {userStarted.current = false; engine.current?.pause();}} aria-label="재생 일시 정지">일시 정지</button>
        : <button type="button" disabled={!!blockReason || busy || failed} onClick={() => {userStarted.current = true; engine.current?.play();}} aria-label="YouTube 재생">재생</button>}
      <button type="button" disabled={!!blockReason || busy || state.index >= tracks.length - 1} onClick={() => engine.current?.next()} aria-label="다음 곡">다음</button>
    </div>
    {failed && <button className="youtube-player-retry" type="button" disabled={!!blockReason} onClick={() => {userStarted.current = false; engine.current?.retry();}}>재생 연결 다시 확인</button>}
  </section>;
}
