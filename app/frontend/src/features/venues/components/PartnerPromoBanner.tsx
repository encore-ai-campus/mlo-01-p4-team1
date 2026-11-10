import React, { useEffect, useState } from 'react';
import { VenueEventSummary } from '../types/venue.types';
import { fetchActiveEvents } from '../api/venues.api';

interface PartnerPromoBannerProps {
  gu: string;
  onSelectEvent?: (event: VenueEventSummary) => void;
}

export const PartnerPromoBanner: React.FC<PartnerPromoBannerProps> = ({ gu, onSelectEvent }) => {
  const [events, setEvents] = useState<VenueEventSummary[]>([]);
  const [currentIndex, setCurrentIndex] = useState<number>(0);

  useEffect(() => {
    fetchActiveEvents(gu)
      .then((data) => {
        if (data.success && data.events.length > 0) {
          setEvents(data.events);
        } else {
          setEvents([]);
        }
      })
      .catch(() => setEvents([]));
  }, [gu]);

  useEffect(() => {
    if (events.length <= 1) return;
    const interval = setInterval(() => {
      setCurrentIndex((prev) => (prev + 1) % events.length);
    }, 4500);
    return () => clearInterval(interval);
  }, [events.length]);

  if (events.length === 0) {
    return (
      <div className="fitplace-banner" style={{
        background: 'linear-gradient(135deg, #1e293b, #242424)',
        color: '#f8fafc',
        padding: '14px 20px',
        borderRadius: '14px',
        marginBottom: '20px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        boxShadow: '0 4px 12px rgba(15, 23, 42, 0.15)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>

          <div>
            <div style={{ fontWeight: 700, fontSize: '15px' }}>
              날씨에 어울리는, 오늘의 취향
            </div>
            <div style={{ fontSize: '12px', color: '#94a3b8' }}>
              {gu}의 카페·주점과 추천 음악을 함께 둘러보세요.
            </div>
          </div>
        </div>
        <a className="fp-banner-link" href="/">음악 큐레이션 보기</a>
      </div>
    );
  }

  const currentEv = events[currentIndex];

  return (
    <div
      onClick={() => onSelectEvent && onSelectEvent(currentEv)}
      style={{
        background: 'linear-gradient(135deg, #4f46e5, #ff383e)',
        color: '#ffffff',
        padding: '16px 22px',
        borderRadius: '14px',
        marginBottom: '20px',
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        boxShadow: '0 4px 14px rgba(79, 70, 229, 0.25)',
        transition: 'transform 0.2s',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
        <span style={{ fontSize: '28px' }}>🔥</span>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{
              backgroundColor: '#ef4444',
              color: '#ffffff',
              padding: '2px 8px',
              borderRadius: '6px',
              fontSize: '11px',
              fontWeight: 700,
            }}>
              {currentEv.badge_text}
            </span>
            <span style={{ fontWeight: 800, fontSize: '16px' }}>
              {currentEv.title}
            </span>
          </div>
          <div style={{ fontSize: '13px', color: '#e0e7ff', marginTop: '4px' }}>
            {currentEv.discount_info} • <strong>남은 수량: {currentEv.remaining_quantity}개</strong> (현재 방문 예정: {currentEv.intended_count}명)
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
        <span style={{
          backgroundColor: 'rgba(255, 255, 255, 0.2)',
          padding: '6px 12px',
          borderRadius: '8px',
          fontSize: '12px',
          fontWeight: 600,
        }}>
          현장 QR 인증 혜택 받기 &rarr;
        </span>
      </div>
    </div>
  );
};
