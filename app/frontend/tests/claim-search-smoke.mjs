import { chromium } from 'playwright';
import assert from 'node:assert/strict';

// Run against a local Vite server. Mock the platform API to exercise the
// visible search, selection and claim request without altering real accounts.
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
try {
  const page = await browser.newPage();
  const owner = { account_id: 'owner', username: 'owner', nickname: '점주', memberships: [{ type: 'BUSINESS', status: 'ACTIVE' }] };
  const stores = [
    { id: 'store-1', revision: 1, display_name: '첫 번째 매장', my_role: 'OWNER', status: 'ACTIVE' },
    { id: 'store-2', revision: 1, display_name: '두 번째 매장', my_role: 'OWNER', status: 'ACTIVE' },
  ];
  let submitted;
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (!path.startsWith('/api/')) return route.continue();
    let data;
    if (path.endsWith('/sessions/current')) data = { account: owner, csrf_token: 'test' };
    else if (path.endsWith('/policies')) data = { version: 'test', documents: [] };
    else if (path === '/api/auth/me') return route.fulfill({ json: { role: null, allowed_menus: [] } });
    else if (path.endsWith('/claim-venues')) {
      assert.equal(url.searchParams.get('region'), '마포구');
      assert.equal(url.searchParams.get('q'), '테스트');
      data = { items: [{ venue_id: 'VN_TEST_01', venue_name: '테스트 카페', road_address: '서울 마포구 테스트로 1' }] };
    } else if (path === '/api/platform/v1/workspaces/store-1/music' || path === '/api/platform/v1/workspaces/store-2/music') {
      data = { revision: 1, preferences: {}, exclusions: [], applied_playlist: '', playlists: [] };
    } else if (path === '/api/platform/v1/workspaces/store-1/memberships') data = { items: [] };
    else if (path === '/api/platform/v1/workspaces/store-2/memberships') {
      return route.fulfill({ status: 503, json: { message: '구성원 조회 실패' } });
    }
    else if (path.endsWith('/workspace-connections') && request.method() === 'POST') {
      submitted = request.postDataJSON();
      data = { id: 'claim-1', revision: 1, state: 'APPROVED', approved_workspace_id: 'store-1' };
    } else if (path.endsWith('/workspaces')) data = { items: stores };
    else if (path.endsWith('/workspace-claims') || path.endsWith('/workspace-invitations')) data = { items: [] };
    else throw new Error(`Unexpected API request: ${request.method()} ${path}`);
    await route.fulfill({ json: { success: true, data } });
  });
  await page.goto('http://127.0.0.1:18762/platform/business');
  assert.equal(await page.getByRole('link', { name: '뮤더에서 선곡 만들기' }).getAttribute('href'), '/muther');
  await page.getByLabel('시·군·구').fill('마포구');
  await page.getByLabel('매장명').fill('테스트');
  await page.getByRole('button', { name: '매장 검색', exact: true }).click();
  await page.getByRole('button', { name: '테스트 카페 · 서울 마포구 테스트로 1' }).click();
  // A changed query must not silently submit the previously selected store.
  await page.getByLabel('매장명').fill('다른 매장');
  assert.equal(await page.getByRole('button', { name: '매장 연결하기' }).isDisabled(), true);
  assert.equal(await page.getByText('선택한 매장:', { exact: false }).count(), 0);
  await page.getByLabel('매장명').fill('테스트');
  await page.getByRole('button', { name: '매장 검색', exact: true }).click();
  await page.getByRole('button', { name: '테스트 카페 · 서울 마포구 테스트로 1' }).click();
  await page.getByRole('button', { name: '매장 연결하기' }).click();
  await page.getByText('매장을 연결했습니다.').waitFor();
  await page.getByRole('heading', { name: '첫 번째 매장 · 음악 운영' }).waitFor();
  assert.equal(submitted.fixture_id, 'catalog:VN_TEST_01');
  assert.equal(submitted.display_name, '테스트 카페');
  assert.equal(submitted.address, '서울 마포구 테스트로 1');
  // If the second store's permission data fails, keep the first store's music
  // and members together instead of displaying a partially switched store.
  await page.getByRole('button', { name: /첫 번째 매장/ }).click();
  await page.getByRole('heading', { name: '첫 번째 매장 · 음악 운영' }).waitFor();
  await page.getByRole('button', { name: /두 번째 매장/ }).click();
  await page.getByText('구성원 조회 실패').waitFor();
  assert.equal(await page.getByRole('heading', { name: '첫 번째 매장 · 음악 운영' }).count(), 1);
  assert.equal(await page.getByRole('heading', { name: '두 번째 매장 · 음악 운영' }).count(), 0);
  console.log('점주 매장 검색·선택·즉시 연결 화면 검증 통과');
} finally {
  await browser.close();
}
