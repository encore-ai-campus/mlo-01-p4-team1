import {useEffect, useState, type ReactNode} from 'react';
import {api, canonical, key} from '../../shared/api';
import {useRef} from 'react';
import type {Option} from '../../shared/types';

export type AdminMe={authenticated:boolean;role?:string;allowed_menus:{path:string;label:string}[]};
export function useAdminSession(path:string){
  const [me,setMe]=useState<AdminMe|null>(null),[error,setError]=useState(''),[attempt,setAttempt]=useState(0);
  useEffect(()=>{const controller=new AbortController();setError('');
    void api<AdminMe>('/api/auth/me','GET',undefined,undefined,controller.signal).then(setMe).catch(e=>{if(!controller.signal.aborted)setError(e.message);});
    return()=>controller.abort();},[attempt]);
  return {me,error,setError,retry:()=>setAttempt(x=>x+1),allowed:!!(me?.authenticated&&me.role==='ADMIN'&&me.allowed_menus.some(m=>m.path===path))};
}
export function useAdminMutationKey(){
  const keys=useRef(new Map<string,string>());
  return (scope:string,body:unknown)=>{const signature=scope+canonical(body);if(!keys.current.has(signature))keys.current.set(signature,key());return keys.current.get(signature)!;};
}
export function useMusicLabels(){
  const [options,setOptions]=useState<Option[]>([]);
  useEffect(()=>{const c=new AbortController();void api<{items:Option[]}>('/api/recommendation-options','GET',undefined,undefined,c.signal).then(x=>setOptions(x.items)).catch(()=>{});return()=>c.abort();},[]);
  return {options,label:(value:unknown)=>Array.isArray(value)?value.map(code=>options.find(o=>o.code===code)?.label||String(code)).join(' · ')||'—':value==null||value===''?'—':String(value)};
}
export const number=(value:number|undefined)=>value===undefined?'—':value.toLocaleString('ko-KR');
export const dateTime=(value:unknown)=>{if(!value)return '—';const date=new Date(String(value));return Number.isNaN(date.getTime())?'—':date.toLocaleString('ko-KR',{timeZone:'Asia/Seoul'});};
export function Badge({children,tone='blue'}:{children:ReactNode;tone?:'blue'|'green'|'amber'|'muted'|'red'}){return <span className={'admin-status '+tone}>{children}</span>;}
export function Metrics({items}:{items:{label:string;value:ReactNode;icon:string;tone?:string}[]}){
  return <div className="admin-metrics">{items.map(item=><article className="admin-metric" key={item.label}><span className={'admin-metric-icon '+(item.tone||'')} aria-hidden="true">{item.icon}</span><div><span>{item.label}</span><strong>{item.value}</strong></div></article>)}</div>;
}
/** The former muther. administrator header is intentionally removed.
 * Legacy tool pages return to the single platform administrator console.
 */
export function PlatformAdminReturn(){
  return <nav className="admin-platform-return" aria-label="메인 플랫폼 관리자 이동"><a href="/platform/admin">← 메인 플랫폼 관리자</a></nav>;
}
export function AdminLayout({title,subtitle,error,onRetry,children}:{me:AdminMe|null;path:string;title:string;subtitle:string;error?:string;onRetry?:()=>void;children:ReactNode}){
  return <main className="admin admin-dashboard"><PlatformAdminReturn/><h1>{title}</h1><p className="admin-subtitle">{subtitle}</p>
    {error&&<p className="notice error" role="alert">{error}{onRetry&&<button onClick={onRetry}>다시 확인</button>}</p>}{children}
  </main>;
}
export function NoAdminAccess({loaded}:{loaded:boolean}){return <section className="admin-panel admin-empty" role="status">{loaded?<p>이 메뉴의 관리자 권한이 필요합니다. <a href="/platform/admin/login">로그인</a></p>:<p>관리자 권한을 확인하고 있습니다.</p>}</section>;}
