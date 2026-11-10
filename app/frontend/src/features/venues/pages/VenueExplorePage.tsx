import React, { useEffect, useState } from 'react';
import '../venue-design.css';
import { useVenueList } from '../hooks/useVenueList';
import { useVenueDetail } from '../hooks/useVenueDetail';
import { GatewayRoleTabs } from '../components/GatewayRoleTabs';
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

export const VenueExplorePage: React.FC<{ readOnly?: boolean; showVisitRecommendations?: boolean }> = ({
  readOnly = false,
  showVisitRecommendations = false,
}) => {
  const currentUserId = 'demo_user_final';
  const [venueMcpEnabled, setVenueMcpEnabled] = useState(false);

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
      {!readOnly && (
        <>
          <div className="fitplace-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px', flexWrap: 'wrap', gap: '10px' }}>
            <a className="fitplace-logo" href="/venues">핏플레이스</a>
            <div style={{ flex: 1, minWidth: '320px' }}>
              <GatewayRoleTabs currentTab="USER" /><span className="fitplace-header-title">카페·주점 탐색</span>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              {/* 사장님 매장 등록 / 이벤트 발행 버튼 */}
              <button
                type="button"
                onClick={() => setIsClaimModalOpen(true)}
                style={{
                  padding: '8px 12px',
                  backgroundColor: '#f8fafc',
                  border: '1px solid #cbd5e1',
                  borderRadius: '10px',
                  fontSize: '12px',
                  fontWeight: 700,
                  cursor: 'pointer',
                }}
              >
                🏪 점주 매장 연결
              </button>

              <button
                type="button"
                onClick={() => setIsPublishEventOpen(true)}
                style={{
                  padding: '8px 12px',
                  backgroundColor: '#fee2e2',
                  color: '#dc2626',
                  border: '1px solid #fecdd3',
                  borderRadius: '10px',
                  fontSize: '12px',
                  fontWeight: 800,
                  cursor: 'pointer',
                }}
              >
                🎉 점주 이벤트 발행
              </button>

              {/* 알림 벨 팝오버 */}
              <NotificationPopover
                userId={currentUserId}
                onSelectLink={(url) => window.location.href = url}
              />

              {/* 마이페이지 버튼 */}
              <button
                type="button"
                onClick={() => setIsMyPageOpen(true)}
                style={{
                  padding: '8px 14px',
                  backgroundColor: '#2563eb',
                  color: '#ffffff',
                  border: 'none',
                  borderRadius: '10px',
                  fontSize: '13px',
                  fontWeight: 700,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                }}
              >
                <span>👤</span>
                <span>마이페이지</span>
              </button>
            </div>
          </div>

          {/* 2. 상단 파트너 매장 & 선착순 이벤트 홍보 배너 */}
          <PartnerPromoBanner
            gu={gu}
            onSelectEvent={(ev) => handleOpenEvent(ev)}
          />

          {/* 3. 방문 조건과 자연어 매장 추천 */}
          {venueMcpEnabled ? <>
            {showVisitRecommendations && <VisitRecommendationSection onOpenVenue={handleOpenDetail} />}
            <VenueConciergeSection key={gu} gu={gu} onSelectVenue={handleOpenDetail} />
          </> : <GeminiConciergeSection gu={gu} weather={weather} onSelectVenue={handleOpenDetail} />}
        </>
      )}

      {/* 4. PA 5단계 가격 & 필터 바 */}
      <VenueFilterBar
        gu={gu}
        setGu={setGu}
        weather={weather}
        setWeather={setWeather}
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

      <div className="fitplace-results-heading"><h2>{gu}, 오늘의 발견</h2><p>매장 정보를 살펴보고 취향에 맞는 장소를 찾아보세요.</p></div>
      {/* 5. 메인 레이아웃: 좌측 인터랙티브 지도 + 우측 추천 매장 리스트 */}
      <div className="fitplace-results" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px', alignItems: 'start' }}>
        {/* 좌측 인터랙티브 매장 핀 지도 */}
        <div className="fitplace-map-card" style={{
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
                🛡️ 폐업 제보 관리자 검수함 &rarr;
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

        {/* 우측: 추천 매장 리스트 */}
        <div className="fitplace-list">
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
