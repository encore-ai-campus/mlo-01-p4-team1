import {afterEach, describe, expect, it, vi} from 'vitest';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {YoutubePlayer} from './YoutubePlayer';
import {YoutubePlaybackController, type PlaybackAdapter, type PlaybackCandidate, type PlaybackResolution, type PlaybackSnapshot} from './youtubePlayerController';

const tracks = [1, 2, 3].map(index => ({track_id: `track-${index}`, track_name: `Song ${index}`, artists_name: 'Artist'}));
const candidate = (index: number): PlaybackCandidate => ({video_id: `testVID000${index}`, title: `Recording ${index}`, channel_title: 'Official channel', duration_seconds: 180, source: 'CATALOG_MAPPING', match_status: 'VERIFIED'});
const ready = (id: string, values = [candidate(1)]): PlaybackResolution => ({track_id: id, status: 'READY', candidates: values});
const settle = async () => {for (let n = 0; n < 12; n++) await Promise.resolve();};

function harness(resolve = vi.fn(async (id: string, _excluded: string[], _signal: AbortSignal) => ready(id))) {
  const adapter = {load: vi.fn(), play: vi.fn(), pause: vi.fn()};
  const states: PlaybackSnapshot[] = [];
  const controller = new YoutubePlaybackController(tracks, adapter, resolve, state => states.push(state));
  return {controller, adapter, resolve, states};
}

