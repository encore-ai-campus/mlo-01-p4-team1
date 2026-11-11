import React, { useEffect, useState } from 'react';
import '../venue-design.css';
import { useVenueList } from '../hooks/useVenueList';
import { useVenueDetail } from '../hooks/useVenueDetail';
import { PartnerPromoBanner } from '../components/PartnerPromoBanner';
import { GeminiConciergeSection } from '../components/GeminiConciergeSection';
import { VenueConciergeSection } from '../components/VenueConciergeSection';
import { fetchVisitOptions } from '../api/visitRecommendation.api';
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
import { weatherLabel, type Weather } from '../../region-weather/WeatherPanel';
import type { RegionOption } from '../../region-weather/RegionPicker';

export const VenueExplorePage: React.FC<{ readOnly?: boolean; showVisitRecommendations?: boolean; showServiceHeader?: boolean }> = ({
  readOnly = false,
  showVisitRecommendations = false,
  showServiceHeader = false,
}) => {
  const currentUserId = 'demo_user_final';
  const [venueMcpEnabled, setVenueMcpEnabled] = useState(false);
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

  useEffect(() => {
    const controller = new AbortController();
    fetchVisitOptions(controller.signal)
      .then(options => setVenueMcpEnabled(options !== null))
      .catch(() => {
        if (!controller.signal.aborted) setVenueMcpEnabled(false);
      });
    return () => controller.abort();
  }, []);

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
    reload: reloadVenues,
  } = useVenueList('강남구');

  useEffect(() => {
    const controller = new AbortController();
    setLiveWeather(null);
    setWeatherLoading(true);
    setWeather('');
    const target = gu.startsWith('인천 ') ? gu.slice(3) : gu;
    const province = gu.startsWith('인천 ') || ['강화군','계양구','남동구','동구','미추홀구','부평구','연수구','옹진군'].includes(gu) ? '인천' : gu.endsWith('구') ? '서울' : '경기';
    const refresh = async () => {
      try {
        const regions = await api<{items: RegionOption[]}>('/api/regions/tree', 'GET', undefined, undefined, controller.signal);
        const region = regions.items.find(item => item.province?.startsWith(province) && (item.district === target || item.district?.startsWith(`${target} `)));
        if (!region) throw new Error('지역 없음');
        const result = await api<Weather>(`/api/weather?region_id=${encodeURIComponent(region.region_id)}`, 'GET', undefined, undefined, controller.signal);
        const current = result.current;
        if (result.is_test || !current || current.status === 'stale' || current.status === 'missing' || current.pty === undefined) throw new Error('현재 날씨 없음');
        const label = weatherLabel(current.pty, current.sky);
        if (label === '확인 불가') throw new Error('현재 날씨 없음');
        if (!controller.signal.aborted) {
          setLiveWeather({label, area: region.locality || region.name, temperature: typeof current.temperature === 'number' && Number.isFinite(current.temperature) ? current.temperature : null});
          setWeather([3,7].includes(current.pty) ? 'SNOW' : current.pty > 0 ? 'RAIN' : 'CLEAR');
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
    alert(`선택 메뉴: ${menu.menu_name} (${menu.price.toLocaleString()}원) - PA ${menu.price_tier}단계`);
  };

  return (
    <div className="fitplace" style={{
      maxWidth: '1280px',
      margin: '0 auto',
      padding: '24px 20px',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      color: '#0f172a',
    }}>
      {showServiceHeader && readOnly && <div className="fitplace-header" style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap'}}>
        <a className="fitplace-logo" href="/venues">핏플레이스</a>
        <div><span className="fitplace-header-title">나에게 딱 맞는 하루</span></div>
        <div className="fp-header-actions">
          <a href={serviceAccount?.role === 'USER' ? '/platform/account' : '/platform/login'}>{serviceAccount?.role === 'USER' ? `${serviceAccount.nickname || '회원'} · 마이페이지` : '로그인'}</a>
          <div className="fp-header-menu-wrap">
            <button type="button" className="fp-header-menu-trigger" aria-label="전체 메뉴" aria-expanded={headerMenuOpen} aria-controls="fp-platform-header-menu" onClick={() => setHeaderMenuOpen(open => !open)}><span aria-hidden="true">☰</span></button>
            {headerMenuOpen && <>
              <div className="fp-header-menu-backdrop" aria-hidden="true" onClick={() => setHeaderMenuOpen(false)} />
              <nav id="fp-platform-header-menu" className="fp-header-menu" aria-label="전체 메뉴">
                <div className="fp-header-menu-title"><strong>전체 메뉴</strong><button type="button" aria-label="메뉴 닫기" onClick={() => setHeaderMenuOpen(false)}>×</button></div>
                <a href="/">음악 큐레이션 <span aria-hidden="true">›</span></a>
              </nav>
            </>}
          </div>
        </div>
      </div>}
      {!readOnly && (
        <>
          <div className="fitplace-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px', flexWrap: 'wrap', gap: '10px' }}>
            <a className="fitplace-logo" href="/venues">핏플레이스</a>
            <div style={{ flex: 1, minWidth: '320px' }}>
              <span className="fitplace-header-title">나에게 딱 맞는 하루</span>
            </div>

            <div className="fp-header-actions" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              {/* 알림 벨 팝오버 */}
              <NotificationPopover
                userId={currentUserId}
                onSelectLink={(url) => window.location.href = url}
              />

              <button type="button" className="fp-header-icon fp-profile-button" aria-label="마이페이지" onClick={() => setIsMyPageOpen(true)}>
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="7.5" r="3.5"/><path d="M4.5 20v-2.2a4.3 4.3 0 0 1 4.3-4.3h6.4a4.3 4.3 0 0 1 4.3 4.3V20"/></svg>
              </button>
              <div className="fp-header-menu-wrap">
                <button type="button" className="fp-header-menu-trigger" aria-label="전체 메뉴" aria-expanded={headerMenuOpen} aria-controls="fp-header-menu" onClick={() => setHeaderMenuOpen(open => !open)}><span aria-hidden="true">☰</span></button>
                {headerMenuOpen && <>
                  <div className="fp-header-menu-backdrop" aria-hidden="true" onClick={() => setHeaderMenuOpen(false)} />
                  <nav id="fp-header-menu" className="fp-header-menu" aria-label="전체 메뉴">
                    <div className="fp-header-menu-title"><strong>전체 메뉴</strong><button type="button" aria-label="메뉴 닫기" onClick={() => setHeaderMenuOpen(false)}>×</button></div>
                    <a href="/">음악 큐레이션 <span aria-hidden="true">›</span></a>
                    <details className="fp-owner-menu">
                      <summary>사장님 페이지 <span aria-hidden="true">⌄</span></summary>
                      <div className="fp-owner-submenu">
                        <a href="/">매장 음악 관리 <span aria-hidden="true">›</span></a>
                        <button type="button" onClick={() => { setHeaderMenuOpen(false); setIsClaimModalOpen(true); }}>사장님 등록 <span aria-hidden="true">›</span></button>
                        <button type="button" onClick={() => { setHeaderMenuOpen(false); setIsPublishEventOpen(true); }}>이벤트 발행 <span aria-hidden="true">›</span></button>
                      </div>
                    </details>
                    <a href="/login">로그인 <span aria-hidden="true">›</span></a>
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
        setGu={setGu}
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

      {!readOnly && <details className="fitplace-recommendations"><summary>나에게 맞는 장소 추천받기 <span>AI 추천</span></summary><div className="fitplace-recommendations-body">          {/* 2. 상단 파트너 매장 & 선착순 이벤트 홍보 배너 */}
          <PartnerPromoBanner
            gu={gu}
            onSelectEvent={(ev) => handleOpenEvent(ev)}
          />

          {/* 3. 방문 조건과 자연어 매장 추천 */}
          {venueMcpEnabled ? <>
            {showVisitRecommendations && <VisitRecommendationSection onOpenVenue={handleOpenDetail} />}
            <VenueConciergeSection key={gu} gu={gu} onSelectVenue={handleOpenDetail} />
          </> : <GeminiConciergeSection gu={gu} weather={weather} onSelectVenue={handleOpenDetail} />}
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
            <div style={{ textAlign: 'center', padding: '30px', color: '#ef4444' }}>
              ⚠️ {error}
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
            temperature={null}
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
            onSuccess={() => reloadVenues()}
          />

          {/* 점주 이벤트 발행 모달 */}
          <OwnerEventPublishModal
            isOpen={isPublishEventOpen}
            onClose={() => setIsPublishEventOpen(false)}
            venueId={venues[0]?.venue_id || 'venue_seoul_0001'}
            venueName={venues[0]?.venue_name || '대표 매장'}
            onSuccess={() => reloadVenues()}
          />
        </>
      )}
    </div>
  );
};
