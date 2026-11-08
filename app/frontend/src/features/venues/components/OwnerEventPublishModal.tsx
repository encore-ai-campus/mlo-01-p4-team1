import React, { useState } from 'react';

interface OwnerEventPublishModalProps {
  isOpen: boolean;
  onClose: () => void;
  venueId: string;
  venueName?: string;
  ownerId?: string;
  onSuccess?: () => void;
}

export const OwnerEventPublishModal: React.FC<OwnerEventPublishModalProps> = ({
  isOpen,
  onClose,
  venueId,
  venueName = '내 매장',
  ownerId = 'owner_demo',
  onSuccess,
}) => {
  const [title, setTitle] = useState<string>('오늘의 비/눈 1+1 타임세일');
  const [discountInfo, setDiscountInfo] = useState<string>('대표 메뉴 1+1 특별 증정');
  const [badgeText, setBadgeText] = useState<string>('선착순 1+1');
  const [totalQuantity, setTotalQuantity] = useState<number>(10);
  const [durationHours, setDurationHours] = useState<number>(3);
  const [loading, setLoading] = useState<boolean>(false);

  if (!isOpen) return null;

  const handlePublish = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      const res = await fetch(`/api/owner/venues/${venueId}/events`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          owner_id: ownerId,
          title,
          discount_info: discountInfo,
          badge_text: badgeText,
          total_quantity: totalQuantity,
          duration_hours: durationHours,
        }),
      });
      const data = await res.json();
      if (data.success) {
        alert('실시간 이벤트가 성공적으로 발행되었습니다!\n찜한 단골 고객들에게 알림이 발송되었습니다.');
        if (onSuccess) onSuccess();
        onClose();
      } else {
        alert(data.message || '이벤트 발행에 실패했습니다.');
      }
    } catch {
      alert('오류가 발생했습니다.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      backgroundColor: 'rgba(0,0,0,0.6)',
      zIndex: 1300,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
    }}>
      <div style={{
        backgroundColor: '#ffffff',
        borderRadius: '16px',
        padding: '24px',
        width: '440px',
        maxWidth: '90%',
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <div>
            <h3 style={{ margin: 0, fontSize: '18px', fontWeight: 800 }}>🎉 실시간 타임 이벤트 발행</h3>
            <span style={{ fontSize: '12px', color: '#64748b' }}>{venueName} ({venueId})</span>
          </div>
          <button type="button" onClick={onClose} style={{ background: 'none', border: 'none', fontSize: '16px', cursor: 'pointer' }}>✕</button>
        </div>

        <form onSubmit={handlePublish}>
          <div style={{ marginBottom: '12px' }}>
            <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>이벤트 제목</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
              style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
            />
          </div>

          <div style={{ display: 'flex', gap: '8px', marginBottom: '12px' }}>
            <div style={{ flex: 1 }}>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>배지 문구</label>
              <input
                type="text"
                value={badgeText}
                onChange={(e) => setBadgeText(e.target.value)}
                style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ flex: 1 }}>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>혜택 요약</label>
              <input
                type="text"
                value={discountInfo}
                onChange={(e) => setDiscountInfo(e.target.value)}
                style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
              />
            </div>
          </div>

          <div style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
            <div style={{ flex: 1 }}>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>선착순 수량 (개)</label>
              <input
                type="number"
                min={1}
                max={100}
                value={totalQuantity}
                onChange={(e) => setTotalQuantity(Number(e.target.value))}
                style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ flex: 1 }}>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>진행 시간 (시간)</label>
              <select
                value={durationHours}
                onChange={(e) => setDurationHours(Number(e.target.value))}
                style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
              >
                <option value={1}>1시간</option>
                <option value={2}>2시간</option>
                <option value={3}>3시간</option>
                <option value={5}>5시간</option>
              </select>
            </div>
          </div>

          <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <button type="button" onClick={onClose} style={{ padding: '8px 14px', borderRadius: '8px', border: 'none', background: '#f1f5f9', cursor: 'pointer' }}>취소</button>
            <button type="submit" disabled={loading} style={{ padding: '8px 16px', borderRadius: '8px', border: 'none', background: '#e11d48', color: '#ffffff', fontWeight: 700, cursor: 'pointer' }}>
              {loading ? '발행 중...' : '🚀 이벤트 즉시 발행'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
