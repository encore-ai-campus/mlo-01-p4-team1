import React, { useEffect, useRef, useState } from 'react';
import { VenueItem } from '../types/venue.types';
import { MapBounds } from '../hooks/useVenueList';

interface VenueInteractiveMapProps {
  gu: string;
  weather: string;
  temperature: number | null;
  venues: VenueItem[];
  selectedVenueId: string | null;
  onSelectVenue: (venueId: string) => void;
  onBoundsChange?: (bounds: MapBounds, detectedGu?: string) => void;
}

// 서울 25개 자치구 및 경기 30개 시·군 중심 좌표 (매장 0개일 때 폴백 및 지도 이동 감지용)
const GU_CENTER_COORDS: Record<string, [number, number]> = {
  강남구: [37.5172, 127.0286],
  서초구: [37.4837, 127.0324],
  송파구: [37.5145, 127.1059],
  강동구: [37.5301, 127.1238],
  마포구: [37.5563, 126.9226],
  은평구: [37.6176, 126.9227],
  서대문구: [37.5791, 126.9368],
  용산구: [37.5326, 126.9905],
  종로구: [37.5730, 126.9794],
  중구: [37.5641, 126.9979],
  성동구: [37.5446, 127.0439],
  광진구: [37.5385, 127.0823],
  영등포구: [37.5264, 126.8962],
  관악구: [37.4784, 126.9516],
  동작구: [37.5124, 126.9393],
  강서구: [37.5510, 126.8495],
  양천구: [37.5170, 126.8665],
  구로구: [37.4954, 126.8874],
  금천구: [37.4569, 126.8955],
  성북구: [37.5894, 127.0167],
  동대문구: [37.5744, 127.0398],
  중랑구: [37.6065, 127.0927],
  노원구: [37.6542, 127.0568],
  도봉구: [37.6688, 127.0471],
  강북구: [37.6396, 127.0257],
  // 경기도 30개 시·군
  수원시: [37.2636, 127.0286],
  성남시: [37.4200, 127.1267],
  안양시: [37.3943, 126.9568],
  광명시: [37.4785, 126.8646],
  고양시: [37.6584, 126.8320],
  부천시: [37.5034, 126.7660],
  용인시: [37.2411, 127.1776],
  남양주시: [37.6360, 127.2165],
  시흥시: [37.3800, 126.8030],
  의정부시: [37.7381, 127.0337],
  광주시: [37.4294, 127.2551],
  군포시: [37.3617, 126.9352],
  하남시: [37.5393, 127.2149],
  구리시: [37.5943, 127.1296],
  의왕시: [37.3449, 126.9683],
  파주시: [37.7600, 126.7800],
  김포시: [37.6152, 126.7156],
  화성시: [37.1995, 126.8313],
  평택시: [36.9921, 127.1129],
  오산시: [37.1499, 127.0772],
  이천시: [37.2720, 127.4350],
  안성시: [37.0080, 127.2797],
  양주시: [37.7853, 127.0458],
  포천시: [37.8949, 127.2003],
  동두천시: [37.9035, 127.0605],
  과천시: [37.4292, 126.9876],
  여주시: [37.2983, 127.6374],
  양평군: [37.4912, 127.4876],
  가평군: [37.8315, 127.5096],
  연천군: [38.0966, 127.0750],
  // 인천광역시 10개 구·군
  남동구: [37.4473, 126.7314],
  부평구: [37.5070, 126.7218],
  미추홀구: [37.4636, 126.6505],
  연수구: [37.4102, 126.6783],
  계양구: [37.5374, 126.7377],
  강화군: [37.7465, 126.4881],
  동구: [37.4739, 126.6432],
  옹진군: [37.4463, 126.6368],
  '인천 서구': [37.5454, 126.6759],
  '인천 중구': [37.4737, 126.6217],
};

const escapeHtml = (value: string) => value.replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character] || character));

declare global {
  interface Window {
    L?: any;
  }
}

function pinLabel(venue: VenueItem) {
  const category = venue.category === 'CAFE' ? '카페' : '주점';
  const event = venue.active_event && !venue.active_event.is_ended ? ', 이벤트 진행 중' : '';
  return '지도에서 ' + category + ' ' + venue.venue_name + event + ' 상세 보기';
}

