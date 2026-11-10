import { useState, type FormEvent } from 'react';
import { postVisitRecommendation, type VisitRecommendation } from '../api/visitRecommendation.api';
import { VisitCards } from './VisitRecommendationSection';

const quickQueries = [
  '성수카페거리 레트로 카페', '성수카페거리 2만원 이하 아늑한 술집',
  '을지로 1만원 이하 술집', '을지로 아늑한 카페',
];

function noMatchText(result: VisitRecommendation, area: string) {
  switch (result.empty_reason) {
    case 'MULTIPLE_LOCATIONS': return '검색할 장소를 한 곳만 입력해 주세요.';
    case 'AMBIGUOUS_LOCATION': return '같은 이름의 장소가 있습니다. 검색할 곳을 골라 주세요.';
    case 'LOCATION_CONFLICT': return '입력한 자치구와 동 이름이 일치하는 곳을 찾지 못했습니다.';
    case 'STATION_REQUIRED': return '출구 번호만으로는 위치를 찾기 어렵습니다. 역 이름을 입력해 주세요.';
    case 'STATION_UNSUPPORTED': return '입력한 역을 위치 자료에서 찾지 못했습니다. 역 이름을 확인해 주세요.';
    case 'PLACE_UNSUPPORTED': return '입력한 장소를 위치 자료에서 찾지 못했습니다. 가까운 역이나 동네 이름으로 검색해 주세요.';
    case 'EXIT_UNSUPPORTED': return '입력한 출구를 찾지 못했습니다. 역 이름만으로도 주변을 검색할 수 있습니다.';
    case 'REGION_UNSUPPORTED': return '입력한 지역을 위치 자료에서 찾지 못했습니다.';
    case 'NO_REGISTERED_VENUES': return area + '에서 등록된 시연 매장을 찾지 못했습니다.';
    case 'NO_NEARBY_VENUES': return area + '에 등록된 시연 매장이 없습니다.';
    case 'NO_CATEGORY': {
      const category = result.request.venue_category;
      return area + ' 시연 데이터에서 '
        + (category === 'PUB' ? '술집으로' : category === 'CAFE' ? '카페로' : '카페나 술집으로')
        + ' 확인된 매장이 없습니다.';
    }
    case 'NO_MOOD': return area + '에서 입력한 분위기가 등록된 매장을 찾지 못했습니다.';
    case 'NO_BUDGET': return area + '에서 확인된 1인 예산이 입력 범위에 드는 매장을 찾지 못했습니다.';
    case 'NO_OUTDOOR': return area + '에서 야외 좌석이 확인된 매장을 찾지 못했습니다.';
    case 'NO_MENU': return area + '에서 입력한 메뉴가 확인된 매장을 찾지 못했습니다.';
    case 'CHECK_WEATHER_SEATING': return '방문 시간대 날씨를 기준으로 야외 좌석 이용을 확인한 매장이 없습니다.';
    default: return area + '에서 입력한 조건에 맞는 매장을 찾지 못했습니다.';
  }
}

export function VenueConciergeSection({ gu, onSelectVenue }: {
  gu: string;
  onSelectVenue: (venueId: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<VisitRecommendation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  function updateQuery(value: string) {
    setQuery(value);
    setResult(null);
    setError('');
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = query.trim();
    if (!prompt || loading) return;
    setResult(null);
    setError('');
    setLoading(true);
    try {
      setResult(await postVisitRecommendation({
        query: prompt, region: gu, limit: 5,
      }));
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setLoading(false);
    }
  }

  const location = result?.request.location;
  const area = location?.radius_m
    ? location.label + (location.kind === 'EXIT' ? '' : ' 주변') + ' 직선 ' + location.radius_m / 1000 + 'km 이내'
    : location?.label ?? result?.request.region ?? gu;
  const nearbyCafe = result?.empty_reason === 'NO_CATEGORY' && result.request.venue_category === 'PUB'
    && location ? location.label + (location.radius_m ? ' 근처 카페' : ' 카페') : null;

  return <section className="visit-recs" aria-labelledby="venue-concierge-title">
    <h2 id="venue-concierge-title">조건으로 매장 추천</h2>
    <p>역·동네·주요 장소 이름과 원하는 조건을 입력해 주세요. 장소를 생략하면 {gu}에서 찾습니다.</p>
    <form className="visit-recs__query" onSubmit={submit}>
      <ul className="visit-recs__tags" aria-label="예시 검색어">
        {quickQueries.map(prompt => <li key={prompt}>
          <button type="button" disabled={loading} onClick={() => updateQuery(prompt)}>{prompt}</button>
        </li>)}
      </ul>
      <label>원하는 매장
        <input type="text" maxLength={240} value={query} disabled={loading}
          placeholder="예: 을지로 1만원 이하 술집"
          onChange={event => updateQuery(event.target.value)} />
      </label>
      <button className="visit-recs__submit" type="submit" disabled={loading || !query.trim()}>
        매장 추천 받기
      </button>
    </form>

    {loading && <p role="status">조건에 맞는 매장을 찾는 중입니다.</p>}
    {error && <p role="alert">{error}</p>}
    {result && <div className="visit-recs__results" aria-live="polite">
      <h3>추천 결과</h3>
      {location && <p>{area}{location.source_url && <> · <a href={location.source_url} target="_blank" rel="noreferrer">{location.source_label}</a></>}</p>}
      {location?.unmatched_exit != null && <p>{location.unmatched_exit}번 출구 좌표를 확인할 수 없어 {location.label} 주변을 검색했습니다.</p>}
      {result.ranked.length > 0 ? <>
        <p>{!location && area + '에서 '}조건에 맞는 {result.counts.ranked}곳 중 상위 {result.ranked.length}곳입니다. 1인 주문 예시는 공개 메뉴 단가 기준입니다.</p>
        <VisitCards venues={result.ranked} onOpenVenue={onSelectVenue} distanceFrom={location?.label} />
      </> : <>
        <p>{noMatchText(result, area)}</p>
        {!!result.location_candidates?.length && <ul className="visit-recs__tags" aria-label="같은 이름의 장소 후보">
          {result.location_candidates.map(candidate => <li key={candidate.label}>
            <button type="button" onClick={() => updateQuery(candidate.query)}>{candidate.label}</button>
          </li>)}
        </ul>}
        {nearbyCafe && <button type="button" className="visit-recs__submit" onClick={() => updateQuery(nearbyCafe)}>
          {nearbyCafe}로 검색어 바꾸기
        </button>}
      </>}
    </div>}
  </section>;
}
