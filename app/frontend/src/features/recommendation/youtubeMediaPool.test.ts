import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {YoutubeMediaPool, type AudioResolution} from './youtubeMediaPool';
import type {PlaybackCandidate, PlaybackTrack} from './youtubePlayerController';
import type {YoutubeIframeOptions} from './youtubeIframeApi';

class FakeElement extends EventTarget {
  children: FakeElement[] = [];
  parent: FakeElement | null = null;
  className = ''; textContent = ''; title = ''; src = ''; alt = '';
  allow = ''; allowFullscreen = false; referrerPolicy = '';
  constructor(readonly tag: string) {super();}
  append(...values: FakeElement[]) {for (const value of values) {value.parent = this; this.children.push(value);}}
  replaceChildren(...values: FakeElement[]) {for (const child of this.children) child.parent = null; this.children = []; this.append(...values);}
  remove() {if (this.parent) this.parent.children = this.parent.children.filter(value => value !== this); this.parent = null;}
  removeAttribute(name: string) {if (name === 'src') this.src = '';}
}
class FakeAudio extends FakeElement {
  controls = false; preload = ''; muted = false; currentTime = 0; duration = 180; readyState = 0;
  ranges: [number, number][] = [];
  get buffered() {const values = this.ranges; return {length: values.length, start: (n: number) => values[n][0], end: (n: number) => values[n][1]};}
  load = vi.fn(); pause = vi.fn(); play = vi.fn(async () => {});
  emit(name: string) {this.dispatchEvent(new Event(name));}
  constructor() {super('audio');}
}
class FakePlayer {
  static players: FakePlayer[] = [];
  time = 0; id = '';
  loadVideoById = vi.fn((id: string) => {this.id = id;});
  playVideo = vi.fn(); pauseVideo = vi.fn(); destroy = vi.fn(); mute = vi.fn(); unMute = vi.fn();
  seekTo = vi.fn((seconds: number, _allow: boolean) => {this.time = seconds;});
  getCurrentTime = () => this.time;
  getDuration = () => 180;
  getVideoData = () => ({video_id: this.id});
  constructor(_frame: HTMLIFrameElement, readonly options: YoutubeIframeOptions) {FakePlayer.players.push(this);}
  ready() {this.options.events.onReady();}
  state(value: number) {this.options.events.onStateChange({data: value});}
  error(value: number) {this.options.events.onError({data: value});}
}
const track = (n: number): PlaybackTrack => ({track_id: `track-${n}`, track_name: `Song ${n}`, artists_name: 'Artist'});
const candidate = (n: number, audio = false): PlaybackCandidate => ({video_id: `testVID000${n}`, title: `Song ${n}`, channel_title: 'Official', duration_seconds: 180, source: 'CATALOG_MAPPING', ...(audio ? {playback_hint: 'AUDIO' as const} : {})});
const source = (n: number, patch: Partial<AudioResolution> = {}): AudioResolution => ({status: 'READY', video_id: candidate(n).video_id, audio_url: 'https://rr1.googlevideo.com/media', thumbnail_url: 'https://i.ytimg.com/vi/example/hqdefault.jpg', expires_at: new Date(Date.now() + 600000).toISOString(), ...patch});
const settle = async () => {for (let n = 0; n < 8; n++) await Promise.resolve();};

function harness(resolve = vi.fn(async (_track: PlaybackTrack, value: PlaybackCandidate, _signal: AbortSignal) => source(Number(value.video_id.slice(-1))))) {
  const audios: FakeAudio[] = [], host = new FakeElement('div');
  vi.stubGlobal('document', {createElement: (tag: string) => {if (tag !== 'audio') return new FakeElement(tag); const audio = new FakeAudio(); audios.push(audio); return audio;}});
  vi.stubGlobal('window', {location: {origin: 'http://127.0.0.1:5173'}});
  let current = {candidate: candidate(1), track: track(1)};
  const callbacks = {onState: vi.fn(), onError: vi.fn(), onAutoplayBlocked: vi.fn(), onRoute: vi.fn(), onPreparation: vi.fn(), onTransition: vi.fn()};
  const pool = new YoutubeMediaPool({api: {Player: FakePlayer}, host: host as unknown as HTMLDivElement, current: () => current, resolveAudio: resolve, ...callbacks});
  return {pool, host, audios, resolve, ...callbacks, select: (n: number, audio = false) => {current = {candidate: candidate(n, audio), track: track(n)};}};
}
async function warmIframe(h: ReturnType<typeof harness>, n = 2) {
  const abort = new AbortController(), prepared = h.pool.prepare(candidate(n), track(n), abort.signal);
  const frame = FakePlayer.players.at(-1)!; frame.ready(); frame.state(1); frame.time = 2.1;
  await vi.advanceTimersByTimeAsync(2600);
  expect(await prepared).toBe('ready');
  return {abort, frame};
}
async function bufferedAudio(h: ReturnType<typeof harness>, n = 2) {
  const abort = new AbortController(), prepared = h.pool.prepare(candidate(n, true), track(n), abort.signal);
  await settle(); const audio = h.audios.at(-1)!;
  audio.readyState = 3; audio.ranges = [[0, 2.5]]; audio.emit('canplay');
  expect(await prepared).toBe('ready');
  return {abort, audio};
}

