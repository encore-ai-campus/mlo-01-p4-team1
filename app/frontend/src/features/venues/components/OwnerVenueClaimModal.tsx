import React, { useState } from 'react';

interface OwnerVenueClaimModalProps {
  isOpen: boolean;
  onClose: () => void;
  ownerId?: string;
  onSuccess?: () => void;
}

export const OwnerVenueClaimModal: React.FC<OwnerVenueClaimModalProps> = ({
  isOpen,
  onClose,
  ownerId = 'owner_demo',
  onSuccess,
}) => {
  const [mode, setMode] = useState<'CLAIM' | 'REGISTER'>('CLAIM');
  const [venueId, setVenueId] = useState<string>('venue_seoul_0001');
  const [venueName, setVenueName] = useState<string>('');
  const [guName, setGuName] = useState<string>('강남구');
  const [roadAddress, setRoadAddress] = useState<string>('');
  const [category, setCategory] = useState<'CAFE' | 'PUB'>('CAFE');
  const [bizNumber, setBizNumber] = useState<string>('123-45-67890');
  const [loading, setLoading] = useState<boolean>(false);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      const endpoint = mode === 'CLAIM' ? '/api/owner/venues/claim' : '/api/owner/venues/register';
      const payload = mode === 'CLAIM'
        ? { owner_id: ownerId, venue_id: venueId, business_number: bizNumber }
        : { owner_id: ownerId, venue_name: venueName, gu_name: guName, road_address: roadAddress, category, business_number: bizNumber };

      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (data.success) {
        alert(data.message);
        if (onSuccess) onSuccess();
        onClose();
      } else {
        alert(data.message || '등록에 실패했습니다.');
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
          <h3 style={{ margin: 0, fontSize: '18px', fontWeight: 800 }}>🏪 점주 매장 연결 및 등록</h3>
          <button type="button" onClick={onClose} style={{ background: 'none', border: 'none', fontSize: '16px', cursor: 'pointer' }}>✕</button>
        </div>

        {/* 탭 전환 */}
        <div style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
          <button
            type="button"
            onClick={() => setMode('CLAIM')}
            style={{
              flex: 1,
              padding: '8px',
              borderRadius: '8px',
              border: mode === 'CLAIM' ? '1px solid #2563eb' : '1px solid #cbd5e1',
              backgroundColor: mode === 'CLAIM' ? '#2563eb' : '#ffffff',
              color: mode === 'CLAIM' ? '#ffffff' : '#334155',
              fontWeight: 700,
              fontSize: '13px',
              cursor: 'pointer',
            }}
          >
            기존 매장 소유권 연결
          </button>
          <button
            type="button"
            onClick={() => setMode('REGISTER')}
            style={{
              flex: 1,
              padding: '8px',
              borderRadius: '8px',
              border: mode === 'REGISTER' ? '1px solid #2563eb' : '1px solid #cbd5e1',
              backgroundColor: mode === 'REGISTER' ? '#2563eb' : '#ffffff',
              color: mode === 'REGISTER' ? '#ffffff' : '#334155',
              fontWeight: 700,
              fontSize: '13px',
              cursor: 'pointer',
            }}
          >
            신규 매장 직접 등록
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          {mode === 'CLAIM' ? (
            <div style={{ marginBottom: '12px' }}>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>매장 고유 식별자 (venue_id)</label>
              <input
                type="text"
                value={venueId}
                onChange={(e) => setVenueId(e.target.value)}
                required
                style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
              />
            </div>
          ) : (
            <>
              <div style={{ marginBottom: '10px' }}>
                <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>매장명</label>
                <input
                  type="text"
                  value={venueName}
                  onChange={(e) => setVenueName(e.target.value)}
                  placeholder="예: 달빛 로스터리 카페"
                  required
                  style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
                />
              </div>
              <div style={{ display: 'flex', gap: '8px', marginBottom: '10px' }}>
                <div style={{ flex: 1 }}>
                  <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>자치구</label>
                  <input
                    type="text"
                    value={guName}
                    onChange={(e) => setGuName(e.target.value)}
                    style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
                  />
                </div>
                <div style={{ flex: 1 }}>
                  <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>업종</label>
                  <select
                    value={category}
                    onChange={(e) => setCategory(e.target.value as any)}
                    style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
                  >
                    <option value="CAFE">카페</option>
                    <option value="PUB">주점/펍</option>
                  </select>
                </div>
              </div>
              <div style={{ marginBottom: '10px' }}>
                <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>도로명 주소</label>
                <input
                  type="text"
                  value={roadAddress}
                  onChange={(e) => setRoadAddress(e.target.value)}
                  placeholder="예: 서울특별시 강남구 테헤란로 123"
                  style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
                />
              </div>
            </>
          )}

          <div style={{ marginBottom: '16px' }}>
            <label style={{ display: 'block', fontSize: '12px', fontWeight: 700, marginBottom: '4px' }}>사업자 등록번호</label>
            <input
              type="text"
              value={bizNumber}
              onChange={(e) => setBizNumber(e.target.value)}
              style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #cbd5e1', boxSizing: 'border-box' }}
            />
          </div>

          <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <button type="button" onClick={onClose} style={{ padding: '8px 14px', borderRadius: '8px', border: 'none', background: '#f1f5f9', cursor: 'pointer' }}>취소</button>
            <button type="submit" disabled={loading} style={{ padding: '8px 16px', borderRadius: '8px', border: 'none', background: '#2563eb', color: '#ffffff', fontWeight: 700, cursor: 'pointer' }}>
              {loading ? '처리 중...' : '등록 완료'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
