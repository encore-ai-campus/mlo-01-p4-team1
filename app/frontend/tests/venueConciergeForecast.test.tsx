import { afterEach, expect, test, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { VisitVenue } from '../src/features/venues/api/visitRecommendation.api';
import { describeVisitForecast, VisitCards } from '../src/features/venues/components/VisitRecommendationSection';

const forecast: VisitVenue['forecast'] = {
  provider: 'project_weather',
  state: 'DRY',
  condition: 'CLEAR',
  issued_at: '2026-11-10T17:30:00+09:00',
  start_at: '2026-11-10T20:00:00+09:00',
  end_at: '2026-11-10T21:00:00+09:00',
};

afterEach(() => vi.useRealTimers());

test('해당 방문 시간과 발표가 확인된 예보에만 기상청 실예보를 붙인다', () => {
  const fresh = describeVisitForecast(forecast, Date.parse('2026-11-10T18:00:00+09:00'));
  expect(fresh.available).toBe(true);
  expect(fresh.text).toMatch(/기상청 실예보.*맑음.*20:00.*21:00/);
  expect(describeVisitForecast({ ...forecast, issued_at: null }, Date.parse('2026-11-10T18:00:00+09:00')))
    .toEqual({ text: '방문 시간대 예보 확인 불가', available: false });
});

test('서버가 보낸 예보 없음과 유효성 문제를 구분해서 표시한다', () => {
  const now = Date.parse('2026-11-10T18:00:00+09:00');
  const missing = { ...forecast, state: 'NO_FORECAST' as const, condition: null };
  expect(describeVisitForecast({ ...missing, reason: 'TIME_NOT_COVERED' }, now).text)
    .toBe('방문 시간대 예보 없음 · 날씨 미반영');
  expect(describeVisitForecast({ ...missing, reason: 'STALE_OR_FUTURE_SEGMENT' }, now).text)
    .toContain('예보 만료 또는 유효성 확인 불가');
});

test('미래 방문의 예보라도 발표 유효기간이 지나면 매장만 표시하고 날씨 근거는 숨긴다', () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-10T19:30:00+09:00'));
  const venue: VisitVenue = {
    venue_id: 'fixture-cafe', name: '테스트 카페', address: '인천광역시 연수구',
    sub_category: null, avg_budget_krw: 15000, matched_moods: [], matched_orders: [],
    forecast, weather_match: { label: '맑은 날 어울리는 매장', source: 'TAG_MATCH' },
  };
  const html = renderToStaticMarkup(<VisitCards venues={[venue]} />);
  expect(html).toContain('테스트 카페');
  expect(html).toContain('예보 만료');
  expect(html).not.toContain('기상청 실예보');
  expect(html).not.toContain('맑은 날 어울리는 매장');
});