function CoordinatePins({ venues, onSelectVenue, notice = '지도 타일 연결 대기 · 매장 위치 핀' }: Pick<VenueInteractiveMapProps, 'venues' | 'onSelectVenue'> & { notice?: string }) {
  const pins = venues.filter(({ latitude, longitude }) => Number.isFinite(latitude) && Number.isFinite(longitude)).slice(0, 40);
  if (!pins.length) return null;

  const latitudes = pins.map(({ latitude }) => latitude);
  const longitudes = pins.map(({ longitude }) => longitude);
  const south = Math.min(...latitudes);
  const north = Math.max(...latitudes);
  const west = Math.min(...longitudes);
  const east = Math.max(...longitudes);
  const latRange = Math.max(north - south, 0.002);
  const lngRange = Math.max(east - west, 0.002);

  return (
    <div aria-label="좌표로 표시한 매장 위치" role="group" style={{
      position: 'absolute', inset: 0, backgroundColor: '#e8eef5',
      backgroundImage: 'linear-gradient(#fff 1px, transparent 1px), linear-gradient(90deg, #fff 1px, transparent 1px)',
      backgroundSize: '36px 36px',
    }}>
      <p style={{ margin: '12px', fontSize: '12px', color: '#334155' }}>{notice}</p>
      {pins.map((venue) => (
        <button
          key={venue.venue_id}
          type="button"
          aria-label={pinLabel(venue)}
          data-venue-id={venue.venue_id}
          onClick={() => onSelectVenue(venue.venue_id)}
          style={{
            position: 'absolute', transform: 'translate(-50%, -50%)',
            left: 50 + (venue.longitude - (east + west) / 2) / lngRange * 80 + '%',
            top: 50 - (venue.latitude - (north + south) / 2) / latRange * 76 + '%',
            borderRadius: '999px', border: '1px solid #ffffff', padding: '4px 9px',
            backgroundColor: '#2563eb', color: '#ffffff', cursor: 'pointer', fontSize: '11px',
          }}
        >
          {venue.venue_name}
        </button>
      ))}
    </div>
  );
}

