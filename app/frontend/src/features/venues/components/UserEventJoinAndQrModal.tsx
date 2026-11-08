import React, { useState } from 'react';
import { VenueEventSummary } from '../types/venue.types';
import { postEventIntent, postVerifyQr } from '../api/venues.api';

interface UserEventJoinAndQrModalProps {
  event: VenueEventSummary | null;
  isOpen: boolean;
  onClose: () => void;
  userId?: string;
  onStockUpdated?: (eventId: number, newStock: number) => void;
}

export const UserEventJoinAndQrModal: React.FC<UserEventJoinAndQrModalProps> = ({
  event,
  isOpen,
  onClose,
  userId = 'demo_user_final',
  onStockUpdated,
}) => {
  const [step, setStep] = useState<'INTENT' | 'QR_ISSUED' | 'VERIFIED'>('INTENT');
  const [qrToken, setQrToken] = useState<string>('');
  const [pinInput, setPinInput] = useState<string>('1234');
  const [loading, setLoading] = useState<boolean>(false);
  const [currentStock, setCurrentStock] = useState<number>(event?.remaining_quantity || 10);
  const [intendedCount, setIntendedCount] = useState<number>(event?.intended_count || 0);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  React.useEffect(() => {
    if (event) {
      setCurrentStock(event.remaining_quantity);
      setIntendedCount(event.intended_count);
      setStep('INTENT');
      setQrToken('');
      setErrorMsg(null);
    }
  }, [event?.event_id]);

  if (!isOpen || !event) return null;

  // 1. 유저 [참여할게요] 클릭 -> 재고 미차감, QR 발급
  const handleJoinIntent = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await postEventIntent(event.event_id, userId);
      if (res.success || res.code === 200) {
        setQrToken(res.qr_auth_code || `QR_${event.event_id}_${userId}`);
        setCurrentStock(res.remaining_quantity);
        setIntendedCount(res.intended_count);
        setStep('QR_ISSUED');
      } else if (res.code === 409) {
        // 이미 참여 신청한 경우 기존 QR로 직행
        setQrToken(res.qr_auth_code || `QR_${event.event_id}_${userId}`);
        setStep('QR_ISSUED');
      } else {
        setErrorMsg(res.message || '참여 신청에 실패했습니다.');
      }
    } catch (err: any) {
      setErrorMsg(err.message || '오류가 발생했습니다.');
    } finally {
      setLoading(false);
    }
  };

  // 2. 발표 시연용 원클릭 현장 QR 스캔 시뮬레이션 (즉시 인증 & 재고 -1 차감)
  const handleVerifySimulation = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await postVerifyQr(event.event_id, userId, qrToken);
      if (res.success || res.code === 200) {
        const newStock = res.remaining_quantity;
        setCurrentStock(newStock);
        setStep('VERIFIED');
        if (onStockUpdated) {
          onStockUpdated(event.event_id, newStock);
        }
      } else if (res.code === 409) {
        setErrorMsg('⚠️ 오늘 이미 현장 인증을 완료하셨습니다 (1일 1회 제한).');
        setStep('VERIFIED');
      } else {
        setErrorMsg(res.message || 'QR 인증에 실패했습니다.');
      }
    } catch (err: any) {
      setErrorMsg(err.message || '인증 오류가 발생했습니다.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      backgroundColor: 'rgba(0,0,0,0.65)',
      zIndex: 1200,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      backdropFilter: 'blur(4px)',
    }}>
      <div style={{
        backgroundColor: '#ffffff',
        borderRadius: '20px',
        padding: '28px',
        width: '460px',
        maxWidth: '92%',
        boxShadow: '0 20px 40px rgba(0, 0, 0, 0.25)',
        textAlign: 'center',
        position: 'relative',
      }}>
        <button
          type="button"
          onClick={onClose}
          style={{
            position: 'absolute',
            top: '16px',
            right: '16px',
            background: 'none',
            border: 'none',
            fontSize: '18px',
            cursor: 'pointer',
            color: '#94a3b8',
          }}
        >
          ✕
        </button>

        {/* 뱃지 및 제목 */}
        <div style={{
          display: 'inline-block',
          backgroundColor: '#fee2e2',
          color: '#e11d48',
          fontSize: '12px',
          fontWeight: 800,
          padding: '3px 10px',
          borderRadius: '9999px',
          marginBottom: '8px',
        }}>
          {event.badge_text}
        </div>

        <h3 style={{ margin: '0 0 6px 0', fontSize: '20px', fontWeight: 800, color: '#0f172a' }}>
          {event.title}
        </h3>

        <div style={{ fontSize: '13px', color: '#64748b', marginBottom: '18px' }}>
          {event.discount_info}
        </div>

        {/* 실시간 재고 & 방문 예정 표시 영역 */}
        <div style={{
          backgroundColor: '#f8fafc',
          border: '1px solid #e2e8f0',
          borderRadius: '12px',
          padding: '12px',
          marginBottom: '20px',
          display: 'flex',
          justifyContent: 'space-around',
        }}>
          <div>
            <div style={{ fontSize: '11px', color: '#64748b', fontWeight: 600 }}>남은 실시간 재고</div>
            <div style={{ fontSize: '18px', fontWeight: 900, color: '#e11d48' }}>
              🔥 {currentStock}개
            </div>
          </div>
          <div style={{ borderLeft: '1px solid #e2e8f0' }} />
          <div>
            <div style={{ fontSize: '11px', color: '#64748b', fontWeight: 600 }}>현재 방문 예정 인원</div>
            <div style={{ fontSize: '18px', fontWeight: 900, color: '#2563eb' }}>
              👥 {intendedCount}명
            </div>
          </div>
        </div>

        {/* 단계 1: [참여할게요] 클릭 전 안내 */}
        {step === 'INTENT' && (
          <div>
            <p style={{ fontSize: '13px', color: '#475569', lineHeight: 1.6, margin: '0 0 20px 0' }}>
              <strong>[참여할게요]</strong>를 누르면 재고가 바로 차감되지 않고, 점주에게 예상 방문 인원이 전달되며 <strong>현장 QR 인증권</strong>이 발급됩니다!
            </p>

            <button
              type="button"
              disabled={loading}
              onClick={handleJoinIntent}
              style={{
                width: '100%',
                padding: '14px',
                backgroundColor: '#e11d48',
                color: '#ffffff',
                border: 'none',
                borderRadius: '12px',
                fontSize: '15px',
                fontWeight: 800,
                cursor: loading ? 'not-allowed' : 'pointer',
                boxShadow: '0 4px 12px rgba(225, 29, 72, 0.3)',
              }}
            >
              {loading ? '신청 처리 중...' : '🙋 [참여할게요] 신청하고 현장 QR 받기'}
            </button>
          </div>
        )}

        {/* 단계 2: QR 발급 완료 & 현장 인증 화면 (발표 시연 원클릭 버튼 포함) */}
        {step === 'QR_ISSUED' && (
          <div>
            <div style={{
              backgroundColor: '#ffffff',
              border: '2px dashed #6366f1',
              borderRadius: '16px',
              padding: '16px',
              display: 'inline-block',
              marginBottom: '16px',
            }}>
              {/* 시각적 모던 QR 그래픽 SVG */}
              <svg width="140" height="140" viewBox="0 0 100 100" style={{ display: 'block', margin: '0 auto' }}>
                <rect width="100" height="100" fill="#ffffff" />
                {/* 3개 모서리 파인더 패턴 */}
                <rect x="5" y="5" width="26" height="26" fill="#1e1b4b" rx="4" />
                <rect x="9" y="9" width="18" height="18" fill="#ffffff" rx="2" />
                <rect x="13" y="13" width="10" height="10" fill="#6366f1" rx="2" />

                <rect x="69" y="5" width="26" height="26" fill="#1e1b4b" rx="4" />
                <rect x="73" y="9" width="18" height="18" fill="#ffffff" rx="2" />
                <rect x="77" y="13" width="10" height="10" fill="#6366f1" rx="2" />

                <rect x="5" y="69" width="26" height="26" fill="#1e1b4b" rx="4" />
                <rect x="9" y="73" width="18" height="18" fill="#ffffff" rx="2" />
                <rect x="13" y="77" width="10" height="10" fill="#6366f1" rx="2" />

                {/* 중앙 데이터 패턴 도트 */}
                <rect x="36" y="10" width="6" height="6" fill="#1e1b4b" rx="1" />
                <rect x="46" y="10" width="8" height="6" fill="#6366f1" rx="1" />
                <rect x="36" y="22" width="10" height="6" fill="#1e1b4b" rx="1" />
                <rect x="10" y="38" width="8" height="6" fill="#6366f1" rx="1" />
                <rect x="22" y="38" width="6" height="6" fill="#1e1b4b" rx="1" />
                <rect x="36" y="36" width="28" height="28" fill="#4338ca" rx="4" />
                <text x="50" y="54" fontSize="11" fill="#ffffff" fontWeight="bold" textAnchor="middle">QR</text>
                <rect x="70" y="38" width="12" height="6" fill="#1e1b4b" rx="1" />
                <rect x="40" y="70" width="8" height="8" fill="#1e1b4b" rx="1" />
                <rect x="52" y="70" width="14" height="6" fill="#6366f1" rx="1" />
                <rect x="72" y="70" width="16" height="16" fill="#1e1b4b" rx="2" />
              </svg>

              <div style={{ fontSize: '11px', color: '#6366f1', fontWeight: 700, marginTop: '8px' }}>
                인증 코드: {qrToken.slice(0, 18)}...
              </div>
            </div>

            {/* 4자리 확인 PIN 코드 입력창 */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', marginBottom: '16px' }}>
              <span style={{ fontSize: '12px', color: '#64748b', fontWeight: 600 }}>매장 확인 PIN:</span>
              <input
                type="text"
                value={pinInput}
                onChange={(e) => setPinInput(e.target.value)}
                maxLength={4}
                style={{
                  width: '70px',
                  textAlign: 'center',
                  padding: '6px',
                  fontSize: '14px',
                  fontWeight: 800,
                  borderRadius: '6px',
                  border: '1px solid #cbd5e1',
                  letterSpacing: '2px',
                }}
              />
            </div>

            {/* 발표 시연용 원클릭 시뮬레이션 버튼 (핵심!) */}
            <button
              type="button"
              disabled={loading}
              onClick={handleVerifySimulation}
              style={{
                width: '100%',
                padding: '13px',
                backgroundColor: '#2563eb',
                color: '#ffffff',
                border: 'none',
                borderRadius: '12px',
                fontSize: '14px',
                fontWeight: 800,
                cursor: loading ? 'not-allowed' : 'pointer',
                boxShadow: '0 4px 14px rgba(37, 99, 235, 0.3)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '8px',
              }}
            >
              <span>🎯</span>
              <span>[발표 시연용: 현장 QR 스캔 시뮬레이션 (즉시 재고 -1 차감)]</span>
            </button>
          </div>
        )}

        {/* 단계 3: 현장 인증 완료 상태 (1일 1회 제한 및 Two-Tower 갱신 반영) */}
        {step === 'VERIFIED' && (
          <div style={{
            backgroundColor: '#f0fdf4',
            border: '1px solid #86efac',
            borderRadius: '14px',
            padding: '20px',
          }}>
            <div style={{ fontSize: '32px', marginBottom: '8px' }}>🎉</div>
            <div style={{ fontSize: '17px', fontWeight: 800, color: '#166534', marginBottom: '6px' }}>
              현장 QR 인증 완료!
            </div>
            <div style={{ fontSize: '13px', color: '#15803d', lineHeight: 1.5, marginBottom: '14px' }}>
              재고가 <strong>-1개</strong> 차감되었습니다 (현재 남은 수량: <strong>{currentStock}개</strong>).<br />
              취향 딥러닝(Two-Tower) 가중치가 즉시 반영되었습니다.
            </div>

            <div style={{
              fontSize: '11px',
              backgroundColor: '#dcfce7',
              color: '#166534',
              padding: '6px 10px',
              borderRadius: '6px',
              fontWeight: 700,
              display: 'inline-block',
            }}>
              ✅ 오늘 참여 완료 (동일 이벤트 1일 1회 제한 적용됨)
            </div>

            <div style={{ marginTop: '16px' }}>
              <button
                type="button"
                onClick={onClose}
                style={{
                  padding: '8px 24px',
                  backgroundColor: '#16a34a',
                  color: '#ffffff',
                  border: 'none',
                  borderRadius: '8px',
                  fontWeight: 700,
                  fontSize: '13px',
                  cursor: 'pointer',
                }}
              >
                닫기
              </button>
            </div>
          </div>
        )}

        {errorMsg && (
          <div style={{ color: '#ef4444', fontSize: '12px', marginTop: '12px', fontWeight: 600 }}>
            {errorMsg}
          </div>
        )}
      </div>
    </div>
  );
};
