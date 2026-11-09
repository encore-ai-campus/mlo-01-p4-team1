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

export interface VenueSearchParams {
  sigungu?: string;
  dong?: string;
  category?: VenueCategory;
  price_tier?: BudgetTier | '';
  store_type?: StoreType;
  event_only?: boolean;
  partner_only?: boolean;
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

  const res = await fetch(`/api/venues?${query.toString()}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch venues: ${res.statusText}`);
  }
  return res.json();
}

export async function fetchVenueDetail(venueId: string, weather: string = 'CLEAR'): Promise<{ success: boolean; venue: VenueDetail }> {
  const res = await fetch(`/api/venues/${venueId}?weather=${encodeURIComponent(weather)}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch venue detail: ${res.statusText}`);
  }
  return res.json();
}

export async function fetchActiveEvents(gu: string = '강남구'): Promise<{ success: boolean; gu: string; events: VenueEventSummary[] }> {
  const res = await fetch(`/api/venues/events/active?gu=${encodeURIComponent(gu)}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch active events: ${res.statusText}`);
  }
  return res.json();
}

export async function postEventIntent(eventId: number, userId: string): Promise<any> {
  const res = await fetch(`/api/venues/events/${eventId}/intent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId }),
  });
  return res.json();
}

export async function postVerifyQr(eventId: number, userId: string, qrAuthCode?: string): Promise<any> {
  const res = await fetch(`/api/venues/events/${eventId}/verify-qr`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, qr_auth_code: qrAuthCode }),
  });
  return res.json();
}

export async function toggleBookmark(venueId: string, userId: string): Promise<{ success: boolean; is_bookmarked: boolean; message: string }> {
  const res = await fetch(`/api/venues/${venueId}/bookmark`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId }),
  });
  return res.json();
}

export async function postReview(venueId: string, reviewData: {
  content: string;
  rating: number;
  weather_condition?: string;
  weather_temp?: number;
  user_id?: string;
}): Promise<any> {
  const res = await fetch(`/api/venues/${venueId}/reviews`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(reviewData),
  });
  return res.json();
}

export async function postClosureReport(venueId: string, reason: string, userId: string): Promise<any> {
  const res = await fetch(`/api/venues/${venueId}/closure-report`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ venue_id: venueId, reason, user_id: userId }),
  });
  return res.json();
}

export async function fetchMySummary(userId: string): Promise<{ success: boolean; summary: MySummaryData }> {
  const res = await fetch(`/api/venues/my/summary?user_id=${encodeURIComponent(userId)}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch my summary: ${res.statusText}`);
  }
  return res.json();
}

export async function postAiConcierge(payload: {
  query: string;
  gu: string;
  weather?: string;
  time_slot?: string;
  budget_tier?: BudgetTier | '';
  category?: VenueCategory;
}): Promise<{ success: boolean; briefing: ConciergeBriefing; candidate_venues: VenueItem[] }> {
  const res = await fetch('/api/venues/ai-concierge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`Failed to query AI concierge: ${res.statusText}`);
  }
  return res.json();
}
