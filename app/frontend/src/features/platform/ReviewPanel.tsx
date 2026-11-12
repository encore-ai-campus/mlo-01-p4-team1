import { useEffect, useState } from 'react';
import { call, etag, raw } from './api';
import { Badge, LINKED_VENUE_FIXTURE, type Claim } from './WorkspacePanel';

export type ReviewTab = 'workspace' | 'closures';

interface ClosureItem {
  venue_id: string;
  venue_name: string;
  gu_name: string;
  status: 'OPEN' | 'CLOSURE_REPORTED' | 'CLOSED_HIDDEN';
  status_label: string;
  closure_report_count: number;
  version: number;
  last_reported_at: string | null;
  last_reviewed_at: string | null;
  decision_reason_code: string;
  notes: string;
  pending_claim_count: number;
  has_active_workspace: boolean;
}

interface ClosureSummaryCounts {
  closure_reported: number;
  closed_hidden: number;
  open: number;
}

interface VenueMetricsResponse {
  success: boolean;
  closures: {
    reported: number;
    hidden: number;
    open: number;
    total_decisions: number;
  };
  interactions: {
    likes: number;
    reviews: number;
    closure_reports: number;
    false_event_reports: number;
  };
  events: {
    active: number;
    total: number;
  };
  outbox: {
    pending: number;
    sent: number;
    failed: number;
  };
  dispatcher: {
    mode: string;
    kafka_configured: boolean;
    queue_depth: number;
    enqueued_count: number;
    processed_count: number;
    dropped_count: number;
    kafka_sent_count: number;
    kafka_failed_count: number;
  };
  catalog: {
    venue_count?: number;
    available_regions_count?: number;
    loaded_regions_count?: number;
    loaded_venues_count?: number;
    reload_count: number;
    reload_error_count: number;
    loaded_from_cache?: boolean;
  };
  lakehouse?: {
    dataset_version: string;
    db_synced_venues_count: number;
    db_synced_regions_count: number;
    s3_raw_bucket_configured: boolean;
    s3_lake_bucket_configured: boolean;
    spark_job_module: string;
    raw_prefix: string;
    lake_prefix: string;
  };
  as_of: string;
}

export interface ReviewPanelProps {
  admin: boolean;
  canWorkspace?: boolean;
  canClosures?: boolean;
  canMetrics?: boolean;
  initialTab?: ReviewTab;
}

const CLOSURE_FILTERS = [
  { key: 'ACTIVE_QUEUE', label: '제보·숨김 전체' },
  { key: 'CLOSURE_REPORTED', label: '폐업 제보 접수' },
  { key: 'CLOSED_HIDDEN', label: '폐업 확정(숨김)' },
  { key: 'OPEN', label: '정상 영업' },
  { key: 'ALL', label: '전체 보기' },
] as const;

function resolveInitialTab(
  canWorkspace: boolean,
  canClosures: boolean,
  canMetrics: boolean,
  explicit?: ReviewTab,
): ReviewTab {
  const hasClosuresTab = canClosures || canMetrics;
  if (explicit === 'closures' && hasClosuresTab) return 'closures';
  if (explicit === 'workspace' && canWorkspace) return 'workspace';
  if (typeof window !== 'undefined') {
    const params = new URLSearchParams(window.location.search);
    const tabParam = params.get('tab');
    if ((tabParam === 'closures' || window.location.hash === '#closures') && hasClosuresTab) {
      return 'closures';
    }
    if (tabParam === 'workspace' && canWorkspace) {
      return 'workspace';
    }
  }
  if (!canWorkspace && hasClosuresTab) return 'closures';
  return 'workspace';
}

