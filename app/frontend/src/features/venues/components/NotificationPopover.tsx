import React, { useState, useEffect } from 'react';
import { fetchMySummary } from '../api/venues.api';

interface NotificationItem {
  notification_id: number;
  title: string;
  message: string;
  link_url: string;
  is_read: boolean;
  created_at: string;
}

interface NotificationPopoverProps {
  userId?: string;
  onSelectLink?: (link: string) => void;
}

export const NotificationPopover: React.FC<NotificationPopoverProps> = ({ userId = 'guest_anonymous', onSelectLink }) => {
  const [isOpen, setIsOpen] = useState<boolean>(false);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [unreadCount, setUnreadCount] = useState<number>(0);

  useEffect(() => {
    fetchMySummary(userId)
      .then((res) => {
        if (res.success && res.summary) {
          setNotifications(res.summary.notifications || []);
          setUnreadCount(res.summary.unread_notifications_count || 0);
        }
      })
      .catch(() => {});
  }, [userId, isOpen]);

  return (
    <div style={{ position: 'relative' }}>
      {/* 알림 벨 버튼 */}
      <button
        type="button"
        aria-expanded={isOpen}
        onClick={() => setIsOpen(!isOpen)}
        style={{
          position: 'relative',
          background: '#ffffff',
          border: '1px solid #e2e8f0',
          borderRadius: '10px',
          padding: '8px 12px',
          cursor: 'pointer',
          fontSize: '16px',
          display: 'flex',
          alignItems: 'center',
          gap: '6px',
          boxShadow: '0 1px 3px rgba(0,0,0,0.05)',
        }}
      >
        <span>알림</span>
        {unreadCount > 0 && (
          <span style={{
            backgroundColor: '#ef4444',
            color: '#ffffff',
            borderRadius: '9999px',
            fontSize: '11px',
            fontWeight: 800,
            padding: '1px 6px',
          }}>
            {unreadCount}
          </span>
        )}
      </button>

      {/* 팝오버 패널 */}
      {isOpen && (
        <div style={{
          position: 'absolute',
          top: '44px',
          right: 0,
          width: '320px',
          backgroundColor: '#ffffff',
          borderRadius: '14px',
          boxShadow: '0 10px 25px rgba(0,0,0,0.15)',
          border: '1px solid #e2e8f0',
          zIndex: 1050,
          overflow: 'hidden',
        }}>
          <div style={{
            padding: '12px 16px',
            borderBottom: '1px solid #f1f5f9',
            fontWeight: 800,
            fontSize: '14px',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}>
            <span>실시간 알림함</span>
            <span style={{ fontSize: '11px', color: '#64748b' }}>최근 20건</span>
          </div>

          <div style={{ maxHeight: '360px', overflowY: 'auto' }}>
            {notifications.length === 0 ? (
              <div style={{ padding: '30px', textAlign: 'center', color: '#94a3b8', fontSize: '13px' }}>
                새로운 알림이 없습니다.
              </div>
            ) : (
              notifications.map((n) => (
                <div
                  key={n.notification_id}
                  onClick={() => {
                    if (n.link_url && onSelectLink) onSelectLink(n.link_url);
                    setIsOpen(false);
                  }}
                  style={{
                    padding: '12px 16px',
                    borderBottom: '1px solid #f8fafc',
                    cursor: n.link_url ? 'pointer' : 'default',
                    backgroundColor: n.is_read ? '#ffffff' : '#f0fdf4',
                    transition: 'background-color 0.15s',
                  }}
                >
                  <div style={{ fontSize: '13px', fontWeight: 700, color: '#0f172a', marginBottom: '2px' }}>
                    {n.title}
                  </div>
                  <div style={{ fontSize: '12px', color: '#64748b', lineHeight: 1.4 }}>
                    {n.message}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
};
