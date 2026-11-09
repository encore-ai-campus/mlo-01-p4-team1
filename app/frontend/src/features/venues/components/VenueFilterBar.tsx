import React from 'react';
import { BudgetTier, VenueCategory, StoreType } from '../types/venue.types';

interface VenueFilterBarProps {
  gu: string;
  setGu: (gu: string) => void;
  weather: string;
  setWeather: (w: string) => void;
  category: VenueCategory;
  setCategory: (c: VenueCategory) => void;
  priceTier: BudgetTier | '';
  setPriceTier: (p: BudgetTier | '') => void;
  storeType: StoreType;
  setStoreType: (s: StoreType) => void;
  eventOnly: boolean;
  setEventOnly: (val: boolean) => void;
  partnerOnly: boolean;
  setPartnerOnly: (val: boolean) => void;
  openNow: boolean;
  setOpenNow: (val: boolean) => void;
  searchQuery: string;
  setSearchQuery: (q: string) => void;
  totalCount: number;
}

export const VenueFilterBar: React.FC<VenueFilterBarProps> = ({
  gu,
  setGu,
  weather,
  setWeather,
  category,
  setCategory,
  priceTier,
  setPriceTier,
  storeType,
  setStoreType,
  eventOnly,
  setEventOnly,
  partnerOnly,
  setPartnerOnly,
  openNow,
  setOpenNow,
  searchQuery,
  setSearchQuery,
  totalCount,
}) => {
  const guList = [
    '강남구', '강동구', '강북구', '강서구', '관악구',
    '광진구', '구로구', '금천구', '노원구', '도봉구',
    '동대문구', '동작구', '마포구', '서대문구', '서초구',
    '성동구', '성북구', '송파구', '양천구', '영등포구',
    '용산구', '은평구', '종로구', '중구', '중랑구',
  ];

  const budgetOptions: Array<{ label: string; value: BudgetTier }> = [
    { label: '1만원 미만', value: 'UNDER_10K' },
    { label: '1만~2만원 미만', value: '10K_20K' },
    { label: '2만~3만원 미만', value: '20K_30K' },
    { label: '3만~5만원 미만', value: '30K_50K' },
    { label: '5만원 이상', value: 'OVER_50K' },
  ];

  return (
    <div style={{
      backgroundColor: '#ffffff',
      borderRadius: '16px',
      padding: '20px',
      marginBottom: '20px',
      boxShadow: '0 2px 10px rgba(0, 0, 0, 0.05)',
      border: '1px solid #e2e8f0',
    }}>
      {/* 1. 상단 헤더: 자치구 선택 + 실시간 날씨 선택 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px', flexWrap: 'wrap', gap: '12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <span style={{ fontSize: '18px' }}>📍</span>
          <select
            value={gu}
            onChange={(e) => setGu(e.target.value)}
            style={{
              fontSize: '16px',
              fontWeight: 700,
              padding: '6px 12px',
              borderRadius: '8px',
              border: '1px solid #cbd5e1',
              backgroundColor: '#f8fafc',
              cursor: 'pointer',
            }}
          >
            {guList.map((g) => (
              <option key={g} value={g}>{g}</option>
            ))}
          </select>
          <span style={{ fontSize: '14px', color: '#64748b' }}>
            검색 결과 <strong>{totalCount}</strong>개 매장
          </span>
        </div>

        {/* 실시간 날씨 시뮬레이션 토글 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <span style={{ fontSize: '12px', color: '#64748b', fontWeight: 600 }}>실시간 날씨:</span>
          {[
            { label: '☀️ 맑음', val: 'CLEAR' },
            { label: '☔ 비', val: 'RAIN' },
            { label: '❄️ 눈', val: 'SNOW' },
          ].map((w) => (
            <button
              key={w.val}
              type="button"
              onClick={() => setWeather(w.val)}
              style={{
                padding: '4px 10px',
                borderRadius: '6px',
                border: '1px solid #cbd5e1',
                fontSize: '12px',
                fontWeight: 600,
                cursor: 'pointer',
                backgroundColor: weather === w.val ? '#3b82f6' : '#ffffff',
                color: weather === w.val ? '#ffffff' : '#475569',
              }}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      {/* 2. 검색창 */}
      <div style={{ marginBottom: '16px' }}>
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="매장명, 시그니처 메뉴, 동네 이름으로 검색 (예: 아인슈페너, 파전, 역삼동)"
          style={{
            width: '100%',
            padding: '10px 14px',
            borderRadius: '10px',
            border: '1px solid #cbd5e1',
            fontSize: '14px',
            boxSizing: 'border-box',
          }}
        />
      </div>

      {/* 3. 토글 칩 (이벤트 진행 중, 음악 파트너, 지금 영업 중) */}
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '16px' }}>
        <button
          type="button"
          onClick={() => setEventOnly(!eventOnly)}
          style={{
            padding: '6px 14px',
            borderRadius: '9999px',
            border: eventOnly ? '1px solid #ef4444' : '1px solid #e2e8f0',
            backgroundColor: eventOnly ? '#fee2e2' : '#f8fafc',
            color: eventOnly ? '#dc2626' : '#475569',
            fontSize: '13px',
            fontWeight: 700,
            cursor: 'pointer',
          }}
        >
          🔥 이벤트 진행 중만
        </button>

        <button
          type="button"
          onClick={() => setPartnerOnly(!partnerOnly)}
          style={{
            padding: '6px 14px',
            borderRadius: '9999px',
            border: partnerOnly ? '1px solid #8b5cf6' : '1px solid #e2e8f0',
            backgroundColor: partnerOnly ? '#f3e8ff' : '#f8fafc',
            color: partnerOnly ? '#7c3aed' : '#475569',
            fontSize: '13px',
            fontWeight: 700,
            cursor: 'pointer',
          }}
        >
          🎵 음악 파트너 매장
        </button>

        <button
          type="button"
          onClick={() => setOpenNow(!openNow)}
          style={{
            padding: '6px 14px',
            borderRadius: '9999px',
            border: openNow ? '1px solid #10b981' : '1px solid #e2e8f0',
            backgroundColor: openNow ? '#d1fae5' : '#f8fafc',
            color: openNow ? '#059669' : '#475569',
            fontSize: '13px',
            fontWeight: 700,
            cursor: 'pointer',
          }}
        >
          🟢 지금 영업 중
        </button>
      </div>

      {/* 4. 업종 선택 + 음식점 확대 예정 안내 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '13px', fontWeight: 600, color: '#64748b', minWidth: '40px' }}>업종:</span>
        {(['ALL', 'CAFE', 'PUB'] as VenueCategory[]).map((c) => {
          const labels: Record<VenueCategory, string> = { ALL: '전체', CAFE: '☕ 카페', PUB: '🍺 주점' };
          const active = category === c;
          return (
            <button
              key={c}
              type="button"
              onClick={() => setCategory(c)}
              style={{
                padding: '5px 12px',
                borderRadius: '8px',
                border: active ? '1px solid #2563eb' : '1px solid #cbd5e1',
                backgroundColor: active ? '#2563eb' : '#ffffff',
                color: active ? '#ffffff' : '#334155',
                fontSize: '13px',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              {labels[c]}
            </button>
          );
        })}
        <span style={{
          fontSize: '11px',
          color: '#94a3b8',
          backgroundColor: '#f1f5f9',
          padding: '4px 8px',
          borderRadius: '6px',
        }}>
          🍽️ 일반 음식점 (Track 3 확대 예정)
        </span>
      </div>

      {/* 5. PA 5단계 가격 필터 칩 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '13px', fontWeight: 600, color: '#64748b', minWidth: '40px' }}>예산:</span>
        <button
          type="button"
          onClick={() => setPriceTier('')}
          style={{
            padding: '5px 12px',
            borderRadius: '8px',
            border: priceTier === '' ? '1px solid #0f172a' : '1px solid #cbd5e1',
            backgroundColor: priceTier === '' ? '#0f172a' : '#ffffff',
            color: priceTier === '' ? '#ffffff' : '#334155',
            fontSize: '12px',
            fontWeight: 600,
            cursor: 'pointer',
          }}
        >
          전체 예산
        </button>
        {budgetOptions.map((b) => {
          const active = priceTier === b.value;
          return (
            <button
              key={b.value}
              type="button"
              onClick={() => setPriceTier(active ? '' : b.value)}
              style={{
                padding: '5px 12px',
                borderRadius: '8px',
                border: active ? '1px solid #f59e0b' : '1px solid #cbd5e1',
                backgroundColor: active ? '#fef3c7' : '#ffffff',
                color: active ? '#b45309' : '#334155',
                fontSize: '12px',
                fontWeight: 700,
                cursor: 'pointer',
              }}
            >
              {b.label}
            </button>
          );
        })}
      </div>

      {/* 6. 형태 (로컬 vs 체인) */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '13px', fontWeight: 600, color: '#64748b', minWidth: '40px' }}>형태:</span>
        {(['ALL', 'LOCAL', 'CHAIN'] as StoreType[]).map((s) => {
          const labels: Record<StoreType, string> = { ALL: '전체', LOCAL: '개인/로컬', CHAIN: '체인점' };
          const active = storeType === s;
          return (
            <button
              key={s}
              type="button"
              onClick={() => setStoreType(s)}
              style={{
                padding: '4px 10px',
                borderRadius: '6px',
                border: active ? '1px solid #475569' : '1px solid #e2e8f0',
                backgroundColor: active ? '#475569' : '#ffffff',
                color: active ? '#ffffff' : '#64748b',
                fontSize: '12px',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              {labels[s]}
            </button>
          );
        })}
      </div>
    </div>
  );
};