export function ReviewPanel({
  admin,
  canWorkspace = true,
  canClosures = false,
  canMetrics = false,
  initialTab,
}: ReviewPanelProps) {
  const hasClosuresTab = canClosures || canMetrics;
  const [activeTab, setActiveTab] = useState<ReviewTab>(() =>
    resolveInitialTab(canWorkspace, canClosures, canMetrics, initialTab),
  );
  const [claims, setClaims] = useState<Claim[]>([]);
  const [closures, setClosures] = useState<ClosureItem[]>([]);
  const [summaryCounts, setSummaryCounts] = useState<ClosureSummaryCounts | null>(null);
  const [metrics, setMetrics] = useState<VenueMetricsResponse | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('ACTIVE_QUEUE');
  const [guFilter, setGuFilter] = useState<string>('');
  const [notesByVenue, setNotesByVenue] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  async function refreshWorkspace() {
    setClaims((await call<{ items: Claim[] }>('/admin/workspace-claims')).items);
  }

  async function refreshClosuresAndMetrics(nextStatus = statusFilter, nextGu = guFilter) {
    const query = new URLSearchParams({ status: nextStatus });
    if (nextGu.trim()) query.set('gu', nextGu.trim());

    const closuresPromise = canClosures
      ? raw<{ success: boolean; closures: ClosureItem[]; summary_counts: ClosureSummaryCounts }>(
          `/api/admin/venues/closures?${query.toString()}`,
        )
      : Promise.resolve(null);
    const metricsPromise = canMetrics
      ? raw<VenueMetricsResponse>('/api/admin/venues/metrics')
      : Promise.resolve(null);

    const [closuresData, metricsData] = await Promise.all([closuresPromise, metricsPromise]);
    if (closuresData) {
      setClosures(closuresData.closures || []);
      setSummaryCounts(closuresData.summary_counts || null);
    }
    if (metricsData) {
      setMetrics(metricsData);
    }
  }

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setError('');
    try {
      await work();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!admin) return;
    if (activeTab === 'workspace' && canWorkspace) {
      void run(refreshWorkspace);
    } else if (activeTab === 'closures' && hasClosuresTab) {
      void run(() => refreshClosuresAndMetrics(statusFilter, guFilter));
    }
  }, [admin, activeTab, canWorkspace, hasClosuresTab]);

  function selectTab(nextTab: ReviewTab) {
    setError('');
    setMessage('');
    setActiveTab(nextTab);
    if (typeof window !== 'undefined') {
      const url = new URL(window.location.href);
      url.searchParams.set('tab', nextTab);
      window.history.replaceState(null, '', url.pathname + url.search);
    }
  }

  async function resolveClosure(item: ClosureItem, decision: 'CLOSED_HIDDEN' | 'OPEN') {
    const note = (notesByVenue[item.venue_id] ?? item.notes ?? '').trim();
    const res = await raw<{ success: boolean; message: string }>(
      `/api/admin/venues/closures/${encodeURIComponent(item.venue_id)}/resolve`,
      'POST',
      {
        decision,
        notes: note || (decision === 'CLOSED_HIDDEN' ? '관리자 검수 후 폐업 숨김 처리' : '관리자 검수 후 정상 영업 복구'),
        reason_code: 'ADMIN_REVIEW',
        version: item.version,
      },
      etag(item.version),
    );
    await refreshClosuresAndMetrics(statusFilter, guFilter);
    setMessage(res.message || '매장 상태를 갱신했습니다.');
  }

  return (
    <>
      {activeTab === 'workspace' && canWorkspace ? (
        <section className="p-flow">
          <span>① 합성 등록 정보 확인</span>
          <b>→</b>
          <span>② 별도 관리권한 심사</span>
          <b>→</b>
          <span>③ 매장 OWNER 부여</span>
        </section>
      ) : (
        <section className="p-flow">
          <span>① 사용자 폐업 제보 접수</span>
          <b>→</b>
          <span>② 관리자 패스키 검수</span>
          <b>→</b>
          <span>③ 카탈로그 숨김 · Outbox 발행</span>
        </section>
      )}

      {error && (
        <div className="p-alert error" role="alert">
          {error} <a href="/admin/passkey">패스키 인증</a>
        </div>
      )}
      {message && (
        <p className="p-alert" role="status">
          {message}
        </p>
      )}

      {!admin ? (
        <section className="p-card">
          <h2>관리자 인증이 필요합니다</h2>
          <p>별도 매장 심사 권한과 최근 5분 이내 패스키 인증을 확인합니다.</p>
          <div className="p-actions">
            <a className="p-link-button" href="/platform/login">
              심사 관리자 로그인
            </a>
            <a href="/admin/passkey">패스키 인증</a>
          </div>
        </section>
      ) : (
        <>
          {hasClosuresTab && (
            <div className="p-tabs" role="tablist" aria-label="관리자 심사 메뉴">
              {canWorkspace && (
                <button
                  type="button"
                  role="tab"
                  aria-selected={activeTab === 'workspace'}
                  onClick={() => selectTab('workspace')}
                >
                  매장 관리권한 심사
                </button>
              )}
              <button
                type="button"
                role="tab"
                aria-selected={activeTab === 'closures'}
                onClick={() => selectTab('closures')}
              >
                폐업 제보 검수 · 현황
              </button>
            </div>
          )}

          {activeTab === 'workspace' && canWorkspace && (
            <section className="p-card" role="tabpanel" aria-label="매장 관리권한 심사">
              <div className="p-card-heading">
                <h2>승인 대기 신청</h2>
                <button disabled={busy} onClick={() => void run(refreshWorkspace)}>
                  새로고침
                </button>
              </div>
              {claims.length === 0 && <p className="p-muted">대기 중인 신청이 없습니다.</p>}
              {claims.map((claim) => (
                <article className="p-review" key={claim.id}>
                  <div>
                    <Badge value={claim.state} />
                    <h3>{claim.display_name}</h3>
                    {(claim.review_context.fixture_id === LINKED_VENUE_FIXTURE ||
                      claim.review_context.fixture_id.startsWith('catalog:')) && (
                      <p className="p-muted">손님 카탈로그에 연결할 매장</p>
                    )}
                    <p>
                      {claim.address} · 신청 계정 {claim.review_context.applicant_label}
                    </p>
                    <p className="p-muted">
                      합성 확인 {claim.match_result} / {claim.business_status} · 내용 v{claim.content_revision}
                    </p>
                  </div>
                  <div className="p-actions">
                    {[
                      ['APPROVE', 'DEMO_SCENARIO_CONFIRMED', '승인'],
                      ['REQUEST_MORE', 'CORRECTION_REQUIRED', '보완 요청'],
                      ['REJECT', 'AUTHORITY_UNCONFIRMED', '거절'],
                    ].map(([decision, reason_code, label]) => (
                      <button
                        key={decision}
                        disabled={busy}
                        className={decision === 'APPROVE' ? 'p-primary' : ''}
                        onClick={() =>
                          void run(async () => {
                            await call(
                              `/admin/workspace-claims/${claim.id}/decisions`,
                              'POST',
                              { decision, reason_code },
                              etag(claim.revision),
                            );
                            await refreshWorkspace();
                            setMessage('심사 결과를 저장했습니다.');
                          })
                        }
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </article>
              ))}
            </section>
          )}

          {activeTab === 'closures' && hasClosuresTab && (
            <div role="tabpanel" aria-label="폐업 제보 검수 및 현황">
              {(metrics || summaryCounts) && (
                <section className="p-summary" aria-label="매장 운영 현황 지표">
                  <div>
                    <small>폐업 제보 접수</small>
                    <strong>{metrics?.closures.reported ?? summaryCounts?.closure_reported ?? 0}</strong>
                  </div>
                  <div>
                    <small>폐업 확정(숨김)</small>
                    <strong>{metrics?.closures.hidden ?? summaryCounts?.closed_hidden ?? 0}</strong>
                  </div>
                  <div>
                    <small>누적 제보 · 허위 신고</small>
                    <strong>
                      {(metrics?.interactions.closure_reports ?? 0) +
                        (metrics?.interactions.false_event_reports ?? 0)}
                    </strong>
                    {metrics && (
                      <small>
                        폐업 {metrics.interactions.closure_reports} · 허위 이벤트{' '}
                        {metrics.interactions.false_event_reports}
                      </small>
                    )}
                  </div>
                  <div>
                    <small>Outbox · 스트리밍</small>
                    <strong>
                      {metrics
                        ? `${metrics.outbox.pending} / ${metrics.outbox.sent}`
                        : '0 / 0'}
                    </strong>
                    {metrics && (
                      <small>
                        {metrics.dispatcher.mode === 'kafka' ? 'Kafka 활성' : 'Local Fallback'} · 큐{' '}
                        {metrics.dispatcher.queue_depth} · 카탈로그{' '}
                        {(metrics.catalog.loaded_venues_count ?? metrics.catalog.venue_count ?? 0).toLocaleString()}건
                        {metrics.lakehouse
                          ? ` · DB 미러 ${metrics.lakehouse.db_synced_venues_count.toLocaleString()}건 · S3 ${
                              metrics.lakehouse.s3_raw_bucket_configured || metrics.lakehouse.s3_lake_bucket_configured
                                ? '연결됨'
                                : '대기'
                            }`
                          : ''}
                      </small>
                    )}
                  </div>
                </section>
              )}

              {canClosures ? (
                <section className="p-card">
                  <div className="p-card-heading">
                    <h2>폐업 제보 검수 목록</h2>
                    <button
                      disabled={busy}
                      onClick={() => void run(() => refreshClosuresAndMetrics(statusFilter, guFilter))}
                    >
                      새로고침
                    </button>
                  </div>

                  <div className="p-closure-filters">
                    {CLOSURE_FILTERS.map((f) => (
                      <button
                        key={f.key}
                        type="button"
                        disabled={busy}
                        className={statusFilter === f.key ? 'p-primary' : ''}
                        onClick={() => {
                          setStatusFilter(f.key);
                          void run(() => refreshClosuresAndMetrics(f.key, guFilter));
                        }}
                      >
                        {f.label}
                      </button>
                    ))}
                    <input
                      type="text"
                      value={guFilter}
                      onChange={(e) => setGuFilter(e.target.value)}
                      placeholder="자치구 검색 (예: 강남구)"
                      aria-label="자치구 필터"
                    />
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void run(() => refreshClosuresAndMetrics(statusFilter, guFilter))}
                    >
                      적용
                    </button>
                  </div>

                  {closures.length === 0 && (
                    <p className="p-muted">조건에 해당하는 폐업 제보 및 검수 내역이 없습니다.</p>
                  )}

                  {closures.map((item) => (
                    <article className="p-review" key={item.venue_id}>
                      <div>
                        <span className={`p-badge ${item.status.toLowerCase()}`}>{item.status_label}</span>
                        <h3>
                          {item.venue_name} <small className="p-muted">({item.venue_id})</small>
                        </h3>
                        <p>
                          {item.gu_name || '자치구 미상'} · 누적 제보 {item.closure_report_count}회 · 버전 v
                          {item.version}
                        </p>
                        <p className="p-muted">
                          {item.has_active_workspace ? '연결 워크스페이스 활성 · ' : ''}
                          {item.pending_claim_count > 0
                            ? `연결 심사 대기 ${item.pending_claim_count}건 · `
                            : ''}
                          최근 사유: {item.notes || '기록 없음'}
                        </p>
                      </div>
                      <div className="p-actions">
                        <input
                          type="text"
                          className="p-closure-note"
                          value={notesByVenue[item.venue_id] ?? ''}
                          onChange={(e) =>
                            setNotesByVenue((prev) => ({ ...prev, [item.venue_id]: e.target.value }))
                          }
                          placeholder="검수 메모 (선택)"
                          aria-label={`${item.venue_name} 검수 메모`}
                        />
                        {item.status !== 'CLOSED_HIDDEN' && (
                          <button
                            type="button"
                            disabled={busy}
                            className="p-primary"
                            onClick={() => void run(() => resolveClosure(item, 'CLOSED_HIDDEN'))}
                          >
                            폐업 확정(숨김)
                          </button>
                        )}
                        {item.status !== 'OPEN' && (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => void run(() => resolveClosure(item, 'OPEN'))}
                          >
                            정상 영업 복구
                          </button>
                        )}
                      </div>
                    </article>
                  ))}
                </section>
              ) : (
                <section className="p-card">
                  <h2>운영 현황</h2>
                  <p className="p-muted">
                    운영 지표 조회 전용 권한입니다. 폐업 확정·복구 처리는 매장 운영 권한(`manage_venues`)과
                    패스키 인증이 필요합니다.
                  </p>
                </section>
              )}
            </div>
          )}
        </>
      )}
    </>
  );
}
