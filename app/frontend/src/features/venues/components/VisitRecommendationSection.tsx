import { useEffect, useState, type FormEvent } from 'react';
import {
  fetchVisitOptions,
  postVisitRecommendation,
  type VisitOptions,
  type VisitRecommendation,
  type VisitVenue,
} from '../api/visitRecommendation.api';
import './visit-recommendations.css';

const seoulDateTime = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const weatherLabels: Record<string, string> = {
  CLEAR: '맑음', CLOUDY: '구름 많음', OVERCAST: '흐림', RAIN: '비', RAIN_SNOW: '비 또는 눈',
};

const hourInMs = 60 * 60 * 1000;
const won = new Intl.NumberFormat('ko-KR');

function seoulInputTime(timestamp: number) {
  return new Date(timestamp + 9 * hourInMs).toISOString().slice(0, 16);
}

function forecastText(forecast: VisitVenue['forecast']) {
  if (forecast.state === 'NO_FORECAST') return '방문 시간대 예보 없음';
  const source = forecast.provider === 'project_weather' ? '기상청 실예보' : '방문 시간대 예보';
  const label = weatherLabels[forecast.condition ?? ''];
  const condition = forecast.state === 'DRY'
    ? [label, '강수 없음'].filter(Boolean).join(' · ')
    : label ?? '강수 예상';
  const range = forecast.start_at && forecast.end_at
    ? seoulDateTime.formatRange(new Date(forecast.start_at), new Date(forecast.end_at))
    : null;
  return [source, condition, range].filter(Boolean).join(' · ');
}

export function VisitCards({ venues, onOpenVenue, distanceFrom }: {
  venues: VisitVenue[];
  onOpenVenue: (venueId: string) => void;
  distanceFrom?: string;
}) {
  return <ol className="visit-recs__cards">
    {venues.map((venue, index) => {
      const menuNames = [...new Set(venue.matched_orders.flat().map(menu => menu.name))];
      return <li key={venue.venue_id}>
        <button type="button" className="visit-recs__card"
          aria-label={venue.name + ' 매장 상세 보기'} onClick={() => onOpenVenue(venue.venue_id)}>
          <strong><span className="visit-recs__rank">{index + 1}위</span>{venue.name}</strong>
          <span>{venue.address}</span>
          {venue.distance_m != null && <span>{distanceFrom && distanceFrom + '에서 '}직선 약 {won.format(venue.distance_m)}m</span>}
          {venue.sub_category && <span>{venue.sub_category}</span>}
          {venue.budget_example
            ? <span>1인 주문 예시 {won.format(venue.budget_example.amount_krw)}원</span>
            : venue.avg_budget_krw != null && <span>1인 평균 예산 약 {won.format(venue.avg_budget_krw)}원</span>}
          <span className="visit-recs__forecast">{forecastText(venue.forecast)}</span>
          {venue.weather_match && <span>{venue.weather_match.label}</span>}
          {venue.matched_moods.length > 0 && <span>{venue.matched_moods.map(mood => mood.label).join(' · ')}</span>}
          {menuNames.length > 0
            ? <span>추천 메뉴 · {menuNames.join(', ')}</span>
            : !!venue.representative_menus?.length && <span>대표 메뉴 · {venue.representative_menus.join(', ')}</span>}
          <span className="visit-recs__detail">매장 상세 보기 <span aria-hidden="true">→</span></span>
        </button>
      </li>;
    })}
  </ol>;
}