beforeEach(() => {FakePlayer.players = []; vi.useFakeTimers({toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance']}); vi.setSystemTime(new Date('2026-11-10T12:00:00Z'));});
afterEach(() => {vi.useRealTimers(); vi.unstubAllGlobals();});

describe('two-slot media preparation', () => {
  it('isolates preview PLAYING/ENDED from the current track state machine', async () => {
    const h = harness(); h.pool.load(candidate(1).video_id);
    const active = FakePlayer.players[0]; active.ready(); active.state(1); h.onState.mockClear();
    const abort = new AbortController(), result = h.pool.prepare(candidate(2), track(2), abort.signal), preview = FakePlayer.players[1];
    preview.ready(); preview.state(1); preview.state(0);
    expect(h.onState).not.toHaveBeenCalled(); expect(active.pauseVideo).not.toHaveBeenCalled();
    abort.abort(); expect(await result).toBe('unknown'); h.pool.destroy();
  });

  it('requires both media progress and 2.5 seconds wall time before retaining a muted iframe', async () => {
    const h = harness(), result = h.pool.prepare(candidate(2), track(2), new AbortController().signal);
    let finished = false; void result.then(() => {finished = true;});
    const preview = FakePlayer.players[0]; preview.ready(); preview.state(1); preview.time = 2.2;
    await vi.advanceTimersByTimeAsync(2400); expect(finished).toBe(false);
    await vi.advanceTimersByTimeAsync(200); expect(await result).toBe('ready');
    expect(preview.mute).toHaveBeenCalledOnce(); expect(preview.pauseVideo).toHaveBeenCalledOnce(); expect(preview.seekTo).toHaveBeenCalledWith(0, true);
    h.pool.destroy();
  });

  it('does not infer playback readiness from elapsed wall time without media progress', async () => {
    const h = harness(), result = h.pool.prepare(candidate(2), track(2), new AbortController().signal), preview = FakePlayer.players[0];
    preview.ready(); preview.state(1); preview.time = 1.9;
    await vi.advanceTimersByTimeAsync(18000); expect(await result).toBe('unknown'); expect(preview.destroy).toHaveBeenCalledOnce();
    h.pool.destroy();
  });

  it('promotes the retained iframe without rebuilding or reloading it and detaches its old cancellation signal', async () => {
    const h = harness(); h.pool.load(candidate(1).video_id); FakePlayer.players[0].ready();
    const {abort, frame} = await warmIframe(h); const old = FakePlayer.players[0];
    h.select(2); h.pool.load(candidate(2).video_id); abort.abort();
    expect(FakePlayer.players).toHaveLength(2); expect(frame.loadVideoById).toHaveBeenCalledOnce(); expect(frame.destroy).not.toHaveBeenCalled();
    expect(old.destroy).toHaveBeenCalledOnce(); expect(frame.unMute).toHaveBeenCalledOnce(); expect(frame.playVideo).toHaveBeenCalledOnce();
    frame.state(1); expect(h.onState).toHaveBeenLastCalledWith(1); expect(h.onTransition).toHaveBeenLastCalledWith(expect.objectContaining({track_id: 'track-2', prepared: true, route: 'iframe'}));
    h.pool.destroy();
  });

  it('destroys a 150 iframe before querying audio and waits for at least 2 seconds of usable buffer', async () => {
    const h = harness(), result = h.pool.prepare(candidate(2), track(2), new AbortController().signal), preview = FakePlayer.players[0];
    preview.ready(); preview.error(150); expect(preview.destroy).toHaveBeenCalledOnce(); expect(h.resolve).toHaveBeenCalledOnce();
    await settle(); const audio = h.audios[0]; expect(audio.preload).toBe('auto'); expect(audio.muted).toBe(true); expect(audio.play).not.toHaveBeenCalled();
    let finished = false; void result.then(() => {finished = true;});
    audio.readyState = 3; audio.ranges = [[0, 1.9]]; audio.emit('canplay'); await settle(); expect(finished).toBe(false);
    audio.ranges = [[0, 2]]; audio.emit('progress'); expect(await result).toBe('ready');
    expect(h.onState).not.toHaveBeenCalled(); h.pool.destroy();
  });

  it('promotes buffered audio without another source lookup and only then unmutes and starts it', async () => {
    const h = harness(); const {audio, abort} = await bufferedAudio(h); h.select(2, true); h.pool.load(candidate(2).video_id); abort.abort();
    expect(h.resolve).toHaveBeenCalledOnce(); expect(h.audios).toHaveLength(1); expect(audio.muted).toBe(false); expect(audio.play).toHaveBeenCalledOnce();
    audio.emit('playing'); audio.emit('ended'); expect(h.onState.mock.calls.map(call => call[0])).toEqual([1, 0]); h.pool.destroy();
  });

  it('keeps a playing direct audio track alive after its initial 20-second preparation deadline', async () => {
    const h = harness(); h.select(1, true); h.pool.load(candidate(1).video_id); await settle();
    h.audios[0].emit('playing'); await vi.advanceTimersByTimeAsync(30000);
    expect(h.onError).not.toHaveBeenCalled(); expect(h.onState).toHaveBeenCalledWith(1); h.pool.destroy();
  });

  it('does not treat a 153 environment failure as a reason to extract audio', () => {
    const h = harness(); h.pool.load(candidate(1).video_id); const frame = FakePlayer.players[0]; frame.ready(); frame.error(153);
    expect(h.onError).toHaveBeenCalledWith(153); expect(h.resolve).not.toHaveBeenCalled(); h.pool.destroy();
  });

  it.each(['BUSY', 'RATE_LIMITED', 'TIMEOUT', 'EXTRACTOR_UNAVAILABLE', 'NETWORK_ERROR'])('returns unknown for transient %s without disrupting the active song', async reason => {
    const h = harness(vi.fn(async () => ({status: 'UNAVAILABLE', video_id: candidate(2).video_id, reason} as AudioResolution)));
    h.pool.load(candidate(1).video_id); const active = FakePlayer.players[0]; active.ready();
    const prepared = h.pool.prepare(candidate(2, true), track(2), new AbortController().signal);
    expect(await prepared).toBe('unknown'); expect(active.destroy).not.toHaveBeenCalled(); expect(h.onError).not.toHaveBeenCalled(); h.pool.destroy();
  });

  it.each([
    {audio_url: 'https://example.com/file.webm'},
    {audio_url: 'http://rr1.googlevideo.com/media'},
    {video_id: candidate(3).video_id},
    {expires_at: 'not-a-date'},
  ])('rejects an invalid or already-expiring source %j', async patch => {
    const h = harness(vi.fn(async () => source(2, patch)));
    expect(await h.pool.prepare(candidate(2, true), track(2), new AbortController().signal)).toBe('unavailable');
    expect(h.audios).toHaveLength(0); h.pool.destroy();
  });

  it('treats an expiring signed address as unknown instead of marking the recording unavailable', async () => {
    const h = harness(vi.fn(async () => source(2, {expires_at: new Date(Date.now() + 10000).toISOString()})));
    expect(await h.pool.prepare(candidate(2, true), track(2), new AbortController().signal)).toBe('unknown');
    expect(h.audios).toHaveLength(0); h.pool.destroy();
  });

  it('exposes the retained audio lifetime so its caller can refresh before a short address expires', async () => {
    const h = harness(vi.fn(async () => source(2, {expires_at: new Date(Date.now() + 70000).toISOString()})));
    await bufferedAudio(h);
    expect(h.pool.preparedLifetimeMs()).toBe(70000);
    await vi.advanceTimersByTimeAsync(10000); expect(h.pool.preparedLifetimeMs()).toBe(60000); h.pool.destroy();
  });

  it('cancels an unfinished audio lookup and ignores its late successful response', async () => {
    let finish!: (value: AudioResolution) => void;
    const resolve = vi.fn((_track: PlaybackTrack, _candidate: PlaybackCandidate, _signal: AbortSignal) => new Promise<AudioResolution>(done => {finish = done;}));
    const h = harness(resolve), abort = new AbortController(), prepared = h.pool.prepare(candidate(2, true), track(2), abort.signal);
    abort.abort(); expect(await prepared).toBe('unknown'); expect(resolve.mock.calls[0][2].aborted).toBe(true);
    finish(source(2)); await settle(); expect(h.audios).toHaveLength(0); expect(h.host.children).toHaveLength(0); h.pool.destroy();
  });

  it('discards ready preview media without pausing the current song', async () => {
    const h = harness(); h.pool.load(candidate(1).video_id); const active = FakePlayer.players[0]; active.ready();
    const {frame} = await warmIframe(h); h.pool.discardPrepared();
    expect(frame.destroy).toHaveBeenCalledOnce(); expect(active.destroy).not.toHaveBeenCalled(); expect(active.pauseVideo).not.toHaveBeenCalled(); h.pool.destroy();
  });

  it('never starts a timed-out current iframe when its ready callback arrives late', async () => {
    const h = harness(); h.pool.load(candidate(1).video_id); const frame = FakePlayer.players[0];
    await vi.advanceTimersByTimeAsync(20001);
    expect(h.onError).toHaveBeenCalledWith(153);
    frame.ready(); frame.state(1);
    expect(frame.loadVideoById).not.toHaveBeenCalled(); expect(h.onState).not.toHaveBeenCalled(); h.pool.destroy();
  });

  it('destroys both slots and ignores delayed iframe and audio callbacks after disposal', async () => {
    const h = harness(); h.pool.load(candidate(1).video_id); const active = FakePlayer.players[0]; active.ready();
    const {audio} = await bufferedAudio(h); h.pool.destroy(); h.onState.mockClear(); h.onError.mockClear();
    active.state(1); active.error(150); audio.emit('playing'); audio.emit('ended'); audio.emit('error');
    expect(h.onState).not.toHaveBeenCalled(); expect(h.onError).not.toHaveBeenCalled(); expect(audio.src).toBe(''); expect(h.host.children).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(30000); expect(h.onError).not.toHaveBeenCalled();
  });
});
