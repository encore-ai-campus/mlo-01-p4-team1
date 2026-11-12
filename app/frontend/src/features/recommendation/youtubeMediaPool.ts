import type {PlaybackAdapter, PlaybackCandidate, PlaybackTrack} from './youtubePlayerController';
import type {YoutubeIframeApi, YoutubeIframePlayer} from './youtubeIframeApi';

export type AudioResolution = {status: 'READY' | 'UNAVAILABLE'; video_id: string; audio_url?: string; thumbnail_url?: string; expires_at?: string; reason?: string; message?: string};
type Prepared = 'ready' | 'unavailable' | 'unknown';
type Mode = 'iframe' | 'audio';
type Slot = {
  candidate: PlaybackCandidate; track: PlaybackTrack; panel: HTMLDivElement; media: HTMLDivElement;
  player?: YoutubeIframePlayer; audio?: HTMLAudioElement; mode: Mode; warm: boolean; ready: boolean;
  disposed: boolean; switching: boolean; iframeReady: boolean; abort: AbortController; expiresAt: number; loadAt: number;
  deadline?: ReturnType<typeof setTimeout>;
  finish: (result: Prepared) => void; promise: Promise<Prepared>; unlink: () => void;
};
export type MediaPoolOptions = {
  api: YoutubeIframeApi | (() => Promise<YoutubeIframeApi>); host: HTMLDivElement;
  current: () => {candidate: PlaybackCandidate; track: PlaybackTrack} | null;
  shouldAutoplay?: () => boolean;
  allowCandidate: (track: PlaybackTrack, candidate: PlaybackCandidate) => boolean;
  resolveAudio: (track: PlaybackTrack, candidate: PlaybackCandidate, signal: AbortSignal) => Promise<AudioResolution>;
  onState: (value: number) => void; onError: (code: number) => void; onAutoplayBlocked: () => void;
  onRoute: (mode: Mode) => void;
  onTransition?: (value: {track_id: string; route: Mode; prepared: boolean; milliseconds: number}) => void;
};
const TRANSIENT = new Set(['BUSY', 'RATE_LIMITED', 'TIMEOUT', 'EXTRACTOR_UNAVAILABLE', 'NETWORK_ERROR']);

// The next slot cues an iframe or buffers an authorized audio source.
// Its events stay separate from the current track until the slot is promoted.
export class YoutubeMediaPool implements PlaybackAdapter {
  private active: Slot | null = null;
  private prepared: Slot | null = null;
  private warming: Slot | null = null;
  private slots = new Set<Slot>();
  private destroyed = false;
  constructor(private options: MediaPoolOptions) {}

