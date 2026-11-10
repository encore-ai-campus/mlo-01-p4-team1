import { useEffect, useState } from 'react';
import type { VenueDetail } from '../types/venue.types';
import { fetchVenueDetail } from '../api/venues.api';

export function useVenueDetail(venueId: string | null, weather: string = 'CLEAR') {
  const [venue, setVenue] = useState<VenueDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setVenue(null);
    setError(null);
    if (!venueId) return;

    const controller = new AbortController();
    void fetchVenueDetail(venueId, weather, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setVenue(data.venue);
      },
      () => {
        if (!controller.signal.aborted) setError('매장 상세 정보를 불러오지 못했습니다.');
      },
    );
    return () => controller.abort();
  }, [venueId, weather]);

  return { venue: venue?.venue_id === venueId ? venue : null, error };
}