describe('embedded YouTube queue controller', () => {
  it('loads automatically but advances only after the actual current recording played and ended', async () => {
    const {controller, adapter, resolve} = harness();
    await controller.start();
    expect(adapter.load).toHaveBeenCalledWith('testVID0001');
    controller.onPlayerState(0);
    expect(resolve).toHaveBeenCalledTimes(1);
    controller.onPlayerState(1); controller.onPlayerState(0); controller.onPlayerState(0);
    await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1', 'track-2']);
    expect(controller.snapshot().index).toBe(1);
  });

  it('keeps a blocked autoplay on its current song until a user initiates play', async () => {
    const {controller, adapter, resolve} = harness();
    await controller.start(); controller.onAutoplayBlocked();
    expect(controller.snapshot().phase).toBe('blocked');
    expect(controller.snapshot().skipped).toEqual([]);
    expect(resolve).toHaveBeenCalledTimes(1);
    controller.play(); expect(adapter.play).toHaveBeenCalledTimes(1);
    controller.onPlayerState(1); expect(controller.snapshot().phase).toBe('playing');
  });

  it('does not misreport a zero-time loading delay as a browser autoplay block or skip a song', async () => {
    const {controller, resolve} = harness(); await controller.start(); controller.onLoadingDelayed();
    expect(controller.snapshot().phase).toBe('loading'); expect(controller.snapshot().message).toContain('길어지고');
    expect(controller.snapshot().message).not.toContain('브라우저'); expect(controller.snapshot().skipped).toEqual([]);
    expect(resolve).toHaveBeenCalledTimes(1);
    controller.onPlayerState(1); controller.onLoadingDelayed(); expect(controller.snapshot().phase).toBe('playing');
  });

  it('skips unmapped or pending registrations with explicit omissions while continuing to a reviewed registry recording', async () => {
    const resolver = vi.fn(async (id: string): Promise<PlaybackResolution> => id === 'track-3' ? ready(id, [{...candidate(3), source: 'MAPPING_REGISTRY'}]) : {track_id: id, status: 'MAPPING_REQUIRED', reason: id === 'track-1' ? 'UNMAPPED' : 'PENDING', candidates: []});
    const {controller, adapter} = harness(resolver); await controller.start(); await settle();
    expect(controller.snapshot().index).toBe(2); expect(controller.snapshot().skipped.map(item => item.track_id)).toEqual(['track-1', 'track-2']);
    expect(controller.snapshot().candidate?.source).toBe('MAPPING_REGISTRY'); expect(adapter.load).toHaveBeenCalledWith('testVID0003');
  });

  it('refreshes the current mapping without losing earlier omissions or resetting the queue', async () => {
    const {controller, resolve} = harness(); await controller.start(1); controller.retry(); await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-2', 'track-2']); expect(controller.snapshot().index).toBe(1);
  });

  it('tries an alternate recording on 150, then re-resolves once with exclusions before skipping', async () => {
    const resolver = vi.fn(async (id: string, _excluded: string[], _signal: AbortSignal) => ready(id, [candidate(1), candidate(2)]));
    const {controller, adapter} = harness(resolver);
    await controller.start(); controller.onPlayerError(150);
    expect(adapter.load.mock.calls.map(call => call[0])).toEqual(['testVID0001', 'testVID0002']);
    controller.onPlayerError(101); await settle();
    expect(resolver.mock.calls[1][1]).toEqual(['testVID0001', 'testVID0002']);
    expect(controller.snapshot().skipped[0].track_id).toBe('track-1');
    expect(controller.snapshot().index).toBe(1);
  });

  it('does not make an environment error or unavailable search skip the rest of the queue', async () => {
    const {controller, resolve} = harness(); await controller.start(); controller.onPlayerError(153);
    expect(controller.snapshot().phase).toBe('error'); expect(controller.snapshot().skipped).toEqual([]);
    expect(resolve).toHaveBeenCalledTimes(1);
    const unavailable = harness(vi.fn(async (id: string) => ({track_id: id, status: 'SEARCH_UNAVAILABLE', candidates: [], message: '검색 연결 준비 중'})));
    await unavailable.controller.start(); await settle();
    expect(unavailable.resolve).toHaveBeenCalledTimes(1);
    expect(unavailable.controller.snapshot().phase).toBe('error');
    expect(unavailable.controller.snapshot().index).toBe(0);
  });

  it('continues to the next local preview candidate after an unavailable iframe', async () => {
    const resolver = vi.fn(async (id: string) => ready(id, [{...candidate(Number(id.at(-1))),
      source: 'TAGGED_JSON_PREVIEW', match_status: 'CANDIDATE'}]));
    const {controller, adapter} = harness(resolver);
    await controller.start(); controller.onPlayerError(153); await settle();
    expect(controller.snapshot().skipped.map(item => item.track_id)).toEqual(['track-1']);
    expect(controller.snapshot().index).toBe(1);
    expect(adapter.load).toHaveBeenLastCalledWith('testVID0002');
    expect(adapter.play).not.toHaveBeenCalled();
  });

  it('records an actually blocked recording when its alternative search is unavailable, then holds on the next unmapped song', async () => {
    const resolver = vi.fn(async (id: string, excluded: string[], _signal: AbortSignal): Promise<PlaybackResolution> => id === 'track-1' && !excluded.length ? ready(id) : {track_id: id, status: 'SEARCH_UNAVAILABLE', candidates: []});
    const {controller} = harness(resolver);
    await controller.start(); controller.onPlayerError(150); await settle();
    expect(controller.snapshot().skipped.map(item => item.track_id)).toEqual(['track-1']);
    expect(controller.snapshot().index).toBe(1); expect(controller.snapshot().phase).toBe('error');
    expect(resolver).toHaveBeenCalledTimes(3);
  });

  it('lists no-match songs and never reports successful full playback when all fail', async () => {
    const {controller} = harness(vi.fn(async (id: string) => ({track_id: id, status: 'NO_MATCH', candidates: []})));
    await controller.start(); await settle();
    expect(controller.snapshot().skipped.map(item => item.track_id)).toEqual(tracks.map(track => track.track_id));
    expect(controller.snapshot().phase).toBe('unavailable');
    expect(controller.snapshot().message).not.toContain('완료');
  });

  it('aborts in-flight mapping and prevents an old response from playing after pending condition changes', async () => {
    let deliver: ((value: PlaybackResolution) => void) | null = null;
    const {controller, adapter, resolve} = harness(vi.fn((id: string, _excluded: string[], _signal: AbortSignal) => new Promise<PlaybackResolution>(accept => {deliver = accept;})));
    const started = controller.start(); controller.setBlocked('변경 조건 미반영');
    expect(resolve.mock.calls[0][2].aborted).toBe(true);
    deliver!(ready('track-1')); await started;
    expect(adapter.load).not.toHaveBeenCalled(); expect(controller.snapshot().phase).toBe('blocked');
    controller.play(); expect(adapter.play).not.toHaveBeenCalled();
    controller.setBlocked(null); expect(controller.snapshot().phase).toBe('paused');
  });

  it('pauses synchronously on a result gate and requires a fresh play gesture to resume', async () => {
    const {controller, adapter} = harness(); await controller.start(); controller.onPlayerState(1);
    controller.setBlocked('제외 저장 중');
    expect(adapter.pause).toHaveBeenCalled(); expect(controller.snapshot().phase).toBe('blocked');
    controller.setBlocked(null); expect(controller.snapshot().phase).toBe('paused');
    expect(adapter.play).not.toHaveBeenCalled(); controller.play(); expect(adapter.play).toHaveBeenCalledTimes(1);
  });

  it('rejects a resolver result for a different track and drops duplicate or invalid video IDs', async () => {
    const mismatch = harness(vi.fn(async () => ready('other-track'))); await mismatch.controller.start();
    expect(mismatch.adapter.load).not.toHaveBeenCalled(); expect(mismatch.controller.snapshot().phase).toBe('error');
    const valid = harness(vi.fn(async (id: string) => ready(id, [candidate(1), {...candidate(1)}, {...candidate(2), video_id: 'bad'}])));
    await valid.controller.start(); valid.controller.onPlayerError(150); await settle();
    expect(valid.adapter.load.mock.calls.map(call => call[0]).filter(id => id === 'bad')).toEqual([]);
    expect(valid.resolve.mock.calls[1][1]).toEqual(['testVID0001']);
  });

  it('stops a stale async result after destruction and cleans up media immediately', async () => {
    let deliver: ((value: PlaybackResolution) => void) | null = null;
    const {controller, adapter} = harness(vi.fn(() => new Promise<PlaybackResolution>(accept => {deliver = accept;})));
    const started = controller.start(); controller.destroy(); deliver!(ready('track-1')); await started;
    expect(adapter.pause).toHaveBeenCalled(); expect(adapter.load).not.toHaveBeenCalled();
  });

  it('renders the first song and in-place playback controls without requiring playlist approval or an LLM', () => {
    const html = renderToStaticMarkup(createElement(YoutubePlayer, {playlistId: 'result-1', tracks, blockReason: '변경 조건 미반영'}));
    expect(html).toContain('선곡표 연속 재생'); expect(html).toContain('Song 1');
    expect(html).toContain('YouTube 재생'); expect(html).not.toContain('외부 연결 준비');
    expect(html).toContain('변경 조건 미반영'); expect(html).not.toContain('임시 목록');
  });
});

