import React from 'react';
import { RegionSelect } from './RegionSelect';
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
  const seoulGuList = [
    '강남구', '강동구', '강북구', '강서구', '관악구',
    '광진구', '구로구', '금천구', '노원구', '도봉구',
    '동대문구', '동작구', '마포구', '서대문구', '서초구',
    '성동구', '성북구', '송파구', '양천구', '영등포구',
    '용산구', '은평구', '종로구', '중구', '중랑구',
  ];

  const gyeonggiCityList = [
    '가평군', '고양시', '과천시', '광명시', '광주시',
    '구리시', '군포시', '김포시', '남양주시', '동두천시',
    '부천시', '성남시', '수원시', '시흥시', '안양시',
    '안성시', '양주시', '양평군', '여주시', '연천군',
    '오산시', '용인시', '의왕시', '의정부시', '이천시',
    '파주시', '평택시', '포천시', '하남시', '화성시',
  ];

  const incheonGuList = [
    '강화군', '계양구', '남동구', '동구', '미추홀구',
    '부평구', '연수구', '옹진군', '인천 서구', '인천 중구',
  ];

  const budgetOptions: Array<{ label: string; value: BudgetTier }> = [
    { label: '1만원 미만', value: 'UNDER_10K' },
    { label: '1만~2만원 미만', value: '10K_20K' },
    { label: '2만~3만원 미만', value: '20K_30K' },
    { label: '3만~5만원 미만', value: '30K_50K' },
    { label: '5만원 이상', value: 'OVER_50K' },
  ];

  const chip = (label: string, active: boolean, onClick: () => void) => (
    <button type="button" className="fp-chip" aria-pressed={active} onClick={onClick}>{label}</button>
  );
  return <section className="fitplace-filters" aria-label="매장 검색 및 필터">
    <div className="fp-filter-top"><div className="fp-region"><RegionSelect value={gu} onChange={setGu} groups={[
      {label:'서울특별시 (25개 자치구)', options:seoulGuList},
      {label:'경기도 (30개 시·군)', options:gyeonggiCityList},
      {label:'인천광역시 (10개 구·군)', options:incheonGuList},
    ]} /><span>검색 결과 {totalCount.toLocaleString()}개 매장</span></div>
      <div className="fp-weather"><span>날씨</span>{[['맑음','CLEAR'],['비','RAIN'],['눈','SNOW']].map(([label,value]) => <React.Fragment key={value}>{chip(label, weather === value, () => setWeather(value))}</React.Fragment>)}</div>
    </div>
    <input className="fp-search" type="search" aria-label="매장 검색" value={searchQuery} onChange={e => setSearchQuery(e.target.value)} placeholder="매장명, 대표 메뉴, 동네로 검색해 보세요" />
    <div className="fp-filter-row fp-quick">{chip('이벤트 진행 중',eventOnly,()=>setEventOnly(!eventOnly))}{chip('추천 음악 큐레이션',partnerOnly,()=>setPartnerOnly(!partnerOnly))}{chip('영업 중',openNow,()=>setOpenNow(!openNow))}</div>
    <div className="fp-filter-row"><span className="fp-filter-label">업종</span><div className="fp-chip-row">{chip('전체',category==='ALL',()=>setCategory('ALL'))}{chip('카페',category==='CAFE',()=>setCategory('CAFE'))}{chip('주점',category==='PUB',()=>setCategory('PUB'))}<button type="button" className="fp-chip" disabled>일반 음식점 · 준비 중</button></div></div>
    <div className="fp-filter-row"><span className="fp-filter-label">예산</span><div className="fp-chip-row">{chip('전체 예산',priceTier==='',()=>setPriceTier(''))}{budgetOptions.map(b=><React.Fragment key={b.value}>{chip(b.label,priceTier===b.value,()=>setPriceTier(priceTier===b.value?'':b.value))}</React.Fragment>)}</div></div>
    <div className="fp-filter-row"><span className="fp-filter-label">형태</span><div className="fp-chip-row">{chip('전체',storeType==='ALL',()=>setStoreType('ALL'))}{chip('개인/로컬',storeType==='LOCAL',()=>setStoreType('LOCAL'))}{chip('체인점',storeType==='CHAIN',()=>setStoreType('CHAIN'))}</div></div>
  </section>;
};
