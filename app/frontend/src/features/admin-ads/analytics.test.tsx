import {describe,it,expect} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {AnalyticsResults} from './AdAnalytics';
import {analyticsQuery,dailyAnalytics,emptyAnalyticsFilters,type AnalyticsReport,type AnalyticsRow} from './analytics';

const zero={impressions:0,matched_clicks:0,early_clicks:0,unmatched_clicks:0,pending_clicks:0,ctr_percent:null};
const snapshot:AnalyticsReport={report_available:true,report_id:'synthetic-report',as_of:'2026-11-09T01:00:00Z',status:'PROVISIONAL',policy_confirmed:false,is_stale:true,scope:{},rows:[],totals:zero,source_snapshot:{report_id:'synthetic-source',as_of:'2026-11-09T01:00:00Z',scope:{}}};
describe('advertising analytics reporting',()=>{
  it('encodes filters independently and converts the observation cutoff from KST',()=>{
    const query=new URLSearchParams(analyticsQuery({...emptyAnalyticsFilters,date_from:'2026-11-01',date_to:'2026-11-09',ad_id:'ad? &one',placement:'top',media_id:'site-one',cutoff:'2026-11-09T10:00'}));
    expect(query.get('ad_id')).toBe('ad? &one');expect(query.get('placement')).toBe('top');
    expect(query.get('media_id')).toBe('site-one');expect(query.get('cutoff')).toBe('2026-11-09T01:00:00.000Z');
  });
  it.each([
    {date_from:'2026-11-01'},
    {date_from:'2026-11-09',date_to:'2026-11-01'},
    {date_from:'2026-02-30',date_to:'2026-03-01'},
    {date_from:'2024-01-01',date_to:'2026-01-01'},
    {cutoff:'2026-11-09T25:00'},
  ])('rejects an incomplete or misleading observation range %j',overrides=>{
    expect(()=>analyticsQuery({...emptyAnalyticsFilters,...overrides})).toThrow();
  });
  it('aggregates a daily CTR from counts rather than averaging percentages',()=>{
    const rows:AnalyticsRow[]=[
      {date_kst:'2026-11-09',ad_id:'a',placement:'top',media_id:'site',...zero,impressions:1,matched_clicks:1,ctr_percent:100},
      {date_kst:'2026-11-09',ad_id:'b',placement:'bottom',media_id:'site',...zero,impressions:99,matched_clicks:0,ctr_percent:0},
    ];
    expect(dailyAnalytics(rows)).toEqual([{day:'2026-11-09',impressions:100,clicks:1,ctr:1}]);
  });
  it('distinguishes pending materialization from a genuine zero-result report',()=>{
    const pending=renderToStaticMarkup(<AnalyticsResults report={{...snapshot,report_available:false,status:'PENDING',as_of:null,totals:{impressions:null,matched_clicks:null,early_clicks:null,unmatched_clicks:null,pending_clicks:null,ctr_percent:null}}}/>);
    expect(pending).toContain('성과 집계를 기다리고 있습니다');expect(pending).not.toContain('선택한 조건의 집계 결과는 0건');
    const empty=renderToStaticMarkup(<AnalyticsResults report={snapshot}/>);
    expect(empty).toContain('선택한 조건의 집계 결과는 0건');expect(empty).toContain('성과 보고 정책 확인 전');expect(empty).toContain('집계 지연');expect(empty).toContain('KST');
    expect(empty).not.toContain('0%');
  });
  it('distinguishes disabled analytics from a collection that is still pending',()=>{
    const html=renderToStaticMarkup(<AnalyticsResults report={{...snapshot,enabled:false,report_available:false,status:'PENDING'}}/>);
    expect(html).toContain('성과 리포트가 아직 활성화되지 않았습니다');
    expect(html).toContain('운영 설정을 확인해 주세요');
    expect(html).not.toContain('성과 집계를 기다리고 있습니다');
    expect(html).not.toContain('선택한 조건의 집계 결과는 0건');
  });
  it('renders all click classifications and escapes identifiers in a materialized report',()=>{
    const rows:AnalyticsRow[]=[{date_kst:'2026-11-09',ad_id:'<ad-test>',placement:'top',media_id:'test-site',...zero,impressions:10,matched_clicks:2,ctr_percent:20,early_clicks:1,pending_clicks:1}];
    const html=renderToStaticMarkup(<AnalyticsResults report={{...snapshot,rows,totals:rows[0]}}/>);
    for(const label of ['유효 노출','연결 클릭','조기 클릭','미연결 클릭','처리 대기 클릭','일자별 추이','광고·매체별 상세'])expect(html).toContain(label);
    expect(html).toContain('&lt;ad-test&gt;');expect(html).toContain('20%');
  });
});
