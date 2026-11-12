import {Icon} from './Icons';
import {useEffect,useState} from 'react';
import {api} from '../shared/api';
import './header-brand.css';

type Page='home'|'playlist';
export function Header({login=false,showGuide,isDemo=false,musicSource,weatherIsTest,currentPage='home',onNavigate}:{
  login?:boolean;showGuide?:()=>void;isDemo?:boolean;musicSource?:string;weatherIsTest?:boolean;
  currentPage?:Page;onNavigate?:(page:Page)=>void;
}){
  const [account,setAccount]=useState<{role?:string;nickname?:string;platform_enabled?:boolean}|null>(null);
  useEffect(()=>{if(!login)void api<{role?:string;nickname?:string;platform_enabled?:boolean}>('/api/auth/me').then(setAccount).catch(()=>{});},[login]);
  const weatherSource=weatherIsTest===false?'기상청 날씨':weatherIsTest===true?'날씨 샘플':'';
  function selectPage(event:React.MouseEvent<HTMLAnchorElement>,page:Page){
    if(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey||!onNavigate)return;
    event.preventDefault();onNavigate(page);
  }
  return <header className={'header'+(login?' login-header':'')} id="top">
    <a className="brand muther-brand" href="/muther" onClick={event=>selectPage(event,'home')} aria-label="muther · 오늘의 날씨를 들어보세요">
      <span className="muther-wordmark-image" aria-hidden="true"><img src="/muther-wordmark.png" alt="" /></span>
      <span className="muther-description">오늘의 날씨를 들어보세요</span>
    </a>
    {login?<nav aria-label="로그인 메뉴"><a className="home-link" href="/muther"><Icon name="home"/>선곡 페이지로 <Icon name="arrow"/></a></nav>:
      <nav className="muther-header-nav" aria-label="주 메뉴">
        <button type="button" onClick={showGuide}>사용 안내</button>
        <a href="/playlist" aria-current={currentPage==='playlist'?'page':undefined}
          onClick={event=>selectPage(event,'playlist')}>오늘의 선곡</a>
        <a href="/platform">itda</a>
        <a href="/venues">핏플레이스</a>
        <a href={account?.role==='USER'?'/platform/account':'/platform/login'} className="muther-nav-account">{account?.role==='USER'?`${account.nickname} · 마이페이지`:'로그인'}</a>
      </nav>}
    {isDemo&&!login&&<span className="demo-note" title={'음악: '+(musicSource==='TAGGED_JSON'?'태깅 JSON의 곡 정보':musicSource==='CSV'?'제공 CSV의 실제 곡 정보':'로컬 자료')+' · 날씨: '+(weatherSource||'지역 선택 후 조회')}>
      로컬 데모 · {weatherSource||'날씨 확인 전'}
    </span>}
  </header>;
}
