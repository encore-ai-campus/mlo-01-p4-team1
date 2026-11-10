import {describe,it,expect} from 'vitest';
import {parseAnalyticsReport} from './analytics';
const totals={impressions:0,matched_clicks:0,early_clicks:0,unmatched_clicks:0,pending_clicks:0,ctr_percent:null};
const ready={enabled:true,report_available:true,report_id:'synthetic-report',status:'PROVISIONAL',policy_confirmed:false,as_of:'2026-11-09T00:00:00Z',is_stale:false,scope:{},rows:[],totals,source_snapshot:{report_id:'synthetic-source',as_of:'2026-11-09T00:00:00Z',scope:{}}};
describe('materialized analytics response validation',()=>{
  it('accepts a traced ready snapshot and a separate disabled pending state',()=>{
    expect(parseAnalyticsReport(ready)).toBe(ready);
    const pending={...ready,enabled:false,report_available:false,status:'PENDING',as_of:null,report_id:null,source_snapshot:null,totals:{impressions:null,matched_clicks:null,early_clicks:null,unmatched_clicks:null,pending_clicks:null,ctr_percent:null}};
    expect(parseAnalyticsReport(pending)).toBe(pending);
  });
  it.each([null,{},[],{...ready,report_available:'true'},{...ready,totals:{}},{...ready,totals:{...totals,impressions:'0'}},{...ready,totals:{...totals,matched_clicks:-1}},{...ready,source_snapshot:null},{...ready,as_of:'not-a-date'},{...ready,rows:[{date_kst:'invalid'}]}])('rejects malformed successful HTTP data %j',invalid=>{
    expect(()=>parseAnalyticsReport(invalid)).toThrow('성과 리포트 응답 형식');
  });
});
