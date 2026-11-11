import {NICKNAME_HELP,nicknameValid} from '../../shared/accountPolicy';
import {useEffect,useState} from 'react';
import {call,raw,setCsrf,type Account,type Policy} from './api';
import {SignupForm,LoginForm} from './AuthForms';
import {WorkspacePanel} from './WorkspacePanel';
import {ReviewPanel} from './ReviewPanel';
import {PlatformAdminHome,PlatformRecovery,PlatformLegal,type PlatformAdmin} from './PlatformSupport';
import {AccountSecurity,PersonalRecords} from './AccountSecurity';
import './platform.css';
import {CinematicNavLink} from '../cinematic/CinematicNavLink';
import {VenueExplorePage} from '../venues/pages/VenueExplorePage';
import {OwnerSongRequestsPortal} from '../song-requests/SongRequests';

type Boot={account?:Account;session?:{csrf_token:string};csrf_token?:string;song_requests_only?:boolean;can_request_songs?:boolean};
type Admin=PlatformAdmin;
export function Platform(){
  const path=location.pathname;
  const [account,setAccount]=useState<Account|null>(null),[policy,setPolicy]=useState<Policy|null>(null),[admin,setAdmin]=useState<Admin|null>(null);
  const [loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  const [nickname,setNickname]=useState(''),[terms,setTerms]=useState(false);
  const [songPortal,setSongPortal]=useState<{canRequest:boolean}|null>(null);
  const types=account?.memberships.filter(m=>m.status==='ACTIVE').map(m=>m.type)||[];
  const adminSignedIn=admin?.role==='ADMIN'||admin?.passkey_required;
  const canReview=admin?.role==='ADMIN'&&admin.allowed_menus.some(menu=>menu.path==='/platform/review');
  useEffect(()=>{let active=true;void(async()=>{try{
    const boot=await call<Boot>('/sessions/current');if(!active)return;setCsrf(boot.session?.csrf_token||boot.csrf_token||'');
    if(boot.song_requests_only)setSongPortal({canRequest:boot.can_request_songs===true});
    const [p,a]=await Promise.all([call<{version:string;documents:{kind:string;text:string}[]}>('/policies'),raw<Admin>('/api/auth/me')]);
    if(!active)return;setPolicy({version:p.version,documents:Object.fromEntries(p.documents.map(document=>[document.kind,document.text]))});setAdmin(a);
    if(boot.account){setAccount(boot.account);setNickname(boot.account.nickname);}
  }catch(e){if(active)setError((e as Error).message);}finally{if(active)setLoading(false);}})();return()=>{active=false;};},[]);
  async function run(work:()=>Promise<void>){setBusy(true);setError('');setMessage('');try{await work();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  async function enterType(type:'CONSUMER'|'BUSINESS'){
    const result=await call<{next_action:{path:string}}>('/me/context','PUT',{membership_type:type});location.assign(result.next_action.path);
  }
  async function addType(type:'CONSUMER'|'BUSINESS'){
    await call('/me/memberships','POST',{membership_type:type,policy_version:policy?.version,acknowledgements:{terms:true,privacy_notice:true}});await enterType(type);
  }
  const titles:Record<string,string>={'/platform/signup':'하나의 계정으로 시작하세요','/platform/login':'로그인','/platform/admin/login':'메인 플랫폼 관리자 로그인','/platform/admin':'플랫폼 관리자','/platform/account/recovery':'계정 찾기','/platform/terms':'이용약관','/platform/privacy':'개인정보 처리방침','/platform/privacy-contact':'개인정보 관련 문의','/platform/business':'뮤더 · 매장과 팀 관리','/platform/review':'매장 관리권한 심사','/platform/account':'내 계정과 이용 유형','/platform/consumer':'핏플레이스 · 장소 탐색'};
  const signup=path==='/platform/signup',administratorLogin=path==='/platform/admin/login',login=path==='/platform/login'||administratorLogin;
  if(songPortal)return <div className="platform-page">
    <header className="p-header"><a className="p-logo" href="/platform/business">muther.</a>
      {account
        ? <button disabled={busy} onClick={()=>void run(async()=>{await call('/sessions/current','DELETE');location.assign('/platform/login');})}>로그아웃</button>
        : adminSignedIn&&<button disabled={busy} onClick={()=>void run(async()=>{
          const result=await raw<{logout_url:string|null}>('/api/auth/logout','POST',{});
          location.assign(result.logout_url||'/platform/admin/login');
        })}>로그아웃</button>}
    </header>
    <main className="p-main">
      {error&&<p className="p-alert error" role="alert">{error}</p>}
      {loading?<p role="status">계정을 확인하고 있습니다.</p>:path==='/platform/admin'?<PlatformAdminHome admin={admin}/>:
      ['/platform/terms','/platform/privacy','/platform/privacy-contact'].includes(path)?<PlatformLegal path={path} policy={policy}/>:
      !['/platform','/platform/business','/platform/login','/platform/admin/login'].includes(path)?
        <section className="p-card"><h1>매장 곡 신청</h1><a href="/platform/business">곡 신청으로 이동</a></section>:
      !account?<section className="p-card p-auth"><h1>{administratorLogin?'운영자 로그인':'곡 신청 로그인'}</h1><LoginForm administrator={administratorLogin} songPortal/></section>:
      songPortal.canRequest?<OwnerSongRequestsPortal/>:<section className="p-card"><h1>곡 신청 권한이 없습니다</h1><p>승인된 매장의 점주 계정으로 이용해 주세요.</p></section>}
    </main>
  </div>;
  if (path==='/platform/consumer' && account && types.includes('CONSUMER')) return <VenueExplorePage readOnly showServiceHeader enableAi showVisitRecommendations/>;
  return <div className="platform-page"><header className="p-header">
    <a className="p-logo" href="/platform"><b>◈</b> 메인 플랫폼 <small>이름 미정</small></a>
    <nav className="p-public-nav" aria-label="플랫폼 소개"><CinematicNavLink/><a href="/platform#services">서비스 소개</a></nav>
    <span className="p-demo">LOCAL MVP · 합성 정보만 사용</span>
    <div className="p-header-account">{loading?<span className="p-muted">연결 중…</span>:account?<>
      <a href="/platform/account">{account.nickname}님 · 마이페이지</a><button onClick={()=>void run(async()=>{await call('/sessions/current','DELETE');location.assign('/platform');})}>로그아웃</button>
    </>:adminSignedIn?<>
      <a href="/platform/admin">{admin.passkey_required?'관리자 · 추가 인증 대기':'플랫폼 관리자'}</a>
      <button onClick={()=>void run(async()=>{await raw('/api/auth/logout','POST',{});location.assign('/platform');})}>로그아웃</button>
    </>:<><a href="/platform/login">로그인</a><a className="p-header-signup" href="/platform/signup">회원가입</a></>}</div>
  </header>
  <div className={`p-layout ${account||canReview?'':'p-public-layout'}`}>
    {(account||canReview)&&<aside className="p-sidebar" aria-label="내 서비스 메뉴">
      {account&&<><p>MY ACCOUNT</p><a className={path==='/platform'?'active':''} href="/platform">서비스 선택</a><a className={path==='/platform/account'?'active':''} href="/platform/account">마이페이지</a><p>MY SERVICES</p>
        {types.includes('CONSUMER')&&<a className={path==='/platform/consumer'?'active':''} href="/platform/consumer">핏플레이스 · 장소 탐색</a>}
        {types.includes('BUSINESS')&&<a className={path==='/platform/business'?'active':''} href="/platform/business">사업자 · muther.</a>}</>}
      {canReview&&<><p>OPERATIONS</p><a className={path==='/platform/review'?'active':''} href="/platform/review">매장 승인 심사</a></>}
    </aside>}
  <main className="p-main"><div className="p-heading"><p className="p-eyebrow">ONE ACCOUNT, TWO SERVICES</p><h1>{titles[path]||(account?'이용할 서비스를 선택하세요':'음악과 일상을 잇는 시작점')}</h1><p>사용자와 사업자가 하나의 계정으로 연결됩니다.</p></div>
  {error&&<div className="p-alert error" role="alert">{error}</div>}{message&&<div className="p-alert" role="status">{message}</div>}
  {loading?<p role="status">플랫폼 연결 중…</p>:signup||login?<section className="p-card p-auth">
    {account?<><h2>이미 로그인되어 있습니다</h2><a className="p-link-button" href="/platform">내 서비스로 이동</a></>:signup?<SignupForm policy={policy}/>:<LoginForm administrator={administratorLogin}/>}
  </section>:
  path==='/platform/account/recovery'?<PlatformRecovery/>:
  ['/platform/terms','/platform/privacy','/platform/privacy-contact'].includes(path)?<PlatformLegal path={path} policy={policy}/>:path==='/platform/admin'?<PlatformAdminHome admin={admin}/>:path==='/platform/review'?(canReview?<ReviewPanel admin/>:<section className="p-card"><h2>접근 권한이 없습니다</h2><p>매장 심사 권한이 있는 관리자만 이용할 수 있습니다.</p><a href="/platform">메인으로 돌아가기</a></section>):
  !account&&path!=='/platform'?<section className="p-card"><h2>계정으로 시작하세요</h2><p>로그인 후 이용 유형과 매장 연결 상태를 확인합니다.</p><a className="p-link-button" href="/platform/login">로그인</a></section>:
  path==='/platform/account'&&account?<><section className="p-card"><h2>{account.nickname}님의 공통 계정</h2><dl className="p-facts"><div><dt>아이디</dt><dd>{account.username}</dd></div><div><dt>복구 이메일</dt><dd>{account.email_masked||'미등록 · 선택 사항'}</dd></div><div><dt>연결 유형</dt><dd>{types.map(type=>type==='BUSINESS'?'사업자':'사용자').join(' · ')}</dd></div></dl>{account.email_status==='PENDING'&&<p className="p-alert">이메일 확인이 대기 중입니다. <a href="#account-security">확인 코드 입력하기</a></p>}<form onSubmit={e=>{e.preventDefault();void run(async()=>{if(!nicknameValid(nickname))throw new Error(`별명은 ${NICKNAME_HELP}입니다.`);setAccount(await call<Account>('/me','PATCH',{nickname}));setMessage('별명을 변경했습니다.');});}}><label>별명<input value={nickname} onChange={e=>setNickname(e.target.value)} maxLength={10}/><small>{NICKNAME_HELP}</small></label><button disabled={busy}>별명 저장</button></form></section><section className="p-card"><h2>이용 유형 추가</h2><details><summary>적용 안내 확인</summary><p>{policy?.documents.terms}</p><p>{policy?.documents.privacy_notice}</p></details><label className="p-check"><input type="checkbox" checked={terms} onChange={e=>setTerms(e.target.checked)}/>약관에 동의하고 처리 안내를 확인했습니다.</label><div className="p-actions">{(['CONSUMER','BUSINESS'] as const).map(type=><button key={type} disabled={busy||!terms||types.includes(type)} onClick={()=>void run(()=>addType(type))}>{type==='BUSINESS'?'사업자':'사용자'} 유형 추가</button>)}</div></section><div id="account-security"><AccountSecurity account={account} policy={policy} onAccount={setAccount}/></div><PersonalRecords/></>:
  account&&['/platform/business','/platform/consumer'].includes(path)&&!types.includes(path==='/platform/business'?'BUSINESS':'CONSUMER')?<section className="p-card"><h2>이용 유형을 먼저 추가해 주세요</h2><a href="/platform/account">마이페이지에서 유형 추가</a></section>:
  path==='/platform/consumer'?(account?<VenueExplorePage readOnly enableAi showVisitRecommendations/>:<section className="p-card"><h2>핏플레이스 · 카페·주점 탐색</h2><p>공통 계정으로 로그인해 주세요.</p><a className="p-link-button" href="/platform/login">로그인</a></section>):
  path==='/platform/business'&&account?<WorkspacePanel account={account}/>:
  <>
    {!account&&<section className="p-hero"><div><p className="p-eyebrow">ONE ACCOUNT, TWO SERVICES</p><h2>일상의 음악부터,<br/>매장의 분위기까지.</h2><p>하나의 계정으로 핏플레이스와 muther.를 만나보세요.<br/>원하는 서비스부터 시작할 수 있습니다.</p><a className="p-link-button" href="/platform/signup">지금 시작하기</a><a className="p-hero-more" href="#services">서비스 둘러보기 →</a></div><div className="p-hero-graphic" aria-hidden="true"><span>나의 음악, 우리 매장</span><div><b>핏플레이스</b><b>muther.</b></div><small>하나의 계정으로 연결되는 두 서비스</small></div></section>}
    <div className="p-columns" id="services">{(['CONSUMER','BUSINESS'] as const).filter(type=>!account||types.includes(type)).map(type=><section className="p-card p-service-card" key={type}><p className="p-eyebrow">{type==='CONSUMER'?'FOR YOU':'FOR YOUR BUSINESS'}</p><h2>{type==='CONSUMER'?'핏플레이스':'매장 운영자를 위한 muther.'}</h2><p>{type==='CONSUMER'?'날씨와 분위기에 어울리는 카페와 주점을 찾아보세요.':'매장의 음악을 설정하고, 팀원과 함께 분위기를 관리하세요.'}</p>{account?<button className="p-primary" disabled={busy} onClick={()=>void run(()=>enterType(type))}>{type==='CONSUMER'?'핏플레이스로 이동':'muther.로 이동'}</button>:<a href={`/platform/signup?type=${type}`}>{type==='CONSUMER'?'일반 사용자로 시작하기':'매장 운영자로 시작하기'} →</a>}</section>)}</div>
    {account?<p className="p-muted">다른 서비스도 이용하고 싶으신가요? <a href="/platform/account">마이페이지에서 유형 추가</a></p>:<section className="p-service-note"><h2>하나의 계정, 나에게 맞는 이용</h2><p>가입할 때 첫 이용 유형을 선택하세요. 이후 같은 계정에 다른 유형을 추가할 수 있습니다.</p></section>}
  </>}
  <footer className="p-footer"><nav aria-label="법적 고지 및 문의"><a href="/platform/terms">이용약관</a><span aria-hidden="true">|</span><a href="/platform/privacy">개인정보 처리방침</a><span aria-hidden="true">|</span><a href="/platform/privacy-contact">개인정보 관련 문의</a></nav><p>로컬 MVP · 합성 정보만 사용</p></footer></main></div></div>;
}
