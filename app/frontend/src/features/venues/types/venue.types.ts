export type BudgetTier = 'UNDER_10K' | '10K_20K' | '20K_30K' | '30K_50K' | 'OVER_50K';

export type VenueCategory = 'ALL' | 'CAFE' | 'PUB';

export type StoreType = 'ALL' | 'LOCAL' | 'CHAIN';

export interface MenuItem {
  menu_id: string;
  venue_id: string;
  menu_name: string;
  price: number;
  price_confirmed?: boolean;
  menu_source?: string;
  price_tier: number;
  is_signature: boolean;
  weather_match: string;
  category: string;
}

export interface VenueEventSummary {
  event_id: number;
  venue_id: string;
  title: string;
  description?: string;
  badge_text: string;
  discount_info: string;
  total_quantity?: number;
  remaining_quantity: number;
  intended_count: number;
  start_at?: string;
  end_at?: string;
  is_ended?: boolean;
  is_ended_grace_period?: boolean;
  status_label?: string;
}

export interface MusicBanner {
  has_music_banner: boolean;
  banner_type: 'APPROVED_PLAYLIST' | 'WEATHER_THEME';
  playlist_id: string;
  playlist_title: string;
  curator_note: string;
  music_mood: string;
  badge_text: string;
}

export interface VenueResearch {
  description?: string;
  representative_menus?: string;
  seating?: string;
  sources?: string[];
  checked_on?: string;
  limitations?: string;
}

export interface VenueItem {
  venue_id: string;
  venue_name: string;
  gu_name: string;
  dong_name: string;
  road_address: string;
  category: 'CAFE' | 'PUB';
  sub_category?: string;
  price_tier: number | null;
  avg_price: number | null;
  rating: number | null;
  review_count: number | null;
  is_partner: boolean;
  mood_tags: string[];
  music_mood?: string;
  recommendation_style?: string;
  latitude: number | null;
  longitude: number | null;
  is_open_now: boolean | null;
  status_code: 'OPEN' | 'CLOSURE_REPORTED' | 'CLOSED_HIDDEN';
  status_label: string;
  has_closure_warning: boolean;
  music_banner?: MusicBanner | null;
  active_event?: VenueEventSummary | null;
  active_events?: VenueEventSummary[];
  thumbnail_url?: string;
  interior_photos?: string[];
  matched_menus_preview: MenuItem[];
  match_score: number;
  dl_similarity?: number;
  research?: VenueResearch | null;
}

export interface ConciergeBriefing {
  concierge_title: string;
  summary_briefing: string;
  recommended_venue_id: string | null;
  recommended_venue_name: string | null;
  recommended_menu_combination: string[];
  reasoning: string;
  model_used: string;
}

export interface MenuTab {
  tab_name: string;
  items: MenuItem[];
}

export interface VenueReviewItem {
  interaction_id: number;
  user_id: string;
  content: string;
  rating: number;
  weather_condition: string;
  weather_temp: number;
  created_at: string;
}

export interface VenueBudgetExample {
  amount_krw: number;
  items: Array<{ name: string; price_krw: number }>;
  source_url: string;
  checked_on: string;
  limits: string;
}

interface RegisteredVenueDetail extends VenueItem {
  curated_listing?: never;
  menu_tabs: MenuTab[];
  all_menus_count: number;
  applied_playlist: { id: string; name: string } | null;
  recent_reviews?: VenueReviewItem[];
}

interface CuratedVenueDetail extends Pick<VenueItem, 'venue_id' | 'venue_name' | 'category' | 'sub_category' | 'road_address' | 'dong_name'> {
  research?: VenueResearch | null;
  curated_listing: {
    preview_only?: boolean;
    moods: string[];
    representative_menus: string[];
    seat_summary: string;
    checked_on: string;
    registry_url: string;
    budget_example: VenueBudgetExample | null;
  };
}

export type VenueDetail = RegisteredVenueDetail | CuratedVenueDetail;

export interface MySummaryData {
  bookmarked_venues: Array<{
    venue_id: string;
    venue_name: string;
    gu_name: string;
    category: string;
    bookmarked_at: string;
  }>;
  reviews: Array<{
    review_id: number;
    venue_id: string;
    venue_name: string;
    content: string;
    rating: number;
    weather_condition: string;
    weather_temp: number;
    created_at: string;
  }>;
  participations: Array<{
    participation_id: number;
    event_id: number;
    title: string;
    status: string;
    status_label: string;
    qr_auth_code: string;
    verified_at: string | null;
    created_at: string;
  }>;
  notifications: Array<{
    notification_id: number;
    title: string;
    message: string;
    link_url: string;
    is_read: boolean;
    created_at: string;
  }>;
  unread_notifications_count: number;
}
