import React from 'react';

interface GatewayRoleTabsProps {
  currentTab?: 'USER' | 'ADMIN';
  onSelectTab?: (tab: 'USER' | 'ADMIN') => void;
}

export const GatewayRoleTabs: React.FC<GatewayRoleTabsProps> = ({ currentTab = 'USER', onSelectTab }) => {
  return (
    <div style={{
      display: 'flex',
      backgroundColor: '#faf8f5',
      padding: '4px',
      borderRadius: '12px',
      marginBottom: '20px',
      border: '1px solid #e8e4e0',
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
          backgroundColor: currentTab === 'USER' ? '#ff383e' : 'transparent',
          color: currentTab === 'USER' ? '#ffffff' : '#77736f',
          transition: 'all 0.2s',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '8px',
        }}
      >
        <span>일반 사용자</span>
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
          backgroundColor: currentTab === 'ADMIN' ? '#ff383e' : 'transparent',
          color: currentTab === 'ADMIN' ? '#ffffff' : '#77736f',
          transition: 'all 0.2s',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '8px',
        }}
      >
        <span>매장 관리자</span>
      </button>
    </div>
  );
};
