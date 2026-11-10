import React from 'react';
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { VenueCard } from './VenueCard';
import { VenueMenuPreviewStrip } from './VenueMenuPreviewStrip';
import { VenueFilterBar } from './VenueFilterBar';
import { nextRegionIndex } from './RegionSelect';
import type { VenueItem } from '../types/venue.types';

const venue: VenueItem = {venue_id:'v1',venue_name:'실제 매장',gu_name:'강남구',dong_name:'신사동',road_address:'주소',category:'CAFE',price_tier:1,avg_price:9000,rating:4.9,review_count:150,is_partner:false,mood_tags:[],latitude:37,longitude:127,is_open_now:true,status_code:'OPEN',status_label:'정상',has_closure_warning:false,matched_menus_preview:[{menu_id:'m1',venue_id:'v1',menu_name:'아주 긴 실제 메뉴 이름도 잘리지 않고 표시됩니다',price:6000,price_tier:1,is_signature:true,weather_match:'ALL',category:'커피'}],match_score:90};
const noop=()=>{};
describe('매장 탐색 디자인',()=>{
 it('API 매장명·영업 상태·메뉴를 유지하고 미확인 평점은 노출하지 않는다',()=>{
  const html=renderToStaticMarkup(<VenueCard venue={venue} selected/>);
  expect(html).toContain('실제 매장'); expect(html).toContain('영업 중'); expect(html).toContain('아주 긴 실제 메뉴 이름도 잘리지 않고 표시됩니다'); expect(html).toContain('평점 준비 중'); expect(html).not.toContain('4.9'); expect(html).toContain('is-selected');
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
});
