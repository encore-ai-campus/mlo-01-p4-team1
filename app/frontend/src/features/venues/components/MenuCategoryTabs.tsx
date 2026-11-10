import React, { useState } from 'react';
import { MenuTab, MenuItem } from '../types/venue.types';

interface MenuCategoryTabsProps {
  menuTabs: MenuTab[];
  onSelectMenu?: (menu: MenuItem) => void;
}

export const MenuCategoryTabs: React.FC<MenuCategoryTabsProps> = ({ menuTabs, onSelectMenu }) => {
  const [activeTabIdx, setActiveTabIdx] = useState<number>(0);

  if (!menuTabs || menuTabs.length === 0) {
    return (
      <div style={{ padding: '20px', textAlign: 'center', color: '#94a3b8' }}>
        등록된 메뉴 정보가 없습니다.
      </div>
    );
  }

  const currentTab = menuTabs[activeTabIdx] || menuTabs[0];

  return (
    <div>
      <h4 style={{ fontSize: '16px', fontWeight: 800, margin: '0 0 12px 0', color: '#242424' }}>
        📋 전체 메뉴판 (16종)
      </h4>

      {/* 5개 탭 헤더 (스크롤바 없이 한눈에 보이는 알약형 탭) */}
      <div style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: '6px',
        marginBottom: '16px',
        paddingBottom: '10px',
        borderBottom: '1px solid #e8e4e0',
      }}>
        {menuTabs.map((tab, idx) => {
          const isActive = idx === activeTabIdx;
          return (
            <button
              key={idx}
              type="button"
              onClick={() => setActiveTabIdx(idx)}
              style={{
                padding: '7px 12px',
                border: isActive ? '1px solid transparent' : '1px solid #e8e4e0',
                backgroundColor: isActive ? '#fff0ef' : '#ffffff',
                borderRadius: '9999px',
                color: isActive ? '#ff383e' : '#334155',
                fontWeight: isActive ? 800 : 600,
                fontSize: '12.5px',
                cursor: 'pointer',
                whiteSpace: 'nowrap',
                transition: 'all 0.15s',
              }}
            >
              {tab.tab_name} <span style={{ color: isActive ? '#ff383e' : '#77736f' }}>({tab.items.length})</span>
            </button>
          );
        })}
      </div>

      {/* 활성 탭 메뉴 리스트 (배민 스타일 2열 그리드) */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
        {currentTab.items.map((menu) => (
          <div
            key={menu.menu_id}
            onClick={() => onSelectMenu && onSelectMenu(menu)}
            style={{
              backgroundColor: '#f8fafc',
              border: '1px solid #e8e4e0',
              borderRadius: '10px',
              padding: '12px 14px',
              cursor: 'pointer',
              transition: 'background-color 0.15s',
              display: 'flex',
              flexDirection: 'column',
              justifyContent: 'space-between',
            }}
            onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#faf8f5')}
            onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = '#f8fafc')}
          >
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                {menu.is_signature && (
                  <span style={{
                    backgroundColor: '#fef3c7',
                    color: '#b45309',
                    fontSize: '10px',
                    fontWeight: 800,
                    padding: '1px 5px',
                    borderRadius: '4px',
                  }}>
                    대표
                  </span>
                )}
                {menu.weather_match === 'RAIN' && <span style={{ fontSize: '11px' }} title="비 오는 날 추천">☔</span>}
                {menu.weather_match === 'CLEAR' && <span style={{ fontSize: '11px' }} title="맑은 날 추천">☀️</span>}
                <span style={{ fontSize: '13px', fontWeight: 700, color: '#1e293b' }}>
                  {menu.menu_name}
                </span>
              </div>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: '8px' }}>
              <span style={{ fontSize: '14px', fontWeight: 900, color: '#ff383e' }}>
                {menu.price.toLocaleString()}원
              </span>
              <span style={{
                fontSize: '11px',
                fontWeight: 700,
                color: '#475569',
                backgroundColor: '#e8e4e0',
                padding: '2px 6px',
                borderRadius: '4px',
              }}>
                {menu.price_tier === 1 ? '1만원 미만' : menu.price_tier === 2 ? '1~2만원대' : menu.price_tier === 3 ? '2~3만원대' : menu.price_tier === 4 ? '3~5만원대' : '5만원+'}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
