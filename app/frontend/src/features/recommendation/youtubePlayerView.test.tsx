import {renderToStaticMarkup} from 'react-dom/server';
import {expect, it} from 'vitest';
import {allowedPlaybackCandidate, sourceThumbnail, YoutubePlayer} from './YoutubePlayer';
import type {PlaybackCandidate} from './youtubePlayerController';

const track = {track_id: 'tagged-fixture', track_name: '테스트 곡', artists_name: '테스트 가수',
  youtube_video_id: 'testVID0001', cover_image_url: '/approved-cover.jpg'};

it('accepts a collected candidate only for its own local preview track', () => {
  const local: PlaybackCandidate = {video_id: track.youtube_video_id, title: track.track_name, channel_title: track.artists_name,
    duration_seconds: 240, source: 'TAGGED_JSON_PREVIEW', match_status: 'CANDIDATE'};
  expect(allowedPlaybackCandidate(track, local, true)).toBe(true);
  expect(allowedPlaybackCandidate(track, local, false)).toBe(false);
  expect(allowedPlaybackCandidate({...track, youtube_video_id: 'testVID0002'}, local, true)).toBe(false);
  expect(allowedPlaybackCandidate(track, {...local, match_status: 'VERIFIED'}, true)).toBe(false);
  expect(allowedPlaybackCandidate(track, {...local, source: 'MAPPING_REGISTRY'}, true)).toBe(false);
  expect(allowedPlaybackCandidate(track, {...local, source: 'MAPPING_REGISTRY', match_status: 'VERIFIED'}, false)).toBe(true);
});

it('uses an unreviewed video ID only for the local preview thumbnail', () => {
  expect(sourceThumbnail(track, true)).toBe('https://i.ytimg.com/vi/testVID0001/hqdefault.jpg');
  expect(sourceThumbnail(track, false)).toBeNull();
  expect(sourceThumbnail({...track, youtube_video_id: '../outside/'}, true)).toBeNull();
  const preview = renderToStaticMarkup(<YoutubePlayer playlistId="fixture" tracks={[track]} showSourceThumbnail/>);
  expect(preview).toContain('https://i.ytimg.com/vi/testVID0001/hqdefault.jpg');
  expect(preview).toContain('수집 영상 썸네일');
  expect(preview).not.toContain('<iframe');
  expect(preview).not.toContain('<audio');
  const normal = renderToStaticMarkup(<YoutubePlayer playlistId="fixture" tracks={[track]}/>);
  expect(normal).toContain('/approved-cover.jpg');
  expect(normal).not.toContain('i.ytimg.com');
});
