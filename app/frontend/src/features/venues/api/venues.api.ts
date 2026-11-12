import {
  VenueItem,
  VenueDetail,
  VenueEventSummary,
  ConciergeBriefing,
  MySummaryData,
  BudgetTier,
  VenueCategory,
  StoreType,
} from '../types/venue.types';
import { csrfHeaders, setCsrf } from '../../../shared/api';

export async function ensureVenueCsrfHeaders(): Promise<Record<string, string>> {
  const existing = csrfHeaders();
  if (existing['X-CSRFToken']) return existing;
  try {
    const res = await fetch('/api/auth/me', { credentials: 'same-origin' });
    if (res.ok) {
      const data = await res.json();
      if (typeof data?.csrf_token === 'string' && data.csrf_token) {
        setCsrf(data.csrf_token);
        return { 'X-CSRFToken': data.csrf_token };
      }
    }
  } catch {
    // Fall back to any existing cookie token cleanly
  }
  return csrfHeaders();
}

export class VenueApiError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = 'VenueApiError';
    this.status = status;
    this.code = code;
  }
}

export async function readApiResponse<T = any>(res: Response, fallbackLabel: string): Promise<T> {
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data?.message || data?.detail || res.statusText || `HTTP ${res.status}`;
    throw new VenueApiError(`${fallbackLabel}: ${detail}`, res.status, data?.code);
  }
  return data as T;
}

export async function venuePostJson<T = any>(
  url: string,
  body: unknown,
  extraHeaders: Record<string, string> = {},
): Promise<T> {
  const csrf = await ensureVenueCsrfHeaders();
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...csrf, ...extraHeaders },
    body: JSON.stringify(body),
  });
  return readApiResponse<T>(res, '요청을 처리하지 못했습니다');
}

export interface VenueSearchParams {
  sigungu?: string;
  dong?: string;
  category?: VenueCategory;
  price_tier?: BudgetTier | '';
  store_type?: StoreType;
  event_only?: boolean;
  partner_only?: boolean;
  partner_exact?: boolean;
  open_now?: boolean;
  q?: string;
  weather?: string;
  time_slot?: string;
  user_id?: string;
  min_lat?: number;
  max_lat?: number;
  min_lng?: number;
  max_lng?: number;
}

export async function fetchVenues(params: VenueSearchParams): Promise<{ success: boolean; gu: string; total_count: number; venues: VenueItem[] }> {
  const query = new URLSearchParams();
  if (params.sigungu) query.set('sigungu', params.sigungu);
  if (params.dong) query.set('dong', params.dong);
  if (params.category && params.category !== 'ALL') query.set('category', params.category);
  if (params.price_tier) query.set('price_tier', params.price_tier);
  if (params.store_type && params.store_type !== 'ALL') query.set('store_type', params.store_type === 'LOCAL' ? '로컬' : '체인');
  if (params.event_only) query.set('event_only', '1');
  if (params.partner_only) query.set('partner_only', '1');
  if (params.partner_exact) query.set('partner_exact', '1');
  if (params.open_now) query.set('open_now', '1');
  if (params.q) query.set('q', params.q);
  if (params.weather) query.set('weather', params.weather);
  if (params.time_slot) query.set('time_slot', params.time_slot);
  if (params.user_id) query.set('user_id', params.user_id);
  if (params.min_lat !== undefined && params.max_lat !== undefined && params.min_lng !== undefined && params.max_lng !== undefined) {
    query.set('min_lat', String(params.min_lat));
    query.set('max_lat', String(params.max_lat));
    query.set('min_lng', String(params.min_lng));
    query.set('max_lng', String(params.max_lng));
  }

  const res = await fetch(`/api/venues?${query.toString()}`, { credentials: 'same-origin' });
  return readApiResponse(res, '매장 목록을 불러오지 못했습니다');
}

export async function fetchVenueDetail(venueId: string, weather: string = 'CLEAR', signal?: AbortSignal): Promise<{ success: boolean; venue: VenueDetail }> {
  const path = '/api/venues/' + encodeURIComponent(venueId);
  const res = await fetch(path + '?weather=' + encodeURIComponent(weather), { signal, credentials: 'same-origin' });
  return readApiResponse(res, '매장 상세 정보를 불러오지 못했습니다');
}

export async function fetchActiveEvents(gu: string = '강남구'): Promise<{ success: boolean; gu: string; events: VenueEventSummary[] }> {
  const res = await fetch(`/api/venues/events/active?gu=${encodeURIComponent(gu)}`, { credentials: 'same-origin' });
  return readApiResponse(res, '진행 중인 이벤트를 불러오지 못했습니다');
}

export async function postEventIntent(eventId: number, userId: string): Promise<any> {
  return venuePostJson(`/api/venues/events/${eventId}/intent`, { user_id: userId });
}

export async function postVerifyQr(eventId: number, userId: string, qrAuthCode?: string): Promise<any> {
  return venuePostJson(`/api/venues/events/${eventId}/verify-qr`, { user_id: userId, qr_auth_code: qrAuthCode });
}

export async function toggleBookmark(venueId: string, userId: string): Promise<{ success: boolean; is_bookmarked: boolean; message: string }> {
  return venuePostJson(`/api/venues/${venueId}/bookmark`, { user_id: userId });
}

export async function postReview(venueId: string, reviewData: {
  content: string;
  rating: number;
  weather_condition?: string;
  weather_temp?: number;
  user_id?: string;
}): Promise<any> {
  return venuePostJson(`/api/venues/${venueId}/reviews`, reviewData);
}

export async function postClosureReport(venueId: string, reason: string, userId: string): Promise<any> {
  return venuePostJson(`/api/venues/${venueId}/closure-report`, { venue_id: venueId, reason, user_id: userId });
}

export async function fetchMySummary(userId: string): Promise<{ success: boolean; summary: MySummaryData }> {
  const res = await fetch(`/api/venues/my/summary?user_id=${encodeURIComponent(userId)}`, { credentials: 'same-origin' });
  return readApiResponse(res, '마이페이지 정보를 불러오지 못했습니다');
}

export async function postAiConcierge(payload: {
  query: string;
  gu: string;
  weather?: string;
  time_slot?: string;
  budget_tier?: BudgetTier | '';
  category?: VenueCategory;
}): Promise<{ success: boolean; briefing: ConciergeBriefing; candidate_venues: VenueItem[] }> {
  return venuePostJson('/api/venues/ai-concierge', payload);
}
