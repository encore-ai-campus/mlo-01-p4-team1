import React, { useState, useEffect } from 'react';
import { MySummaryData } from '../types/venue.types';
import { fetchMySummary } from '../api/venues.api';

interface MyPageDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  userId?: string;
  onSelectVenue?: (venueId: string) => void;
}

export const MyPageDrawer: React.FC<MyPageDrawerProps> = ({
  isOpen,
  onClose,
  userId = 'guest_anonymous',
  onSelectVenue,
}) => {
  const [activeTab, setActiveTab] = useState<'BOOKMARKS' | 'REVIEWS' | 'EVENTS'>('BOOKMARKS');
  const [summary, setSummary] = useState<MySummaryData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  useEffect(() => {
    if (!isOpen) return;
    setLoading(true);
    fetchMySummary(userId)
      .then((res) => {
        if (res.success && res.summary) {
          setSummary(res.summary);
        }
      })
      .finally(() => setLoading(false));
  }, [isOpen, userId]);

  if (!isOpen) return null;

  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      backgroundColor: 'rgba(0,0,0,0.4)',
      zIndex: 1150,
      display: 'flex',
      justifyContent: 'flex-end',
      backdropFilter: 'blur(2px)',
    }}>
      <div style={{
        width: '460px',
        maxWidth: '100%',
        height: '100%',
        backgroundColor: '#ffffff',
        overflowY: 'auto',
        boxShadow: '-4px 0 20px rgba(0,0,0,0.15)',
        display: 'flex',
        flexDirection: 'column',
      }}>
        {/* 헤더 */}
        <div style={{
          padding: '20px',
          borderBottom: '1px solid #e2e8f0',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          <div>
            <h3 style={{ margin: 0, fontSize: '18px', fontWeight: 800 }}>👤 마이페이지</h3>
            <span style={{ fontSize: '12px', color: '#64748b' }}>유저 ID: {userId}</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            style={{
              background: '#f1f5f9',
              border: 'none',
              borderRadius: '50%',
              width: '30px',
              height: '30px',
              fontSize: '14px',
              cursor: 'pointer',
            }}
          >
            ✕
          </button>
        </div>

        {/* 3개 탭 헤더 */}
        <div style={{ display: 'flex', borderBottom: '1px solid #e2e8f0' }}>
          {[
            { key: 'BOOKMARKS' as const, label: '💖 찜한 매장' },
            { key: 'REVIEWS' as const, label: '✍️ 내 리뷰' },
            { key: 'EVENTS' as const, label: '🎟️ 참여 이벤트/QR' },
          ].map((tab) => {
            const isActive = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => setActiveTab(tab.key)}
                style={{
                  flex: 1,
                  padding: '12px 6px',
                  border: 'none',
                  background: 'none',
                  borderBottom: isActive ? '3px solid #2563eb' : '3px solid transparent',
                  color: isActive ? '#2563eb' : '#64748b',
                  fontWeight: isActive ? 800 : 600,
                  fontSize: '13px',
                  cursor: 'pointer',
                }}
              >
                {tab.label}
              </button>
            );
          })}
        </div>

        {/* 컨텐츠 영역 */}
        <div style={{ padding: '20px', flex: 1 }}>
          {loading && <div style={{ textAlign: 'center', color: '#94a3b8' }}>로딩 중...</div>}

          {/* 1. 찜한 매장 탭 */}
          {activeTab === 'BOOKMARKS' && summary && (
            <div>
              {summary.bookmarked_venues.length === 0 ? (
                <div style={{ textAlign: 'center', color: '#94a3b8', padding: '40px 0' }}>
                  아직 찜한 매장이 없습니다.
                </div>
              ) : (
                summary.bookmarked_venues.map((v) => (
                  <div
                    key={v.venue_id}
                    onClick={() => {
                      if (onSelectVenue) onSelectVenue(v.venue_id);
                      onClose();
                    }}
                    style={{
                      padding: '12px 14px',
                      backgroundColor: '#f8fafc',
                      border: '1px solid #e2e8f0',
                      borderRadius: '10px',
                      marginBottom: '10px',
                      cursor: 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                    }}
                  >
                    <div>
                      <div style={{ fontWeight: 700, fontSize: '14px' }}>{v.venue_name}</div>
                      <div style={{ fontSize: '12px', color: '#64748b', marginTop: '2px' }}>
                        {v.gu_name} • {v.category === 'CAFE' ? '카페' : '주점'}
                      </div>
                    </div>
                    <span style={{ fontSize: '18px' }}>💖</span>
                  </div>
                ))
              )}
            </div>
          )}

          {/* 2. 내 리뷰 탭 */}
          {activeTab === 'REVIEWS' && summary && (
            <div>
              {summary.reviews.length === 0 ? (
                <div style={{ textAlign: 'center', color: '#94a3b8', padding: '40px 0' }}>
                  작성한 날씨 한줄평 리뷰가 없습니다.
                </div>
              ) : (
                summary.reviews.map((r) => (
                  <div
                    key={r.review_id}
                    style={{
                      padding: '14px',
                      backgroundColor: '#f8fafc',
                      border: '1px solid #e2e8f0',
                      borderRadius: '10px',
                      marginBottom: '10px',
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                      <span style={{ fontWeight: 700, fontSize: '13px' }}>{r.venue_name}</span>
                      <span style={{ color: '#eab308', fontSize: '12px', fontWeight: 700 }}>
                        {'⭐'.repeat(r.rating || 5)}
                      </span>
                    </div>
                    <div style={{ fontSize: '13px', color: '#334155', lineHeight: 1.4 }}>
                      {r.content}
                    </div>
                    <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '6px' }}>
                      당시 날씨: {r.weather_condition} ({r.weather_temp}℃)
                    </div>
                  </div>
                ))
              )}
            </div>
          )}

          {/* 3. 오늘 참여 이벤트 탭 */}
          {activeTab === 'EVENTS' && summary && (
            <div>
              {summary.participations.length === 0 ? (
                <div style={{ textAlign: 'center', color: '#94a3b8', padding: '40px 0' }}>
                  참여한 실시간 이벤트가 없습니다.
                </div>
              ) : (
                summary.participations.map((p) => (
                  <div
                    key={p.participation_id}
                    style={{
                      padding: '14px',
                      backgroundColor: p.status === 'VERIFIED' ? '#f0fdf4' : '#fff1f2',
                      border: p.status === 'VERIFIED' ? '1px solid #86efac' : '1px solid #fecdd3',
                      borderRadius: '10px',
                      marginBottom: '10px',
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontWeight: 800, fontSize: '14px', color: '#0f172a' }}>
                        {p.title}
                      </span>
                      <span style={{
                        fontSize: '11px',
                        fontWeight: 700,
                        backgroundColor: p.status === 'VERIFIED' ? '#16a34a' : '#e11d48',
                        color: '#ffffff',
                        padding: '2px 8px',
                        borderRadius: '4px',
                      }}>
                        {p.status_label}
                      </span>
                    </div>
                    <div style={{ fontSize: '12px', color: '#64748b', marginTop: '6px' }}>
                      인증 코드: <code>{p.qr_auth_code}</code>
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
