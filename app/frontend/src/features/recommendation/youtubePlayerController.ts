export type PlaybackTrack = {track_id: string; track_name: string; artists_name: string};
export type PlaybackCandidate = {video_id: string; title: string; channel_title: string; duration_seconds: number; source: 'CATALOG_MAPPING' | 'YOUTUBE_API' | 'MAPPING_REGISTRY'; match_status?: 'VERIFIED' | 'CANDIDATE'; playback_hint?: 'AUDIO'};
export type PlaybackResolution = {track_id: string; status: 'READY' | 'SEARCH_UNAVAILABLE' | 'NO_MATCH' | 'MAPPING_REQUIRED'; candidates: PlaybackCandidate[]; reason?: 'UNMAPPED' | 'PENDING' | 'BLOCKED'; message?: string};
export type PlaybackPhase = 'idle' | 'resolving' | 'loading' | 'playing' | 'paused' | 'blocked' | 'unavailable' | 'complete' | 'error';
export type PlaybackSnapshot = {index: number; phase: PlaybackPhase; message: string; candidate: PlaybackCandidate | null; skipped: {track_id: string; track_name: string; reason: string}[]; playedTrackIds: string[]};
export type PlaybackAdapter = {load: (videoId: string) => void; play: () => void; pause: () => void; prepare?: (candidate: PlaybackCandidate, track: PlaybackTrack, signal: AbortSignal) => Promise<'ready' | 'unavailable' | 'unknown'>; discardPrepared?: () => void; preparedLifetimeMs?: () => number};
export type PlaybackResolver = (trackId: string, excludedVideoIds: string[], signal: AbortSignal) => Promise<PlaybackResolution>;
type NextPreparation = {owner: number; index: number; request: AbortController; resolution: PlaybackResolution | null; lookupResolution?: PlaybackResolution; attempted: string[]; preparedAt: number | null; validUntil: number | null};
const PREPARED_REFRESH_MS = 90000;
const PREPARED_TTL_MS = 120000;
const PREPARED_EXPIRY_MARGIN_MS = 15000;
const PREPARED_REFRESH_MARGIN_MS = 30000;

