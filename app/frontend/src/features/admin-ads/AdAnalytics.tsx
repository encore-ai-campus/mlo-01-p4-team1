import {useEffect,useRef,useState,type FormEvent} from 'react';
import {api} from '../../shared/api';
import {analyticsQuery,parseAnalyticsReport,dailyAnalytics,emptyAnalyticsFilters,type AnalyticsReport} from './analytics';

const number=(value:number|null)=>value===null?'—':value.toLocaleString('ko-KR');
const rate=(value:number|null)=>value===null?'—':value.toLocaleString('ko-KR',{maximumFractionDigits:4})+'%';
const instant=(value:string|null)=>value&&Number.isFinite(Date.parse(value))?new Date(value).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false})+' KST':'미집계';

export function AnalyticsResults({report}:{report:AnalyticsReport}){
  if(report.enabled===false)return <p className="ads-analytics-pending" role="status">성과 리포트가 아직 활성화되지 않았습니다. 운영 설정을 확인해 주세요.</p>;
  if(!report.report_available)return <p className="ads-analytics-pending" role="status">성과 집계를 기다리고 있습니다. 집계가 준비된 뒤 조회해 주세요. 현재 상태를 노출·클릭 0건으로 해석하지 않습니다.</p>;
  const metrics:[string,number|null,boolean?][]=[['유효 노출',report.totals.impressions],['연결 클릭',report.totals.matched_clicks],['CTR',report.totals.ctr_percent,true],['조기 클릭',report.totals.early_clicks],['미연결 클릭',report.totals.unmatched_clicks],['처리 대기 클릭',report.totals.pending_clicks]];
  const daily=dailyAnalytics(report.rows);
  return <>
    <p className="ads-stats-basis">관찰 종료 · {instant(report.as_of)} {report.is_stale&&<strong className="ads-analytics-stale">· 집계 지연</strong>}</p>
    {report.source_snapshot&&<p className="ads-stats-basis">원본 집계 스냅샷 기준 · {instant(report.source_snapshot.as_of)}</p>}
    <div className="ads-analytics-metrics">{metrics.map(([label,value,percentage])=><article className="card" key={label}><h3>{label}</h3><strong>{percentage?rate(value):number(value)}</strong></article>)}</div>
    <p className="ads-analytics-policy" role="note">잠정 집계 · {report.policy_confirmed?'정책 확인됨':'성과 보고 정책 확인 전'} · 노출에 연결된 클릭만 CTR에 포함합니다. 조기·미연결·처리 대기 클릭은 별도로 표시하며, 노출이 없으면 CTR은 —입니다.</p>
    {report.report_policy_version&&<p className="ads-stats-basis">지표 기준 · {report.report_policy_version}</p>}
    {!report.rows.length?<p role="status">선택한 조건의 집계 결과는 0건입니다.</p>:<>
      <h3>일자별 추이 · KST</h3>
      <div className="table-scroll"><table className="ads-table"><caption className="sr-only">일자별 유효 노출과 연결 클릭 및 합계 기준 CTR</caption><thead><tr><th>노출 귀속일</th><th>유효 노출</th><th>연결 클릭</th><th>CTR</th></tr></thead><tbody>{daily.map(row=><tr key={row.day}><td>{row.day}</td><td>{number(row.impressions)}</td><td>{number(row.clicks)}</td><td>{rate(row.ctr)}</td></tr>)}</tbody></table></div>
      <h3>광고·매체별 상세</h3>
      <div className="table-scroll"><table className="ads-table ads-analytics-table"><caption className="sr-only">광고 성과 상세 집계</caption><thead><tr>{['귀속일','광고 ID','위치','매체 ID','유효 노출','연결 클릭','CTR','조기','미연결','대기'].map(label=><th key={label}>{label}</th>)}</tr></thead><tbody>{report.rows.map(row=><tr key={JSON.stringify([row.date_kst,row.ad_id,row.placement,row.media_id])}><td>{row.date_kst}</td><td>{row.ad_id}</td><td>{row.placement==='top'?'상단':'하단'}</td><td>{row.media_id}</td><td>{number(row.impressions)}</td><td>{number(row.matched_clicks)}</td><td>{rate(row.ctr_percent)}</td><td>{number(row.early_clicks)}</td><td>{number(row.unmatched_clicks)}</td><td>{number(row.pending_clicks)}</td></tr>)}</tbody></table></div>
    </>}
  </>;
}

export function AdAnalytics(){
  const [filters,setFilters]=useState({...emptyAnalyticsFilters}),[report,setReport]=useState<AnalyticsReport|null>(null);
  const [loading,setLoading]=useState(true),[error,setError]=useState('');
  const current=useRef<AbortController|null>(null),applied=useRef('');
  async function load(query:string){
    current.current?.abort();const controller=new AbortController();current.current=controller;applied.current=query;
    setLoading(true);setError('');setReport(null);
    try{const value=await api<unknown>('/api/admin/ads/analytics'+(query?'?'+query:''),'GET',undefined,undefined,controller.signal);if(!controller.signal.aborted)setReport(parseAnalyticsReport(value));}
    catch(error){if(!controller.signal.aborted)setError((error as Error).message);}
    finally{if(!controller.signal.aborted)setLoading(false);}
  }
  useEffect(()=>{void load('');return()=>current.current?.abort();},[]);
  function submit(event:FormEvent){
    event.preventDefault();
    try{void load(analyticsQuery(filters));}catch(error){setError((error as Error).message);setReport(null);}
  }
  return <section className="card ads-analytics" aria-labelledby="ads-analytics-title"><h2 id="ads-analytics-title">광고 성과 리포트</h2>
    <p>유효 노출을 기준으로 집계한 성과입니다. 게시 관리의 원장·예상 사용량과 구분합니다.</p>
    <form className="ads-analytics-filters" onSubmit={submit}>
      <label>시작일 · KST<input type="date" value={filters.date_from} onChange={e=>setFilters({...filters,date_from:e.target.value})}/></label>
      <label>종료일 · KST<input type="date" value={filters.date_to} onChange={e=>setFilters({...filters,date_to:e.target.value})}/></label>
      <label>광고 ID<input maxLength={128} value={filters.ad_id} onChange={e=>setFilters({...filters,ad_id:e.target.value})}/></label>
      <label>노출 위치<select value={filters.placement} onChange={e=>setFilters({...filters,placement:e.target.value})}><option value="">전체</option><option value="top">상단</option><option value="bottom">하단</option></select></label>
      <label>매체 ID<input maxLength={128} value={filters.media_id} onChange={e=>setFilters({...filters,media_id:e.target.value})}/></label>
      <label>관찰 종료 · KST<input type="datetime-local" value={filters.cutoff} onChange={e=>setFilters({...filters,cutoff:e.target.value})}/></label>
      <button className="primary" disabled={loading}>{loading?'조회 중…':'성과 조회'}</button>
    </form>
    {error&&<p className="notice error" role="alert">{error} <button disabled={loading} onClick={()=>void load(applied.current)}>다시 조회</button></p>}
    {loading?<p role="status">성과 집계를 조회하고 있습니다.</p>:report&&<AnalyticsResults report={report}/>}
  </section>;
}
