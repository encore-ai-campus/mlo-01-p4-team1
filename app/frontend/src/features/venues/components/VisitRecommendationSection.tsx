import { useEffect, useState, type FormEvent } from 'react';
import {
  postVisitRecommendation,
  type VisitCatalogMetadata,
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

export function formatVenueVisitTime(value: string) {
  const time = Date.parse(value);
  return Number.isFinite(time) ? seoulDateTime.format(time) : null;
}

function forecastExpiry(forecast: VisitVenue['forecast']) {
  const end = Date.parse(forecast.end_at ?? '');
  if (forecast.state === 'NO_FORECAST' || !Number.isFinite(end)) return null;
  if (forecast.provider !== 'project_weather') return end;

  const issuedAt = Date.parse(forecast.issued_at ?? '');
  // Match the project weather freshness window in venue_forecast.py.
  return Number.isFinite(issuedAt) ? Math.min(end, issuedAt + 2 * hourInMs) : null;
}

export function describeVisitForecast(forecast: VisitVenue['forecast'], now: number) {
  if (forecast.reason === 'STALE_OR_FUTURE_SEGMENT') {
    return { text: '예보 만료 또는 유효성 확인 불가 · 날씨 미반영', available: false };
  }
  if (forecast.reason === 'PAST_VISIT') return { text: '지난 방문 시간 · 예보 미반영', available: false };
  if (forecast.state === 'NO_FORECAST') return { text: '방문 시간대 예보 없음 · 날씨 미반영', available: false };

  const start = Date.parse(forecast.start_at ?? '');
  const end = Date.parse(forecast.end_at ?? '');
  const expiry = forecastExpiry(forecast);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end || expiry === null) {
    return { text: '방문 시간대 예보 확인 불가', available: false };
  }
  if (expiry <= now) return { text: '예보 만료 · 최신 예보는 다시 조회해 주세요.', available: false };

  const source = forecast.provider === 'project_weather' ? '기상청 실예보' : '방문 시간대 예보';
  const label = weatherLabels[forecast.condition ?? ''];
  const condition = forecast.state === 'DRY'
    ? [label, '강수 없음'].filter(Boolean).join(' · ')
    : label ?? '강수 예상';
  const range = seoulDateTime.formatRange(new Date(start), new Date(end));
  return { text: [source, condition, range].join(' · '), available: true };
}

export function venueCatalogNotice(metadata?: VisitCatalogMetadata) {
  if (metadata?.preview_only !== true) return null;
  const source = metadata.source_basis?.includes('CSV') ? '팀 CSV 시연 자료' : '매장 시연 자료';
  return source + ' · 메뉴·가격은 확인 필요'
    + (metadata.budget_basis ? ' · 1인 평균 예산은 참고값' : '');
}

export function VisitCards({ venues, onOpenVenue, distanceFrom, previewCatalog = false }: {
  venues: VisitVenue[];
  onOpenVenue?: (venueId: string) => void;
  distanceFrom?: string;
  previewCatalog?: boolean;
}) {
  const Card = onOpenVenue ? 'button' : 'article';
  const [now, setNow] = useState(Date.now);
  const nextExpiry = Math.min(...venues.map(venue => forecastExpiry(venue.forecast))
    .filter((expiry): expiry is number => expiry !== null && expiry > now));
  useEffect(() => {
    if (!Number.isFinite(nextExpiry)) return;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.max(1, nextExpiry - Date.now()));
    return () => window.clearTimeout(timer);
  }, [nextExpiry]);

  return <ol className="visit-recs__cards">
    {venues.map((venue, index) => {
      const menuNames = [...new Set(venue.matched_orders.flat().map(menu => menu.name))];
      const forecast = describeVisitForecast(venue.forecast, now);
      return <li key={venue.venue_id}>
        <Card className="visit-recs__card" {...(onOpenVenue ? {
          type: 'button',
          'aria-label': venue.name + ' 매장 상세 보기',
          onClick: () => onOpenVenue(venue.venue_id),
        } : {})}>
          <strong><span className="visit-recs__rank">{index + 1}위</span>{venue.name}</strong>
          <span>{venue.address}</span>
          {venue.distance_m != null && <span>{distanceFrom && distanceFrom + '에서 '}직선 약 {won.format(venue.distance_m)}m</span>}
          {venue.sub_category && <span>{venue.sub_category}</span>}
          {!previewCatalog && venue.budget_example
            ? <span>1인 주문 예시 {won.format(venue.budget_example.amount_krw)}원
              {venue.paired_menu && <> · {venue.budget_example.items.map(item => item.name).join(' + ')}</>}
            </span>
            : venue.avg_budget_krw != null && <span>1인 평균 예산 약 {won.format(venue.avg_budget_krw)}원</span>}
          <span className={forecast.available ? 'visit-recs__forecast' : 'visit-recs__forecast--unavailable'}>{forecast.text}</span>
          {forecast.available && venue.weather_match && <span>{venue.weather_match.label}</span>}
          {venue.matched_moods.length > 0 && <span>{venue.matched_moods.map(mood => mood.label).join(' · ')}</span>}
          {menuNames.length > 0
            ? <span>추천 메뉴 · {menuNames.join(', ')}</span>
            : !!venue.representative_menus?.length && <span>대표 메뉴 · {venue.representative_menus.join(', ')}</span>}
          {venue.paired_menu && <span>곁들일 메뉴 · {venue.paired_menu.name}</span>}
          {onOpenVenue && <span className="visit-recs__detail">매장 상세 보기 <span aria-hidden="true">→</span></span>}
        </Card>
      </li>;
    })}
  </ol>;
}

export function VisitRecommendationSection({ options, onOpenVenue }: {
  options: VisitOptions;
  onOpenVenue: (venueId: string) => void;
}) {
  const [region, setRegion] = useState('');
  const [initialVisitAt] = useState(() => seoulInputTime(Date.now() + hourInMs));
  const [mood, setMood] = useState('');
  const [menuIds, setMenuIds] = useState<string[]>([]);
  const [result, setResult] = useState<VisitRecommendation | null>(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const regions = options.regions;
  const catalogMetadata = result?.catalog_metadata ?? options.catalog_metadata;
  const catalogNotice = venueCatalogNotice(catalogMetadata);
  const moodTags = options.tags.filter(tag => tag.kind === 'MOOD');
  const menuTags = options.tags.filter(tag => tag.kind !== 'MOOD');

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
    <p>지원 지역과 방문 일시를 선택해 주세요. 확인된 시간대 예보를 추천에 반영합니다.</p>
    {regions.length === 0 ? <p>현재 추천을 제공하는 지역이 없습니다.</p>
      : <form onSubmit={submit} onChange={clearResult}>
        <fieldset disabled={submitting}>
          <label>지역
            <select required value={region} onChange={event => setRegion(event.target.value)}>
              <option value="">지역 선택</option>
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
    {result && <div className="visit-recs__results" aria-live="polite">
      <h3>{result.ranked.length > 0 ? '추천 결과' : '조건에 맞는 매장 없음'}</h3>
      <p>{formatVenueVisitTime(result.request.visit_at) ?? '선택한 시간'} 방문 기준</p>
      {catalogNotice && <p>{catalogNotice}</p>}
      {result.ranked.length > 0
        ? <VisitCards venues={result.ranked}
          onOpenVenue={result.catalog_source === 'PUBLIC_SNAPSHOT' ? undefined : onOpenVenue}
          previewCatalog={catalogMetadata?.preview_only} />
        : <p>지역이나 방문 조건을 바꿔 다시 조회해 주세요.</p>}
    </div>}
  </section>;
}
