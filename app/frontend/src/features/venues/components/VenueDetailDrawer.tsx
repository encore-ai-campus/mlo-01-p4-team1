import React, { useState } from 'react';
import { VenueDetail, VenueEventSummary } from '../types/venue.types';
import { LiveMusicBanner } from './LiveMusicBanner';
import { MenuCategoryTabs } from './MenuCategoryTabs';
import { postReview, postClosureReport } from '../api/venues.api';

interface VenueDetailDrawerProps {
  venue: VenueDetail | null;
  isOpen: boolean;
  onClose: () => void;
  onOpenEventModal?: (event: VenueEventSummary) => void;
  userId?: string;
  currentWeather?: string;
  readOnly?: boolean;
}

export const VenueDetailDrawer: React.FC<VenueDetailDrawerProps> = ({
  venue,
  isOpen,
  onClose,
  onOpenEventModal,
  userId = 'guest_anonymous',
  currentWeather = 'CLEAR',
  readOnly = false,
}) => {
  const [reviewContent, setReviewContent] = useState<string>('');
  const [reviewRating, setReviewRating] = useState<number>(5);
  const [reviewSubmitting, setReviewSubmitting] = useState<boolean>(false);
  const [closureReporting, setClosureReporting] = useState<boolean>(false);
  const [closureReason, setClosureReason] = useState<string>('');
  const [showClosureModal, setShowClosureModal] = useState<boolean>(false);

  if (!isOpen || !venue) {
    return null;
  }

  const handleReviewSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!reviewContent.trim()) return;
    setReviewSubmitting(true);
    try {
      const res = await postReview(venue.venue_id, {
        content: reviewContent,
        rating: reviewRating,
        weather_condition: currentWeather,
        weather_temp: 21.5,
        user_id: userId,
      });
      if (res.success) {
        alert('날씨 스냅샷 한줄평이 성공적으로 등록되었습니다!');
        setReviewContent('');
      }
    } catch {
      alert('리뷰 등록에 실패했습니다.');
    } finally {
      setReviewSubmitting(false);
    }
  };

  const handleClosureReport = async () => {
    if (!closureReason.trim()) {
      alert('폐업 제보 사유를 입력해주세요.');
      return;
    }
    setClosureReporting(true);
    try {
      const res = await postClosureReport(venue.venue_id, closureReason, userId);
      if (res.success) {
        alert('폐업 제보가 접수되었습니다. 관리자 확인 후 처리됩니다.');
        setShowClosureModal(false);
        setClosureReason('');
      }
    } catch {
      alert('제보 처리 중 오류가 발생했습니다.');
    } finally {
      setClosureReporting(false);
    }
  };

  const activeEvent = venue.curated_listing ? null : venue.active_events?.[0];

  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      backgroundColor: 'rgba(0, 0, 0, 0.5)',
      zIndex: 1000,
      display: 'flex',
      justifyContent: 'flex-end',
      backdropFilter: 'blur(3px)',
    }}>
      <div role="dialog" aria-modal="true" aria-label={venue.venue_name + ' 매장 상세'} style={{
        width: '540px',
        maxWidth: '100%',
        height: '100%',
        backgroundColor: '#ffffff',
        overflowY: 'auto',
        boxShadow: '-4px 0 24px rgba(0, 0, 0, 0.15)',
        display: 'flex',
        flexDirection: 'column',
      }}>
        {/* 1. 상단 이미지 및 닫기 버튼 (고시인성 화이트&인디고 배너 + 매장 분위기 사진) */}
        <div style={{
          minHeight: '154px',
          background: venue.category === 'CAFE'
            ? 'linear-gradient(135deg, #1d4ed8 0%, #0f172a 100%)'
            : 'linear-gradient(135deg, #6d28d9 0%, #1e1b4b 100%)',
          position: 'relative',
          padding: '20px 22px',
          color: '#ffffff',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'space-between',
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span style={{
                backgroundColor: '#ffffff',
                color: venue.category === 'CAFE' ? '#1d4ed8' : '#6d28d9',
                padding: '4px 10px',
                borderRadius: '9999px',
                fontSize: '12px',
                fontWeight: 800,
                boxShadow: '0 2px 6px rgba(0,0,0,0.18)',
              }}>
                {venue.category === 'CAFE' ? '☕ 카페' : '🍺 주점'}
              </span>
              <span style={{
                fontSize: '13px',
                color: '#f8fafc',
                fontWeight: 700,
                backgroundColor: 'rgba(255,255,255,0.18)',
                padding: '3px 9px',
                borderRadius: '6px',
              }}>
                {venue.sub_category || '추천 매장'}
              </span>
            </div>

            <button
              type="button"
              onClick={onClose}
              aria-label="상세 닫기"
              style={{
                background: 'rgba(255,255,255,0.2)',
                border: '1px solid rgba(255,255,255,0.35)',
                color: '#ffffff',
                fontSize: '16px',
                fontWeight: 800,
                width: '34px',
                height: '34px',
                borderRadius: '50%',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                padding: 0,
              }}
            >
              ✕
            </button>
          </div>

          <div style={{ marginTop: '14px' }}>
            {/* style.css의 전역 h2 { color: var(--navy) }를 덮어쓰기 위해 명시적 #ffffff 지정 */}
            <h2 style={{
              margin: '0 0 6px 0',
              fontSize: '24px',
              fontWeight: 900,
              color: '#ffffff',
              letterSpacing: '-0.5px',
              textShadow: '0 2px 8px rgba(0, 0, 0, 0.35)',
            }}>
              {venue.venue_name}
            </h2>
            <div style={{ fontSize: '13px', color: '#e2e8f0', fontWeight: 500 }}>
              📍 {venue.road_address} ({venue.dong_name})
            </div>
          </div>
        </div>

        {/* 2. 본문 컨텐츠 */}
        <div style={{ padding: '20px', flex: 1 }}>
          {venue.curated_listing ? <section aria-label="공개 자료로 확인한 매장 정보">
            <h3>확인한 매장 정보</h3>
            {!!venue.curated_listing.moods.length && <p>{venue.curated_listing.moods.join(' · ')}</p>}
            <h4>대표 메뉴</h4>
            <p>{venue.curated_listing.representative_menus.join(' · ')}</p>
            {venue.curated_listing.budget_example ? <div>
              <h4>1인 주문 예시 {venue.curated_listing.budget_example.amount_krw.toLocaleString('ko-KR')}원</h4>
              <ul>{venue.curated_listing.budget_example.items.map(item =>
                <li key={item.name}>{item.name} · {item.price_krw.toLocaleString('ko-KR')}원</li>
              )}</ul>
              <p>{venue.curated_listing.budget_example.limits}</p>
              <a href={venue.curated_listing.budget_example.source_url} target="_blank" rel="noreferrer">공개 메뉴 확인</a>
            </div> : <p>1인 주문 가격 미확인</p>}
            <p>{venue.curated_listing.checked_on} 확인 · <a href={venue.curated_listing.registry_url}
              target="_blank" rel="noreferrer">상가정보 출처</a></p>
          </section> : <>
            {/* 2-0. 매장 분위기 & 가격/영업시간 핵심 요약 바 */}
            <div style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: '16px',
              padding: '12px 14px',
              backgroundColor: '#f8fafc',
              borderRadius: '10px',
              border: '1px solid #e2e8f0',
            }}>
              <div>
                <div style={{ fontSize: '13.5px', color: '#0f172a', fontWeight: 800 }}>
                  💰 1인 평균 {venue.avg_price.toLocaleString()}원 &nbsp;·&nbsp; <span style={{ color: '#d97706' }}>★ {venue.rating.toFixed(1)}</span> <span style={{ color: '#475569', fontWeight: 600 }}>({venue.review_count}건)</span>
                </div>
                <div style={{ fontSize: '12px', color: '#334155', fontWeight: 600, marginTop: '4px' }}>
                  🕒 {(venue as any).open_hours || '매일 09:00 ~ 22:00'} &nbsp;·&nbsp; 📞 {(venue as any).phone || '02-555-0199'}
                </div>
              </div>

              {!readOnly && (
                <button
                  type="button"
                  onClick={() => setShowClosureModal(true)}
                  style={{
                    fontSize: '11.5px',
                    color: '#b91c1c',
                    background: '#fee2e2',
                    border: '1px solid #fca5a5',
                    padding: '6px 11px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    fontWeight: 800,
                    whiteSpace: 'nowrap',
                  }}
                >
                  🚨 폐업 제보
                </button>
              )}
            </div>

          {/* 실시간 파트너 음악 배너 */}
          <LiveMusicBanner banner={venue.music_banner} />

          {/* 실시간 타임 이벤트 배너 & 참여하기 버튼 */}
          {activeEvent && (
            <div style={{
              backgroundColor: '#fff1f2',
              border: '1px solid #fecdd3',
              borderRadius: '12px',
              padding: '16px',
              marginBottom: '20px',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                <span style={{
                  backgroundColor: '#e11d48',
                  color: '#ffffff',
                  fontSize: '11px',
                  fontWeight: 800,
                  padding: '2px 8px',
                  borderRadius: '4px',
                }}>
                  {activeEvent.badge_text}
                </span>
                <span style={{ fontSize: '12px', color: '#be123c', fontWeight: 700 }}>
                  남은 수량: {activeEvent.remaining_quantity}개 (현재 방문 예정: {activeEvent.intended_count}명)
                </span>
              </div>

              <div style={{ fontWeight: 800, fontSize: '15px', color: '#9f1239', marginBottom: '4px' }}>
                {activeEvent.title}
              </div>
              <div style={{ fontSize: '13px', color: '#be123c', marginBottom: '12px' }}>
                {activeEvent.discount_info}
              </div>

              {!readOnly && (
                <button
                  type="button"
                  onClick={() => onOpenEventModal && onOpenEventModal(activeEvent)}
                  style={{
                    width: '100%',
                    padding: '10px',
                    backgroundColor: '#e11d48',
                    color: '#ffffff',
                    border: 'none',
                    borderRadius: '8px',
                    fontWeight: 800,
                    fontSize: '14px',
                    cursor: 'pointer',
                    boxShadow: '0 2px 8px rgba(225, 29, 72, 0.3)',
                  }}
                >
                  🙋 [참여할게요] 클릭 및 현장 QR 인증권 받기
                </button>
              )}
            </div>
          )}

          {venue.applied_playlist && (
            <section aria-label="현재 매장 선곡표" style={{ background: '#eff6ff', borderRadius: 12, padding: 14, marginBottom: 16 }}>
              <strong>현재 매장 선곡표</strong>
              <div>{venue.applied_playlist.name}</div>
            </section>
          )}
          <h3>메뉴 {venue.all_menus_count}개</h3>
          <MenuCategoryTabs menuTabs={venue.menu_tabs} />

          {/* 날씨 스냅샷 한줄평 작성 폼 */}
          {!readOnly && (
            <div style={{ marginTop: '28px', paddingTop: '20px', borderTop: '1px solid #e2e8f0' }}>
              <h4 style={{ fontSize: '15px', fontWeight: 800, margin: '0 0 10px 0' }}>
                📸 날씨 스냅샷 한줄평 남기기
              </h4>
              <form onSubmit={handleReviewSubmit}>
                <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
                  <select
                    value={reviewRating}
                    onChange={(e) => setReviewRating(Number(e.target.value))}
                    style={{
                      padding: '8px',
                      borderRadius: '8px',
                      border: '1px solid #cbd5e1',
                      fontSize: '13px',
                      fontWeight: 600,
                    }}
                  >
                    <option value={5}>⭐⭐⭐⭐⭐ (5점)</option>
                    <option value={4}>⭐⭐⭐⭐ (4점)</option>
                    <option value={3}>⭐⭐⭐ (3점)</option>
                  </select>

                  <input
                    type="text"
                    value={reviewContent}
                    onChange={(e) => setReviewContent(e.target.value)}
                    placeholder={`오늘 ${currentWeather} 날씨에 어울리는 방문 소감을 남겨주세요`}
                    style={{
                      flex: 1,
                      padding: '8px 12px',
                      borderRadius: '8px',
                      border: '1px solid #cbd5e1',
                      fontSize: '13px',
                    }}
                  />

                  <button
                    type="submit"
                    disabled={reviewSubmitting || !reviewContent.trim()}
                    style={{
                      padding: '8px 16px',
                      backgroundColor: '#2563eb',
                      color: '#ffffff',
                      border: 'none',
                      borderRadius: '8px',
                      fontWeight: 700,
                      fontSize: '13px',
                      cursor: reviewSubmitting ? 'not-allowed' : 'pointer',
                    }}
                  >
                    등록
                  </button>
                </div>
              </form>

              {/* 방문자 날씨 한줄평 리스트 */}
              {venue.recent_reviews && venue.recent_reviews.length > 0 && (
                <div style={{ marginTop: '14px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {venue.recent_reviews.map((rev) => (
                    <div
                      key={rev.interaction_id}
                      style={{
                        backgroundColor: '#f8fafc',
                        borderRadius: '8px',
                        padding: '10px 12px',
                        border: '1px solid #e2e8f0',
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '4px' }}>
                        <span style={{ fontSize: '12px', fontWeight: 700, color: '#334155' }}>
                          {rev.user_id}
                        </span>
                        <span style={{ color: '#eab308', fontSize: '11px', fontWeight: 700 }}>
                          {'⭐'.repeat(rev.rating || 5)}
                        </span>
                      </div>
                      <div style={{ fontSize: '13px', color: '#1e293b', lineHeight: 1.4 }}>
                        {rev.content}
                      </div>
                      <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '4px' }}>
                        당시 날씨: {rev.weather_condition} ({rev.weather_temp}℃)
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          </>}
        </div>
      </div>

      {/* 폐업 제보 모달 */}
      {!readOnly && !venue.curated_listing && showClosureModal && (
        <div style={{
          position: 'fixed',
          inset: 0,
          backgroundColor: 'rgba(0,0,0,0.6)',
          zIndex: 1100,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}>
          <div style={{
            backgroundColor: '#ffffff',
            borderRadius: '16px',
            padding: '24px',
            width: '420px',
            maxWidth: '90%',
          }}>
            <h3 style={{ margin: '0 0 12px 0', fontSize: '18px', fontWeight: 800, color: '#dc2626' }}>
              🚨 매장 폐업 / 정보오류 제보
            </h3>
            <p style={{ fontSize: '13px', color: '#64748b', lineHeight: 1.5, margin: '0 0 14px 0' }}>
              제보가 접수되면 즉시 <strong>⚠️ 폐업 제보 접수</strong> 경고 배지가 표시되며 관리자 현장 확인이 진행됩니다.
            </p>

            <textarea
              value={closureReason}
              onChange={(e) => setClosureReason(e.target.value)}
              placeholder="예: 간판이 내려가고 임대 현수막이 붙어있어요, 다른 매장으로 바뀌었어요"
              rows={3}
              style={{
                width: '100%',
                padding: '10px',
                borderRadius: '8px',
                border: '1px solid #cbd5e1',
                fontSize: '13px',
                boxSizing: 'border-box',
                marginBottom: '16px',
              }}
            />

            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
              <button
                type="button"
                onClick={() => setShowClosureModal(false)}
                style={{
                  padding: '8px 14px',
                  backgroundColor: '#f1f5f9',
                  border: 'none',
                  borderRadius: '8px',
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                취소
              </button>
              <button
                type="button"
                disabled={closureReporting || !closureReason.trim()}
                onClick={handleClosureReport}
                style={{
                  padding: '8px 16px',
                  backgroundColor: '#dc2626',
                  color: '#ffffff',
                  border: 'none',
                  borderRadius: '8px',
                  fontWeight: 700,
                  cursor: closureReporting ? 'not-allowed' : 'pointer',
                }}
              >
                {closureReporting ? '제보 접수 중...' : '제보하기'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
