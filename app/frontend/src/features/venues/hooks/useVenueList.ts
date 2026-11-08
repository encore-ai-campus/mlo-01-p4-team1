import { useState, useEffect, useCallback } from 'react';
import { VenueItem, BudgetTier, VenueCategory, StoreType } from '../types/venue.types';
import { fetchVenues, VenueSearchParams } from '../api/venues.api';

export function useVenueList(initialGu: string = '강남구') {
  const [gu, setGu] = useState<string>(initialGu);
  const [dong, setDong] = useState<string>('');
  const [category, setCategory] = useState<VenueCategory>('ALL');
  const [priceTier, setPriceTier] = useState<BudgetTier | ''>('');
  const [storeType, setStoreType] = useState<StoreType>('ALL');
  const [eventOnly, setEventOnly] = useState<boolean>(false);
  const [partnerOnly, setPartnerOnly] = useState<boolean>(false);
  const [openNow, setOpenNow] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [weather, setWeather] = useState<string>('CLEAR');

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
  }, [gu, dong, category, priceTier, storeType, eventOnly, partnerOnly, openNow, searchQuery, weather]);

  useEffect(() => {
    loadVenues();
  }, [loadVenues]);

  return {
    gu,
    setGu,
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