  private clearTimers(slot: Slot) {clearTimeout(slot.deadline);}
  private dispose(slot: Slot) {
    if (slot.disposed) return;
    slot.disposed = true; this.clearTimers(slot); slot.unlink(); slot.abort.abort();
    try {slot.player?.destroy();} catch { /* Remaining media still needs cleanup. */ }
    try {slot.audio?.pause(); slot.audio?.removeAttribute('src'); slot.audio?.load();} catch { /* Already detached media. */ }
    slot.panel.remove(); this.slots.delete(slot);
    if (this.prepared === slot) this.prepared = null;
    if (this.warming === slot) this.warming = null;
    if (this.active === slot) this.active = null;
    slot.finish('unknown');
  }
  discardPrepared() {
    if (this.warming) this.dispose(this.warming);
    if (this.prepared) this.dispose(this.prepared);
  }
  private currentSlot(slot: Slot) {return !this.destroyed && !slot.disposed && (this.active === slot || this.warming === slot || this.prepared === slot);}
  private makeSlot(candidate: PlaybackCandidate, track: PlaybackTrack, warm: boolean, signal?: AbortSignal): Slot {
    const panel = document.createElement('div'), media = document.createElement('div');
    panel.className = `youtube-media-slot ${warm ? 'is-preview' : 'is-active'}`;
    if (warm) {
      panel.inert = true;
      panel.setAttribute('aria-hidden', 'true');
    }
    panel.append(media); this.options.host.append(panel);
    let resolve!: (value: Prepared) => void; let settled = false;
    const promise = new Promise<Prepared>(done => {resolve = done;});
    const slot: Slot = {candidate, track, panel, media, mode: 'iframe', warm, ready: false, disposed: false, switching: false, iframeReady: false,
      abort: new AbortController(), expiresAt: Infinity, loadAt: performance.now(), promise, unlink: () => {},
      finish: result => {if (!settled) {settled = true; resolve(result);}}};
    if (signal) {
      const cancel = () => {if (slot !== this.active) this.dispose(slot);};
      signal.addEventListener('abort', cancel, {once: true}); slot.unlink = () => signal.removeEventListener('abort', cancel);
    }
    this.slots.add(slot); return slot;
  }
  private completePreparation(slot: Slot, result: Prepared) {
    if (!this.currentSlot(slot) || !slot.warm) return;
    this.clearTimers(slot);
    if (result === 'ready') {
      slot.ready = true; this.warming = null; this.prepared = slot;
      slot.finish('ready');
    } else {slot.finish(result); this.dispose(slot);}
  }
  private matches(slot: Slot) {return slot.player?.getVideoData?.().video_id === slot.candidate.video_id;}
  private fail(slot: Slot, code: number, result: Prepared = 'unknown') {
    if (!this.currentSlot(slot)) return;
    if (slot.warm) this.completePreparation(slot, result);
    else {this.dispose(slot); this.options.onError(code);}
  }
  private startIframe(slot: Slot) {
    const frame = document.createElement('iframe');
    frame.title = 'YouTube 선곡표 삽입 재생';
    const id = slot.warm ? '' : encodeURIComponent(slot.candidate.video_id);
    frame.src = `https://www.youtube.com/embed/${id}?enablejsapi=1&playsinline=1&rel=0&origin=${encodeURIComponent(window.location.origin)}`;
    frame.allow = 'autoplay; encrypted-media; fullscreen; picture-in-picture'; frame.allowFullscreen = true;
    frame.referrerPolicy = 'strict-origin-when-cross-origin'; slot.media.append(frame);
    slot.deadline = setTimeout(() => this.iframeUnavailable(slot, 153), 20000);
    const attach = (api: YoutubeIframeApi) => {
      if (!this.currentSlot(slot) || slot.switching) return;
      slot.player = new api.Player(frame, {events: {
        onReady: () => {
          if (!this.currentSlot(slot) || !slot.player || slot.switching || slot.iframeReady) return;
          slot.iframeReady = true;
          clearTimeout(slot.deadline);
          if (slot.warm) {
            if (!slot.player.cueVideoById || !slot.player.mute || !slot.player.getVideoData) {this.completePreparation(slot, 'unknown'); return;}
            slot.deadline = setTimeout(() => this.completePreparation(slot, 'unknown'), 18000);
            slot.player.mute();
            slot.player.cueVideoById(slot.candidate.video_id);
          } else if (this.options.shouldAutoplay?.() !== false) {
            slot.player.loadVideoById(slot.candidate.video_id);
          } else {
            this.emitState(slot, 5);
          }
        },
        onStateChange: event => {
          if (!this.currentSlot(slot) || slot.switching || !this.matches(slot)) return;
          if (!slot.warm) {this.emitState(slot, event.data); return;}
          if (event.data === 5 && slot.iframeReady && !slot.ready) this.completePreparation(slot, 'ready');
          if (event.data === 1) slot.player?.pauseVideo();
        },
        onError: event => this.iframeUnavailable(slot, event.data),
        onAutoplayBlocked: () => {
          if (!this.currentSlot(slot) || slot.switching) return;
          if (slot.warm) this.completePreparation(slot, 'unknown'); else this.options.onAutoplayBlocked();
        },
      }});
    };
    if (typeof this.options.api === 'function') {
      void this.options.api().then(attach).catch(() => this.iframeUnavailable(slot, 153));
    } else attach(this.options.api);
  }
  private iframeUnavailable(slot: Slot, code: number) {
    if (!this.currentSlot(slot) || slot.switching) return;
    if ([101, 150].includes(code) || (!slot.warm && slot.candidate.match_status === 'VERIFIED')) void this.startAudio(slot, code);
    else this.fail(slot, code, [2, 100].includes(code) ? 'unavailable' : 'unknown');
  }
  private emitState(slot: Slot, value: number) {
    if (slot !== this.active || slot.warm || slot.disposed) return;
    if (value === 1) clearTimeout(slot.deadline);
    if (value === 1 && slot.loadAt >= 0) {
      this.options.onTransition?.({track_id: slot.track.track_id, route: slot.mode, prepared: slot.ready, milliseconds: Math.round(performance.now() - slot.loadAt)});
      slot.loadAt = -1;
    }
    this.options.onState(value);
  }
  private async startAudio(slot: Slot, iframeError = 150) {
    if (!this.currentSlot(slot) || slot.switching) return;
    if (!this.options.allowCandidate(slot.track, slot.candidate)) {this.fail(slot, iframeError, 'unavailable'); return;}
    slot.switching = true; this.clearTimers(slot);
    try {slot.player?.destroy();} catch { /* An unresponsive iframe is still detached below. */ }
    slot.iframeReady = false; slot.player = undefined; slot.media.replaceChildren();
    slot.deadline = setTimeout(() => {slot.abort.abort(); this.fail(slot, iframeError);}, 26000);
    let source: AudioResolution;
    try {source = await this.options.resolveAudio(slot.track, slot.candidate, slot.abort.signal);}
    catch {this.fail(slot, iframeError); return;}
    if (!this.currentSlot(slot)) return;
    if (source.status !== 'READY') {this.fail(slot, iframeError, TRANSIENT.has(source.reason || '') ? 'unknown' : 'unavailable'); return;}
    const expiry = Date.parse(source.expires_at || '');
    if (source.video_id !== slot.candidate.video_id || !/^https:\/\/[^/]*\.googlevideo\.com\//.test(source.audio_url || '') || !Number.isFinite(expiry)) {
      this.fail(slot, iframeError, 'unavailable'); return;
    }
    if (expiry < Date.now() + 15000) {this.fail(slot, iframeError, 'unknown'); return;}
    this.clearTimers(slot); slot.mode = 'audio'; slot.expiresAt = expiry;
    const image = document.createElement('img'); image.alt = `${slot.track.track_name} · 영상 이미지`;
    const fallbackCover = slot.track.cover_image_url || '/default-cover.svg';
    image.src = /^https:\/\/(i\.ytimg\.com|img\.youtube\.com)\//.test(source.thumbnail_url || '') ? source.thumbnail_url! : fallbackCover;
    image.addEventListener('error', () => {if (this.currentSlot(slot)) image.src = fallbackCover;}, {once: true});
    const audio = document.createElement('audio'); audio.controls = !slot.warm; audio.preload = 'auto'; audio.muted = slot.warm;
    slot.audio = audio; slot.media.className = 'youtube-media-audio'; slot.media.append(image, audio);
    const ownsAudio = () => this.currentSlot(slot) && slot.audio === audio;
    audio.addEventListener('playing', () => {if (ownsAudio() && !slot.warm) this.emitState(slot, 1);});
    audio.addEventListener('pause', () => {if (ownsAudio() && !slot.warm) this.emitState(slot, 2);});
    audio.addEventListener('ended', () => {if (ownsAudio() && !slot.warm) this.emitState(slot, 0);});
    audio.addEventListener('error', () => {if (ownsAudio()) this.fail(slot, iframeError, 'unavailable');});
    slot.deadline = setTimeout(() => this.fail(slot, iframeError), 20000);
    const buffered = () => {
      if (!ownsAudio() || !slot.warm) return;
      const seconds = audio.buffered.length ? audio.buffered.end(0) - audio.buffered.start(0) : 0;
      if (audio.readyState >= 3 && seconds >= Math.min(2, audio.duration || 2)) this.completePreparation(slot, 'ready');
    };
    audio.addEventListener('canplay', buffered); audio.addEventListener('progress', buffered); audio.addEventListener('loadeddata', buffered);
    audio.src = source.audio_url!; audio.load();
    if (!slot.warm) {
      this.options.onRoute('audio');
      if (this.options.shouldAutoplay?.() !== false) this.play();
      else {this.clearTimers(slot); this.emitState(slot, 5);}
    }
  }
  async prepare(candidate: PlaybackCandidate, track: PlaybackTrack, signal: AbortSignal): Promise<Prepared> {
    if (this.destroyed || signal.aborted || !this.options.allowCandidate(track, candidate)) return 'unknown';
    this.discardPrepared();
    const slot = this.makeSlot(candidate, track, true, signal); this.warming = slot;
    if (candidate.playback_hint === 'AUDIO' && candidate.source !== 'TAGGED_JSON_PREVIEW') void this.startAudio(slot); else this.startIframe(slot);
    return slot.promise;
  }
  preparedLifetimeMs() {return this.prepared ? Math.max(0, this.prepared.expiresAt - Date.now()) : 0;}
  load(videoId: string) {
    const current = this.options.current();
    if (!current || this.destroyed || current.candidate.video_id !== videoId || !this.options.allowCandidate(current.track, current.candidate)) return;
    const slot = this.prepared;
    if (slot && slot.candidate.video_id === videoId && slot.track.track_id === current.track.track_id && slot.expiresAt > Date.now() + 15000) {
      this.prepared = null; slot.unlink();
      if (this.active) this.dispose(this.active);
      this.active = slot; slot.loadAt = performance.now();
      slot.warm = false; slot.panel.className = 'youtube-media-slot is-active';
      slot.panel.inert = false; slot.panel.removeAttribute('aria-hidden');
      if (slot.audio) slot.audio.controls = true;
      this.options.onRoute(slot.mode);
      if (this.options.shouldAutoplay?.() !== false) this.play();
      else this.emitState(slot, 5);
      return;
    }
    this.discardPrepared(); if (this.active) this.dispose(this.active);
    const next = this.makeSlot(current.candidate, current.track, false); this.active = next;
    this.options.onRoute('iframe');
    if (current.candidate.playback_hint === 'AUDIO' && current.candidate.source !== 'TAGGED_JSON_PREVIEW') void this.startAudio(next); else this.startIframe(next);
  }
  play() {
    const slot = this.active; if (!slot || slot.disposed) return;
    if (slot.mode === 'audio' && slot.audio) {
      if (slot.expiresAt <= Date.now() + 5000) {void this.refreshAudio(slot); return;}
      slot.audio.muted = false;
      void slot.audio.play().catch(error => {if (!this.currentSlot(slot)) return; if (error.name === 'NotAllowedError') this.options.onAutoplayBlocked(); else this.options.onError(150);});
    } else if (slot.iframeReady) {slot.player?.unMute?.(); slot.player?.playVideo();}
  }
  private async refreshAudio(slot: Slot) {
    slot.audio?.pause(); slot.switching = false; await this.startAudio(slot);
  }
  pause() {const slot = this.active; if (slot?.audio) slot.audio.pause(); else if (slot?.iframeReady) slot.player?.pauseVideo();}
  getCurrentTime() {return this.active?.audio?.currentTime ?? this.active?.player?.getCurrentTime() ?? 0;}
  destroy() {if (this.destroyed) return; this.destroyed = true; for (const slot of [...this.slots]) this.dispose(slot); this.options.host.replaceChildren();}
}
