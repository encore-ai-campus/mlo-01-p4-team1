import {createTransport, cookieCsrf, MutationKeys, Phase4Error} from './transport';

export interface SongPolicy {
  ready: boolean;
  per_submission_limit: number;
  period_limit: number | null;
  window_days: number | null;
  require_approved_venue: boolean | null;
  allow_general_users: boolean | null;
  quota_scope: 'account' | 'venue' | null;
}

export interface Session {
  authenticated: boolean;
  capabilities: string[];
  managed_venues: {venue_id: string; name: string; address: string}[];
  song_policy: SongPolicy;
  csrf_token: string;
}

export interface Quota {
  ready: boolean;
  per_submission_limit: number;
  period_limit: number | null;
  window_days: number | null;
  remaining: number | null;
}

export interface SongInput {
  track_name: string;
  artist_name: string;
  youtube_url?: string | null;
}
export interface SongSubmission {venue_id?: string; items: SongInput[];}
export interface SongCheck {
  items: (SongInput & {duplicate_status: 'NONE' | 'POSSIBLE' | 'EXISTING_TRACK'})[];
  catalog_checked: boolean;
}
interface SongItem extends SongInput {
  id: string;
  review_status: 'PENDING' | 'APPROVED' | 'HOLD' | 'REJECTED' | 'LINKED';
  public_status?: string | null;
  public_reason?: string | null;
  revision: number;
}
export interface SongRequest {id: string; created_at?: string | null; items: SongItem[];}
export interface AdminSongItem extends SongItem {pipeline_status?: string | null; catalog_version?: string | null;}
export interface SongTags {genre: string[]; mood: string[];}
interface TrackMetadata {album_name: string; duration_ms: number; explicit: boolean;}
export interface FinalSongReview extends TrackMetadata {expected_revision: number; tags: SongTags;}
export interface AdminTask {
  id: string;
  request_item_id: string;
  status: string;
  track_name: string;
  artist_name: string;
  revision: number;
  result: {model_version: string; tags: SongTags; metadata?: Partial<TrackMetadata> | null} | null;
  registration?: {track_id: string; tags: SongTags} | null;
}
export type SongReview = {expected_revision: number; reason: string} & (
  {decision: 'APPROVE'; youtube_url: string} |
  {decision: 'LINK_EXISTING'; track_id: string} |
  {decision: 'HOLD' | 'REJECT'}
);

interface Items<T> {items: T[]; has_more?: boolean;}
export const SONG_REQUEST_PAGE_SIZE = 20;

function listQuery(page: number, filters: Record<string, string> = {}) {
  return `?${new URLSearchParams({
    ...filters,
    limit: String(SONG_REQUEST_PAGE_SIZE),
    offset: String(page * SONG_REQUEST_PAGE_SIZE),
  })}`;
}

export function createSongRequestClient() {
  let csrf = '';
  const transport = createTransport({csrf: () => csrf || cookieCsrf()});
  const keys = new MutationKeys();
  async function mutate<T>(path: string, body: unknown) {
    const value = await transport.post<T>(path, body, keys.key(path, body));
    keys.completed(path, body);
    return value;
  }
  const query = (name: string, value?: string) => value ? `?${new URLSearchParams({[name]: value})}` : '';
  return {
    async getSession(signal: AbortSignal) {
      const value = await transport.get<Session>('/session', signal);
      if (typeof value.authenticated !== 'boolean' || !Array.isArray(value.capabilities) ||
          !Array.isArray(value.managed_venues) || !value.song_policy || typeof value.csrf_token !== 'string') {
        throw new Phase4Error('INVALID_SESSION', '곡 신청 권한을 확인할 수 없습니다.');
      }
      if (!signal.aborted) csrf = value.csrf_token;
      return value;
    },
    getQuota: (venue: string | undefined, signal: AbortSignal) =>
      transport.get<Quota>('/song-requests/quota' + query('venue_id', venue), signal),
    checkSongs: (body: SongSubmission) => mutate<SongCheck>('/song-requests/check', body),
    submitSongs: (body: SongSubmission) => mutate<SongRequest>('/song-requests', body),
    getSongRequests: (page: number, signal: AbortSignal) =>
      transport.get<Items<SongRequest>>('/song-requests' + listQuery(page), signal),
    getAdminSongItems: (status: string, page: number, signal: AbortSignal) =>
      transport.get<Items<AdminSongItem>>('/admin/song-items' + listQuery(page, status ? {status} : {}), signal),
    reviewSong: (id: string, body: SongReview) =>
      mutate<AdminSongItem>(`/admin/song-items/${encodeURIComponent(id)}/review`, body),
    getAdminTasks: (page: number, signal: AbortSignal) =>
      transport.get<Items<AdminTask> & {tag_options?: SongTags}>('/admin/tasks' + listQuery(page), signal),
    finalizeSong: (id: string, body: FinalSongReview) =>
      mutate<AdminTask>(`/admin/tasks/${encodeURIComponent(id)}/finalize`, body),
  };
}

export type SongRequestClient = ReturnType<typeof createSongRequestClient>;
