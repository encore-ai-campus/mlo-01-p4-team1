export type BudgetTier = 'UNDER_10K' | '10K_20K' | '20K_30K' | '30K_50K' | 'OVER_50K';

export type VenueCategory = 'ALL' | 'CAFE' | 'PUB';

export type StoreType = 'ALL' | 'LOCAL' | 'CHAIN';

export interface MenuItem {
  menu_id: string;
  venue_id: string;
  menu_name: string;
  price: number;
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

export interface VenueItem {
  venue_id: string;
  venue_name: string;
  gu_name: string;
  dong_name: string;
  road_address: string;
  category: 'CAFE' | 'PUB';
  sub_category?: string;
  price_tier: number;
  avg_price: number;
  rating: number;
  review_count: number;
  is_partner: boolean;
  mood_tags: string[];
  music_mood?: string;
  latitude: number;
  longitude: number;
  is_open_now: boolean;
  status_code: 'OPEN' | 'CLOSURE_REPORTED' | 'CLOSED_HIDDEN';
  status_label: string;
  has_closure_warning: boolean;
  music_banner?: MusicBanner | null;
  active_event?: VenueEventSummary | null;
  active_events?: VenueEventSummary[];
  matched_menus_preview: MenuItem[];
  match_score: number;
  dl_similarity?: number;
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

export interface VenueDetail extends VenueItem {
  menu_tabs: MenuTab[];
  all_menus_count: number;
  applied_playlist: { id: string; name: string } | null;
  recent_reviews?: VenueReviewItem[];
}

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
