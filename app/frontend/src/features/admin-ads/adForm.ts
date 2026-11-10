import {canonical} from '../../shared/api';

export type AdForm={title:string;asset_id:string;link_url:string;placement:'top'|'bottom';starts_at:string;ends_at:string;priority:string;daily_budget_tokens:string;total_budget_tokens:string};
export type TimeChanges={starts_at:boolean;ends_at:boolean};
export const unchangedTimes=():TimeChanges=>({starts_at:false,ends_at:false});
export const kstMinute=(value:string)=>new Date(value).toLocaleString('sv-SE',{timeZone:'Asia/Seoul'}).replace(' ','T').slice(0,16);
function utcMinute(value:string){
  if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value))throw new Error('게시 시각을 확인해 주세요.');
  const result=new Date(value+':00+09:00');
  if(!Number.isFinite(result.getTime())||kstMinute(result.toISOString())!==value)throw new Error('게시 시각을 확인해 주세요.');
  return result.toISOString();
}
export function validatedAdFields(form:AdForm,original?:{starts_at:string;ends_at:string},changes=unchangedTimes()){
  if(!form.title.trim()||!form.starts_at||!form.ends_at||!form.link_url)throw new Error('제목·이동 링크·게시 기간을 입력해 주세요.');
  let url:URL;
  try{url=new URL(form.link_url);}catch{throw new Error('이동 링크는 절대 HTTPS 주소여야 합니다.');}
  if(url.protocol!=='https:'||!url.hostname||url.username||url.password||/[\s\\\u0000-\u001f]/.test(form.link_url))throw new Error('이동 링크는 절대 HTTPS 주소여야 합니다.');
  const priority=Number(form.priority);
  if(!form.priority.trim()||!Number.isSafeInteger(priority))throw new Error('우선순위는 정확하게 표현할 수 있는 정수로 입력해 주세요.');
  const daily=form.daily_budget_tokens.trim(),total=form.total_budget_tokens.trim();
  if(Boolean(daily)!==Boolean(total))throw new Error('예산은 일일·전체 한도를 함께 입력해 주세요.');
  const budgets:Partial<{daily_budget_tokens:number;total_budget_tokens:number}>={};
  if(daily){
    const d=Number(daily),t=Number(total);
    if(!Number.isSafeInteger(d)||!Number.isSafeInteger(t)||d<1||t<1)throw new Error('예산은 1~9,007,199,254,740,991 범위의 정수로 입력해 주세요.');
    budgets.daily_budget_tokens=d;budgets.total_budget_tokens=t;
  }
  const starts_at=original&&!changes.starts_at?original.starts_at:utcMinute(form.starts_at);
  const ends_at=original&&!changes.ends_at?original.ends_at:utcMinute(form.ends_at);
  if(!Number.isFinite(Date.parse(starts_at))||!Number.isFinite(Date.parse(ends_at))||Date.parse(starts_at)>=Date.parse(ends_at))throw new Error('게시 종료 시각은 시작 시각보다 이후여야 합니다.');
  return {title:form.title,link_url:form.link_url,placement:form.placement,starts_at,ends_at,priority,...budgets};
}
export class AdMutationIntents{
  private values=new Map<string,string>();
  constructor(private createKey:()=>string){}
  private signature(scope:string,body:unknown){return scope+':'+canonical(body);}
  key(scope:string,body:unknown){
    const signature=this.signature(scope,body);
    if(!this.values.has(signature))this.values.set(signature,this.createKey());
    return this.values.get(signature)!;
  }
  complete(scope:string,body:unknown){this.values.delete(this.signature(scope,body));}
  beginNew(){this.values.clear();}
}