describe('next-song preparation', () => {
  const active: YoutubePlaybackController[] = [];
  afterEach(() => {active.splice(0).forEach(controller => controller.destroy()); vi.useRealTimers();});

  function preparedHarness(resolve = vi.fn(async (id: string, _excluded: string[], _signal: AbortSignal) => ready(id, [candidate(Number(id.slice(-1)))])), prepare = vi.fn<NonNullable<PlaybackAdapter['prepare']>>(async () => 'ready'), preparedLifetimeMs?: () => number) {
    const adapter = {load: vi.fn(), play: vi.fn(), pause: vi.fn(), prepare, discardPrepared: vi.fn(), preparedLifetimeMs};
    const states: PlaybackSnapshot[] = [];
    const controller = new YoutubePlaybackController(tracks, adapter, resolve, state => states.push(state));
    active.push(controller);
    return {controller, adapter, resolve, states};
  }

  it('waits for actual PLAYING, then prepares only the next song without changing current playback', async () => {
    const {controller, adapter, resolve} = preparedHarness(); await controller.start();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1']); expect(adapter.prepare).not.toHaveBeenCalled();
    controller.onPlayerState(3); controller.onPlayerState(2); await settle();
    expect(resolve).toHaveBeenCalledTimes(1);
    controller.onPlayerState(1); await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1', 'track-2']);
    expect(adapter.prepare.mock.calls[0][0].video_id).toBe('testVID0002'); expect(adapter.prepare.mock.calls[0][1].track_id).toBe('track-2');
    expect(controller.snapshot().index).toBe(0); expect(controller.snapshot().phase).toBe('playing');
    expect(adapter.load.mock.calls.map(call => call[0])).toEqual(['testVID0001']);
  });

  it('promotes the prepared recording synchronously on ENDED without resolving or preparing it again', async () => {
    const {controller, adapter, resolve} = preparedHarness(); await controller.start(); controller.onPlayerState(1); await settle();
    const discarded = adapter.discardPrepared.mock.calls.length;
    controller.onPlayerState(0);
    expect(adapter.load.mock.calls.map(call => call[0])).toEqual(['testVID0001', 'testVID0002']);
    expect(resolve).toHaveBeenCalledTimes(2); expect(adapter.prepare).toHaveBeenCalledTimes(1);
    expect(adapter.discardPrepared).toHaveBeenCalledTimes(discarded); expect(controller.snapshot().index).toBe(1);
    controller.onPlayerState(0); expect(controller.snapshot().index).toBe(1);
  });

  it('also consumes a ready preparation on an explicit next command', async () => {
    const {controller, adapter, resolve} = preparedHarness(); await controller.start(); controller.onPlayerState(1); await settle();
    controller.next();
    expect(adapter.load).toHaveBeenLastCalledWith('testVID0002'); expect(resolve).toHaveBeenCalledTimes(2);
  });

  it('does not duplicate look-ahead on repeated PLAYING or pause/resume notifications', async () => {
    const {controller, adapter, resolve} = preparedHarness(); await controller.start();
    controller.onPlayerState(1); controller.onPlayerState(1); await settle();
    controller.onPlayerState(2); controller.onPlayerState(1); await settle();
    expect(resolve).toHaveBeenCalledTimes(2); expect(adapter.prepare).toHaveBeenCalledTimes(1);
  });

  it('keeps a failed look-ahead lookup from failing or skipping the current song', async () => {
    const resolve = vi.fn(async (id: string, _excluded: string[], _signal: AbortSignal) => {if (id === 'track-2') throw new Error('network unavailable'); return ready(id);});
    const {controller, adapter} = preparedHarness(resolve); await controller.start(); controller.onPlayerState(1); await settle();
    expect(controller.snapshot().phase).toBe('playing'); expect(controller.snapshot().index).toBe(0); expect(controller.snapshot().skipped).toEqual([]);
    expect(adapter.prepare).not.toHaveBeenCalled();
    controller.onPlayerState(1); await settle(); expect(resolve).toHaveBeenCalledTimes(2);
    controller.onPlayerState(0); await settle(); expect(resolve).toHaveBeenCalledTimes(3); expect(controller.snapshot().phase).toBe('error');
  });

  it('does not convert an unknown media check into a skip and falls back to the normal current-song path', async () => {
    const prepare = vi.fn<NonNullable<PlaybackAdapter['prepare']>>(async () => 'unknown');
    const {controller, adapter, resolve} = preparedHarness(undefined, prepare); await controller.start(); controller.onPlayerState(1); await settle();
    expect(controller.snapshot().skipped).toEqual([]); expect(controller.snapshot().phase).toBe('playing');
    controller.onPlayerState(0); await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1', 'track-2', 'track-2']);
    expect(adapter.load).toHaveBeenLastCalledWith('testVID0002'); expect(controller.snapshot().skipped).toEqual([]);
  });

  it('prepares another candidate only after confirmed unavailability and promotes that candidate', async () => {
    const resolve = vi.fn(async (id: string, _excluded: string[], _signal: AbortSignal) => ready(id, id === 'track-2' ? [candidate(2), candidate(3)] : [candidate(1)]));
    const prepare = vi.fn<NonNullable<PlaybackAdapter['prepare']>>().mockResolvedValueOnce('unavailable').mockResolvedValueOnce('ready');
    const {controller, adapter} = preparedHarness(resolve, prepare); await controller.start(); controller.onPlayerState(1); await settle();
    expect(prepare.mock.calls.map(call => call[0].video_id)).toEqual(['testVID0002', 'testVID0003']);
    controller.onPlayerState(0); expect(adapter.load).toHaveBeenLastCalledWith('testVID0003'); expect(controller.snapshot().skipped).toEqual([]);
  });

  it('caches a fully checked unavailable next song and skips it with a reason without resolving or loading it again', async () => {
    const resolve = vi.fn(async (id: string, _excluded: string[], _signal: AbortSignal) => ready(id, id === 'track-2' ? [candidate(2), candidate(3)] : [candidate(Number(id.slice(-1)))]));
    const prepare = vi.fn<NonNullable<PlaybackAdapter['prepare']>>(async () => 'unavailable');
    const {controller, adapter} = preparedHarness(resolve, prepare); await controller.start(); controller.onPlayerState(1); await settle();
    expect(controller.snapshot().index).toBe(0); expect(controller.snapshot().phase).toBe('playing'); expect(controller.snapshot().skipped).toEqual([]);
    expect(prepare).toHaveBeenCalledTimes(2);
    controller.onPlayerState(0); await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1', 'track-2', 'track-3']);
    expect(controller.snapshot().index).toBe(2); expect(controller.snapshot().skipped).toEqual([expect.objectContaining({track_id: 'track-2', reason: expect.stringContaining('사전 확인')})]);
    expect(adapter.load.mock.calls.map(call => call[0])).toEqual(['testVID0001', 'testVID0003']);
  });

  it('refreshes a negative preparation after 90 seconds and can replace it with a playable candidate', async () => {
    vi.useFakeTimers();
    const prepare = vi.fn<NonNullable<PlaybackAdapter['prepare']>>().mockResolvedValueOnce('unavailable').mockResolvedValueOnce('ready');
    const {controller, adapter, resolve} = preparedHarness(undefined, prepare); await controller.start(); controller.onPlayerState(1); await settle();
    await vi.advanceTimersByTimeAsync(90000);
    expect(prepare).toHaveBeenCalledTimes(2); expect(resolve).toHaveBeenCalledTimes(2);
    controller.onPlayerState(0); expect(adapter.load).toHaveBeenLastCalledWith('testVID0002'); expect(controller.snapshot().skipped).toEqual([]);
  });

  it('expires an unrefreshed negative cache instead of skipping a song using an old unavailable decision', async () => {
    vi.useFakeTimers();
    const prepare = vi.fn<NonNullable<PlaybackAdapter['prepare']>>(async () => 'unavailable');
    const {controller, adapter, resolve} = preparedHarness(undefined, prepare); await controller.start(); controller.onPlayerState(1); await settle();
    controller.onPlayerState(2); await vi.advanceTimersByTimeAsync(120000); controller.next(); await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1', 'track-2', 'track-2']);
    expect(adapter.load).toHaveBeenLastCalledWith('testVID0002'); expect(controller.snapshot().skipped).toEqual([]);
  });

  it('does not claim all candidates unavailable when the bounded check left other candidates untested', async () => {
    const resolve = vi.fn(async (id: string, _excluded: string[], _signal: AbortSignal) => ready(id, id === 'track-2' ? Array.from({length: 6}, (_, n) => candidate(n + 1)) : [candidate(1)]));
    const prepare = vi.fn<NonNullable<PlaybackAdapter['prepare']>>(async () => 'unavailable');
    const {controller, adapter} = preparedHarness(resolve, prepare); await controller.start(); controller.onPlayerState(1); await settle();
    expect(prepare).toHaveBeenCalledTimes(5); controller.next(); await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1', 'track-2', 'track-2']);
    expect(adapter.load).toHaveBeenLastCalledWith('testVID0001'); expect(controller.snapshot().skipped).toEqual([]);
  });

  it('cancels pending media checks and ignores late success after navigating elsewhere', async () => {
    let deliver: ((value: 'ready') => void) | null = null;
    const prepare = vi.fn<NonNullable<PlaybackAdapter['prepare']>>(() => new Promise(accept => {deliver = accept;}));
    const {controller, adapter, resolve} = preparedHarness(undefined, prepare); await controller.start(); controller.onPlayerState(1); await settle();
    const signal = prepare.mock.calls[0][2]; controller.previous(); await settle();
    expect(signal.aborted).toBe(true); deliver!('ready'); await settle();
    controller.next(); await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1', 'track-2', 'track-1', 'track-2']);
    expect(adapter.load.mock.calls.map(call => call[0])).toEqual(['testVID0001', 'testVID0001', 'testVID0002']);
  });

  it('aborts a pending next-song resolver when retry replaces the current generation', async () => {
    let deliver: ((value: PlaybackResolution) => void) | null = null;
    const resolve = vi.fn((id: string, _excluded: string[], _signal: AbortSignal): Promise<PlaybackResolution> => id === 'track-2' ? new Promise(accept => {deliver = accept;}) : Promise.resolve(ready(id)));
    const {controller, adapter} = preparedHarness(resolve); await controller.start(); controller.onPlayerState(1); await settle();
    const signal = resolve.mock.calls[1][2]; controller.retry(); await settle();
    expect(signal.aborted).toBe(true); deliver!(ready('track-2', [candidate(2)])); await settle();
    expect(adapter.prepare).not.toHaveBeenCalled(); expect(controller.snapshot().index).toBe(0);
  });

  it('discards ready media and cancels its signal when blocked or destroyed', async () => {
    const blocked = preparedHarness(); await blocked.controller.start(); blocked.controller.onPlayerState(1); await settle();
    const signal = blocked.adapter.prepare.mock.calls[0][2]; const before = blocked.adapter.discardPrepared.mock.calls.length;
    blocked.controller.setBlocked('제외 저장 중');
    expect(signal.aborted).toBe(true); expect(blocked.adapter.discardPrepared).toHaveBeenCalledTimes(before + 1);
    expect(blocked.controller.snapshot().phase).toBe('blocked');
    const destroyed = preparedHarness(); await destroyed.controller.start(); destroyed.controller.onPlayerState(1); await settle();
    const otherSignal = destroyed.adapter.prepare.mock.calls[0][2]; destroyed.controller.destroy(); expect(otherSignal.aborted).toBe(true);
  });

  it('refreshes ready media after 90 seconds without another catalog request', async () => {
    vi.useFakeTimers();
    const {controller, adapter, resolve} = preparedHarness(); await controller.start(); controller.onPlayerState(1); await settle();
    const firstSignal = adapter.prepare.mock.calls[0][2];
    await vi.advanceTimersByTimeAsync(90000);
    expect(firstSignal.aborted).toBe(true); expect(adapter.prepare).toHaveBeenCalledTimes(2); expect(resolve).toHaveBeenCalledTimes(2);
    expect(controller.snapshot().phase).toBe('playing'); controller.onPlayerState(0);
    expect(adapter.load).toHaveBeenLastCalledWith('testVID0002'); expect(resolve).toHaveBeenCalledTimes(2);
  });

  it('expires paused media after 120 seconds and re-resolves instead of promoting expired resources', async () => {
    vi.useFakeTimers();
    const {controller, adapter, resolve} = preparedHarness(); await controller.start(); controller.onPlayerState(1); await settle();
    controller.onPlayerState(2); await vi.advanceTimersByTimeAsync(120000);
    expect(adapter.prepare).toHaveBeenCalledTimes(1); expect(adapter.prepare.mock.calls[0][2].aborted).toBe(true);
    controller.next(); await settle(); expect(resolve).toHaveBeenCalledTimes(3);
    expect(adapter.load).toHaveBeenLastCalledWith('testVID0002');
  });

  it('refreshes a ready 70-second audio URL after 40 seconds without re-querying the catalog', async () => {
    vi.useFakeTimers();
    const {controller, adapter, resolve} = preparedHarness(undefined, undefined, () => 70000);
    await controller.start(); controller.onPlayerState(1); await settle();
    await vi.advanceTimersByTimeAsync(39999); expect(adapter.prepare).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1); expect(adapter.prepare).toHaveBeenCalledTimes(2);
    expect(resolve).toHaveBeenCalledTimes(2); controller.onPlayerState(0);
    expect(adapter.load).toHaveBeenLastCalledWith('testVID0002'); expect(resolve).toHaveBeenCalledTimes(2);
  });

  it('expires a paused 70-second audio URL at 55 seconds, preserving the 15-second safety margin', async () => {
    vi.useFakeTimers();
    const {controller, adapter, resolve} = preparedHarness(undefined, undefined, () => 70000);
    await controller.start(); controller.onPlayerState(1); await settle(); controller.onPlayerState(2);
    const signal = adapter.prepare.mock.calls[0][2];
    await vi.advanceTimersByTimeAsync(54999); expect(signal.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1); expect(signal.aborted).toBe(true);
    controller.next(); await settle(); expect(resolve).toHaveBeenCalledTimes(3);
  });

  it('never promotes a lifetime-expired resource even if a suspended tab has not run its expiry timer', async () => {
    vi.useFakeTimers();
    const {controller, adapter, resolve} = preparedHarness(undefined, undefined, () => 70000);
    await controller.start(); controller.onPlayerState(1); await settle();
    vi.setSystemTime(Date.now() + 55000); controller.next(); await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1', 'track-2', 'track-2']);
    expect(adapter.load).toHaveBeenLastCalledWith('testVID0002');
  });

  it('preserves the 90/120-second policy for an iframe that reports unlimited lifetime', async () => {
    vi.useFakeTimers();
    const {controller, adapter, resolve} = preparedHarness(undefined, undefined, () => Infinity);
    await controller.start(); controller.onPlayerState(1); await settle();
    await vi.advanceTimersByTimeAsync(89999); expect(adapter.prepare).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1); expect(adapter.prepare).toHaveBeenCalledTimes(2);
    controller.onPlayerState(2); await vi.advanceTimersByTimeAsync(120000);
    expect(adapter.prepare).toHaveBeenCalledTimes(2); controller.next(); await settle();
    expect(resolve).toHaveBeenCalledTimes(3);
  });

  it('drops media with at most 15 seconds remaining instead of caching it as unavailable', async () => {
    const {controller, adapter, resolve} = preparedHarness(undefined, undefined, () => 15000);
    await controller.start(); controller.onPlayerState(1); await settle();
    expect(adapter.prepare.mock.calls[0][2].aborted).toBe(true); expect(controller.snapshot().skipped).toEqual([]);
    controller.next(); await settle(); expect(resolve).toHaveBeenCalledTimes(3); expect(controller.snapshot().skipped).toEqual([]);
  });

  it('keeps the decision-cache lifetime independent of a missing media resource', async () => {
    vi.useFakeTimers();
    const prepare = vi.fn<NonNullable<PlaybackAdapter['prepare']>>(async () => 'unavailable');
    const lifetime = vi.fn(() => 0);
    const {controller, resolve} = preparedHarness(undefined, prepare, lifetime);
    await controller.start(); controller.onPlayerState(1); await settle();
    expect(lifetime).not.toHaveBeenCalled(); controller.next(); await settle();
    expect(resolve.mock.calls.map(call => call[0])).toEqual(['track-1', 'track-2', 'track-3']);
    expect(controller.snapshot().skipped[0].track_id).toBe('track-2');
  });
});
