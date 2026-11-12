import {renderToStaticMarkup} from 'react-dom/server';
import {afterEach, expect, it, vi} from 'vitest';
import {CatalogPublicationForm} from './CatalogPublicationForm';
import {createSongRequestClient, type AdminTask, type SongCatalogPublication} from './client';

const task: AdminTask = {
  id: 'task-1', request_item_id: 'item-1', status: 'REGISTERED', track_name: '신청 곡',
  artist_name: '아티스트', source_ref: 'https://www.youtube.com/watch?v=ABCDEFGHIJK',
  revision: 5, is_current: true, result: null,
  registration: {track_id: 'youtube:ABCDEFGHIJK', tags: {genre: ['JAZZ', 'ROCK_PUNK_METAL'], mood: ['CALM']}},
};

afterEach(() => vi.unstubAllGlobals());

it('preselects exact service genres and leaves combined tags and audio for manual review', () => {
  const html = renderToStaticMarkup(<CatalogPublicationForm client={createSongRequestClient()} task={task}
    recommendationGenres={{JAZZ: '재즈', ROCK: '록·펑크', METAL: '메탈'}} onPublished={vi.fn()}/>);

  expect(html).toContain('aria-pressed="true">재즈</button>');
  expect(html).toContain('aria-pressed="false">록·펑크</button>');
  expect(html).toContain('aria-pressed="false">메탈</button>');
  expect(html).toContain('록·펑크·메탈');
  expect(html).not.toContain('음향 특성 출처');
  expect(html).not.toContain('곡 빠르기');
  expect(html).toContain('<button type="submit">추천 카탈로그에 발행</button>');
});

it('requires at least one service genre when all approved genres need mapping', () => {
  const html = renderToStaticMarkup(<CatalogPublicationForm client={createSongRequestClient()} task={task}
    recommendationGenres={{ROCK: '록·펑크', METAL: '메탈'}} onPublished={vi.fn()}/>);

  expect(html).toContain('disabled="">추천 카탈로그에 발행</button>');
});

it('uses the publication route and retains the request key when retrying the same body', async () => {
  const published = {...task, revision: 6, registration: {...task.registration, catalog_version: 'catalog-v2'},
    video_review_pending: true};
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json({code: 'TEMPORARILY_UNAVAILABLE', message: '연결 확인이 필요합니다.'}, {status: 503}))
    .mockResolvedValueOnce(Response.json(published));
  vi.stubGlobal('fetch', fetch);
  vi.stubGlobal('document', {cookie: 'csrftoken=fixture-csrf'});
  const client = createSongRequestClient();
  const body: SongCatalogPublication = {expected_revision: 5, genre_groups: ['JAZZ']};

  await expect(client.publishSong(task.id, body)).rejects.toMatchObject({status: 503});
  await expect(client.publishSong(task.id, body)).resolves.toEqual(published);
  const [path, request] = fetch.mock.calls[1];
  expect(path).toBe('/api/phase4/admin/tasks/task-1/publish');
  expect(request.method).toBe('POST');
  expect(request.headers['X-CSRFToken']).toBe('fixture-csrf');
  expect(request.headers['Idempotency-Key']).toBeTruthy();
  expect(request.headers['Idempotency-Key']).toBe(fetch.mock.calls[0][1].headers['Idempotency-Key']);
  expect(JSON.parse(request.body)).toEqual(body);
});
