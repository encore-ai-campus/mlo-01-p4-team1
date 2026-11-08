import { useState, useEffect, useCallback } from 'react';
import { VenueDetail } from '../types/venue.types';
import { fetchVenueDetail } from '../api/venues.api';

export function useVenueDetail(venueId: string | null, weather: string = 'CLEAR') {
  const [venue, setVenue] = useState<VenueDetail | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadDetail = useCallback(async () => {
    if (!venueId) {
      setVenue(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await fetchVenueDetail(venueId, weather);
      if (data.success) {
        setVenue(data.venue);
      }
    } catch (err: any) {
      setError(err.message || '매장 상세 정보를 불러오지 못했습니다.');
    } finally {
      setLoading(false);
    }
  }, [venueId, weather]);

  useEffect(() => {
    loadDetail();
  }, [loadDetail]);

  return {
    venue,
    loading,
    error,
    reload: loadDetail,
  };
}
