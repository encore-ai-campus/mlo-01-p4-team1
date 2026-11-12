import React, { useEffect, useState } from 'react';
import '../venue-design.css';
import { useVenueList } from '../hooks/useVenueList';
import { useVenueDetail } from '../hooks/useVenueDetail';
import { PartnerPromoBanner } from '../components/PartnerPromoBanner';
import { GeminiConciergeSection } from '../components/GeminiConciergeSection';
import { VenueConciergeSection } from '../components/VenueConciergeSection';
import { fetchVisitOptions, type VenueRecommendationConnection } from '../api/visitRecommendation.api';
import { VisitRecommendationSection } from '../components/VisitRecommendationSection';
import { VenueFilterBar } from '../components/VenueFilterBar';
import { VenueCard } from '../components/VenueCard';
import { VenueInteractiveMap } from '../components/VenueInteractiveMap';
import { VenueDetailDrawer } from '../components/VenueDetailDrawer';
import { UserEventJoinAndQrModal } from '../components/UserEventJoinAndQrModal';
import { NotificationPopover } from '../components/NotificationPopover';
import { MyPageDrawer } from '../components/MyPageDrawer';
import { OwnerVenueClaimModal } from '../components/OwnerVenueClaimModal';
import { OwnerEventPublishModal } from '../components/OwnerEventPublishModal';
import { MenuItem, VenueEventSummary } from '../types/venue.types';
import { api } from '../../../shared/api';
import { forecastHours, weatherLabel, type Weather } from '../../region-weather/WeatherPanel';
import type { RegionOption } from '../../region-weather/RegionPicker';

const INCHEON_DISTRICTS = new Set(['강화군', '계양구', '남동구', '동구', '미추홀구', '부평구', '연수구', '옹진군']);

export function findRegionOptionForDistrict(items: RegionOption[], gu: string): RegionOption | undefined {
  const trimmed = gu.trim();
  const target = trimmed.startsWith('인천 ') ? trimmed.slice(3).trim() : trimmed;
  const province =
    trimmed.startsWith('인천 ') || INCHEON_DISTRICTS.has(trimmed)
      ? '인천'
      : trimmed.endsWith('구')
        ? '서울'
        : '경기';
  return items.find((item) => {
    const itemProvince = item.province?.trim() || '';
    const itemDistrict = item.district?.trim() || '';
    const itemName = item.name?.trim() || '';
    return (
      (itemProvince.startsWith(province) || itemName.startsWith(province)) &&
      (itemDistrict === target || itemDistrict.startsWith(`${target} `) || itemDistrict.endsWith(` ${target}`))
    );
  });
}