export const VenueInteractiveMap: React.FC<VenueInteractiveMapProps> = ({
  gu,
  weather,
  temperature,
  venues,
  selectedVenueId,
  onSelectVenue,
  onBoundsChange,
}) => {
  const mapContainerRef = useRef<HTMLDivElement | null>(null);
  const mapInstanceRef = useRef<any>(null);
  const markersLayerRef = useRef<any>(null);
  const lastGuRef = useRef<string>('');
  const suppressMoveEventRef = useRef<boolean>(false);
  const [locationError, setLocationError] = useState('');
  const [leafletReady, setLeafletReady] = useState(Boolean(window.L));
  const [leafletUnavailable, setLeafletUnavailable] = useState(false);
  const [tileBlocked, setTileBlocked] = useState(false);
  const cameraTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [pendingBounds, setPendingBounds] = useState<{ bounds: MapBounds; nearestGu: string } | null>(null);

  useEffect(() => {
    if (window.L) {
      setLeafletReady(true);
      return;
    }
    if (!document.querySelector('link[href*="leaflet.css"]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
      link.crossOrigin = '';
      document.head.appendChild(link);
    }
    const existing = document.querySelector<HTMLScriptElement>('script[src*="leaflet.js"]');
    const script = existing ?? document.createElement('script');
    const ready = () => setLeafletReady(Boolean(window.L));
    const unavailable = () => setLeafletUnavailable(true);
    script.addEventListener('load', ready);
    script.addEventListener('error', unavailable);
    const fallback = setTimeout(unavailable, 3500);

    if (!existing) {
      script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
      script.crossOrigin = '';
      document.head.appendChild(script);
    }
    return () => {
      clearTimeout(fallback);
      script.removeEventListener('load', ready);
      script.removeEventListener('error', unavailable);
    };
  }, []);

  useEffect(() => () => {
    clearTimeout(cameraTimerRef.current);
    mapInstanceRef.current?.remove();
    mapInstanceRef.current = null;
    markersLayerRef.current = null;
    lastGuRef.current = '';
    suppressMoveEventRef.current = false;
  }, []);

  useEffect(() => {
    const L = window.L;
    if (!leafletReady || !L || !mapContainerRef.current) return;

    // 1. 최초 1회 Leaflet 지도 인스턴스 생성
    if (!mapInstanceRef.current) {
      const defaultCenter = GU_CENTER_COORDS[gu] || [37.5172, 127.0286];
      const map = L.map(mapContainerRef.current, {
        center: defaultCenter,
        zoom: 14,
        zoomControl: false,
      });

      // 기본 타일은 CARTO Voyager CDN을 사용하고, 오류 시 OSM 타일 -> 좌표 기반 핀으로 순차 폴백한다.
      const tileSources = [
        {
          url: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
          options: {
            subdomains: 'abcd',
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
            maxZoom: 19,
            referrerPolicy: 'strict-origin-when-cross-origin',
          },
        },
        {
          url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
          options: {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            maxZoom: 19,
            referrerPolicy: 'strict-origin-when-cross-origin',
          },
        },
      ];

      let activeLayer: any = null;
      const attachTileLayer = (sourceIndex: number) => {
        if (activeLayer) {
          map.removeLayer(activeLayer);
          activeLayer = null;
        }
        const cfg = tileSources[sourceIndex];
        if (!cfg) {
          setTileBlocked(true);
          return;
        }
        let loaded = 0;
        let errors = 0;
        let switched = false;
        const layer = L.tileLayer(cfg.url, cfg.options);
        layer.on('tileload', () => {
          loaded += 1;
          setTileBlocked(false);
        });
        layer.on('tileerror', () => {
          errors += 1;
          if (!switched && loaded === 0 && errors >= 4) {
            switched = true;
            attachTileLayer(sourceIndex + 1);
          }
        });
        activeLayer = layer.addTo(map);
      };
      attachTileLayer(0);

      L.control.zoom({ position: 'bottomright' }).addTo(map);
      markersLayerRef.current = L.layerGroup().addTo(map);
      mapInstanceRef.current = map;

      // 지도 이동 후 사용자가 재검색 버튼을 누르면 새 영역을 적용한다.
      const handleUserMapMove = () => {
        if (suppressMoveEventRef.current) return;
        const b = map.getBounds();
        const center = map.getCenter();
        const newBounds: MapBounds = {
          min_lat: Number(b.getSouth().toFixed(6)),
          max_lat: Number(b.getNorth().toFixed(6)),
          min_lng: Number(b.getWest().toFixed(6)),
          max_lng: Number(b.getEast().toFixed(6)),
        };

        // 현재 지도 중심 좌표에서 가장 가까운 자치구 판별
        let nearestGu = gu;
        let minDist = Infinity;
        Object.entries(GU_CENTER_COORDS).forEach(([gName, [clat, clng]]) => {
          const d = (clat - center.lat) ** 2 + (clng - center.lng) ** 2;
          if (d < minDist) {
            minDist = d;
            nearestGu = gName;
          }
        });

        setPendingBounds({ bounds: newBounds, nearestGu });
          };

      map.on('dragend', handleUserMapMove);
      map.on('zoomend', handleUserMapMove);
    }

    const map = mapInstanceRef.current;
    const markersLayer = markersLayerRef.current;
    markersLayer.clearLayers();

    // 2. 유효한 위경도를 가진 상위 매장 핀(최대 40개) 렌더링
    const validVenues = venues
      .filter((v) => Number.isFinite(v.latitude) && Number.isFinite(v.longitude))
      .slice(0, 40);

    const boundsPoints: Array<[number, number]> = [];

    validVenues.forEach((v, index) => {
      const isSelected = v.venue_id === selectedVenueId;

      const bgColor = isSelected ? '#ff383e' : '#ffffff';

      const borderColor = isSelected ? '#ff383e' : '#e8e4e0';
      const scale = isSelected ? 'scale(1.15)' : 'scale(1)';
      const pinNumber = String(index + 1).padStart(2, '0');
      const shortName =
        v.venue_name.length > 8 ? `${v.venue_name.slice(0, 8)}…` : v.venue_name;

      const pin = document.createElement('span');
      Object.assign(pin.style, {
        display: 'inline-flex', alignItems: 'center', background: bgColor,
        color: isSelected ? '#fff0ef' : '#242424', padding: '4px 9px', borderRadius: '9999px',
        fontSize: '11px', fontWeight: '800', border: '2px solid ' + borderColor,
        boxShadow: '0 3px 8px rgba(15, 23, 42, 0.35)', whiteSpace: 'nowrap',
        transform: scale, transition: 'transform 0.15s ease',
      });
      pin.className = 'fp-marker';
      pin.textContent = pinNumber + ' ' + shortName;

      const customIcon = L.divIcon({
        className: 'custom-venue-pin',
        html: pin,
        iconSize: [90, 26],
        iconAnchor: [45, 13],
      });
      const marker = L.marker([v.latitude, v.longitude], { icon: customIcon, title: pinLabel(v) });
      marker.on('click', () => onSelectVenue(v.venue_id));

      const tooltip = document.createElement('div');
      tooltip.textContent = v.venue_name + ' (' + v.dong_name + ') · ' + v.match_score + '% 매칭 · 1인 평균 ' +
        (v.avg_price || 10000).toLocaleString() + '원';
      marker.bindTooltip(tooltip, { direction: 'top', offset: [0, -10] });
      marker.addTo(markersLayer);
      const markerElement = marker.getElement();
      markerElement?.setAttribute('aria-label', pinLabel(v));
      markerElement?.setAttribute('data-venue-id', v.venue_id);
      boundsPoints.push([v.latitude, v.longitude]);
    });

    // 3. 드롭다운에서 자치구(gu)가 변경된 경우에만 지도 카메라를 해당 구로 자동 이동 (사용자가 직접 드래그한 경우 카메라 고정!)
    if (lastGuRef.current !== gu) {
      lastGuRef.current = gu;
      setPendingBounds(null);
      suppressMoveEventRef.current = true;
      if (boundsPoints.length > 1) {
        map.fitBounds(boundsPoints, { padding: [36, 36], maxZoom: 15 });
      } else if (boundsPoints.length === 1) {
        map.setView(boundsPoints[0], 15);
      } else {
        const fallbackCenter = GU_CENTER_COORDS[gu] || [37.5172, 127.0286];
        map.setView(fallbackCenter, 14);
      }
      clearTimeout(cameraTimerRef.current);
      cameraTimerRef.current = setTimeout(() => {
        suppressMoveEventRef.current = false;
      }, 450);
    }
  }, [leafletReady, gu, venues, selectedVenueId, onSelectVenue]);

  useEffect(() => {
    const container = mapContainerRef.current;
    if (!leafletReady || !container || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => mapInstanceRef.current?.invalidateSize({ pan: false }));
    observer.observe(container);
    return () => observer.disconnect();
  }, [leafletReady]);

  return (
    <div className="fitplace-map" style={{ position: 'relative', width: '100%', height: '520px', borderRadius: '12px', overflow: 'hidden', border: '1px solid #cbd5e1' }}>
      <div ref={mapContainerRef} role="region" aria-label="매장 지도" style={{ width: '100%', height: '100%', backgroundColor: '#e8eef5' }} />
      {((!leafletReady && leafletUnavailable) || tileBlocked) && (
        <CoordinatePins
          venues={venues}
          onSelectVenue={onSelectVenue}
          notice={tileBlocked ? '외부 지도 타일 서버 응답 제한 · 매장 위치 좌표 뷰' : '지도 타일 연결 대기 · 매장 위치 핀'}
        />
      )}
      <button className="fitplace-location" type="button" aria-label="내 위치로 이동" onClick={() => {
        if (!navigator.geolocation) { setLocationError('위치 기능을 사용할 수 없습니다.'); return; }
        navigator.geolocation.getCurrentPosition((position) => { setLocationError(''); mapInstanceRef.current?.setView([position.coords.latitude, position.coords.longitude], 15); }, () => setLocationError('위치를 확인할 수 없습니다. 위치 권한을 확인해 주세요.'));
      }}>⌖</button>
      {locationError && <div className="fitplace-location-error" role="alert">{locationError}</div>}
      {
        <button
          type="button"
          className="fp-map-research"
          disabled={!pendingBounds}
          title={!pendingBounds ? '지도를 이동하면 이 지역을 다시 검색할 수 있습니다' : undefined}
          onClick={() => {
            if (onBoundsChange && pendingBounds) onBoundsChange(pendingBounds.bounds, pendingBounds.nearestGu);
            setPendingBounds(null);
          }}
          style={{
            position: 'absolute',
            bottom: '20px',
            left: '50%',
            transform: 'translateX(-50%)',
            zIndex: 550,
            backgroundColor: '#ff383e',
            color: '#fff0ef',
            border: '1px solid #ff383e',
            borderRadius: '9999px',
            padding: '8px 16px',
            fontSize: '17px',
            fontWeight: 800,
            boxShadow: '0 4px 12px rgba(15, 23, 42, 0.35)',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            textAlign: 'center',
            gap: '6px',
          }}
        >
          <span>이 지역 재검색</span>
        </button>
      }
      <div className="fitplace-map-legend"
        style={{
          position: 'absolute',
          bottom: '12px',
          left: '12px',
          zIndex: 500,
          backgroundColor: 'rgba(255, 255, 255, 0.94)',
          padding: '6px 12px',
          borderRadius: '8px',
          fontSize: '11px',
          fontWeight: 700,
          color: '#1e293b',
          boxShadow: '0 2px 6px rgba(0,0,0,0.15)',
          display: 'flex',
          alignItems: 'center',
          gap: '10px',
        }}
      >
        <span>📍 {(gu.startsWith('인천 ') || ['남동구', '부평구', '미추홀구', '연수구', '계양구', '강화군', '동구', '옹진군'].includes(gu)) ? `인천광역시 ${gu.replace('인천 ', '')}` : (gu.endsWith('구') ? `서울특별시 ${gu}` : `경기도 ${gu}`)} 실제 위경도 핀 ({Math.min(venues.length, 40)}개 표시)</span>
        <span style={{ color: '#2563eb' }}>☕ 카페</span>
        <span style={{ color: '#7c3aed' }}>🍺 주점</span>
        <span style={{ color: '#dc2626' }}>🔥 이벤트 진행중</span>
      </div>
    </div>
  );
};
