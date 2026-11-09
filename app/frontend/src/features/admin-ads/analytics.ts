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
