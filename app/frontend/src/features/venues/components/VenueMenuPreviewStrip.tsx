import React from 'react';
import { MenuItem } from '../types/venue.types';

interface VenueMenuPreviewStripProps {
  menus: MenuItem[];
  onSelectMenu?: (menu: MenuItem) => void;
}

export const VenueMenuPreviewStrip: React.FC<VenueMenuPreviewStripProps> = ({ menus, onSelectMenu }) => {
  if (!menus || menus.length === 0) {
    return null;
  }

  return (
    <div style={{ marginTop: '12px' }}>
      <div style={{
        fontSize: '11px',
        fontWeight: 700,
        color: '#64748b',
        marginBottom: '6px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}>
        <span>🍽️ 추천 메뉴 프리뷰 (좌우 스크롤)</span>
        <span style={{ fontSize: '10px', color: '#94a3b8' }}>총 {menus.length}개</span>
      </div>

      <div style={{
        display: 'flex',
        gap: '8px',
        overflowX: 'auto',
        paddingBottom: '6px',
        scrollbarWidth: 'thin',
      }}>
        {menus.map((menu) => (
          <div
            key={menu.menu_id}
            onClick={(e) => {
              e.stopPropagation();
              if (onSelectMenu) onSelectMenu(menu);
            }}
            style={{
              flex: '0 0 auto',
              width: '130px',
              backgroundColor: '#f8fafc',
              border: '1px solid #e2e8f0',
              borderRadius: '8px',
              padding: '8px 10px',
              cursor: 'pointer',
              transition: 'background-color 0.2s',
            }}
            onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#f1f5f9')}
            onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = '#f8fafc')}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '3px' }}>
              {menu.is_signature && (
                <span style={{
                  backgroundColor: '#fef3c7',
                  color: '#b45309',
                  fontSize: '9px',
                  fontWeight: 800,
                  padding: '1px 4px',
                  borderRadius: '4px',
                }}>
                  대표
                </span>
              )}
              {menu.weather_match === 'RAIN' && <span style={{ fontSize: '10px' }}>☔</span>}
              {menu.weather_match === 'CLEAR' && <span style={{ fontSize: '10px' }}>☀️</span>}
            </div>

            <div style={{
              fontSize: '12px',
              fontWeight: 700,
              color: '#1e293b',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }} title={menu.menu_name}>
              {menu.menu_name}
            </div>

            <div style={{ fontSize: '11px', color: '#2563eb', fontWeight: 600, marginTop: '2px' }}>
              {menu.price.toLocaleString()}원
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
