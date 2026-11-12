import {useEffect,useState,type ReactNode} from 'react';
import {api} from '../../shared/api';

/** Keep standalone legacy previews, but centralize every account entrance in platform mode. */
export function PlatformAlias({to,children}:{to:string;children:ReactNode}){
  const [legacy,setLegacy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{let active=true;void api<{platform_enabled:boolean}>('/api/auth/me').then(value=>{
    if(!active)return;
    if(value.platform_enabled){const target=new URL(to,location.origin);new URLSearchParams(location.search).forEach((paramValue,paramKey)=>{if(!target.searchParams.has(paramKey))target.searchParams.append(paramKey,paramValue);});location.replace(target.pathname+target.search+(location.hash||target.hash));}else setLegacy(true);
  }).catch(()=>{if(active)setError('계정 화면 연결을 확인하지 못했습니다. 새로고침해 주세요.');});return()=>{active=false;};},[to]);
  return legacy?<>{children}</>:<main className="account-layout"><p role={error?'alert':'status'}>{error||'공통 계정 화면으로 연결하고 있습니다…'}</p></main>;
}
