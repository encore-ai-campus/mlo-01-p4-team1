import type { VenueBudgetExample } from '../types/venue.types';

export type OutdoorPreference = 'ANY' | 'PREFER_OUTDOOR' | 'REQUIRE_OUTDOOR';

export interface VenueTag {
  id: string;
  kind: 'MOOD' | 'CUISINE' | 'MENU_TYPE';
  label: string;
}

export interface VisitOptions {
  regions: string[];
  tags: VenueTag[];
}

export interface VisitConditions {
  region: string;
  visit_at?: string;
  preferred_moods?: string[];
  orders?: Array<{ cuisine: string } | { types: string[] }>;
  outdoor?: OutdoorPreference;
  query?: string;
  budget?: { min_krw: number; max_krw: number };
  limit?: number;
}

export interface VisitVenue {
  venue_id: string;
  name: string;
  address: string;
  sub_category: string | null;
  avg_budget_krw: number | null;
  budget_example?: VenueBudgetExample;
  representative_menus?: string[];
  distance_m?: number;
  weather_match?: { label: string; source: 'DEMO_CSV' | 'SEAT_EVIDENCE' };
  matched_moods: Array<{ id: string; label: string }>;
  matched_orders: Array<Array<{ menu_id: string; name: string }>>;
  seat: { label: string };
  forecast: {
    provider: string;
    state: 'DRY' | 'RAIN' | 'NO_FORECAST';
    condition: string | null;
    start_at: string | null;
    end_at: string | null;
  };
}

export interface VisitRecommendation {
  request: {
    visit_at: string;
    region?: string;
    venue_category?: 'CAFE' | 'PUB' | null;
    location?: {
      label: string;
      kind: 'CITY' | 'DISTRICT' | 'DONG' | 'STATION' | 'EXIT' | 'LANDMARK';
      radius_m?: number;
      source_label?: string;
      source_url?: string;
      unmatched_exit?: number;
    };
  };
  empty_reason?: string;
  location_candidates?: Array<{ label: string; query: string }>;
  counts: { ranked: number };
  ranked: VisitVenue[];
  pending: VisitVenue[];
}

const endpoint = '/api/venues/ai-concierge';

export async function fetchVisitOptions(signal: AbortSignal): Promise<VisitOptions | null> {
  const response = await fetch(endpoint + '/options', { signal, cache: 'no-store' });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error('추천 조건을 불러오지 못했습니다.');
  const data: { mcp: VisitOptions | null } = await response.json();
  return data.mcp;
}

export async function postVisitRecommendation(conditions: VisitConditions): Promise<VisitRecommendation> {
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mcp: conditions }),
  });
  if (response.status === 422) {
    throw new Error(conditions.query
      ? '지원하는 지역과 검색 조건을 확인해 주세요. 예를 들어 ‘잔잔한 술집’ 또는 ‘1만원 이하 카페’로 검색할 수 있습니다.'
      : '선택한 동, 방문 일시와 태그를 확인해 주세요.');
  }
  if (!response.ok) throw new Error('추천을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.');
  const data: { mcp: VisitRecommendation } = await response.json();
  return data.mcp;
}
