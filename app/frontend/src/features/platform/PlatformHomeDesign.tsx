import {useState} from 'react';
import {call,raw,type Account} from './api';
import type {PlatformAdmin} from './PlatformSupport';
import './platform-home-design.css';
import hero from './assets/platform-hero.jpg';
import fitplace from './assets/platform-fitplace.jpg';
import muther from './assets/platform-muther.jpg';

export function PlatformHomeDesign({account,admin,loading}:{account:Account|null;admin:PlatformAdmin|null;loading:boolean}){
  const [open,setOpen]=useState(false);
  const [error,setError]=useState('');
  const loggedIn=!!account||admin?.role==='ADMIN'||!!admin?.passkey_required;
  async function logout(){try{if(account)await call('/sessions/current','DELETE');else await raw('/api/auth/logout','POST',{});location.assign('/platform');}catch(e){setError((e as Error).message);}}
  return <div className="platform-home-design">
    <header className="ph-header"><a className="ph-brand" href="/platform">itda</a>
      <nav className="ph-desktop-nav" aria-label="상단 메뉴"><a href="/venues">핏플레이스</a><a href="/muther">muther</a>{loading?<span>연결 중…</span>:loggedIn?<><a href={account?'/platform/account':'/platform/admin'}>{account?'마이페이지':'관리자'}</a><button onClick={()=>void logout()}>로그아웃</button></>:<><a href="/platform/login">로그인</a><a href="/platform/signup">회원가입</a></>}</nav>
      <nav className="ph-mobile-nav" aria-label="모바일 메뉴"><a href={account?'/platform/account':loggedIn?'/platform/admin':'/platform/login'}>{account?'마이페이지':loggedIn?'관리자':'로그인'}</a><button aria-expanded={open} aria-controls="ph-mobile-menu" onClick={()=>setOpen(!open)}>메뉴</button>{open&&<div className="ph-menu" id="ph-mobile-menu"><a href="/venues">핏플레이스</a><a href="/muther">muther</a>{loggedIn?<button onClick={()=>void logout()}>로그아웃</button>:<a href="/platform/signup">회원가입</a>}</div>}</nav>
    </header>
    {error&&<p className="ph-error" role="alert">{error}</p>}
    <div className="ph-hero" aria-hidden="true"><img src={hero} alt=""/></div>
    <main className="ph-main"><div className="ph-heading"><h1>일상에 맞는 장소와 음악</h1><p>핏플레이스와 뮤더를 만나보세요</p></div>
      <div className="ph-cards" id="services">
        <article className="ph-card"><div className="ph-card-image ph-fit-image"><img src={fitplace} alt=""/></div><div className="ph-card-copy"><p className="ph-label ph-fit">FIT PLACE</p><h2>취향과 입맛을 잇다</h2><p className="ph-description">기분과 취향, 예산을 고려하여<br/>주변의 맛집을 연결해드려요.</p><a href="/venues">핏플레이스 시작하기 →</a></div></article>
        <article className="ph-card"><div className="ph-card-image ph-muther-image"><img src={muther} alt=""/></div><div className="ph-card-copy"><p className="ph-label ph-muther">muther</p><h2>날씨와 음악을 잇다</h2><p className="ph-description">기분의 맞는 음악을 설정하고<br/>오늘의 날씨를 느껴보세요.</p><a href="/muther">뮤더 시작하기 →</a></div></article>
      </div>
    </main>
    <footer className="ph-footer"><nav aria-label="법적 고지 및 문의"><a href="/platform/terms">이용약관</a><span>·</span><a href="/platform/privacy">개인정보 처리방침</a><span>·</span><a href="/platform/privacy-contact">개인정보 관련 문의</a></nav><p><a href="/platform/admin">관리자 페이지</a></p></footer>
  </div>;
}
