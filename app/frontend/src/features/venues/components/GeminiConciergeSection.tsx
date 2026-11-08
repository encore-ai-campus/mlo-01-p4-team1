import React, { useState } from 'react';
import { ConciergeBriefing, BudgetTier, VenueCategory } from '../types/venue.types';
import { postAiConcierge } from '../api/venues.api';

interface GeminiConciergeSectionProps {
  gu: string;
  weather: string;
  onSelectVenue?: (venueId: string) => void;
}

export const GeminiConciergeSection: React.FC<GeminiConciergeSectionProps> = ({ gu, weather, onSelectVenue }) => {
  const [query, setQuery] = useState<string>('');
  const [briefing, setBriefing] = useState<ConciergeBriefing | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const quickChips = [
    { label: '☔ 비 오는 날 2만원대 전집·막걸리 추천', q: '비 오는 날 감성으로 2만원대 전집과 막걸리 마실 포차나 주점 추천해줘', cat: 'PUB' as VenueCategory, tier: '20K_30K' as BudgetTier },
    { label: '☀️ 1만원 미만 카공하기 좋은 로스터리 카페', q: '조용하고 커피 맛있는 1만원 미만 작업/카공 카페 추천해줘', cat: 'CAFE' as VenueCategory, tier: 'UNDER_10K' as BudgetTier },
    { label: '🌙 심야에 분위기 좋은 감성 하이볼 & 이자카야', q: '밤늦게까지 영업하고 분위기 있는 하이볼 맛집 추천해줘', cat: 'PUB' as VenueCategory, tier: '20K_30K' as BudgetTier },
    { label: '🍰 달콤한 수플레 & 베이커리 데이트 코스', q: '비주얼 예쁘고 달콤한 디저트와 커피가 있는 카페 추천해줘', cat: 'CAFE' as VenueCategory, tier: '10K_20K' as BudgetTier },
  ];

  const handleAsk = async (userPrompt: string, cat?: VenueCategory, tier?: BudgetTier) => {
    if (!userPrompt.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await postAiConcierge({
        query: userPrompt,
        gu,
        weather,
        category: cat || 'ALL',
        budget_tier: tier,
      });
      if (data.success && data.briefing) {
        setBriefing(data.briefing);
      }
    } catch (err: any) {
      setError(err.message || 'AI 컨시어지 질의에 실패했습니다.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      background: 'linear-gradient(135deg, #0f172a, #1e1b4b)',
      color: '#f8fafc',
      borderRadius: '16px',
      padding: '22px',
      marginBottom: '24px',
      border: '1px solid #312e81',
      boxShadow: '0 8px 24px rgba(15, 23, 42, 0.3)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <span style={{ fontSize: '24px' }}>✨</span>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span style={{ fontWeight: 800, fontSize: '17px' }}>Gemini 3.8 Flash AI 매장 & 미식 컨시어지</span>
              <span style={{
                backgroundColor: '#3b82f6',
                color: '#ffffff',
                padding: '2px 8px',
                borderRadius: '6px',
                fontSize: '11px',
                fontWeight: 700,
              }}>
                gemini-3.8-flash
              </span>
            </div>
            <div style={{ fontSize: '13px', color: '#94a3b8', marginTop: '2px' }}>
              오늘 {gu}의 날씨({weather})와 예산, 기분에 딱 맞는 최적의 매장과 베스트 메뉴 조합을 브리핑해 드립니다.
            </div>
          </div>
        </div>
      </div>

      {/* 원클릭 추천 질문 칩 */}
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '16px' }}>
        {quickChips.map((chip, idx) => (
          <button
            key={idx}
            type="button"
            onClick={() => {
              setQuery(chip.q);
              handleAsk(chip.q, chip.cat, chip.tier);
            }}
            style={{
              backgroundColor: '#1e293b',
              border: '1px solid #6366f1',
              color: '#ffffff',
              padding: '7px 14px',
              borderRadius: '9999px',
              fontSize: '12.5px',
              fontWeight: 700,
              cursor: 'pointer',
              transition: 'all 0.2s',
            }}
            onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#312e81')}
            onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = '#1e293b')}
          >
            {chip.label}
          </button>
        ))}
      </div>

      {/* 자유 입력 검색바 */}
      <div style={{ display: 'flex', gap: '10px' }}>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={`오늘 ${gu}에서 어떤 분위기나 메뉴를 찾으시나요? (예: 2만원대 파전에 막걸리 로컬 주점)`}
          onKeyDown={(e) => {
            if (e.key === 'Enter') handleAsk(query);
          }}
          style={{
            flex: 1,
            padding: '12px 16px',
            borderRadius: '10px',
            border: '1px solid #4338ca',
            backgroundColor: '#1e1b4b',
            color: '#f8fafc',
            fontSize: '14px',
            outline: 'none',
          }}
        />
        <button
          type="button"
          disabled={loading || !query.trim()}
          onClick={() => handleAsk(query)}
          style={{
            padding: '12px 20px',
            backgroundColor: loading ? '#475569' : '#6366f1',
            color: '#ffffff',
            border: 'none',
            borderRadius: '10px',
            fontWeight: 700,
            fontSize: '14px',
            cursor: loading ? 'not-allowed' : 'pointer',
            transition: 'background-color 0.2s',
          }}
        >
          {loading ? 'AI 분석 중...' : 'AI 질문하기'}
        </button>
      </div>

      {error && (
        <div style={{ color: '#f87171', fontSize: '13px', marginTop: '10px' }}>
          ⚠️ {error}
        </div>
      )}

      {/* AI 브리핑 카드 */}
      {briefing && (
        <div style={{
          backgroundColor: 'rgba(30, 27, 75, 0.8)',
          border: '1px solid #6366f1',
          borderRadius: '12px',
          padding: '18px',
          marginTop: '16px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
            <span style={{ fontWeight: 800, fontSize: '16px', color: '#a5b4fc' }}>
              💡 {briefing.concierge_title}
            </span>
            <span style={{ fontSize: '11px', color: '#818cf8', backgroundColor: '#312e81', padding: '2px 8px', borderRadius: '4px' }}>
              엔진: {briefing.model_used}
            </span>
          </div>

          <p style={{ fontSize: '14px', lineHeight: 1.6, color: '#e2e8f0', margin: '0 0 12px 0' }}>
            {briefing.summary_briefing}
          </p>

          {briefing.recommended_venue_name && (
            <div style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              backgroundColor: '#0f172a',
              padding: '12px 16px',
              borderRadius: '8px',
              border: '1px solid #334155',
            }}>
              <div>
                <div style={{ fontWeight: 700, fontSize: '15px', color: '#38bdf8' }}>
                  🎯 엄선 추천 매장: {briefing.recommended_venue_name}
                </div>
                <div style={{ fontSize: '12px', color: '#94a3b8', marginTop: '2px' }}>
                  어울리는 베스트 메뉴 조합: <strong>{briefing.recommended_menu_combination.join(' + ')}</strong>
                </div>
              </div>
              {briefing.recommended_venue_id && onSelectVenue && (
                <button
                  type="button"
                  onClick={() => onSelectVenue(briefing.recommended_venue_id!)}
                  style={{
                    backgroundColor: '#2563eb',
                    color: '#ffffff',
                    border: 'none',
                    padding: '8px 14px',
                    borderRadius: '6px',
                    fontSize: '12px',
                    fontWeight: 600,
                    cursor: 'pointer',
                  }}
                >
                  매장 바로보기 &rarr;
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
