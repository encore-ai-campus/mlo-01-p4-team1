import { describe, expect, test } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ReviewPanel } from '../src/features/platform/ReviewPanel';
import { PlatformAdminHome, type PlatformAdmin } from '../src/features/platform/PlatformSupport';
import { readApiResponse, VenueApiError } from '../src/features/venues/api/venues.api';
import { findRegionOptionForDistrict } from '../src/features/venues/pages/VenueExplorePage';
import { forecastHours } from '../src/features/region-weather/WeatherPanel';

describe('Platform Review tabs and venue closure integration', () => {
  test('renders both workspace review and closure monitoring tabs when authorized', () => {
    const html = renderToStaticMarkup(
      <ReviewPanel admin canWorkspace canClosures canMetrics initialTab="closures" />,
    );
    expect(html).toContain('role="tablist"');
    expect(html).toContain('매장 관리권한 심사');
    expect(html).toContain('폐업 제보 검수 · 현황');
    expect(html).toContain('폐업 제보 검수 목록');
    expect(html).not.toContain('승인 대기 신청');
  });

  test('renders workspace review tab by default while keeping closures tab selectable', () => {
    const html = renderToStaticMarkup(
      <ReviewPanel admin canWorkspace canClosures canMetrics />,
    );
    expect(html).toContain('승인 대기 신청');
    expect(html).toContain('폐업 제보 검수 · 현황');
  });

  test('auto-selects closures tab when operator only has venue management rights', () => {
    const html = renderToStaticMarkup(
      <ReviewPanel admin canWorkspace={false} canClosures canMetrics />,
    );
    expect(html).toContain('폐업 제보 검수 목록');
    expect(html).not.toContain('승인 대기 신청');
  });

  test('shows passkey authentication guidance when admin passkey is missing', () => {
    const html = renderToStaticMarkup(
      <ReviewPanel admin={false} canWorkspace canClosures canMetrics />,
    );
    expect(html).toContain('관리자 인증이 필요합니다');
    expect(html).toContain('/admin/passkey');
  });

  test('PlatformAdminHome adds direct shortcut link to closures tab when allowed', () => {
    const admin: PlatformAdmin = {
      role: 'ADMIN',
      admin_role: 'OPS_ADMIN',
      csrf_token: 'fixture',
      allowed_menus: [{ path: '/platform/review', label: '매장 권한 심사' }],
      venue_closure_allowed: true,
      venue_metrics_allowed: true,
    };
    const html = renderToStaticMarkup(<PlatformAdminHome admin={admin} />);
    expect(html).toContain('/platform/review?tab=closures');
    expect(html).toContain('폐업 제보 검수 · 현황');
  });
});

describe('Venue HTTP/2 error handling and weather fallback helpers', () => {
  test('readApiResponse formats HTTP/2 empty statusText and JSON messages cleanly', async () => {
    const emptyStatusRes = new Response(JSON.stringify({ code: 'AUTH_REQUIRED', message: '로그인이 필요합니다.' }), {
      status: 401,
      statusText: '',
      headers: { 'Content-Type': 'application/json' },
    });
    await expect(readApiResponse(emptyStatusRes, '매장 목록을 불러오지 못했습니다')).rejects.toMatchObject({
      name: 'VenueApiError',
      status: 401,
      code: 'AUTH_REQUIRED',
      message: '매장 목록을 불러오지 못했습니다: 로그인이 필요합니다.',
    });

    const plainRes = new Response('Bad Gateway', { status: 502, statusText: '' });
    await expect(readApiResponse(plainRes, '매장 목록을 불러오지 못했습니다')).rejects.toThrow(
      '매장 목록을 불러오지 못했습니다: HTTP 502',
    );
    expect(new VenueApiError('err', 403, 'FORBIDDEN').status).toBe(403);
  });

  test('findRegionOptionForDistrict matches Seoul, Incheon, and Gyeonggi districts accurately', () => {
    const options = [
      { region_id: 'r1', name: '서울특별시 강남구 역삼1동', province: '서울특별시', district: '강남구', locality: '역삼1동', nx: 61, ny: 126 },
      { region_id: 'r2', name: '인천광역시 서구 청라1동', province: '인천광역시', district: '서구', locality: '청라1동', nx: 55, ny: 126 },
      { region_id: 'r3', name: '경기도 성남시 분당구 수내1동', province: '경기도', district: '성남시 분당구', locality: '수내1동', nx: 62, ny: 123 },
    ];
    expect(findRegionOptionForDistrict(options, '강남구')?.region_id).toBe('r1');
    expect(findRegionOptionForDistrict(options, '인천 서구')?.region_id).toBe('r2');
    expect(findRegionOptionForDistrict(options, '성남시')?.region_id).toBe('r3');
  });

  test('forecastHours falls back to valid forecast segment when current observation is stale', () => {
    const now = new Date('2026-11-12T00:00:00Z');
    const slot = forecastHours({
      region_name: '강남구',
      as_of: now.toISOString(),
      valid_until: '2026-11-12T03:00:00Z',
      is_test: false,
      current: { status: 'stale', pty: 0, sky: 1 },
      segments: [
        {
          pty: 0,
          sky: 3,
          temperature: 14,
          status: 'valid',
          start_at: '2026-11-11T23:30:00Z',
          end_at: '2026-11-12T00:30:00Z',
        },
      ],
    })[0];
    expect(slot?.pty).toBe(0);
    expect(slot?.sky).toBe(3);
    expect(slot?.temperature).toBeUndefined();
  });
});
