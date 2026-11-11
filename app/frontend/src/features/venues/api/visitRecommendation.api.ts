import { ApiError, csrfHeaders, requestJson } from '../../../shared/api';
import type { VenueBudgetExample } from '../types/venue.types';

export interface VenueTag {
  id: string;
  kind: 'MOOD' | 'CUISINE' | 'MENU_TYPE';
  label: string;
}

export interface VisitCatalogMetadata {
  preview_only?: boolean;
  source_basis?: string | null;
  budget_basis?: string | null;
}

export interface VisitOptions {
  regions: string[];
  tags: VenueTag[];
  catalog_metadata?: VisitCatalogMetadata;
}

export interface VenueLlmAvailability {
  configured: boolean;
  provider: string | null;
  mode?: string | null;
}

export interface VisitOptionsResponse {
  mcp: VisitOptions | null;
  llm: VenueLlmAvailability;
}

export type VenueRecommendationConnection =
  | { status: 'checking' | 'error' }
  | { status: 'ready'; options: VisitOptionsResponse };

export interface VisitConditions {
  region: string;
  visit_at?: string;
  preferred_moods?: string[];
  orders?: Array<{ cuisine: string } | { types: string[] }>;
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
  weather_match?: { label: string; source: 'TAG_MATCH' };
  matched_moods: Array<{ id: string; label: string }>;
  matched_orders: Array<Array<{ menu_id: string; name: string }>>;
  paired_menu?: { menu_id: string; name: string };
  forecast: {
    provider: string;
    state: 'DRY' | 'RAIN' | 'NO_FORECAST';
    condition: string | null;
    reason?: string | null;
    issued_at?: string | null;
    start_at: string | null;
    end_at: string | null;
  };
}

interface VenueLlmBudget {
  min_krw?: number | null;
  max_krw?: number | null;
}

interface UnverifiedVenueCondition {
  text: string;
}

export interface VenueLlmConditions {
  region?: string | null;
  visit_at?: string | null;
  venue_category?: 'CAFE' | 'PUB' | null;
  budget?: VenueLlmBudget | null;
  budget_mode?: 'PREFER' | 'REQUIRE';
  required_moods?: string[] | null;
  preferred_moods?: string[] | null;
  orders?: Array<{ cuisine?: string | null; types?: string[] | null }> | null;
  unverified_conditions?: UnverifiedVenueCondition[] | null;
}

export interface VisitRecommendation {
  status: 'OK' | 'NO_MATCH' | 'NEEDS_CHECK';
  request: {
    visit_at: string;
    region?: string;
    venue_category?: 'CAFE' | 'PUB' | null;
    budget?: VenueLlmBudget | null;
    budget_mode?: 'PREFER' | 'REQUIRE';
    required_moods?: string[];
    preferred_moods?: string[];
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
  catalog_source?: string;
  catalog_metadata?: VisitCatalogMetadata;
  unverified_conditions?: UnverifiedVenueCondition[];
  location_candidates?: Array<{ label: string; query: string }>;
  counts: { ranked: number };
  ranked: VisitVenue[];
}

export interface VenueLlmAnswer {
  status: 'OK' | 'NEEDS_CONFIRMATION' | 'ERROR';
  provider?: string;
  mode?: string;
  model?: string | null;
  interpreted_conditions: VenueLlmConditions;
  message: string;
}

export interface VenueChatResponse {
  success: boolean;
  llm: VenueLlmAnswer;
  mcp: VisitRecommendation | null;
}

const endpoint = '/api/venues/ai-concierge';
const unconfiguredLlm = { configured: false, provider: null };

export async function fetchVisitOptions(signal: AbortSignal): Promise<VisitOptionsResponse> {
  try {
    const data = await requestJson<{ mcp: VisitOptions | null; llm?: VenueLlmAvailability }>(
      endpoint + '/options', { signal, cache: 'no-store' }, 20_000,
    );
    if (data.mcp !== null && (!Array.isArray(data.mcp?.regions) || !Array.isArray(data.mcp.tags))) {
      throw new ApiError('INVALID_RESPONSE', '추천 연결 응답을 확인할 수 없습니다.');
    }
    return { mcp: data.mcp, llm: data.llm ?? unconfiguredLlm };
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return { mcp: null, llm: unconfiguredLlm };
    throw error;
  }
}

export async function postVenueChat(message: string, region: string | undefined, signal: AbortSignal): Promise<VenueChatResponse> {
  const data = await requestJson<VenueChatResponse>(endpoint + '/chat', {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    signal,
    headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
    body: JSON.stringify({ message, region }),
  }, 90_000);
  if (data?.success !== true
    || (data.llm?.status !== 'OK' && data.llm?.status !== 'NEEDS_CONFIRMATION')
    || typeof data.llm.message !== 'string'
    || (data.llm.status === 'OK' && !data.mcp)
    || (data.mcp && (
      !['OK', 'NO_MATCH', 'NEEDS_CHECK'].includes(data.mcp.status)
      || !data.mcp.request || !data.mcp.counts || !Array.isArray(data.mcp.ranked)
      || (data.mcp.ranked.length > 0) !== (data.mcp.status === 'OK')
    ))) {
    throw new ApiError('INVALID_RESPONSE', 'AI 답변을 확인할 수 없습니다.');
  }
  return data;
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
      : '선택한 지역, 방문 일시와 태그를 확인해 주세요.');
  }
  if (!response.ok) throw new Error('추천을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.');
  const data: { mcp: VisitRecommendation } = await response.json();
  return data.mcp;
}
