import {key} from '../../shared/api';
export const BASE='/api/platform/v1';
let csrf='';
export const setCsrf=(value:string)=>{csrf=value;};
export async function raw<T>(path:string,method='GET',body?:unknown,headers:Record<string,string>={}):Promise<T>{
  const response=await fetch(path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json',...(method==='GET'?{}:{'X-CSRFToken':csrf,'Idempotency-Key':key()}),...headers},...(body===undefined?{}:{body:JSON.stringify(body)})});
  if(response.status===204)return undefined as T;
  const value=await response.json();
  if(!response.ok)throw new Error(value.error?.message||value.message||'요청을 처리하지 못했습니다.');
  return value as T;
}
export async function call<T>(path:string,method='GET',body?:unknown,headers:Record<string,string>={}):Promise<T>{
  const value=await raw<{data:T}|undefined>(BASE+path,method,body,headers);
  return value?.data as T;
}
function canonical(value:unknown):string{
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(value&&typeof value==='object')return '{'+Object.entries(value).sort(([a],[b])=>a<b?-1:a>b?1:0).map(([k,v])=>JSON.stringify(k)+':'+canonical(v)).join(',')+'}';
  return JSON.stringify(value);
}
export const etag=(revision:number)=>({'If-Match':'"'+revision+'"'});
export async function ownerWrite<T>(path:string,method:string,body:unknown,revision:number,password:string):Promise<T>{
  if(!crypto.subtle)throw new Error('매장 권한 변경은 localhost 또는 HTTPS 접속에서 진행해 주세요.');
  const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(canonical(body)));
  const body_sha256=Array.from(new Uint8Array(digest),v=>v.toString(16).padStart(2,'0')).join('');
  const grant=await call<{grant:string}>('/reauthentications','POST',{password,intent:{method,path:BASE+path,body_sha256,if_match:etag(revision)['If-Match']}});
  return call<T>(path,method,body,{...etag(revision),'X-Reauthentication-Grant':grant.grant});
}
export type Account={account_id:string;username:string;nickname:string;memberships:{type:'CONSUMER'|'BUSINESS';status:string}[];email_masked:string;email_status:string;email_pending_until:string|null;email_challenge_id?:string|null};
export type Policy={version:string;documents:Record<string,string>};
export const labels:Record<string,string>={OWNER:'소유자',MANAGER:'매니저',STAFF:'직원',DRAFT:'초안',SUBMITTED:'심사 대기',APPROVED:'승인 완료',NEEDS_MORE:'보완 요청',REJECTED:'거절',PENDING:'수락 대기',ACCEPTED:'수락 완료',REVOKED:'회수',DECLINED:'거절',EXPIRED:'만료',CANCELLED:'취소'};
