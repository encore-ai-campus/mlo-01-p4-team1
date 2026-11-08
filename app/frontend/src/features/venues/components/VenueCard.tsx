import React, { useState } from 'react';
import { VenueItem, MenuItem } from '../types/venue.types';
import { VenueMenuPreviewStrip } from './VenueMenuPreviewStrip';
import { toggleBookmark } from '../api/venues.api';

interface VenueCardProps {
  venue: VenueItem;
  onSelectVenue?: (venueId: string) => void;
  onSelectMenu?: (menu: MenuItem) => void;
  userId?: string;
}

export const VenueCard: React.FC<VenueCardProps> = ({ venue, onSelectVenue, onSelectMenu, userId = 'guest_anonymous' }) => {
  const [isBookmarked, setIsBookmarked] = useState<boolean>(false);
  const [bookmarkLoading, setBookmarkLoading] = useState<boolean>(false);

  const handleBookmark = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (bookmarkLoading) return;
    setBookmarkLoading(true);
    try {
      const res = await toggleBookmark(venue.venue_id, userId);
      if (res.success) {
        setIsBookmarked(res.is_bookmarked);
      }
    } catch {
      // ignore
    } finally {
      setBookmarkLoading(false);
    }
  };

  const activeEvent = venue.active_event;

  return (
    <div
      onClick={() => onSelectVenue && onSelectVenue(venue.venue_id)}
      style={{
        backgroundColor: '#ffffff',
        borderRadius: '16px',
        padding: '18px 20px',
        marginBottom: '16px',
        border: venue.has_closure_warning ? '1px solid #fca5a5' : '1px solid #e2e8f0',
        boxShadow: '0 2px 8px rgba(0, 0, 0, 0.04)',
        cursor: 'pointer',
        transition: 'transform 0.15s, box-shadow 0.15s',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.transform = 'translateY(-2px)';
        e.currentTarget.style.boxShadow = '0 6px 16px rgba(0, 0, 0, 0.08)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.transform = 'translateY(0)';
        e.currentTarget.style.boxShadow = '0 2px 8px rgba(0, 0, 0, 0.04)';
      }}
    >
      {/* 1. 상단 뱃지 영역: Two-Tower 매칭률 + 영업 상태 + 파트너 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
          {/* Two-Tower DL 매칭률 뱃지 */}
          <span style={{
            background: 'linear-gradient(135deg, #3b82f6, #6366f1)',
            color: '#ffffff',
            padding: '2px 8px',
            borderRadius: '6px',
            fontSize: '11px',
            fontWeight: 800,
          }}>
            🎯 {venue.match_score}% 취향 매칭
          </span>

          {venue.is_partner && (
            <span style={{
              backgroundColor: '#f3e8ff',
              color: '#7c3aed',
              padding: '2px 7px',
              borderRadius: '6px',
              fontSize: '11px',
              fontWeight: 700,
            }}>
              🎵 음악 파트너
            </span>
          )}

          <span style={{
            backgroundColor: venue.is_open_now ? '#dcfce7' : '#f1f5f9',
            color: venue.is_open_now ? '#15803d' : '#64748b',
            padding: '2px 7px',
            borderRadius: '6px',
            fontSize: '11px',
            fontWeight: 600,
          }}>
            {venue.is_open_now ? '🟢 지금 영업 중' : '⚪ 마감'}
          </span>

          {venue.has_closure_warning && (
            <span style={{
              backgroundColor: '#fee2e2',
              color: '#b91c1c',
              padding: '2px 7px',
              borderRadius: '6px',
              fontSize: '11px',
              fontWeight: 700,
            }}>
              ⚠️ 폐업 제보 접수
            </span>
          )}
        </div>

        {/* 찜하기 하트 버튼 */}
        <button
          type="button"
          onClick={handleBookmark}
          disabled={bookmarkLoading}
          style={{
            background: 'none',
            border: 'none',
            cursor: 'pointer',
            fontSize: '18px',
            color: isBookmarked ? '#ef4444' : '#cbd5e1',
            padding: '4px',
            lineHeight: 1,
          }}
          title="찜하기"
        >
          {isBookmarked ? '❤️' : '🤍'}
        </button>
      </div>

      {/* 2. 매장 기본 정보: 매장명, 업종, 동네 */}
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: '4px' }}>
        <h3 style={{ margin: 0, fontSize: '18px', fontWeight: 800, color: '#0f172a' }}>
          {venue.venue_name}
        </h3>
        <span style={{ fontSize: '12px', color: '#64748b', fontWeight: 600 }}>
          {venue.dong_name}
        </span>
      </div>

      <div style={{ fontSize: '13px', color: '#64748b', marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '8px' }}>
        <span>{venue.category === 'CAFE' ? '☕ 카페' : '🍺 주점'}</span>
        <span>•</span>
        <span>{venue.sub_category || '로컬'}</span>
        <span>•</span>
        <span style={{ color: '#eab308', fontWeight: 700 }}>★ {venue.rating.toFixed(1)}</span>
        <span>({venue.review_count})</span>
      </div>

      {/* 3. 점주 실시간 타임 이벤트 배지 (종료 후 5분 유예 처리 포함) */}
      {activeEvent && (
        <div style={{
          backgroundColor: activeEvent.is_ended_grace_period ? '#f1f5f9' : '#fff1f2',
          border: activeEvent.is_ended_grace_period ? '1px solid #cbd5e1' : '1px solid #fecdd3',
          borderRadius: '8px',
          padding: '8px 12px',
          marginBottom: '10px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <span style={{ fontSize: '14px' }}>{activeEvent.is_ended_grace_period ? '⏱️' : '🔥'}</span>
            <span style={{
              fontSize: '12px',
              fontWeight: 700,
              color: activeEvent.is_ended_grace_period ? '#64748b' : '#e11d48',
            }}>
              {activeEvent.badge_text}: {activeEvent.title}
            </span>
          </div>
          <span style={{
            fontSize: '11px',
            color: activeEvent.is_ended_grace_period ? '#94a3b8' : '#be123c',
            fontWeight: 600,
          }}>
            {activeEvent.is_ended_grace_period
              ? '방금 마감됨'
              : `남은 수량: ${activeEvent.remaining_quantity}개 (방문 예정: ${activeEvent.intended_count}명)`}
          </span>
        </div>
      )}

      {/* 4. 분위기 태그 */}
      <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '4px' }}>
        {venue.mood_tags.map((tag, idx) => (
          <span
            key={idx}
            style={{
              backgroundColor: '#f1f5f9',
              color: '#475569',
              fontSize: '11px',
              padding: '2px 8px',
              borderRadius: '6px',
            }}
          >
            #{tag}
          </span>
        ))}
      </div>

      {/* 5. 조건 부합 메뉴 가로 프리뷰 스트립 (필수 탑재) */}
      <VenueMenuPreviewStrip
        menus={venue.matched_menus_preview}
        onSelectMenu={onSelectMenu}
      />
    </div>
  );
};