// The controller only advances on a real ENDED event, explicit navigation, or a
// recorded unavailable recording. A blocked autoplay never silently skips a song.
export class YoutubePlaybackController {
  private state: PlaybackSnapshot = {index: 0, phase: 'idle', message: '', candidate: null, skipped: [], playedTrackIds: []};
  private request: AbortController | null = null;
  private sequence = 0;
  private stopped = false;
  private blockReason: string | null = null;
  private candidates: PlaybackCandidate[] = [];
  private attempted: string[] = [];
  private requests = 0;
  private began = false;
  private preparation: NextPreparation | null = null;
  private preparationOwner = -1;
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private expiryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private tracks: PlaybackTrack[], private adapter: PlaybackAdapter, private resolve: PlaybackResolver, private publish: (state: PlaybackSnapshot) => void) {}

  snapshot() {return {...this.state, skipped: [...this.state.skipped], playedTrackIds: [...this.state.playedTrackIds]};}

  private update(patch: Partial<PlaybackSnapshot>) {
    if (this.stopped) return;
    this.state = {...this.state, ...patch};
    this.publish(this.snapshot());
  }

  private clearPreparationTimers() {
    if (this.refreshTimer !== null) clearTimeout(this.refreshTimer);
    if (this.expiryTimer !== null) clearTimeout(this.expiryTimer);
    this.refreshTimer = null; this.expiryTimer = null;
  }

  private discardPreparation() {
    this.clearPreparationTimers();
    const previous = this.preparation; this.preparation = null;
    previous?.request.abort();
    this.adapter.discardPrepared?.();
  }

  private invalidate(preservePrepared = false) {
    this.sequence += 1;
    this.request?.abort();
    this.request = null;
    this.began = false;
    this.preparationOwner = -1;
    if (preservePrepared) {this.clearPreparationTimers(); this.preparation = null;}
    else this.discardPreparation();
  }

  async start(index = 0) {
    if (this.stopped || this.blockReason) return;
    const prepared = this.preparation;
    const reuse = prepared?.index === index && prepared.owner === this.sequence && prepared.preparedAt !== null
      && prepared.validUntil !== null && prepared.validUntil > Date.now() && !prepared.request.signal.aborted && ['READY', 'NO_MATCH'].includes(prepared.resolution?.status || '');
    this.invalidate(!!reuse && prepared?.resolution?.status === 'READY');
    if (index >= this.tracks.length) {this.finish(); return;}
    this.adapter.pause();
    this.candidates = []; this.attempted = []; this.requests = 0;
    this.update({index: Math.max(0, index), candidate: null, phase: 'resolving', message: '이 곡의 재생 가능한 영상을 찾고 있어요.'});
    if (reuse && prepared?.resolution) {
      if (prepared.resolution.status === 'NO_MATCH') {
        await this.skipUnavailable(prepared.resolution.message || '사전 확인한 모든 재생 후보를 사용할 수 없어 이 곡을 건너뛰었어요.');
        return;
      }
      this.candidates = [...prepared.resolution.candidates];
      this.attempted = [...prepared.attempted]; this.requests = 1;
      // load() promotes the ready resource before any later prepared cleanup.
      // Its adapter must detach the preparation signal from the promoted player.
      this.loadNextCandidate();
      return;
    }
    await this.findCandidates();
  }

  private preparationIsCurrent(value: NextPreparation) {
    return !this.stopped && !this.blockReason && this.preparation === value
      && value.owner === this.sequence && !value.request.signal.aborted;
  }

  private prepareNext() {
    if (!this.adapter.prepare || this.stopped || this.blockReason || this.preparationOwner === this.sequence) return;
    const index = this.state.index + 1;
    if (!this.tracks[index]) return;
    this.preparationOwner = this.sequence;
    const value: NextPreparation = {owner: this.sequence, index, request: new AbortController(), resolution: null, attempted: [], preparedAt: null, validUntil: null};
    this.preparation = value;
    void this.resolvePreparation(value);
  }

  private async resolvePreparation(value: NextPreparation) {
    try {
      const track = this.tracks[value.index];
      const result = await this.resolve(track.track_id, [], value.request.signal);
      if (!this.preparationIsCurrent(value)) return;
      if (result.track_id !== track.track_id || result.status !== 'READY') {this.discardPreparation(); return;}
      const candidates = result.candidates.filter((candidate, index, all) => /^[A-Za-z0-9_-]{11}$/.test(candidate.video_id) && all.findIndex(item => item.video_id === candidate.video_id) === index);
      value.resolution = {...result, candidates};
      value.lookupResolution = value.resolution;
      await this.prepareCandidates(value);
    } catch {
      // A look-ahead lookup cannot fail, skip, or interrupt the current song.
      if (this.preparationIsCurrent(value)) this.discardPreparation();
    }
  }

  private async prepareCandidates(value: NextPreparation) {
    if (!this.adapter.prepare || !value.resolution) return;
    const candidates = value.resolution.candidates;
    let index = 0;
    for (; index < candidates.length && value.attempted.length < 5; index += 1) {
      const candidate = candidates[index];
      const status = await this.adapter.prepare(candidate, this.tracks[value.index], value.request.signal);
      if (!this.preparationIsCurrent(value)) return;
      if (status !== 'ready' && status !== 'unavailable') {this.discardPreparation(); return;}
      if (status === 'unavailable') {
        value.attempted.push(candidate.video_id);
        this.adapter.discardPrepared?.();
        continue;
      }
      value.resolution = {...value.resolution, candidates: candidates.slice(index)};
      this.cachePreparation(value);
      return;
    }
    if (this.preparationIsCurrent(value) && candidates.length > 0 && index === candidates.length) {
      // Only actual media checks can create this negative cache. Unknown checks,
      // lookup outages, and candidates beyond the bounded check remain unknown.
      value.resolution = {...value.resolution, status: 'NO_MATCH', candidates: [], message: '사전 확인한 모든 재생 후보를 사용할 수 없어 이 곡을 건너뛰었어요.'};
      this.cachePreparation(value);
      return;
    }
    if (this.preparationIsCurrent(value)) this.discardPreparation();
  }

  private cachePreparation(value: NextPreparation) {
    // A negative decision has no signed media URL. Only ready media needs an
    // adapter-provided lifetime; iframe resources can report Infinity.
    let remaining = Infinity;
    if (value.resolution?.status === 'READY' && this.adapter.preparedLifetimeMs) {
      try { remaining = this.adapter.preparedLifetimeMs(); }
      catch { remaining = 0; }
    }
    const lifetime = remaining === Infinity ? PREPARED_TTL_MS
      : Math.min(PREPARED_TTL_MS, Number.isFinite(remaining) ? remaining - PREPARED_EXPIRY_MARGIN_MS : 0);
    if (lifetime <= 0) {this.discardPreparation(); return;}
    const refresh = remaining === Infinity ? PREPARED_REFRESH_MS
      : Math.max(1000, Math.min(PREPARED_REFRESH_MS, remaining - PREPARED_REFRESH_MARGIN_MS));
    value.preparedAt = Date.now();
    value.validUntil = value.preparedAt + lifetime;
    this.refreshTimer = setTimeout(() => this.refreshPreparation(value), refresh);
    this.expiryTimer = setTimeout(() => {
      if (!this.preparationIsCurrent(value)) return;
      this.discardPreparation(); this.preparationOwner = -1;
    }, lifetime);
  }

  private refreshPreparation(previous: NextPreparation) {
    if (!this.preparationIsCurrent(previous) || this.state.phase !== 'playing') return;
    const resolution = previous.lookupResolution || previous.resolution;
    this.discardPreparation();
    if (!resolution) return;
    const value: NextPreparation = {owner: this.sequence, index: previous.index, request: new AbortController(), resolution, lookupResolution: resolution, attempted: [], preparedAt: null, validUntil: null};
    this.preparation = value;
    void this.prepareCandidates(value).catch(() => {
      if (this.preparationIsCurrent(value)) this.discardPreparation();
    });
  }

  private async findCandidates() {
    const track = this.tracks[this.state.index];
    if (!track || this.stopped || this.blockReason) return;
    const sequence = this.sequence;
    const controller = new AbortController(); this.request = controller; this.requests += 1;
    this.update({phase: 'resolving', message: '이 곡의 재생 가능한 영상을 찾고 있어요.'});
    try {
      const result = await this.resolve(track.track_id, [...this.attempted], controller.signal);
      if (controller.signal.aborted || sequence !== this.sequence || this.stopped || this.blockReason) return;
      if (result.track_id !== track.track_id) throw new Error('곡의 영상 응답이 일치하지 않아요. 다시 확인해 주세요.');
      if (result.status === 'MAPPING_REQUIRED') {
        await this.skipUnavailable(result.message || (result.reason === 'BLOCKED' ? '등록한 영상이 차단되어 다른 영상 연결이 필요해요.' : result.reason === 'PENDING' ? '등록한 영상의 원곡·삽입 재생 검수를 기다리고 있어요.' : '이 곡의 영상이 아직 연결되지 않았어요.'));
        return;
      }
      if (result.status === 'SEARCH_UNAVAILABLE') {
        if (this.attempted.length) {
          await this.skipUnavailable('확인된 영상은 삽입 재생을 허용하지 않고, 대체 영상 검색도 현재 연결할 수 없어요.');
          return;
        }
        this.update({phase: 'error', candidate: null, message: result.message || '영상 검색 연결을 확인할 수 없어요. 연결을 다시 확인해 주세요.'});
        return;
      }
      this.candidates = result.candidates.filter((candidate, index, all) => /^[A-Za-z0-9_-]{11}$/.test(candidate.video_id) && !this.attempted.includes(candidate.video_id) && all.findIndex(item => item.video_id === candidate.video_id) === index);
      if (result.status !== 'READY' || !this.candidates.length) {await this.skipUnavailable(result.message || '원곡과 일치하는 재생 가능한 영상을 찾지 못했어요.'); return;}
      this.loadNextCandidate();
    } catch (error) {
      if (!controller.signal.aborted && sequence === this.sequence) this.update({phase: 'error', candidate: null, message: error instanceof Error ? error.message : '영상 연결을 다시 확인해 주세요.'});
    }
  }

  private loadNextCandidate() {
    if (this.stopped || this.blockReason) return;
    const candidate = this.candidates.shift();
    if (!candidate || this.attempted.length >= 5) {void this.skipUnavailable('이 곡의 영상이 삭제되었거나 삽입 재생을 허용하지 않아요.'); return;}
    this.attempted.push(candidate.video_id); this.began = false;
    this.update({candidate, phase: 'loading', message: 'YouTube 영상을 불러오고 있어요.'});
    this.adapter.load(candidate.video_id);
  }

  private async skipUnavailable(reason: string) {
    const track = this.tracks[this.state.index];
    const skipped = this.state.skipped.filter(item => item.track_id !== track.track_id);
    this.update({skipped: [...skipped, {track_id: track.track_id, track_name: track.track_name, reason}]});
    await this.start(this.state.index + 1);
  }

  private finish() {
    this.adapter.pause();
    const count = this.state.playedTrackIds.length;
    this.update({candidate: null, phase: count ? 'complete' : 'unavailable', message: count ? `선곡표의 마지막 곡까지 진행했어요. 재생하지 못한 곡 ${this.state.skipped.length}곡을 확인해 주세요.` : '이 선곡표에서 재생 가능한 영상을 찾지 못했어요. 아래 누락 내역과 연결 설정을 확인해 주세요.'});
  }

  onPlayerState(value: number) {
    if (this.stopped) return;
    if (this.blockReason) {this.adapter.pause(); return;}
    if (!this.state.candidate) return;
    if (value === 1) {
      this.began = true;
      const trackId = this.tracks[this.state.index].track_id;
      this.update({phase: 'playing', message: 'YouTube에서 재생 중이에요.', playedTrackIds: [...new Set([...this.state.playedTrackIds, trackId])]});
      this.prepareNext();
    } else if (value === 0 && this.began) {
      this.began = false;
      void this.start(this.state.index + 1);
    } else if (value === 2 && this.state.phase === 'playing') {
      this.update({phase: 'paused', message: '일시 정지했어요.'});
    }
  }

  onPlayerError(code: number) {
    if (this.stopped || this.blockReason || !this.state.candidate) return;
    this.began = false;
    this.discardPreparation(); this.preparationOwner = -1;
    if ([2, 100, 101, 150].includes(code)) {
      if (this.candidates.length && this.attempted.length < 5) this.loadNextCandidate();
      else if (this.requests < 2 && this.attempted.length < 5) void this.findCandidates();
      else void this.skipUnavailable(`YouTube 삽입 재생 불가(오류 ${code})로 이 곡을 건너뛰었어요.`);
    } else {
      this.adapter.pause();
      this.update({phase: 'error', message: code === 153 ? 'YouTube가 이 페이지의 출처를 확인하지 못했어요(153). 대시보드를 새로고침해 주세요.' : `YouTube 플레이어 연결을 확인해 주세요(오류 ${code}). 이 곡은 자동으로 건너뛰지 않았어요.`});
    }
  }

  onAutoplayBlocked() {
    if (!this.blockReason && this.state.candidate && !this.stopped) this.update({phase: 'blocked', message: '브라우저가 자동 재생을 보류했어요. [재생]을 한 번 눌러 주세요.'});
  }

  onLoadingDelayed() {
    if (!this.blockReason && !this.stopped && this.state.phase === 'loading') this.update({message: '재생 준비가 길어지고 있어요. 필요하면 [재생]을 눌러 주세요.'});
  }

  setBlocked(reason: string | null) {
    if (reason === this.blockReason || this.stopped) return;
    this.blockReason = reason;
    if (reason) {
      const pending = this.state.phase === 'resolving';
      this.invalidate(); this.adapter.pause();
      this.update({phase: 'blocked', message: reason, ...(pending ? {candidate: null} : {})});
    } else this.update({phase: 'paused', message: '변경 상태를 확인했어요. [재생]을 누르면 이어서 재생해요.'});
  }

  play() {
    if (this.stopped || this.blockReason) return;
    if (!this.state.candidate || ['complete', 'unavailable'].includes(this.state.phase)) {void this.start(this.state.phase === 'complete' || this.state.phase === 'unavailable' ? 0 : this.state.index); return;}
    this.update({phase: 'loading', message: '재생을 시작하고 있어요.'}); this.adapter.play();
  }
  pause() {if (!this.stopped) {this.adapter.pause(); this.update({phase: 'paused', message: '일시 정지했어요.'});}}
  previous() {if (!this.blockReason) void this.start(Math.max(0, this.state.index - 1));}
  next() {if (!this.blockReason) void this.start(this.state.index + 1);}
  retry() {if (!this.blockReason) void this.start(this.state.index);}
  destroy() {this.invalidate(); this.adapter.pause(); this.stopped = true;}
}
