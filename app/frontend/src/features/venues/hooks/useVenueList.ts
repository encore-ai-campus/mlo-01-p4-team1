import { useState, useEffect, useCallback } from 'react';
import { VenueItem, BudgetTier, VenueCategory, StoreType } from '../types/venue.types';
import { fetchVenues, VenueSearchParams } from '../api/venues.api';

export interface MapBounds {
  min_lat: number;
  max_lat: number;
  min_lng: number;
  max_lng: number;
}

export function useVenueList(initialGu: string = '강남구') {
  const [gu, setGuState] = useState<string>(initialGu);
  const [mapBounds, setMapBounds] = useState<MapBounds | null>(null);
  const [dong, setDong] = useState<string>('');
  const [category, setCategory] = useState<VenueCategory>('ALL');
  const [priceTier, setPriceTier] = useState<BudgetTier | ''>('');
  const [storeType, setStoreType] = useState<StoreType>('ALL');
  const [eventOnly, setEventOnly] = useState<boolean>(false);
  const [partnerOnly, setPartnerOnly] = useState<boolean>(false);
  const [openNow, setOpenNow] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [weather, setWeather] = useState<string>('CLEAR');

  // 사용자가 드롭다운에서 직접 구(Gu)를 바꿀 때는 기존 지도 BBox 필터를 초기화하여 해당 구로 이동
  const setGu = useCallback((newGu: string) => {
    setMapBounds(null);
    setGuState(newGu);
  }, []);

  const [venues, setVenues] = useState<VenueItem[]>([]);
  const [totalCount, setTotalCount] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadVenues = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params: VenueSearchParams = {
        sigungu: gu,
        dong: dong || undefined,
        category,
        price_tier: priceTier,
        store_type: storeType,
        event_only: eventOnly,
        partner_only: partnerOnly,
        open_now: openNow,
        q: searchQuery || undefined,
        weather,
        ...(mapBounds ? mapBounds : {}),
      };
      const data = await fetchVenues(params);
      if (data.success) {
        setVenues(data.venues);
        setTotalCount(data.total_count);
      }
    } catch (err: any) {
      setError(err.message || '매장 목록을 불러오지 못했습니다.');
    } finally {
      setLoading(false);
    }
  }, [gu, mapBounds, dong, category, priceTier, storeType, eventOnly, partnerOnly, openNow, searchQuery, weather]);

  useEffect(() => {
    loadVenues();
  }, [loadVenues]);

  return {
    gu,
    setGu,
    mapBounds,
    setMapBounds,
    dong,
    setDong,
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
    weather,
    setWeather,
    venues,
    totalCount,
    loading,
    error,
    reload: loadVenues,
  };
}
