import './venue-detail.css';
import React, { useEffect, useRef, useState } from 'react';
import { VenueDetail, VenueEventSummary } from '../types/venue.types';
import { MenuCategoryTabs } from './MenuCategoryTabs';
import { LiveMusicBanner } from './LiveMusicBanner';
import { fetchMySummary, postClosureReport, postReview, toggleBookmark } from '../api/venues.api';

interface VenueDetailDrawerProps {
  venue: VenueDetail | null;
  isOpen: boolean;
  onClose: () => void;
  onOpenEventModal?: (event: VenueEventSummary) => void;
  userId?: string;
  currentWeather?: string;
  readOnly?: boolean;
}

type Section = 'info' | 'menu' | 'reviews' | 'events';
const sections: { id: Section; label: string }[] = [
  { id: 'info', label: '정보' }, { id: 'menu', label: '메뉴' },
  { id: 'reviews', label: '리뷰' }, { id: 'events', label: '이벤트' },
];

export const VenueDetailDrawer: React.FC<VenueDetailDrawerProps> = ({ venue, isOpen, onClose, onOpenEventModal, userId = 'guest_anonymous', currentWeather = 'CLEAR', readOnly = false }) => {
  const [active, setActive] = useState<Section>('info');
  const [bookmarked, setBookmarked] = useState(false);
  const [bookmarkBusy, setBookmarkBusy] = useState(false);
  const [reviewContent, setReviewContent] = useState('');
  const [reviewRating, setReviewRating] = useState(5);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [showReport, setShowReport] = useState(false);
  const [reason, setReason] = useState('');
  const [reportBusy, setReportBusy] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen || !venue) return;
    let cancelled = false;
    setActive('info'); setBookmarked(false); setMessage(''); setShowReport(false); setReason(''); setReviewContent('');
    if (readOnly) return;
    fetchMySummary(userId).then(data => {
      if (!cancelled && data.success) setBookmarked(data.summary.bookmarked_venues.some(item => item.venue_id === venue.venue_id));
    }).catch(() => { if (!cancelled) setMessage('찜 상태를 불러오지 못했습니다.'); });
    return () => { cancelled = true; };
  }, [isOpen, venue?.venue_id, userId, readOnly]);
  useEffect(() => {
    if (!isOpen) return;
    const listener = (event: KeyboardEvent) => { if (event.key === 'Escape') { if (showReport) setShowReport(false); else onClose(); } };
    document.addEventListener('keydown', listener);
    return () => document.removeEventListener('keydown', listener);
  }, [isOpen, onClose, showReport]);
  if (!isOpen || !venue) return null;

  const goTo = (id: Section) => {
    setActive(id);
    contentRef.current?.querySelector<HTMLElement>(`[data-detail-section="${id}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const handleBookmark = async () => {
    if (bookmarkBusy) return;
    setBookmarkBusy(true); setMessage('');
    try {
      const result = await toggleBookmark(venue.venue_id, userId);
      if (!result.success) throw new Error(result.message || '찜을 변경하지 못했습니다.');
      setBookmarked(result.is_bookmarked);
    } catch { setMessage('찜을 변경하지 못했습니다. 다시 시도해 주세요.'); }
    finally { setBookmarkBusy(false); }
  };
  const handleReview = async (event: React.FormEvent) => {
    event.preventDefault(); if (!reviewContent.trim()) return;
    setReviewBusy(true); setMessage('');
    try {
      const result = await postReview(venue.venue_id, { content: reviewContent.trim(), rating: reviewRating, weather_condition: currentWeather, user_id: userId });
      if (!result.success) throw new Error(result.message);
      setReviewContent(''); setMessage('리뷰가 등록되었습니다. 다시 열면 확인할 수 있습니다.');
    } catch { setMessage('리뷰를 등록하지 못했습니다. 다시 시도해 주세요.'); }
    finally { setReviewBusy(false); }
  };
  const handleReport = async () => {
    if (!reason.trim()) return;
    setReportBusy(true); setMessage('');
    try {
      const result = await postClosureReport(venue.venue_id, reason.trim(), userId);
      if (!result.success) throw new Error(result.message);
      setShowReport(false); setReason(''); setMessage('폐업 제보가 접수되었습니다.');
    } catch { setMessage('폐업 제보를 접수하지 못했습니다. 다시 시도해 주세요.'); }
    finally { setReportBusy(false); }
  };
  const event = venue.curated_listing ? null : (venue.active_events?.find(item => !item.is_ended) || venue.active_event);
  const details = venue as VenueDetail & { open_hours?: string; phone?: string };
  const showOnMap = () => { onClose(); document.querySelector('.fitplace-map-card')?.scrollIntoView({ behavior: 'smooth', block: 'center' }); };

  return <div className="fitplace-detail-overlay" onClick={onClose}>
    <section className="fitplace-detail" role="dialog" aria-modal="true" aria-labelledby="fitplace-detail-title" onClick={event => event.stopPropagation()}>
      <header className="fitplace-detail-header">
        <div><h2 id="fitplace-detail-title">매장 상세</h2><p>매장 정보를 확인해 보세요</p></div>
        <button type="button" className="fitplace-detail-close" onClick={onClose}>닫기</button>
      </header>
      <nav className="fitplace-detail-nav" aria-label="매장 상세 메뉴">
        {sections.map(item => <button type="button" key={item.id} className={active === item.id ? 'is-active' : ''} aria-current={active === item.id ? 'true' : undefined} onClick={() => goTo(item.id)}>{item.label}</button>)}
      </nav>
      <div className="fitplace-detail-content" ref={contentRef}>
        <section className="fitplace-detail-section fitplace-detail-intro" data-detail-section="info">
          <h3>{venue.venue_name}</h3>
          <p className="fitplace-detail-meta">{!venue.curated_listing && <span className="fitplace-detail-status">{venue.is_open_now ? '영업 중' : '영업 종료'}</span>} {venue.category === 'CAFE' ? '카페' : '주점'} · {venue.dong_name} {venue.sub_category ? `· ${venue.sub_category}` : ''}</p>
          {!venue.curated_listing && <p className="fitplace-detail-rating">평점 준비 중 · 1인 {venue.avg_price.toLocaleString()}원</p>}
          {!venue.curated_listing && venue.recommendation_style && <p>{venue.recommendation_style}</p>}
          {!venue.curated_listing && venue.mood_tags?.length > 0 && <p className="fitplace-detail-tags">{venue.mood_tags.map(tag => `#${tag}`).join('  ')}</p>}
        </section>
        <section className="fitplace-detail-section">
          <h3>매장 정보</h3>
          <dl className="fitplace-detail-facts">
            <div><dt>주소</dt><dd>{venue.road_address || '주소 정보가 없습니다.'}</dd></div>
            <div><dt>영업</dt><dd>{details.open_hours || '영업시간 정보가 없습니다.'}</dd></div>
            {details.phone && <div><dt>전화</dt><dd>{details.phone}</dd></div>}
            {!venue.curated_listing && venue.music_banner?.playlist_title && <div><dt>음악</dt><dd>{venue.music_banner.playlist_title}</dd></div>}
          </dl>
          {!readOnly && !venue.curated_listing && <button type="button" className="fitplace-detail-report-link" onClick={() => setShowReport(true)}>폐업·정보 오류 제보</button>}
        </section>
        {!venue.curated_listing && <LiveMusicBanner banner={venue.music_banner} />}
        {venue.curated_listing && <section className="fitplace-detail-section" aria-label="공개 자료로 확인한 매장 정보">
          <h3>확인한 매장 정보</h3><p>{venue.curated_listing.moods.join(' · ')}</p><p>{venue.curated_listing.seat_summary}</p>
          <h4>대표 메뉴</h4><p>{venue.curated_listing.representative_menus.join(' · ')}</p>
          {venue.curated_listing.budget_example ? <div><h4>1인 주문 예시 {venue.curated_listing.budget_example.amount_krw.toLocaleString('ko-KR')}원</h4>
            <ul>{venue.curated_listing.budget_example.items.map(item => <li key={item.name}>{item.name} · {item.price_krw.toLocaleString('ko-KR')}원</li>)}</ul>
            <p>{venue.curated_listing.budget_example.limits}</p><a href={venue.curated_listing.budget_example.source_url} target="_blank" rel="noreferrer">공개 메뉴 확인</a></div> : <p>1인 주문 가격 미확인</p>}
          <p>{venue.curated_listing.checked_on} 확인 · <a href={venue.curated_listing.registry_url} target="_blank" rel="noreferrer">상가정보 출처</a></p>
        </section>}
        {!venue.curated_listing && <section className="fitplace-detail-section fitplace-detail-menu" data-detail-section="menu"><h3>메뉴</h3><MenuCategoryTabs key={venue.venue_id} menuTabs={venue.menu_tabs} /></section>}
        <section className="fitplace-detail-section" data-detail-section="reviews">
          <h3>리뷰</h3>
          <p className="fitplace-detail-muted">자체 평점 준비 중</p>
          {!venue.curated_listing && venue.recent_reviews?.length ? venue.recent_reviews.map(review => <article className="fitplace-detail-review" key={review.interaction_id}><strong>{review.user_id} · ★ {review.rating}</strong><p>{review.content}</p></article>) : <p className="fitplace-detail-muted">아직 등록된 리뷰가 없습니다.</p>}
          {!readOnly && !venue.curated_listing && <form className="fitplace-detail-review-form" onSubmit={handleReview}>
            <label>별점 <select value={reviewRating} onChange={e => setReviewRating(Number(e.target.value))}>{[5,4,3,2,1].map(rating => <option key={rating} value={rating}>{rating}점</option>)}</select></label>
            <label>리뷰 <input value={reviewContent} onChange={e => setReviewContent(e.target.value)} placeholder="방문 소감을 남겨주세요" /></label>
            <button type="submit" disabled={reviewBusy || !reviewContent.trim()}>{reviewBusy ? '등록 중' : '리뷰 등록'}</button>
          </form>}
        </section>
        <section className="fitplace-detail-section" data-detail-section="events"><h3>이벤트</h3>{event ? <><p><strong>{event.title}</strong></p><p>{event.discount_info}</p>{!readOnly && <button type="button" className="fitplace-detail-event" onClick={() => onOpenEventModal?.(event)}>이벤트 참여하기</button>}</> : <p className="fitplace-detail-muted">진행 중인 이벤트가 없습니다.</p>}</section>
        {message && <p className="fitplace-detail-message" role="status">{message}</p>}
      </div>
      <footer className="fitplace-detail-actions">{!readOnly && !venue.curated_listing && <button type="button" disabled={bookmarkBusy} aria-pressed={bookmarked} onClick={handleBookmark}>{bookmarked ? '♥ 찜 해제' : '♡ 찜하기'}</button>}<button type="button" onClick={showOnMap}>지도에서 보기</button></footer>
    </section>
    {showReport && <div className="fitplace-report-overlay" onClick={e => e.stopPropagation()}><div className="fitplace-report" role="dialog" aria-modal="true" aria-labelledby="fitplace-report-title"><h3 id="fitplace-report-title">폐업·정보 오류 제보</h3><p>제보 내용을 확인한 뒤 처리합니다.</p><textarea value={reason} onChange={e => setReason(e.target.value)} placeholder="제보 사유를 입력해 주세요" rows={4}/><div><button type="button" onClick={() => setShowReport(false)}>취소</button><button type="button" disabled={reportBusy || !reason.trim()} onClick={handleReport}>{reportBusy ? '접수 중' : '제보하기'}</button></div></div></div>}
  </div>;
};
