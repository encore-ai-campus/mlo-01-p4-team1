import React, { useState } from 'react';
import { VenueItem, MenuItem } from '../types/venue.types';
import { VenueMenuPreviewStrip } from './VenueMenuPreviewStrip';
import { VenuePhotoGallery } from './VenuePhotoGallery';
import { toggleBookmark } from '../api/venues.api';

interface VenueCardProps { venue: VenueItem; selected?: boolean; onSelectVenue?: (id:string)=>void; onSelectMenu?: (menu:MenuItem)=>void; userId?:string; readOnly?:boolean; allowFeedback?:boolean; }
export const VenueCard: React.FC<VenueCardProps> = ({venue,selected=false,onSelectVenue,onSelectMenu,userId='guest_anonymous',readOnly=false,allowFeedback=false}) => {
  const feedbackEnabled = !readOnly || allowFeedback;
  const [isBookmarked,setBookmarked]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  async function bookmark(e:React.MouseEvent) {
    e.stopPropagation(); if(busy)return; setBusy(true); setError('');
    try { const result=await toggleBookmark(venue.venue_id,userId); if(!result.success)throw new Error(); setBookmarked(result.is_bookmarked); }
    catch {setError('찜을 변경하지 못했습니다.');} finally {setBusy(false);}
  }
  const open=()=>onSelectVenue?.(venue.venue_id);
  return <article className={'fitplace-venue-card'+(selected?' is-selected':'')} onClick={open}>
    <div className="fp-card-title"><div className="fp-card-heading"><h3><button type="button" className="fp-card-open" aria-label={`${venue.venue_name} 상세 보기`} onClick={e=>{e.stopPropagation();open();}}>{venue.venue_name}</button></h3><p className="fp-card-meta">{venue.category==='CAFE'?'카페':'주점'} · {venue.gu_name} {venue.dong_name}{venue.sub_category?` · ${venue.sub_category}`:''}</p></div>{feedbackEnabled && <button type="button" className="fp-card-bookmark" aria-label={isBookmarked?'찜 해제':'찜하기'} aria-pressed={isBookmarked} disabled={busy} onClick={bookmark}>{isBookmarked?'♥':'♡'}</button>}</div>
    <p className="fp-card-facts"><span className="fp-status">{venue.is_open_now?'영업 중':'영업 종료'}</span>{venue.is_partner&&<span className="fp-partner-badge">제휴 매장</span>}<strong>1인 {venue.avg_price.toLocaleString()}원</strong><span>평점 준비 중</span></p>
    {venue.has_closure_warning&&<p className="fp-card-notice">폐업 제보 접수</p>}
    {venue.active_event&&<p className="fp-card-notice">{venue.active_event.badge_text}: {venue.active_event.title} · 남은 수량 {venue.active_event.remaining_quantity}개</p>}
    {!!venue.mood_tags?.length&&<p className="fp-card-tags">{venue.mood_tags.map(t=>`#${t}`).join(' ')}</p>}
    <VenuePhotoGallery name={venue.venue_name} thumbnail={venue.thumbnail_url} photos={venue.interior_photos}/>
    <VenueMenuPreviewStrip menus={venue.matched_menus_preview} onSelectMenu={onSelectMenu}/>
    {!venue.matched_menus_preview?.length && <p className="fp-card-tags">실제 메뉴 확인 중</p>}
    <span className="fitplace-more">메뉴 더보기 →</span>{error&&<p role="status">{error}</p>}
  </article>;
};
