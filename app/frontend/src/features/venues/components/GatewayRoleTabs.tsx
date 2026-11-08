import React from 'react';

interface GatewayRoleTabsProps {
  currentTab?: 'USER' | 'ADMIN';
  onSelectTab?: (tab: 'USER' | 'ADMIN') => void;
}

export const GatewayRoleTabs: React.FC<GatewayRoleTabsProps> = ({ currentTab = 'USER', onSelectTab }) => {
  return (
    <div style={{
      display: 'flex',
      backgroundColor: '#f1f5f9',
      padding: '4px',
      borderRadius: '12px',
      marginBottom: '20px',
      border: '1px solid #e2e8f0',
    }}>
      <button
        type="button"
        onClick={() => {
          if (onSelectTab) onSelectTab('USER');
          window.location.href = '/venues';
        }}
        style={{
          flex: 1,
          padding: '12px 16px',
          borderRadius: '8px',
          border: 'none',
          cursor: 'pointer',
          fontWeight: 600,
          fontSize: '14px',
          backgroundColor: currentTab === 'USER' ? '#2563eb' : 'transparent',
          color: currentTab === 'USER' ? '#ffffff' : '#64748b',
          transition: 'all 0.2s',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '8px',
        }}
      >
        <span>☕🍺</span>
        <span>일반 사용자 (카페·주점 탐색)</span>
      </button>

      <button
        type="button"
        onClick={() => {
          if (onSelectTab) onSelectTab('ADMIN');
          window.location.href = '/';
        }}
        style={{
          flex: 1,
          padding: '12px 16px',
          borderRadius: '8px',
          border: 'none',
          cursor: 'pointer',
          fontWeight: 600,
          fontSize: '14px',
          backgroundColor: currentTab === 'ADMIN' ? '#2563eb' : 'transparent',
          color: currentTab === 'ADMIN' ? '#ffffff' : '#64748b',
          transition: 'all 0.2s',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '8px',
        }}
      >
        <span>🎵📊</span>
        <span>매장 관리자 (음악·이벤트 관리)</span>
      </button>
    </div>
  );
};