export function VisitRecommendationSection({ onOpenVenue }: { onOpenVenue: (venueId: string) => void }) {
  const [options, setOptions] = useState<VisitOptions | null>();
  const [optionsError, setOptionsError] = useState('');
  const [optionsAttempt, setOptionsAttempt] = useState(0);
  const [region, setRegion] = useState('');
  const [initialVisitAt] = useState(() => seoulInputTime(Date.now() + hourInMs));
  const [mood, setMood] = useState('');
  const [menuIds, setMenuIds] = useState<string[]>([]);
  const [result, setResult] = useState<VisitRecommendation | null>(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    fetchVisitOptions(controller.signal).then(setOptions).catch(error => {
      if (!controller.signal.aborted) setOptionsError((error as Error).message);
    });
    return () => controller.abort();
  }, [optionsAttempt]);

  if (options === null) return null;
  const regions = options?.regions.filter(region => region.endsWith('동')) ?? [];
  const moodTags = options?.tags.filter(tag => tag.kind === 'MOOD') ?? [];
  const menuTags = options?.tags.filter(tag => tag.kind !== 'MOOD') ?? [];

  function clearResult() {
    setResult(null);
    setError('');
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const visitAt = form.get('visitAt');
    const budget = Number(form.get('budget'));
    if (!region || typeof visitAt !== 'string' || !visitAt || submitting) return;
    clearResult();
    setSubmitting(true);
    try {
      const orders = menuIds.map(id => menuTags.find(tag => tag.id === id)?.kind === 'CUISINE'
        ? { cuisine: id }
        : { types: [id] });
      const recommendation = await postVisitRecommendation({
        region, visit_at: visitAt + '+09:00', preferred_moods: mood ? [mood] : [], orders,
        budget: budget > 0 ? { min_krw: budget, max_krw: budget } : undefined,
      });
      setResult(recommendation);
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return <section className="visit-recs" aria-labelledby="visit-recs-title">
    <h2 id="visit-recs-title">방문 시간에 맞는 매장 추천</h2>
    <p>서울의 동과 방문 일시를 선택하면 해당 시간대의 예보를 반영해 추천합니다.</p>
    {optionsError ? <p role="alert">{optionsError}{' '}
      <button type="button" onClick={() => { setOptionsError(''); setOptionsAttempt(attempt => attempt + 1); }}>
        다시 불러오기
      </button>
    </p> : !options ? <p role="status">추천 조건을 불러오는 중입니다.</p>
      : regions.length === 0 ? <p>현재 추천을 제공하는 동이 없습니다.</p>
      : <form onSubmit={submit} onChange={clearResult}>
        <fieldset disabled={submitting}>
          <label>동
            <select required value={region} onChange={event => setRegion(event.target.value)}>
              <option value="">동 선택</option>
              {regions.map(region => <option key={region} value={region}>{region}</option>)}
            </select>
          </label>
          <label>방문 일시 (한국시간)
            <input name="visitAt" type="datetime-local" required min={seoulInputTime(Date.now())}
              defaultValue={initialVisitAt} />
          </label>
          <label>분위기 태그
            <select value={mood} onChange={event => setMood(event.target.value)}>
              <option value="">상관없음</option>
              {moodTags.map(tag => <option key={tag.id} value={tag.id}>{tag.label}</option>)}
            </select>
          </label>
          <label>메뉴 태그
            <select value="" disabled={menuIds.length >= 8} onChange={event => {
              const id = event.target.value;
              if (id) setMenuIds(ids => ids.includes(id) || ids.length >= 8 ? ids : [...ids, id]);
            }}>
              <option value="">{menuIds.length >= 8 ? '메뉴 태그 8개 선택됨' : '메뉴 태그 추가'}</option>
              {menuTags.filter(tag => !menuIds.includes(tag.id)).map(tag =>
                <option key={tag.id} value={tag.id}>{tag.label}</option>)}
            </select>
          </label>
          <label>1인 예산 (선택)
            <input name="budget" type="number" min="1" max="1000000" placeholder="원" />
          </label>
          {menuIds.length > 0 && <ul className="visit-recs__tags" aria-label="선택한 메뉴 태그">
            {menuIds.map(id => {
              const name = menuTags.find(tag => tag.id === id)?.label;
              return <li key={id}>
                <button type="button" aria-label={name + ' 태그 제거'} onClick={() => {
                  setMenuIds(ids => ids.filter(tagId => tagId !== id));
                  clearResult();
                }}>{name} <span aria-hidden="true">×</span></button>
              </li>;
            })}
          </ul>}
          <button className="visit-recs__submit" type="submit" disabled={!region}>
            방문 조건으로 추천 받기
          </button>
        </fieldset>
      </form>}
    {submitting && <p role="status">예보를 확인하고 매장을 찾는 중입니다.</p>}
    {error && <p role="alert">{error}</p>}
    {result && <div className="visit-recs__results">
      <h3>추천 결과</h3>
      <p>{seoulDateTime.format(new Date(result.request.visit_at))} 방문 기준</p>
      {result.ranked.length > 0 && <VisitCards venues={result.ranked} onOpenVenue={onOpenVenue} />}
      {result.ranked.length === 0 && <p>선택한 조건에 맞는 매장이 없습니다.</p>}
    </div>}
  </section>;
}
