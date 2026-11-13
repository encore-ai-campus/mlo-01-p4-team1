import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { VenueCard } from './VenueCard';
import { VenuePhotoGallery, venuePhotoUrls } from './VenuePhotoGallery';
import { VenueMenuPreviewStrip } from './VenueMenuPreviewStrip';
import { MenuCategoryTabs } from './MenuCategoryTabs';
import { VenueFilterBar } from './VenueFilterBar';
import { VenueDetailDrawer } from './VenueDetailDrawer';
import { canOpenVenueDetail, VisitCards } from './VisitRecommendationSection';
import { VenueConciergeSection } from './VenueConciergeSection';
import { fetchVisitOptions } from '../api/visitRecommendation.api';
import { nextRegionIndex } from './RegionSelect';
import { VENUE_MAP_TILE_SOURCES } from './VenueInteractiveMap';
import type { VenueItem } from '../types/venue.types';

const venue: VenueItem = {venue_id:'v1',venue_name:'실제 매장',gu_name:'강남구',dong_name:'신사동',road_address:'주소',category:'CAFE',price_tier:1,avg_price:9000,rating:4.9,review_count:150,is_partner:false,mood_tags:[],latitude:37,longitude:127,is_open_now:true,status_code:'OPEN',status_label:'정상',has_closure_warning:false,matched_menus_preview:[{menu_id:'m1',venue_id:'v1',menu_name:'아주 긴 실제 메뉴 이름도 잘리지 않고 표시됩니다',price:6000,price_tier:1,is_signature:true,weather_match:'ALL',category:'커피'}],match_score:90};
const noop=()=>{};
describe('매장 탐색 디자인',()=>{
 it('API 매장명·영업 상태·메뉴를 유지하고 미확인 평점은 노출하지 않는다',()=>{
  const html=renderToStaticMarkup(<VenueCard venue={venue} selected/>);
  expect(html).toContain('실제 매장'); expect(html).toContain('영업 중'); expect(html).toContain('아주 긴 실제 메뉴 이름도 잘리지 않고 표시됩니다'); expect(html).toContain('평점 준비 중'); expect(html).not.toContain('4.9'); expect(html).not.toContain('제휴 매장'); expect(html).toContain('is-selected');
 });
 it('점주 인증 또는 신규 등록된 제휴 매장은 카드에 제휴 매장 배지를 표시한다',()=>{
  const html=renderToStaticMarkup(<VenueCard venue={{...venue,is_partner:true}}/>);
  expect(html).toContain('제휴 매장');
 });
 it('예산 정보가 없으면 카드와 상세에 미확인으로 표시한다',()=>{
  const missing={...venue,avg_price:null};
  expect(renderToStaticMarkup(<VenueCard venue={missing}/>)).toContain('1인 예산 미확인');
  const detail={...missing,menu_tabs:[],all_menus_count:0,applied_playlist:null};
  expect(renderToStaticMarkup(<VenueDetailDrawer venue={detail} isOpen onClose={noop} readOnly/>)).toContain('1인 예산 미확인');
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
 it('가격 미확인 메뉴는 가격 미확인으로 표시하고 미수집 카드는 임의 메뉴를 숨긴다',()=>{
  const unpriced={...venue.matched_menus_preview[0],menu_id:'RM_1',menu_name:'소금 아인슈페너',price:0};
  const stripHtml=renderToStaticMarkup(<VenueMenuPreviewStrip menus={[unpriced]}/>);
  expect(stripHtml).toContain('소금 아인슈페너'); expect(stripHtml).toContain('가격 미확인'); expect(stripHtml).not.toContain('0원');
  const tabsHtml=renderToStaticMarkup(<MenuCategoryTabs menuTabs={[{tab_name:'시그니처/대표',items:[unpriced]}]}/>);
  expect(tabsHtml).toContain('확인된 메뉴판 (1종)'); expect(tabsHtml).toContain('가격 미확인'); expect(tabsHtml).not.toContain('0원');
  const emptyTabsHtml=renderToStaticMarkup(<MenuCategoryTabs menuTabs={[{tab_name:'시그니처/대표',items:[]}]}/>);
  expect(emptyTabsHtml).toContain('확인된 실제 메뉴 정보가 아직 등록되지 않았습니다.');
  const cardHtml=renderToStaticMarkup(<VenueCard venue={{...venue,matched_menus_preview:[]}}/>);
  expect(cardHtml).toContain('실제 메뉴 확인 중');
 });
 it('점주가 적용한 선곡표가 있을 때만 이름을 보여준다',()=>{
  const detail={...venue,menu_tabs:[],all_menus_count:0,applied_playlist:null};
  expect(renderToStaticMarkup(<VenueDetailDrawer venue={detail} isOpen onClose={noop} readOnly/>)).not.toContain('점주 적용 선곡표');
  const html=renderToStaticMarkup(<VenueDetailDrawer venue={{...detail,applied_playlist:{id:'playlist-1',name:'비 오는 날 선곡'}}} isOpen onClose={noop} readOnly/>);
  expect(html).toContain('점주 적용 선곡표'); expect(html).toContain('비 오는 날 선곡');
 });
 it('시연 카탈로그 상세는 확인되지 않은 가격·영업 상태를 표시하지 않는다',()=>{
  const detail={venue_id:'VN_IC_000038',venue_name:'검증 매장',category:'PUB' as const,sub_category:'주점',road_address:'인천 연수구',dong_name:'송도2동',research:{description:'방문 후기',sources:['https://example.org/review']},curated_listing:{preview_only:true,moods:[],representative_menus:[],seat_summary:'',checked_on:'',registry_url:'',budget_example:null}};
  const html=renderToStaticMarkup(<VenueDetailDrawer venue={detail} isOpen onClose={noop} readOnly/>);
  expect(html).toContain('시연 매장 정보'); expect(html).toContain('검증 매장'); expect(html).toContain('방문 후기');
  expect(html).toContain('영업 여부와 메뉴·가격은 확인되지 않았습니다'); expect(html).not.toContain('영업 중'); expect(html).not.toContain('상가정보 출처');
 });
 it('시연 스냅샷 추천만 상세 버튼을 제공하고 운영 스냅샷은 차단한다',()=>{
  const recommendation={venue_id:'VN_IC_000038',name:'검증 매장',address:'인천 연수구',sub_category:null,avg_budget_krw:null,matched_moods:[],matched_orders:[],forecast:{provider:'project_weather',state:'NO_FORECAST' as const,condition:null,start_at:null,end_at:null}};
  const renderCard=(previewOnly?:boolean)=>renderToStaticMarkup(<VisitCards venues={[recommendation]} onOpenVenue={canOpenVenueDetail('PUBLIC_SNAPSHOT',previewOnly===undefined?undefined:{preview_only:previewOnly})?noop:undefined}/>);
  expect(renderCard(true)).toContain('aria-label="검증 매장 매장 상세 보기"');
  expect(renderCard(false)).not.toContain('매장 상세 보기');
  expect(renderCard()).not.toContain('매장 상세 보기');
  expect(canOpenVenueDetail('PUBLIC_SNAPSHOT',{preview_only:false,detail_supported:true})).toBe(true);
  expect(canOpenVenueDetail('PUBLIC_SOURCE_PILOT')).toBe(true);
 });
 it('MCP 옵션 404는 이용 불가 상태로 처리하고 질문을 막는다', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({code:'NOT_FOUND'}), {status:404})));
  try {
   await expect(fetchVisitOptions(new AbortController().signal)).resolves.toBeNull();
   const html = renderToStaticMarkup(<VenueConciergeSection
    connection={{status:'unavailable'}} onReconnect={noop} onSelectVenue={noop}/>);
   expect(html).toContain('현재 AI 매장 추천을 사용할 수 없습니다.');
   expect(html).toContain('type="submit" disabled=""');
  } finally {
   vi.unstubAllGlobals();
  }
 });
 it('2차 추천 카드에 후기와 메뉴 근거를 각각 표시한다', () => {
  const recommendation = {
   venue_id:'review', name:'후기 매장', address:'인천 연수구', sub_category:null, avg_budget_krw:null,
   matched_moods:[], matched_orders:[],
   forecast:{provider:'project_weather',state:'NO_FORECAST' as const,condition:null,start_at:null,end_at:null},
   matched_purposes:[{id:'visit_second_round',label:'2차'}],
  };
  const menuCandidate = {...recommendation, venue_id:'menu', name:'메뉴 후보', matched_purposes:[
   {id:'visit_second_round',label:'2차',menu_names:['생맥주','감자튀김']},
  ]};
  const html = renderToStaticMarkup(<VisitCards venues={[recommendation, menuCandidate]}/>);
  expect(html).toContain('방문 후기 · 2차');
  expect(html).toContain('메뉴로 찾은 후보 · 생맥주 + 감자튀김');
 });
 it('지도 타일은 CARTO API 키 워터마크 CDN 없이 OpenStreetMap 타일과 Origin 리퍼러 정책을 사용한다', () => {
  expect(VENUE_MAP_TILE_SOURCES.length).toBeGreaterThan(0);
  expect(VENUE_MAP_TILE_SOURCES[0].url).toBe('https://tile.openstreetmap.org/{z}/{x}/{y}.png');
  expect(VENUE_MAP_TILE_SOURCES[0].options.referrerPolicy).toBe('strict-origin-when-cross-origin');
  for (const source of VENUE_MAP_TILE_SOURCES) {
   expect(source.url).not.toContain('carto');
  }
 });
});