export const VenueExplorePage: React.FC<{ readOnly?: boolean; allowFeedback?: boolean; showVisitRecommendations?: boolean; showServiceHeader?: boolean; enableAi?: boolean }> = ({
  readOnly = false,
  allowFeedback = false,
  showVisitRecommendations = false,
  showServiceHeader = false,
  enableAi = false,
}) => {
  const currentUserId = 'demo_user_final';
  const [liveWeather, setLiveWeather] = useState<{label:string; area:string; temperature:number | null} | null>(null);
  const [weatherLoading, setWeatherLoading] = useState(true);
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false);
  const [serviceAccount, setServiceAccount] = useState<{role?: string; nickname?: string} | null>(null);
  useEffect(() => {
    if (!showServiceHeader) return;
    let active = true;
    void api<{role?: string; nickname?: string}>('/api/auth/me').then(account => { if (active) setServiceAccount(account); }).catch(() => {});
    return () => { active = false; };
  }, [showServiceHeader]);
  useEffect(() => {
    if (!headerMenuOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setHeaderMenuOpen(false); };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [headerMenuOpen]);
  const [concierge, setConcierge] = useState<VenueRecommendationConnection>({ status: 'checking' });
  const [hasSelectedDistrict, setHasSelectedDistrict] = useState(false);

  useEffect(() => {
    if ((readOnly && !enableAi) || concierge.status !== 'checking') return;
    const controller = new AbortController();
    fetchVisitOptions(controller.signal)
      .then(options => {
        if (!controller.signal.aborted) setConcierge({ status: 'ready', options });
      })
      .catch(() => {
        if (!controller.signal.aborted) setConcierge({ status: 'error' });
      });
    return () => controller.abort();
  }, [readOnly, enableAi, concierge.status]);

  const {
    gu,
    setGu,
    mapBounds,
    setMapBounds,
    weather,
    setWeather,
    category,
    setCategory,
    priceTier,
    setPriceTier,
    storeType,
    setStoreType,
    eventOnly,
    setEventOnly,
    partnerOnly,
    setPartnerOnly,
    openNow,
    setOpenNow,
    searchQuery,
    setSearchQuery,
    venues,
    totalCount,
    loading,
    error,
    errorStatus,
    reload: reloadVenues,
  } = useVenueList('강남구');
  const conciergeRegion = hasSelectedDistrict ? gu : undefined;
  const visitOptions = concierge.status === 'ready' ? concierge.options.mcp : null;

  useEffect(() => {
    const controller = new AbortController();
    setLiveWeather(null);
    setWeatherLoading(true);
    setWeather('');
    const refresh = async () => {
      try {
        const regions = await api<{items: RegionOption[]}>('/api/regions/tree', 'GET', undefined, undefined, controller.signal);
        const region = findRegionOptionForDistrict(regions.items || [], gu);
        if (!region) throw new Error('지역 없음');
        const result = await api<Weather>(`/api/weather?region_id=${encodeURIComponent(region.region_id)}`, 'GET', undefined, undefined, controller.signal);
        const slot = forecastHours(result)[0];
        if (result.is_test || !slot || slot.pty === undefined) throw new Error('현재 날씨 없음');
        const label = weatherLabel(slot.pty, slot.sky);
        if (label === '확인 불가') throw new Error('현재 날씨 없음');
        if (!controller.signal.aborted) {
          setLiveWeather({label, area: region.locality || region.name, temperature: typeof slot.temperature === 'number' && Number.isFinite(slot.temperature) ? slot.temperature : null});
          setWeather([3,7].includes(slot.pty) ? 'SNOW' : slot.pty > 0 ? 'RAIN' : 'CLEAR');
        }
      } catch { if (!controller.signal.aborted) { setLiveWeather(null); setWeather(''); } }
      finally { if (!controller.signal.aborted) setWeatherLoading(false); }
    };
    void refresh();
    const interval = window.setInterval(() => void refresh(), 30000);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [gu, setWeather]);

  // 모달 및 드로어 상태들
  const [detailVenueId, setDetailVenueId] = useState<string | null>(null);
  const { venue: detailVenue, error: detailError } = useVenueDetail(detailVenueId, weather);

  const [activeEventModal, setActiveEventModal] = useState<VenueEventSummary | null>(null);
  const [isEventModalOpen, setIsEventModalOpen] = useState<boolean>(false);

  const [isMyPageOpen, setIsMyPageOpen] = useState<boolean>(false);
  const [isClaimModalOpen, setIsClaimModalOpen] = useState<boolean>(false);
  const [isPublishEventOpen, setIsPublishEventOpen] = useState<boolean>(false);

  const handleOpenDetail = setDetailVenueId;

  const handleOpenEvent = (event: VenueEventSummary) => {
    setActiveEventModal(event);
    setIsEventModalOpen(true);
  };

  const handleSelectMenu = (menu: MenuItem) => {
    const priceText = menu.price > 0
      ? `${menu.price.toLocaleString()}원 - PA ${menu.price_tier}단계`
      : '가격 미확인 (방문 후기·공개 자료 확인 메뉴)';
    alert(`선택 메뉴: ${menu.menu_name} (${priceText})`);
  };

  return (
    <div className="fitplace" style={{
      maxWidth: '1280px',
      margin: '0 auto',
      padding: '24px 20px',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      color: '#0f172a',
    }}>
      {(!readOnly || showServiceHeader) && (
        <>
          <div className="fitplace-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px', flexWrap: 'wrap', gap: '10px' }}>
            <a className="fitplace-logo" href="/venues">핏플레이스</a>
            <div style={{ flex: 1, minWidth: '320px' }}>
              <span className="fitplace-header-title">나에게 딱 맞는 하루</span>
            </div>

            <div className="fp-header-actions" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              {/* 알림 벨 팝오버 */}
              <NotificationPopover
                unavailable={readOnly}
                userId={currentUserId}
                onSelectLink={(url) => window.location.href = url}
              />

              <button type="button" className="fp-header-icon fp-profile-button" aria-label="마이페이지" onClick={() => { if (readOnly) window.location.href = serviceAccount?.role === 'USER' ? '/platform/account' : '/platform/login'; else setIsMyPageOpen(true); }}>
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="7.5" r="3.5"/><path d="M4.5 20v-2.2a4.3 4.3 0 0 1 4.3-4.3h6.4a4.3 4.3 0 0 1 4.3 4.3V20"/></svg>
              </button>
              <div className="fp-header-menu-wrap">
                <button type="button" className="fp-header-menu-trigger" aria-label="전체 메뉴" aria-expanded={headerMenuOpen} aria-controls="fp-header-menu" onClick={() => setHeaderMenuOpen(open => !open)}><span aria-hidden="true">☰</span></button>
                {headerMenuOpen && <>
                  <div className="fp-header-menu-backdrop" aria-hidden="true" onClick={() => setHeaderMenuOpen(false)} />
                  <nav id="fp-header-menu" className="fp-header-menu" aria-label="전체 메뉴">
                    <div className="fp-header-menu-title"><strong>전체 메뉴</strong><button type="button" aria-label="메뉴 닫기" onClick={() => setHeaderMenuOpen(false)}>×</button></div>
                    <details className="fp-owner-menu">
                      <summary>관련 서비스 <span aria-hidden="true">⌄</span></summary>
                      <div className="fp-owner-submenu">
                        <a href="/platform">itda <span aria-hidden="true">›</span></a>
                        <a href="/muther">muther <span aria-hidden="true">›</span></a>
                      </div>
                    </details>
                    <details className="fp-owner-menu">
                      <summary>사장님 페이지 <span aria-hidden="true">⌄</span></summary>
                      <div className="fp-owner-submenu">
                        <a href="/">매장 음악 관리 <span aria-hidden="true">›</span></a>
                        <button type="button" onClick={() => { setHeaderMenuOpen(false); readOnly ? window.alert('현재 플랫폼에서는 해당 기능을 사용할 수 없습니다.') : setIsClaimModalOpen(true); }}>사장님 등록 <span aria-hidden="true">›</span></button>
                        <button type="button" onClick={() => { setHeaderMenuOpen(false); readOnly ? window.alert('현재 플랫폼에서는 해당 기능을 사용할 수 없습니다.') : setIsPublishEventOpen(true); }}>이벤트 발행 <span aria-hidden="true">›</span></button>
                      </div>
                    </details>
                    <a href="/platform/login">로그인 <span aria-hidden="true">›</span></a>
                  </nav>
                </>}
              </div>
            </div>
          </div>

        </>
      )}

      <div className="fitplace-explore-layout">
        <aside className="fitplace-ad-placeholder" aria-label="광고 배너 영역">광고 배너 영역</aside>
        <div className="fitplace-explore-content">
          <div className="fitplace-intro"><p>내 주변의 새로운 발견</p><h1>{gu}의 카페와 주점</h1><span>오늘의 취향에 맞는 장소를 찾아보세요.</span></div>
      {/* 4. PA 5단계 가격 & 필터 바 */}
      <VenueFilterBar
        gu={gu}
        setGu={value => { setHasSelectedDistrict(true); setGu(value); }}
        liveWeather={liveWeather}
        weatherLoading={weatherLoading}
        category={category}
        setCategory={setCategory}
        priceTier={priceTier}
        setPriceTier={setPriceTier}
        storeType={storeType}
        setStoreType={setStoreType}
        eventOnly={eventOnly}
        setEventOnly={setEventOnly}
        partnerOnly={partnerOnly}
        setPartnerOnly={setPartnerOnly}
        openNow={openNow}
        setOpenNow={setOpenNow}
        searchQuery={searchQuery}
        setSearchQuery={setSearchQuery}
        totalCount={totalCount}
      />

      {(!readOnly || enableAi) && <details className="fitplace-recommendations"><summary>나에게 맞는 장소 추천받기 <span>AI 추천</span></summary><div className="fitplace-recommendations-body">          {/* 2. 상단 파트너 매장 & 선착순 이벤트 홍보 배너 */}
          {!readOnly && <PartnerPromoBanner
            gu={gu}
            onSelectEvent={(ev) => handleOpenEvent(ev)}
          />}

          {/* 3. 방문 조건과 자연어 매장 추천 */}
          {showVisitRecommendations && visitOptions && <VisitRecommendationSection options={visitOptions} onOpenVenue={handleOpenDetail} />}
          {!readOnly && concierge.status === 'ready' && !concierge.options.mcp ? <GeminiConciergeSection gu={gu} weather={weather} onSelectVenue={handleOpenDetail} /> : <VenueConciergeSection
            defaultRegion={conciergeRegion}
            connection={concierge}
            onReconnect={() => setConcierge({ status: 'checking' })}
            onSelectVenue={handleOpenDetail}
          />}
</div></details>}
      <div className="fitplace-desktop-result-count"><span>검색 결과 <strong>{totalCount.toLocaleString()}건</strong></span>{!readOnly && <a href="/admin/venues/closures">폐업 제보 관리자 검수함 →</a>}</div>
      <div className="fitplace-results-heading"><h2>{gu}, 오늘의 발견</h2><p>매장 정보를 살펴보고 취향에 맞는 장소를 찾아보세요.</p></div>
      {/* 5. 메인 레이아웃: 좌측 인터랙티브 지도 + 우측 추천 매장 리스트 */}
        {/* 우측: 추천 매장 리스트 */}
        <div className="fitplace-list" tabIndex={0} aria-label="매장 검색 결과 목록">
          <p className="fitplace-count">매장 {totalCount.toLocaleString()}곳 {detailVenueId && <span> · 선택한 매장: {venues.find(v => v.venue_id === detailVenueId)?.venue_name || ''}</span>}</p>
          {loading && (
            <div style={{ textAlign: 'center', padding: '40px', color: '#64748b' }}>
              매장 및 Two-Tower 딥러닝 추천 결과를 불러오는 중입니다...
            </div>
          )}

          {error && (
            <div style={{ textAlign: 'center', padding: '30px', color: '#ef4444' }} role="alert">
              <p>⚠️ {error}</p>
              <div style={{ display: 'flex', justifyContent: 'center', gap: '10px', marginTop: '10px', flexWrap: 'wrap' }}>
                {errorStatus === 401 && <a href="/platform/login">로그인하기</a>}
                {errorStatus === 403 && <a href="/platform/account">사용자 이용 유형 확인</a>}
                <button type="button" onClick={() => void reloadVenues()}>다시 불러오기</button>
              </div>
            </div>
          )}

          {!loading && !error && venues.length === 0 && <p className="fitplace-empty">조건에 맞는 매장이 없습니다. 검색 조건을 바꿔 보세요.</p>}
          {!loading && !error && venues.map((venue) => (
            <div key={venue.venue_id}>
              <VenueCard
                selected={detailVenueId === venue.venue_id}
                venue={venue}
                userId={currentUserId}
                readOnly={readOnly}
        allowFeedback={allowFeedback}
                onSelectVenue={handleOpenDetail}
                onSelectMenu={handleSelectMenu}
              />
            </div>
          ))}
        </div>
      </div>

        <div className={`fitplace-map-card${mapBounds ? " has-bounds" : ""}`} style={{
          backgroundColor: '#ffffff',
          borderRadius: '16px',
          padding: '20px',
          border: '1px solid #e2e8f0',
          boxShadow: '0 2px 10px rgba(0, 0, 0, 0.05)',
          position: 'sticky',
          top: '20px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
            <div style={{ fontWeight: 800, fontSize: '16px', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span>지도에서 매장 찾기</span><span className="fp-map-count">{totalCount.toLocaleString()}곳</span>
              {mapBounds && (
                <button
                  type="button"
                  onClick={() => setMapBounds(null)}
                  style={{
                    fontSize: '11px',
                    fontWeight: 700,
                    padding: '3px 8px',
                    borderRadius: '6px',
                    backgroundColor: '#eff6ff',
                    color: '#2563eb',
                    border: '1px solid #bfdbfe',
                    cursor: 'pointer',
                  }}
                >
                  현 지도 영역 기준 ({totalCount.toLocaleString()}곳) · 구 전체 보기 ✕
                </button>
              )}
            </div>
            {!readOnly && (
              <a href="/admin/venues/closures" style={{ fontSize: '12px', color: '#dc2626', textDecoration: 'none', fontWeight: 600 }}>
                폐업 제보 관리자 검수함 &rarr;
              </a>
            )}
          </div>

          <p className="fp-map-hint">핀을 선택하면 매장 상세를 확인해요.</p>
          <VenueInteractiveMap
            temperature={liveWeather?.temperature ?? null}
            weather={weather}
            gu={gu}
            venues={venues}
            selectedVenueId={detailVenueId}
            onSelectVenue={handleOpenDetail}
            onBoundsChange={(bounds) => {
              setMapBounds(bounds);
            }}
          />
        </div>

      </div>

      {detailVenueId && !detailVenue && (
        <p role={detailError ? 'alert' : 'status'}>
          {detailError ?? '매장 상세를 불러오는 중입니다…'}
        </p>
      )}
      <VenueDetailDrawer
        venue={detailVenue}
        isOpen={detailVenueId !== null}
        onClose={() => setDetailVenueId(null)}
        readOnly={readOnly}
        allowFeedback={allowFeedback}
        onOpenEventModal={handleOpenEvent}
        userId={currentUserId}
        currentWeather={weather}
      />

      {!readOnly && (
        <>
          {/* 유저 이벤트 참여 & 현장 QR 인증 모달 (발표 시연용 원클릭 시뮬레이션 포함) */}
          <UserEventJoinAndQrModal
            event={activeEventModal}
            isOpen={isEventModalOpen}
            onClose={() => {
              setIsEventModalOpen(false);
              reloadVenues();
            }}
            userId={currentUserId}
            onStockUpdated={() => reloadVenues()}
          />

          {/* 마이페이지 드로어 */}
          <MyPageDrawer
            isOpen={isMyPageOpen}
            onClose={() => setIsMyPageOpen(false)}
            userId={currentUserId}
            onSelectVenue={(id) => handleOpenDetail(id)}
          />

          {/* 점주 매장 연결 모달 */}
          <OwnerVenueClaimModal
            isOpen={isClaimModalOpen}
            onClose={() => setIsClaimModalOpen(false)}
            onSuccess={(venueId) => {
              reloadVenues();
              if (venueId) handleOpenDetail(venueId);
            }}
          />

          {/* 점주 이벤트 발행 모달 */}
          <OwnerEventPublishModal
            isOpen={isPublishEventOpen}
            onClose={() => setIsPublishEventOpen(false)}
            venueId={detailVenue?.venue_id || venues[0]?.venue_id || 'venue_seoul_0001'}
            venueName={detailVenue?.venue_name || venues[0]?.venue_name || '대표 매장'}
            onSuccess={() => reloadVenues()}
          />
        </>
      )}
    </div>
  );
};
