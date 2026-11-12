// Browser regression check. All API calls are fulfilled by this fixture.
const assert = require('node:assert/strict');
const {stat} = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const base = 'https://muther-fixture.invalid';
const build = path.resolve(process.env.SMOKE_BUILD_DIR || path.join(__dirname, '../dist'));

async function exercise(browser, role) {
  const context = await browser.newContext(role === 'USER'
    ? {viewport: {width: 1440, height: 900}}
    : {viewport: {width: 390, height: 844}, reducedMotion: 'reduce'});
  try {
    const page = await context.newPage();
    const errors = [];
    const submissions = [];
    const saved = [];
    let regenerationCancels = 0;
    let reads = 0;
    let markFirstSubmission;
    const firstSubmission = new Promise(resolve => { markFirstSubmission = resolve; });
    page.on('pageerror', error => errors.push(error.message));
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== base) return route.abort();
      const filename = path.resolve(build, '.' + (url.pathname === '/playlist' ? '/index.html' : url.pathname));
      if (!filename.startsWith(build + path.sep) || !(await stat(filename).catch(() => null))?.isFile()) {
        return route.fulfill({status: 404});
      }
      await route.fulfill({path: filename});
    });
    await context.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const method = request.method();
      let status = 200;
      let json = {};
      if (path === '/api/auth/me') json = {role, csrf_token: 'fixture'};
      else if (path === '/api/recommendation-options') json = {option_version: 'fixture', items: []};
      else if (path === '/api/regions/tree') json = {items: []};
      else if (path === '/api/c-demo/context') json = {regions: [], csrf_token: 'fixture'};
      else if (path === '/api/ads') json = {ad: null};
      else if (path === '/api/recommendation-jobs' && method === 'POST') {
        submissions.push({key: request.headers()['idempotency-key'], body: request.postDataJSON()});
        // The fixture records the accepted request, then withholds the first
        // response to simulate a timeout after registration.
        if (role !== 'USER' && submissions.length === 1) {
          markFirstSubmission();
          return;
        }
        status = 202;
        json = {job_id: 'job-fixture'};
      } else if (path === '/api/recommendation-jobs/job-fixture') {
        reads++;
        if (role !== 'USER' && reads === 1) {
          status = 503;
          json = {code: 'SERVICE_UNAVAILABLE', message: '작업 조회가 잠시 지연됐어요.'};
        } else if (role === 'USER' && reads === 1) {
          status = 404;
          json = {code: 'NOT_FOUND', message: '작업을 찾을 수 없어요.'};
        } else json = {status: 'SUCCEEDED', playlist_id: 'playlist-fixture'};
      } else if (path === '/api/playlists/playlist-fixture/regenerations' && method === 'POST') {
        status = 202;
        json = {job_id: 'regeneration-fixture'};
      } else if (path === '/api/recommendation-jobs/regeneration-fixture') {
        json = {job_id: 'regeneration-fixture', status: 'RUNNING', playlist_id: null};
      } else if (path === '/api/recommendation-jobs/regeneration-fixture/cancel' && method === 'POST') {
        regenerationCancels++;
        json = {job_id: 'regeneration-fixture', status: 'CANCELLED', playlist_id: null};
      } else if (path === '/api/playlists/playlist-fixture') {
        json = {
          playlist_id: 'playlist-fixture', family_id: 'family-fixture', summary: '시험 선곡표',
          items: [{
            track_id: 'track-fixture', track_name: '시험 선곡', artists_name: '시험 가수',
            album_name: '시험 앨범', cover_image_url: null, genre_group_codes: [], theme_codes: [],
            scores: {W: null, P: null, S: null},
          }],
          generation_method: 'RULE_BASED', is_test: true, approval_revision: 0, approved_playlist_id: null,
          context: {options: submissions[0].body, excluded_track_ids: []},
        };
      } else if (path === '/api/playlist-families/family-fixture/exclusions') {
        json = {family_id: 'family-fixture', revision: 0, excluded_track_ids: [], items: []};
      } else if (path === '/api/playlists/playlist-fixture/feedback') {
        json = {source_playlist_id: 'playlist-fixture', track_reactions: [
          {track_id: 'track-fixture', reaction: null, revision: 0},
        ], playlist_rating: {rating: null, revision: 0}};
      } else if (path === '/api/me/playlists' && method === 'POST') {
        saved.push(request.postDataJSON());
        status = role === 'USER' ? 201 : 401;
        json = {saved: status === 201};
      }
      await route.fulfill({status, json});
    });
    if (role !== 'USER') await page.clock.install();
    await page.goto(base + '/playlist');
    const generate = page.getByRole('button', {name: '선곡표 받기', exact: true});
    const playlist = page.locator('.playlist');
    const loading = playlist.locator('.playlist-loading');
    const cancel = playlist.getByRole('button', {name: '취소', exact: true});
    const track = playlist.locator('.tracks').getByText('시험 선곡', {exact: true});
    assert.equal(await loading.count(), 0, 'Idle selection must not show loading');
    await generate.click();
    if (role !== 'USER') {
      await firstSubmission;
      await loading.waitFor({state: 'visible'});
      assert.equal(await playlist.locator('.playlist-loading--compact').count(), 0);
      await page.waitForFunction(() => {
        const penguin = document.querySelector('.playlist-loading img[src="/penguin-guide-first.png"]');
        return penguin?.complete && penguin.naturalWidth > 0;
      });
      assert(await cancel.isEnabled(), 'Cancellation must stay available during first loading');
      assert.equal(await loading.evaluate(element => element.getAnimations({subtree: true}).length), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.clock.fastForward(15001);
      const resume = page.getByRole('button', {name: '같은 요청 다시 확인', exact: true});
      await resume.waitFor();
      await loading.waitFor({state: 'hidden'});
      await resume.click();
      await page.getByText('작업 조회가 잠시 지연됐어요.').waitFor();
      await loading.waitFor({state: 'hidden'});
      assert(submissions[0].key);
      assert.deepEqual(submissions, [submissions[0], submissions[0]]);
      await resume.click();
    } else {
      await page.getByText('작업을 찾을 수 없어요.').waitFor();
      await generate.click();
    }
    await track.waitFor();
    await loading.waitFor({state: 'hidden'});
    const save = page.getByRole('button', {name: '내 선곡표에 저장', exact: true});
    if (role === 'USER') {
      assert.equal(submissions.length, 2);
      assert.notEqual(submissions[0].key, submissions[1].key);
      await save.click();
      await page.getByText('내 선곡표에 저장했습니다. 마이페이지에서 확인해 주세요.').waitFor();
      assert.deepEqual(saved, [{playlist_id: 'playlist-fixture'}]);
      await playlist.getByRole('button', {name: '다른 선곡표 요청', exact: true}).click();
      await playlist.locator('.playlist-loading--compact').waitFor({state: 'visible'});
      assert(await track.isVisible(), 'The existing playlist must stay visible during regeneration');
      await cancel.click();
      await page.getByText('선곡표 준비를 취소했어요. 기존 선곡표는 유지됩니다.', {exact: true}).waitFor();
      await loading.waitFor({state: 'hidden'});
      assert(await track.isVisible(), 'The existing playlist must stay visible after cancellation');
      assert.equal(regenerationCancels, 1);
    } else {
      assert.equal(submissions.length, 2, 'An accepted job must only resume its GET');
      assert.equal(reads, 2);
      assert.equal(await save.count(), 0);
      const login = page.locator('.playlist').getByRole('link', {name: '로그인', exact: true});
      assert.equal(await login.getAttribute('href'), '/login');
      assert.deepEqual(saved, []);
    }
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.SMOKE_BROWSER || 'msedge'});
  try {
    await exercise(browser, null);
    await exercise(browser, 'USER');
    console.log('PASS: mobile reduced-motion loading, timeout and recovery, member-only saving, and compact regeneration cancellation with playlist preservation.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
