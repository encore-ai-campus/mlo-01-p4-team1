import React from 'react';
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { VenueCard } from './VenueCard';
import { VenuePhotoGallery, venuePhotoUrls } from './VenuePhotoGallery';
import { VenueMenuPreviewStrip } from './VenueMenuPreviewStrip';
import { VenueFilterBar } from './VenueFilterBar';
import { VenueDetailDrawer } from './VenueDetailDrawer';
import { nextRegionIndex } from './RegionSelect';
import type { VenueItem } from '../types/venue.types';

const venue: VenueItem = {venue_id:'v1',venue_name:'실제 매장',gu_name:'강남구',dong_name:'신사동',road_address:'주소',category:'CAFE',price_tier:1,avg_price:9000,rating:4.9,review_count:150,is_partner:false,mood_tags:[],latitude:37,longitude:127,is_open_now:true,status_code:'OPEN',status_label:'정상',has_closure_warning:false,matched_menus_preview:[{menu_id:'m1',venue_id:'v1',menu_name:'아주 긴 실제 메뉴 이름도 잘리지 않고 표시됩니다',price:6000,price_tier:1,is_signature:true,weather_match:'ALL',category:'커피'}],match_score:90};
const noop=()=>{};
describe('매장 탐색 디자인',()=>{
 it('API 매장명·영업 상태·메뉴를 유지하고 미확인 평점은 노출하지 않는다',()=>{
  const html=renderToStaticMarkup(<VenueCard venue={venue} selected/>);
  expect(html).toContain('실제 매장'); expect(html).toContain('영업 중'); expect(html).toContain('아주 긴 실제 메뉴 이름도 잘리지 않고 표시됩니다'); expect(html).toContain('평점 준비 중'); expect(html).not.toContain('4.9'); expect(html).toContain('is-selected');
 });
 it('대표 사진을 먼저 두고 중복 및 잘못된 URL은 제외한다',()=>{
  expect(venuePhotoUrls(' https://example.com/a.jpg ', ['https://example.com/b.jpg','https://example.com/a.jpg','', 'javascript:alert(1)'])).toEqual(['https://example.com/a.jpg','https://example.com/b.jpg']);
  expect(venuePhotoUrls(undefined,[])).toEqual([]);
 });
 it('사진이 없으면 갤러리를 숨기고 사진이 있으면 대표 사진부터 표시한다',()=>{
  expect(renderToStaticMarkup(<VenuePhotoGallery name="매장" photos={[]}/>)).toBe('');
  const html=renderToStaticMarkup(<VenuePhotoGallery name="매장" thumbnail="https://example.com/cover.jpg" photos={['https://example.com/inside.jpg']}/>);
  expect(html).toContain('매장 사진 목록');
  expect(html.indexOf('cover.jpg')).toBeLessThan(html.indexOf('inside.jpg'));
 });
 it('API가 제공한 미리보기 메뉴를 빠짐없이 표시한다',()=>{
  const menus = Array.from({length: 4}, (_, index) => ({...venue.matched_menus_preview[0], menu_id:`menu-${index}`, menu_name:`메뉴 ${index}`}));
  const html = renderToStaticMarkup(<VenueMenuPreviewStrip menus={menus}/>);
  for (const menu of menus) expect(html).toContain(menu.menu_name);
 });
 it('지역 선택과 필터의 선택 상태를 제공한다',()=>{
  const html=renderToStaticMarkup(<VenueFilterBar gu="강남구" setGu={noop} weather="CLEAR" setWeather={noop} category="CAFE" setCategory={noop} priceTier="" setPriceTier={noop} storeType="ALL" setStoreType={noop} eventOnly={false} setEventOnly={noop} partnerOnly={false} setPartnerOnly={noop} openNow={false} setOpenNow={noop} searchQuery="" setSearchQuery={noop} totalCount={1}/>);
  expect(html).toContain('role="combobox"'); expect(html).toContain('aria-expanded="false"'); expect(html).toContain('aria-pressed="true">카페'); expect(html).toContain('disabled=""');
 });
 it('방향키와 처음·끝 이동은 목록 범위를 벗어나지 않는다',()=>{ expect(nextRegionIndex('ArrowDown',0,3)).toBe(1); expect(nextRegionIndex('ArrowUp',0,3)).toBe(0); expect(nextRegionIndex('End',0,3)).toBe(2); expect(nextRegionIndex('Home',2,3)).toBe(0); expect(nextRegionIndex('ArrowDown',2,3)).toBe(2); });
 it('매장 상세에 조사 근거를 표시하고 안전한 링크만 만든다',()=>{
  const detail={...venue,menu_tabs:[],all_menus_count:0,applied_playlist:null,research:{description:'작은 로스터리',representative_menus:'핸드드립|쿠키',seating:'창가 좌석',sources:['https://example.org/review','javascript:alert(1)'],checked_on:'2026-11-11',limitations:'2025년 후기 기준'}};
  const html=renderToStaticMarkup(<VenueDetailDrawer venue={detail} isOpen onClose={noop} readOnly/>);
  expect(html).toContain('방문 후기 기반 매장 정보'); expect(html).toContain('작은 로스터리');
  expect(html).toContain('핸드드립 · 쿠키'); expect(html).toContain('창가 좌석'); expect(html).toContain('2026-11-11');
  expect(html).toContain('2025년 후기 기준'); expect(html).toContain('https://example.org/review'); expect(html).not.toContain('javascript:');
  expect(html).toContain('9,000원');
 });
 it('조사 정보가 없거나 링크만 잘못된 경우 해당 구역을 숨긴다',()=>{
  const detail={...venue,menu_tabs:[],all_menus_count:0,applied_playlist:null};
  expect(renderToStaticMarkup(<VenueDetailDrawer venue={detail} isOpen onClose={noop} readOnly/>)).not.toContain('방문 후기 기반 매장 정보');
  expect(renderToStaticMarkup(<VenueDetailDrawer venue={{...detail,research:{sources:['javascript:alert(1)']}}} isOpen onClose={noop} readOnly/>)).not.toContain('방문 후기 기반 매장 정보');
 });
});
