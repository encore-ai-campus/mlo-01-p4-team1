import {useState} from 'react';
import {SignupForm,LoginForm} from './AuthForms';
import {PlatformRecovery,PlatformLegal,type PlatformAdmin} from './PlatformSupport';
import {call,raw,type Account,type Policy} from './api';
import './platform-public-design.css';

const pages:Record<string,{title:string;subtitle:string}>={
  '/platform/admin':{title:'플랫폼 관리자',subtitle:'사용자와 사업자가 하나의 계정으로 연결됩니다.'},
  '/platform/consumer':{title:'핏플레이스 · 장소 탐색',subtitle:'사용자와 사업자가 하나의 계정으로 연결됩니다.'},
  '/platform/login':{title:'로그인',subtitle:'하나의 계정으로 서비스를 이용하세요.'},
  '/platform/signup':{title:'회원가입',subtitle:'하나의 계정으로 시작하세요.'},
  '/platform/account/recovery':{title:'계정 찾기',subtitle:'사용자·사업자가 함께 쓰는 공통 계정을 찾습니다.'},
  '/platform/admin/login':{title:'메인 플랫폼 관리자 로그인',subtitle:'메인 플랫폼 운영자 계정으로 로그인해 주세요.'},
  '/platform/terms':{title:'이용약관',subtitle:'안내 내용을 확인해 주세요.'},
  '/platform/privacy':{title:'개인정보 처리방침',subtitle:'안내 내용을 확인해 주세요.'},
  '/platform/privacy-contact':{title:'개인정보 관련 문의',subtitle:'안내 내용을 확인해 주세요.'},
};
export const isPublicDesignPath=(path:string)=>Boolean(pages[path]) && path!=='/platform/admin' && path!=='/platform/consumer';

export function PlatformPublicDesign({path,account,admin,policy,loading,error}:{path:string;account:Account|null;admin:PlatformAdmin|null;policy:Policy|null;loading:boolean;error:string}){
  const [menuOpen,setMenuOpen]=useState(false);
  const page=pages[path];
  const logout=async()=>{try{if(account)await call('/sessions/current','DELETE');else await raw('/api/auth/logout','POST',{});location.assign('/platform');}catch{location.reload();}};
  return <div className="platform-page p-design">
    <header className="pd-header"><a href="/platform" className="pd-brand">itda</a>
      <nav className="pd-desktop-nav" aria-label="상단 메뉴"><a href="/venues">핏플레이스</a><a href="/playlist">muther</a>{account||admin?.role==='ADMIN'?<><a href={account?'/platform/account':'/platform/admin'}>{account?'마이페이지':'관리자'}</a><button type="button" onClick={()=>void logout()}>로그아웃</button></>:<><a href="/platform/login">로그인</a><a href="/platform/signup">회원가입</a></>}</nav>
      <nav className="pd-mobile-nav" aria-label="모바일 메뉴"><a href={account?'/platform/account':'/platform/login'}>{account?'마이페이지':'로그인'}</a><button type="button" aria-expanded={menuOpen} aria-controls="pd-menu" onClick={()=>setMenuOpen(!menuOpen)}>메뉴</button>{menuOpen&&<div id="pd-menu" className="pd-menu"><a href="/venues">핏플레이스</a><a href="/playlist">muther</a>{account||admin?.role==='ADMIN'?<button type="button" onClick={()=>void logout()}>로그아웃</button>:<a href="/platform/signup">회원가입</a>}</div>}</nav>
    </header>
    <main className="pd-main"><div className="pd-content"><h1>{page.title}</h1><p className="pd-subtitle">{page.subtitle}</p>
      {error&&<div className="p-alert error" role="alert">{error}</div>}
      {loading?<p role="status">플랫폼 연결 중…</p>:path==='/platform/admin'?<section className="p-card p-auth pd-access-gate"><h2>플랫폼 관리자 로그인이 필요합니다</h2><p>공통 계정과 매장 관리권한 심사를 관리하는 운영자 전용 공간입니다.</p><a className="p-link-button" href="/platform/admin/login">플랫폼 관리자 로그인</a></section>:path==='/platform/consumer'?<section className="p-card p-auth pd-access-gate"><h2>계정으로 시작하세요</h2><p>로그인 후 이용 유형과 매장 연결 상태를 확인합니다.</p><a className="p-link-button" href="/platform/login">로그인</a></section>:path==='/platform/admin/login'&&!admin?null:path==='/platform/signup'||path==='/platform/login'||path==='/platform/admin/login'?<section className="p-card p-auth">{account?<><h2>이미 로그인되어 있습니다</h2><a className="p-link-button" href="/platform">내 서비스로 이동</a></>:path==='/platform/signup'?<SignupForm policy={policy}/>:<LoginForm administrator={path==='/platform/admin/login'}/>}</section>:path==='/platform/account/recovery'?<PlatformRecovery/>:<PlatformLegal path={path} policy={policy}/>}
    </div></main>
    <footer className="pd-footer"><nav aria-label="법적 고지 및 문의"><a href="/platform/terms">이용약관</a><span aria-hidden="true">·</span><a href="/platform/privacy">개인정보 처리방침</a><span aria-hidden="true">·</span><a href="/platform/privacy-contact">개인정보 관련 문의</a></nav><p>통합 플랫폼 · 서비스 정보는 시안용입니다</p></footer>
  </div>;
}
