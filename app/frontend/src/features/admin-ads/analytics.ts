import {ApiError} from '../../shared/api';
export type AnalyticsCounts={impressions:number|null;matched_clicks:number|null;early_clicks:number|null;unmatched_clicks:number|null;pending_clicks:number|null;ctr_percent:number|null};
export type AnalyticsRow={date_kst:string;ad_id:string;placement:'top'|'bottom';media_id:string}&AnalyticsCounts;
export type AnalyticsReport={enabled?:boolean;report_available:boolean;report_id?:string|null;as_of:string|null;status:'PROVISIONAL'|'PENDING';policy_confirmed:boolean;report_policy_version?:string;is_stale:boolean;scope:Record<string,string>;rows:AnalyticsRow[];totals:AnalyticsCounts;source_snapshot?:{report_id:string;as_of:string;scope:Record<string,string>}|null;aggregation_interval_seconds?:number};
export type AnalyticsFilters={date_from:string;date_to:string;ad_id:string;placement:string;media_id:string;cutoff:string};
export const emptyAnalyticsFilters:AnalyticsFilters={date_from:'',date_to:'',ad_id:'',placement:'',media_id:'',cutoff:''};
function validDay(value:string){
  return /^\d{4}-\d{2}-\d{2}$/.test(value)&&Number.isFinite(Date.parse(value))&&new Date(value).toISOString().slice(0,10)===value;
}
export function analyticsQuery(filters:AnalyticsFilters):string{
  const from=filters.date_from,to=filters.date_to;
  if(Boolean(from)!==Boolean(to))throw new Error('조회 시작일과 종료일을 함께 입력해 주세요.');
  if(from&&(!validDay(from)||!validDay(to)||from>to||(Date.parse(to)-Date.parse(from))/86400000>366))throw new Error('조회 기간은 시작일 이후 최대 366일 차이로 입력해 주세요.');
  if(!['','top','bottom'].includes(filters.placement))throw new Error('광고 위치를 확인해 주세요.');
  const query=new URLSearchParams();
  for(const field of ['date_from','date_to','ad_id','placement','media_id'] as const){
    const value=filters[field].trim();
    if(['ad_id','media_id'].includes(field)&&value.length>128)throw new Error('광고·매체 ID는 128자 이하로 입력해 주세요.');
    if(value)query.set(field,value);
  }
  if(filters.cutoff){
    if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(filters.cutoff)||!validDay(filters.cutoff.slice(0,10)))throw new Error('관찰 종료 시각을 확인해 주세요.');
    const when=new Date(filters.cutoff+':00+09:00');
    if(!Number.isFinite(when.getTime())||filters.cutoff.slice(11,13)>'23'||filters.cutoff.slice(14,16)>'59')throw new Error('관찰 종료 시각을 확인해 주세요.');
    query.set('cutoff',when.toISOString());
  }
  return query.toString();
}
export function dailyAnalytics(rows:AnalyticsRow[]){
  const byDay=new Map<string,{day:string;impressions:number;clicks:number;ctr:number|null}>();
  for(const row of rows){
    const value=byDay.get(row.date_kst)||{day:row.date_kst,impressions:0,clicks:0,ctr:null};
    value.impressions+=row.impressions??0;value.clicks+=row.matched_clicks??0;
    value.ctr=value.impressions?value.clicks*100/value.impressions:null;byDay.set(value.day,value);
  }
  return [...byDay.values()].sort((a,b)=>a.day.localeCompare(b.day));
}

function record(value:unknown):value is Record<string,unknown>{return value!==null&&typeof value==='object'&&!Array.isArray(value);}
function stringScope(value:unknown){return record(value)&&Object.values(value).every(item=>typeof item==='string');}
function validInstant(value:unknown):value is string{return typeof value==='string'&&/(?:Z|[+-]\d{2}:\d{2})$/.test(value)&&Number.isFinite(Date.parse(value));}
const counterNames=['impressions','matched_clicks','early_clicks','unmatched_clicks','pending_clicks'] as const;
function validCounts(value:unknown,available:boolean){
  return record(value)&&counterNames.every(name=>available?Number.isSafeInteger(value[name])&&Number(value[name])>=0:value[name]===null)
    &&(available?(value.ctr_percent===null||(typeof value.ctr_percent==='number'&&Number.isFinite(value.ctr_percent)&&value.ctr_percent>=0)):value.ctr_percent===null);
}
export function parseAnalyticsReport(value:unknown):AnalyticsReport{
  const fail=()=>{throw new ApiError('INVALID_RESPONSE','성과 리포트 응답 형식을 확인해 주세요.');};
  if(!record(value)||typeof value.report_available!=='boolean'||typeof value.is_stale!=='boolean'||typeof value.policy_confirmed!=='boolean'||(value.enabled!==undefined&&typeof value.enabled!=='boolean')||!stringScope(value.scope)||!Array.isArray(value.rows)||!validCounts(value.totals,value.report_available))return fail();
  if(value.report_policy_version!==undefined&&typeof value.report_policy_version!=='string')return fail();
  if(!value.report_available){
    if(value.status!=='PENDING'||value.as_of!==null||value.rows.length!==0)return fail();
  }else{
    const snapshot=value.source_snapshot;
    if(value.status!=='PROVISIONAL'||!validInstant(value.as_of)||typeof value.report_id!=='string'||!value.report_id||!record(snapshot)||typeof snapshot.report_id!=='string'||!snapshot.report_id||!validInstant(snapshot.as_of)||!stringScope(snapshot.scope))return fail();
    for(const row of value.rows){
      if(!record(row)||!validDay(String(row.date_kst))||typeof row.ad_id!=='string'||!row.ad_id||typeof row.media_id!=='string'||!row.media_id||!['top','bottom'].includes(String(row.placement))||!validCounts(row,true))return fail();
    }
  }
  return value as AnalyticsReport;
}
