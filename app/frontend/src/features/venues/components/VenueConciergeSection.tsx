import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError } from '../../../shared/api';
import { postVenueChat, type VenueChatResponse, type VenueRecommendationConnection, type VenueTag } from '../api/visitRecommendation.api';
import { canOpenVenueDetail, formatVenueVisitTime, venueCatalogNotice, VisitCards } from './VisitRecommendationSection';
import { withMinimumRecommendationDelay, RecommendationLoading } from './RecommendationLoading';

const quickQueries = [
  '인천 연수구 카페', '경기 수원시 팔달구 카페', '서교동 17,000원 이하 나베 술집',
  '인천 연수구 2차 술집',
];

function summarizeConditions(result: VenueChatResponse, tags: VenueTag[], previewCatalog: boolean) {
  const conditions = result.llm.interpreted_conditions;
  const request = result.mcp?.request;
  const area = request?.location?.label ?? request?.region ?? conditions?.region;
  const category = request?.venue_category ?? conditions?.venue_category;
  const budget = request?.budget ?? conditions?.budget;
  const min = budget?.min_krw;
  const max = budget?.max_krw;
  const amount = min != null && max != null
    ? (min === max ? min.toLocaleString('ko-KR') + '원' : min.toLocaleString('ko-KR') + '~' + max.toLocaleString('ko-KR') + '원')
    : min != null ? min.toLocaleString('ko-KR') + '원 이상'
      : max != null ? max.toLocaleString('ko-KR') + '원 이하' : null;
  const budgetMode = request?.budget_mode ?? conditions?.budget_mode;
  const tagLabel = (id: string) => tags.find(tag => tag.id === id)?.label ?? id;
  const required = (request?.required_moods ?? conditions?.required_moods ?? []).map(tagLabel);
  const preferred = (request?.preferred_moods ?? conditions?.preferred_moods ?? []).map(tagLabel);
  const purposes = (request?.visit_purposes ?? conditions?.visit_purposes ?? []).map(tagLabel);
  const lowerBudget = request?.prefer_lower_budget ?? conditions?.prefer_lower_budget;
  const orders = (conditions?.orders ?? [])
    .map(order => order.cuisine ? tagLabel(order.cuisine) : (order.types ?? []).map(tagLabel).join(' 또는 '))
    .filter(Boolean);
  const visitTime = conditions?.visit_at && formatVenueVisitTime(request?.visit_at ?? conditions.visit_at);

  return [
    area, category === 'CAFE' ? '카페' : category === 'PUB' ? '술집' : null,
    amount && (previewCatalog ? '1인 평균 예산 ' : '1인 예산 ') + amount + (budgetMode === 'PREFER' ? ' 참고' : ''),
    lowerBudget ? '낮은 1인 예산 비교' : null,
    required.length ? '필수 분위기 ' + required.join(', ') : null,
    preferred.length ? '선호 분위기 ' + preferred.join(', ') : null,
    purposes.length ? '방문 목적 ' + purposes.join(', ') : null,
    orders.length ? '메뉴 ' + orders.join(' / ') : null,
    visitTime && '방문 ' + visitTime,
  ].filter(Boolean).join(' · ');
}

function chatFailure(error: unknown) {
  if (error instanceof ApiError) {
    if (error.code === 'LLM_NOT_CONFIGURED') {
      return { message: 'AI 연결 설정 필요. 설정을 확인한 뒤 다시 연결해 주세요.', reconnect: true };
    }
    if (error.code === 'LLM_AUTH_FAILED' || error.code === 'CSRF_FAILED' || error.code === 'ORIGIN_NOT_ALLOWED' || error.status === 404) {
      return { message: 'AI 연결을 다시 확인해 주세요. 입력한 질문은 그대로 있습니다.', reconnect: true };
    }
    if (error.code === 'LLM_TIMEOUT' || error.code === 'REQUEST_TIMEOUT' || error.status === 504) {
      return { message: 'AI 답변 시간이 초과됐습니다. 입력한 질문으로 다시 시도할 수 있습니다.', reconnect: false };
    }
    return { message: error.message, reconnect: false };
  }
  return { message: 'AI 질문에 응답하지 못했습니다. 잠시 후 다시 시도해 주세요.', reconnect: false };
}

