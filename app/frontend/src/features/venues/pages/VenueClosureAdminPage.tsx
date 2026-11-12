import React, { useState, useEffect } from 'react';
import { ensureVenueCsrfHeaders } from '../api/venues.api';

interface ClosureItem {
  venue_id: string;
  venue_name: string;
  gu_name: string;
  status: string;
  status_label: string;
  closure_report_count: number;
  last_reported_at: string | null;
  notes: string;
}

export const VenueClosureAdminPage: React.FC = () => {
  const [closures, setClosures] = useState<ClosureItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  const loadClosures = () => {
    setLoading(true);
    fetch('/api/admin/venues/closures', { credentials: 'same-origin' })
      .then((res) => res.json())
      .then((data) => {
        if (data.success) {
          setClosures(data.closures || []);
        }
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadClosures();
  }, []);

  const handleResolve = async (venueId: string, decision: 'CLOSED_HIDDEN' | 'OPEN') => {
    const confirmMsg = decision === 'CLOSED_HIDDEN'
      ? '해당 매장을 폐업 확정(검색/지도 완전 제외) 처리하시겠습니까?'
      : '해당 매장을 정상 영업으로 복구하시겠습니까?';
    if (!window.confirm(confirmMsg)) return;

    try {
      const csrf = await ensureVenueCsrfHeaders();
      const res = await fetch(`/api/admin/venues/closures/${venueId}/resolve`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', ...csrf },
        body: JSON.stringify({ decision, notes: `관리자 ${decision} 판정` }),
      });
      const data = await res.json();
      if (data.success) {
        alert(data.message);
        loadClosures();
      }
    } catch {
      alert('오류가 발생했습니다.');
    }
  };

  return (
    <div style={{
      maxWidth: '1000px',
      margin: '0 auto',
      padding: '30px 20px',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
        <div>
          <h2 style={{ margin: '0 0 6px 0', fontSize: '24px', fontWeight: 800 }}>
            🛡️ 매장 폐업 제보 관리자 검수함
          </h2>
          <div style={{ fontSize: '13px', color: '#64748b' }}>
            유저들이 제보한 폐업 의심 매장을 확인하고 영구 제외하거나 정상 복구합니다.
          </div>
        </div>
        <a href="/venues" style={{ textDecoration: 'none', color: '#2563eb', fontWeight: 700, fontSize: '14px' }}>
          &larr; 매장 탐색 화면으로
        </a>
      </div>

      {loading && <div style={{ textAlign: 'center', padding: '40px' }}>목록 조회 중...</div>}

      {!loading && closures.length === 0 && (
        <div style={{
          textAlign: 'center',
          padding: '60px',
          backgroundColor: '#f8fafc',
          borderRadius: '16px',
          border: '1px solid #e2e8f0',
          color: '#64748b',
        }}>
          ✅ 현재 검수 대기 중인 폐업 제보 매장이 없습니다.
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
        {closures.map((c) => (
          <div
            key={c.venue_id}
            style={{
              backgroundColor: '#ffffff',
              border: c.status === 'CLOSED_HIDDEN' ? '1px solid #cbd5e1' : '1px solid #fecdd3',
              borderRadius: '12px',
              padding: '18px 20px',
              boxShadow: '0 2px 6px rgba(0,0,0,0.04)',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}
          >
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                <span style={{
                  backgroundColor: c.status === 'CLOSED_HIDDEN' ? '#e2e8f0' : '#fee2e2',
                  color: c.status === 'CLOSED_HIDDEN' ? '#475569' : '#dc2626',
                  fontSize: '11px',
                  fontWeight: 800,
                  padding: '2px 8px',
                  borderRadius: '4px',
                }}>
                  {c.status_label} (제보 {c.closure_report_count}건)
                </span>
                <span style={{ fontWeight: 800, fontSize: '16px', color: '#0f172a' }}>
                  {c.venue_name}
                </span>
                <span style={{ fontSize: '12px', color: '#64748b' }}>({c.gu_name})</span>
              </div>
              <div style={{ fontSize: '13px', color: '#475569', marginTop: '6px' }}>
                사유/메모: {c.notes || '상세 사유 없음'}
              </div>
            </div>

            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                type="button"
                onClick={() => handleResolve(c.venue_id, 'OPEN')}
                style={{
                  padding: '8px 14px',
                  backgroundColor: '#f1f5f9',
                  border: '1px solid #cbd5e1',
                  borderRadius: '8px',
                  fontSize: '12px',
                  fontWeight: 700,
                  cursor: 'pointer',
                }}
              >
                정상 복구 (OPEN)
              </button>
              <button
                type="button"
                onClick={() => handleResolve(c.venue_id, 'CLOSED_HIDDEN')}
                style={{
                  padding: '8px 14px',
                  backgroundColor: '#dc2626',
                  color: '#ffffff',
                  border: 'none',
                  borderRadius: '8px',
                  fontSize: '12px',
                  fontWeight: 700,
                  cursor: 'pointer',
                }}
              >
                폐업 확정 (숨김)
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
