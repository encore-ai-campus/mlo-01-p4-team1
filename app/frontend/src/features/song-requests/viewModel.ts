import type {Session, SongInput, SongSubmission} from './client';
import {canonicalYoutubeVideoUrl} from '../recommendation/youtubeReview';

export function songEligibility(session: Session, venueId?: string): string | null {
  if (!session.authenticated) return '로그인 후 곡을 신청하실 수 있습니다.';
  const policy = session.song_policy;
  if (policy.ready !== true || typeof policy.require_approved_venue !== 'boolean' || typeof policy.allow_general_users !== 'boolean' || !['account', 'venue'].includes(policy.quota_scope || '') || !Number.isInteger(policy.per_submission_limit) || policy.per_submission_limit < 1 || policy.per_submission_limit > 20) return '곡 신청 규약을 준비 중입니다. 신청 내역은 계속 확인하실 수 있습니다.';
  if (!policy.allow_general_users && !session.managed_venues.length) return '승인된 업체가 있는 계정만 신청하실 수 있습니다.';
  if (policy.require_approved_venue && !venueId) return '승인된 내 업체를 선택해 주세요.';
  if (venueId && !session.managed_venues.some(venue => venue.venue_id === venueId)) return '관리 권한이 확인된 업체를 선택해 주세요.';
  return null;
}

export function validateSongs(items: SongInput[], limit = 20): string | null {
  if (!items.length || items.length > Math.min(20, limit)) return `한 번에 1~${Math.min(20, limit)}곡을 입력해 주세요.`;
  if (items.some(item => !item.track_name.trim() || !item.artist_name.trim())) return '모든 곡의 곡명과 아티스트를 입력해 주세요.';
  const seen = new Set<string>();
  for (const item of items) {
    const key = `${item.track_name.trim().toLocaleLowerCase()}\n${item.artist_name.trim().toLocaleLowerCase()}`;
    if (seen.has(key)) return '같은 곡명과 아티스트가 반복됩니다. 버전이 다르면 곡명에 버전을 표시해 주세요.';
    seen.add(key);
    if (item.youtube_url?.trim() && !canonicalYoutubeVideoUrl(item.youtube_url.trim())) return '재생할 YouTube 영상 주소를 입력해 주세요.';
  }
  return null;
}

export function cleanSongs(items: SongInput[], venueId?: string): SongSubmission {
  return {...(venueId ? {venue_id: venueId} : {}), items: items.map(item => ({track_name: item.track_name.trim(), artist_name: item.artist_name.trim(), ...(item.youtube_url?.trim() ? {youtube_url: item.youtube_url.trim()} : {})}))};
}

const labels: Record<string, string> = {
  '접수됨': '접수됨', '보류': '보류', '반려': '반려', '등록 완료': '등록 완료', '기존 등록 곡 안내': '기존 등록 곡 안내', '처리 중': '처리 중',
  '태깅 결과 수신 · 검수 대기': '태깅 결과 수신 · 검수 대기',
  PENDING: '검수 대기', NEEDS_INFO: '보완 요청', APPROVED: '승인', REJECTED: '반려', HOLD: '보류', LINKED: '기존 곡 연결',
  REVIEW_PENDING: '검수 대기', PROCESSING: '처리 중', REGISTERED: '등록 완료', AVAILABLE: '이용 가능', FAILED: '처리 확인 중',
  APPROVED_WAITING_PROCESSING: '승인 · 처리 대기', PUBLISHED: '등록 완료', RECEIVED: '처리 중', VALIDATION_PENDING: '최종 확인 중',
  INPUT_PENDING: '입력 확인 대기', READY: '처리 대기', RUNNING: '처리 중',
  RECEIVED_PENDING_REVIEW: '결과 수신 · 검수 대기', CANCELLED: '취소', SUPERSEDED: '링크 변경 · 이전 작업',
};
export const statusLabel = (value?: string | null) => value ? labels[value] || '처리 상태 확인 중' : '처리 상태 확인 중';
export const dateLabel = (value?: string | null) => value && Number.isFinite(new Date(value).getTime()) ? new Intl.DateTimeFormat('ko-KR', {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(value)) : '시각 확인 중';

const tagLabels: ReadonlyMap<string, string> = new Map([
  ['POP', '팝'], ['K-POP', 'K-POP'], ['J-POP', 'J-POP'], ['ROCK_PUNK_METAL', '록·펑크·메탈'],
  ['ELECTRONIC_DANCE', '일렉트로닉·댄스'], ['HIPHOP', '힙합'], ['RNB_SOUL_FUNK', 'R&B·소울·펑크'],
  ['JAZZ', '재즈'], ['FOLK_COUNTRY_BLUES', '포크·컨트리·블루스'], ['OPERA', '오페라'],
  ['CLASSICAL', '클래식'], ['NEWAGE', '뉴에이지'], ['REGGAE_SKA', '레게·스카'], ['LATIN', '라틴'],
  ['CALM', '잔잔한'], ['EMOTIONAL', '감성적인'], ['PEACEFUL', '평화로운'], ['DREAMY', '몽환적인'],
  ['ENERGETIC', '활기찬'], ['BRIGHT', '밝은'], ['PARTY', '파티 분위기'], ['GROOVY', '그루브 있는'],
  ['INTENSE', '강렬한'],
]);
export const tagLabel = (code: string) => tagLabels.get(code) ?? code;