export function VenueConciergeSection({ defaultRegion, connection, onReconnect, onSelectVenue }: {
  defaultRegion?: string;
  connection: VenueRecommendationConnection;
  onReconnect: () => void;
  onSelectVenue: (venueId: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<VenueChatResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ReturnType<typeof chatFailure> | null>(null);
  const requestInProgress = useRef<AbortController | null>(null);

  useEffect(() => () => requestInProgress.current?.abort(), []);

  const options = connection.status === 'ready' ? connection.options : null;
  const provider = options?.llm;
  const providerName = provider?.provider === 'codex_cli' && provider.mode === 'LOCAL_TEST' ? 'Codex 로컬 테스트'
    : provider?.provider === 'gemini' && provider.mode === 'API' ? 'Gemini API' : null;
  const configured = Boolean(options?.mcp && provider?.configured === true);
  const canAsk = configured && !error?.reconnect;
  const needsReconnect = connection.status === 'error' || (connection.status === 'ready' && !canAsk);

  function updateQuery(value: string) {
    setQuery(value);
    setResult(null);
    setError(current => current?.reconnect ? current : null);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = query.trim();
    if (!prompt || !canAsk || requestInProgress.current) return;

    const controller = new AbortController();
    requestInProgress.current = controller;
    setResult(null);
    setError(null);
    setLoading(true);
    try {
      const response = await withMinimumRecommendationDelay(postVenueChat(prompt, defaultRegion, controller.signal));
      if (!controller.signal.aborted) setResult(response);
    } catch (error) {
      if (!controller.signal.aborted) setError(chatFailure(error));
    } finally {
      requestInProgress.current = null;
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  const mcp = result?.mcp;
  const location = mcp?.request.location;
  const catalogMetadata = mcp?.catalog_metadata ?? options?.mcp?.catalog_metadata;
  const catalogNotice = venueCatalogNotice(catalogMetadata);
  const previewCatalog = catalogMetadata?.preview_only === true;
  const interpreted = result ? summarizeConditions(result, options?.mcp?.tags ?? [], previewCatalog) : '';
  const unverified = [...new Set([
    ...(mcp?.unverified_conditions ?? []),
    ...(result?.llm.interpreted_conditions?.unverified_conditions ?? []),
  ].map(condition => condition.text))];
  const needsConfirmation = result?.llm.status === 'NEEDS_CONFIRMATION'
    || mcp?.status === 'NEEDS_CHECK' || unverified.length > 0;
  const matched = result?.llm.status === 'OK' && mcp?.status === 'OK' && mcp.ranked.length > 0 && !needsConfirmation;
  const resultTitle = needsConfirmation ? '조건 확인 필요' : matched ? '추천 결과' : '조건에 맞는 매장 없음';

  return <section className="visit-recs fitplace-concierge" aria-labelledby="venue-concierge-title">
    <header>
      <h2 id="venue-concierge-title">AI 매장 질문</h2>
      <p>어떤 분위기를 찾고 있나요? 지역과 원하는 조건을 적어 주세요.</p>
      {defaultRegion && <p>질문에 지역을 쓰지 않으면 선택한 {defaultRegion}에서 찾습니다.</p>}
    </header>
    <div id="venue-concierge-provider" role="status">
      {connection.status === 'checking' ? <p>매장 추천 연결을 확인하는 중입니다.</p>
        : connection.status === 'unavailable' ? <p>현재 AI 매장 추천을 사용할 수 없습니다.</p>
        : connection.status === 'error' ? <p>매장 추천에 연결하지 못했습니다.</p>
          : !options?.mcp ? <p>이 환경에서 매장 추천 연결을 사용할 수 없습니다.</p>
            : !configured ? <p><strong>AI 연결 설정 필요</strong>{providerName && ' · ' + providerName} · 방문 조건으로 매장을 조회할 수 있습니다.</p>
              : <p><strong>{providerName ?? 'AI 질문 사용 가능'}</strong></p>}
    </div>
    {catalogNotice && <p>{catalogNotice}</p>}
    <form className="visit-recs__query" onSubmit={submit} aria-busy={loading} aria-describedby="venue-concierge-provider">
      <ul className="visit-recs__tags" aria-label="예시 질문">
        {quickQueries.map(prompt => <li key={prompt}>
          <button type="button" disabled={loading} onClick={() => updateQuery(prompt)}>{prompt}</button>
        </li>)}
      </ul>
      <div className="fitplace-concierge-input visit-recs__input-row">
        <label>원하는 매장
          <input type="text" maxLength={240} value={query} disabled={loading}
            placeholder="예: 인천 연수구에서 조용한 카페 찾아줘"
            onChange={event => updateQuery(event.target.value)} />
        </label>
        <button className="visit-recs__submit" type="submit" disabled={!canAsk || loading || !query.trim()}>
          {error && canAsk ? '다시 질문하기' : 'AI에게 질문하기'}
        </button>
      </div>
    </form>

    {loading && <RecommendationLoading />}
    {error && <p role="alert">{error.message}</p>}
    {needsReconnect && <button type="button" className="visit-recs__submit" onClick={() => {
      setError(null);
      onReconnect();
    }}>다시 연결</button>}
    {canAsk && !loading && !result && !error && (
      <div className="fp-ai-empty">
        <strong>추천 결과</strong>
        <p>질문을 선택하거나 직접 입력하면 추천 결과가 여기에 표시됩니다.</p>
      </div>
    )}
    {result && <div className="visit-recs__results fp-ai-result" aria-live="polite">
      <h3>{resultTitle}</h3>
      <p>{needsConfirmation && unverified.length ? '매장 자료로 확인할 수 없는 조건이 있습니다.'
        : result.llm.message}</p>
      {interpreted && <p><strong>해석한 조건</strong> · {interpreted}</p>}
      {unverified.length > 0 && <p><strong>조회 한계</strong> · {unverified.join(' · ')} 조건은 매장 자료에서 확인할 수 없습니다.</p>}
      {matched && <>
        <p>조건에 맞는 {mcp.counts.ranked}곳 중 상위 {mcp.ranked.length}곳입니다.</p>
        {mcp.catalog_source === 'PUBLIC_SNAPSHOT' && !catalogNotice && <p>매장 자료에 포함된 메뉴와 예산을 표시합니다. 매장별 실제 메뉴판은 확인이 필요합니다.</p>}
        {location?.source_url && <p>위치 자료 · <a href={location.source_url} target="_blank" rel="noreferrer">{location.source_label}</a></p>}
        <VisitCards venues={mcp.ranked}
          onOpenVenue={canOpenVenueDetail(mcp.catalog_source, mcp.catalog_metadata) ? onSelectVenue : undefined}
          previewCatalog={previewCatalog}
          distanceFrom={location?.label} />
      </>}
      {!!mcp?.location_candidates?.length && <ul className="visit-recs__tags" aria-label="같은 이름의 장소 후보">
        {mcp.location_candidates.map(candidate => <li key={candidate.label}>
          <button type="button" onClick={() => updateQuery(candidate.query)}>{candidate.label}</button>
        </li>)}
      </ul>}
    </div>}
  </section>;
}
