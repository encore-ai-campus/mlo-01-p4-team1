import React, { useEffect, useRef, useState } from 'react';
import { VenueItem } from '../types/venue.types';
import { MapBounds } from '../hooks/useVenueList';

interface VenueInteractiveMapProps {
  gu: string;
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

declare global {
  interface Window {
    L?: any;
  }
}

export const VenueInteractiveMap: React.FC<VenueInteractiveMapProps> = ({
  gu,
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
  const onBoundsChangeRef = useRef(onBoundsChange);
  onBoundsChangeRef.current = onBoundsChange;
  const [leafletReady, setLeafletReady] = useState<boolean>(Boolean(window.L));
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
    let script = document.querySelector('script[src*="leaflet.js"]') as HTMLScriptElement | null;
    if (!script) {
      script = document.createElement('script');
      script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
      script.crossOrigin = '';
      script.onload = () => setLeafletReady(true);
      document.head.appendChild(script);
    } else {
      script.addEventListener('load', () => setLeafletReady(true));
    }
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
        zoomControl: true,
      });

      // OpenStreetMap 공식 한글 도로명 타일 레이어 (API 키 불필요, 무제한 무료)
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19,
      }).addTo(map);

      markersLayerRef.current = L.layerGroup().addTo(map);
      mapInstanceRef.current = map;

      // 사용자가 지도를 드래그(Pan)하거나 줌(Zoom)했을 때 현재 보고 있는 맵 영역(BBox) 기준으로 매장 자동 갱신
      let debounceTimer: any = null;
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
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
          if (onBoundsChangeRef.current) {
            onBoundsChangeRef.current(newBounds, nearestGu);
          }
        }, 350);
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

    validVenues.forEach((v) => {
      const isCafe = v.category === 'CAFE';
      const hasEvent = Boolean(v.active_event && !v.active_event.is_ended);
      const isSelected = v.venue_id === selectedVenueId;

      const bgColor = hasEvent
        ? '#dc2626'
        : isCafe
        ? '#2563eb'
        : '#7c3aed';

      const borderColor = isSelected ? '#facc15' : '#ffffff';
      const scale = isSelected ? 'scale(1.15)' : 'scale(1)';
      const iconEmoji = hasEvent ? '🔥' : isCafe ? '☕' : '🍺';
      const shortName =
        v.venue_name.length > 8 ? `${v.venue_name.slice(0, 8)}…` : v.venue_name;

      const customIcon = L.divIcon({
        className: 'custom-venue-pin',
        html: `
          <div style="
            display: inline-flex;
            align-items: center;
            gap: 4px;
            background: ${bgColor};
            color: #ffffff;
            padding: 4px 9px;
            border-radius: 9999px;
            font-size: 11px;
            font-weight: 800;
            border: 2px solid ${borderColor};
            box-shadow: 0 3px 8px rgba(15, 23, 42, 0.35);
            white-space: nowrap;
            transform: ${scale};
            transition: transform 0.15s ease;
          ">
            <span>${iconEmoji}</span>
            <span>${shortName}</span>
          </div>
        `,
        iconSize: [90, 26],
        iconAnchor: [45, 13],
      });

      const marker = L.marker([v.latitude, v.longitude], { icon: customIcon });
      marker.on('click', () => {
        onSelectVenue(v.venue_id);
      });

      marker.bindTooltip(
        `<div style="font-size:12px;font-weight:700;">
          ${v.venue_name} (${v.dong_name})<br/>
          <span style="color:#2563eb;">🎯 ${v.match_score}% 매칭</span> · 1인 평균 ${(v.avg_price || 10000).toLocaleString()}원
        </div>`,
        { direction: 'top', offset: [0, -10] }
      );

      marker.addTo(markersLayer);
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
      setTimeout(() => {
        suppressMoveEventRef.current = false;
      }, 450);
    }
  }, [leafletReady, gu, venues, selectedVenueId]);

  return (
    <div style={{ position: 'relative', width: '100%', height: '520px', borderRadius: '12px', overflow: 'hidden', border: '1px solid #cbd5e1' }}>
      <div ref={mapContainerRef} style={{ width: '100%', height: '100%' }} />
      {pendingBounds && (
        <button
          type="button"
          onClick={() => {
            if (onBoundsChange) {
              onBoundsChange(pendingBounds.bounds, pendingBounds.nearestGu);
            }
          }}
          style={{
            position: 'absolute',
            top: '14px',
            left: '50%',
            transform: 'translateX(-50%)',
            zIndex: 550,
            backgroundColor: '#0f172a',
            color: '#ffffff',
            border: '2px solid #38bdf8',
            borderRadius: '9999px',
            padding: '8px 16px',
            fontSize: '12px',
            fontWeight: 800,
            boxShadow: '0 4px 12px rgba(15, 23, 42, 0.35)',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
          }}
        >
          <span>🔄 현 지도 영역 내 매장 탐색 중 ({pendingBounds.nearestGu} 일대)</span>
        </button>
      )}
      <div
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
