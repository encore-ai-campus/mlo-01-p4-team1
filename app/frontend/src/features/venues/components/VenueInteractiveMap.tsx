import React, { useEffect, useRef } from 'react';
import { VenueItem } from '../types/venue.types';

interface VenueInteractiveMapProps {
  gu: string;
  venues: VenueItem[];
  selectedVenueId: string | null;
  onSelectVenue: (venueId: string) => void;
}

// 서울 25개 자치구 중심 좌표 (매장 0개일 때 폴백용)
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
}) => {
  const mapContainerRef = useRef<HTMLDivElement | null>(null);
  const mapInstanceRef = useRef<any>(null);
  const markersLayerRef = useRef<any>(null);
  const [leafletReady, setLeafletReady] = React.useState<boolean>(Boolean(window.L));

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

    // 3. 핀들의 실제 위경도 분포에 맞춰 지도 카메라 자동 이동 (fitBounds)
    if (boundsPoints.length > 1) {
      map.fitBounds(boundsPoints, { padding: [36, 36], maxZoom: 16 });
    } else if (boundsPoints.length === 1) {
      map.setView(boundsPoints[0], 15);
    } else {
      const fallbackCenter = GU_CENTER_COORDS[gu] || [37.5172, 127.0286];
      map.setView(fallbackCenter, 14);
    }
  }, [leafletReady, gu, venues, selectedVenueId]);

  return (
    <div style={{ position: 'relative', width: '100%', height: '520px', borderRadius: '12px', overflow: 'hidden', border: '1px solid #cbd5e1' }}>
      <div ref={mapContainerRef} style={{ width: '100%', height: '100%' }} />
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
        <span>📍 서울특별시 {gu} 실제 위경도 핀 ({Math.min(venues.length, 40)}개 표시)</span>
        <span style={{ color: '#2563eb' }}>☕ 카페</span>
        <span style={{ color: '#7c3aed' }}>🍺 주점</span>
        <span style={{ color: '#dc2626' }}>🔥 이벤트 진행중</span>
      </div>
    </div>
  );
};
