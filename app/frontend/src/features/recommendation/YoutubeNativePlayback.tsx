import {useEffect, useState} from 'react';
import {api} from '../../shared/api';
import type {PlaybackTrack} from './youtubePlayerController';
import {checkedTrackLinks, type YoutubeMappingSnapshot, type YoutubeTrackLinks} from './youtubeDestinations';
import './YoutubeNativePlayback.css';

export type NativeYoutubeQueue = {playlist_id: string; tracks: {track_id: string; video_id: string; source: string; native_verified?: boolean}[]; missing: {track_id: string; track_name: string; reason: string}[]; total_count: number; ready_count: number; unreviewed_count?: number; url: string | null; is_complete: boolean; track_links?: YoutubeTrackLinks[]};

export function checkedNativeQueueHref(queue: NativeYoutubeQueue, playlistId: string, tracks: PlaybackTrack[]): string | null {
  if (!queue || !Array.isArray(queue.tracks) || !Array.isArray(queue.missing)) return null;
  if (queue.playlist_id !== playlistId || queue.total_count !== tracks.length || queue.ready_count !== queue.tracks.length || !queue.ready_count || queue.ready_count + queue.missing.length !== queue.total_count) return null;
  if (queue.is_complete !== (queue.ready_count === queue.total_count && !queue.missing.length)) return null;
  if (queue.unreviewed_count !== undefined && (!Number.isSafeInteger(queue.unreviewed_count) || queue.unreviewed_count < 0 || queue.tracks.some(track => typeof track.native_verified !== 'boolean') || queue.unreviewed_count !== queue.tracks.filter(track => !track.native_verified).length)) return null;
  const allIds = [...queue.tracks.map(track => track.track_id), ...queue.missing.map(track => track.track_id)];
  if (new Set(allIds).size !== tracks.length || allIds.some(id => !tracks.some(track => track.track_id === id))) return null;
  const ordered = tracks.filter(track => queue.tracks.some(item => item.track_id === track.track_id)).map(track => track.track_id);
  if (ordered.some((id, index) => queue.tracks[index].track_id !== id)) return null;
  const videoIds = queue.tracks.map(track => track.video_id);
  if (videoIds.some(id => !/^[A-Za-z0-9_-]{11}$/.test(id)) || new Set(videoIds).size !== videoIds.length || !queue.url) return null;
  try {
    const url = new URL(queue.url);
    if (url.protocol !== 'https:' || url.hostname !== 'www.youtube.com' || url.pathname !== '/watch_videos' || url.username || url.password || url.port || url.hash || [...url.searchParams.keys()].join(',') !== 'video_ids' || url.searchParams.get('video_ids') !== videoIds.join(',')) return null;
    return url.href;
  } catch {return null;}
}

export function YoutubeNativeQueueView({queue, tracks, playlistId, blockReason, onOpen}: {queue: NativeYoutubeQueue; tracks: PlaybackTrack[]; playlistId: string; blockReason?: string | null; onOpen?: () => void}) {
  const href = !blockReason ? checkedNativeQueueHref(queue, playlistId, tracks) : null;
  if (!href) return null;
  return <>
    <a className="youtube-native-cta" href={href} target="_blank" rel="noopener noreferrer" onClick={onOpen}>
      {queue.is_complete ? 'YouTube 새 탭에서 열기 ↗' : `YouTube 새 탭에서 ${queue.ready_count}/${queue.total_count}곡 열기 ↗`}
    </a>
    {!!queue.unreviewed_count && <small className="youtube-native-note">미검수 영상 {queue.unreviewed_count}곡 포함</small>}
  </>;
}

export function YoutubeNativePlayback({playlistId, tracks, blockReason, mappingRevision = 0, onOpen, onMappings}: {playlistId: string; tracks: PlaybackTrack[]; blockReason?: string | null; mappingRevision?: number; onOpen?: () => void; onMappings?: (snapshot: YoutubeMappingSnapshot | null) => void}) {
  const [snapshot, setSnapshot] = useState<{queue: NativeYoutubeQueue; mappingRevision: number} | null>(null);
  const queueKey = tracks.map(track => track.track_id).join('|');
  useEffect(() => {
    const controller = new AbortController(); setSnapshot(null); onMappings?.(null);
    void api<NativeYoutubeQueue>(`/api/playlists/${encodeURIComponent(playlistId)}/youtube-queue`, 'GET', undefined, undefined, controller.signal).then(result => {
      if (!controller.signal.aborted) {if (result.playlist_id !== playlistId) throw new Error('선곡표의 영상 연결 정보가 일치하지 않아요.'); const links = result.track_links === undefined ? null : checkedTrackLinks(result.track_links, tracks);
        if (result.track_links !== undefined && links === null) throw new Error('곡별 영상 연결 정보가 일치하지 않아요.');
        setSnapshot({queue: result, mappingRevision});
        if (links) onMappings?.({playlist_id: playlistId, mappingRevision, track_links: links});}
    }).catch(() => {if (!controller.signal.aborted) setSnapshot(null);});
    return () => controller.abort();
  }, [playlistId, queueKey, mappingRevision, onMappings]);
  if (snapshot?.queue.playlist_id !== playlistId || snapshot.mappingRevision !== mappingRevision || blockReason || !snapshot.queue.ready_count) return null;
  return <div className="youtube-native-playback">
    <YoutubeNativeQueueView queue={snapshot.queue} tracks={tracks} playlistId={playlistId} onOpen={onOpen}/>
  </div>;
}
