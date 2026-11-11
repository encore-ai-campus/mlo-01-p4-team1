import {useEffect,useState,type FormEvent} from 'react';
import {call,type Policy} from './api';
import {passwordValid,PASSWORD_HELP} from '../../shared/accountPolicy';

type DashboardAccess={allowed:boolean;configured:boolean;path:string|null};
export type PlatformAdmin={role?:string;admin_role?:string|null;passkey_required?:boolean;platform_enabled?:boolean;private_demo_review_allowed?:boolean;csrf_token:string;allowed_menus:{path:string;label:string}[];operations_dashboard?:DashboardAccess;log_dashboard?:DashboardAccess};
const LEGACY_DASHBOARD_ROUTES=new Set(['/api/admin/operations/kibana','/api/admin/operations/dashboard','/api/admin/operations/logs']);
const OPERATIONS_PATH='/api/admin/operations/dashboard';
const BEHAVIOR_PATH='/api/admin/operations/logs';

function DashboardTile({label,description,expected,access}:{label:string;description:string;expected:string;access:DashboardAccess}){
  const connected=access.configured&&access.path===expected;
  return <article className="p-admin-dashboard-tile">
    <h4>{label}</h4><p>{description}</p>
    {connected?<a className="p-admin-dashboard-link" href={expected}>{label} 열기 ↗</a>
      :<span className="p-admin-dashboard-link p-admin-dashboard-unavailable" aria-disabled="true">연결 준비 중</span>}
  </article>;
}

export function PlatformAdminHome({admin}:{admin:PlatformAdmin|null}){
  if(admin?.passkey_required)return <section className="p-card p-admin-status"><span className="p-badge">추가 인증 대기</span><h2>관리자 1차 로그인을 완료했습니다</h2><p>메인 플랫폼의 관리자 계정이 확인되었습니다. 관리자 업무를 사용하려면 추가 인증을 마쳐야 합니다.</p><a className="p-link-button" href="/admin/passkey">패스키 인증</a></section>;
  if(admin?.role!=='ADMIN')return <section className="p-card"><h2>플랫폼 관리자 로그인이 필요합니다</h2><p>공통 계정과 매장 관리권한 심사를 관리하는 운영자 전용 공간입니다.</p><a className="p-link-button" href="/platform/admin/login">플랫폼 관리자 로그인</a></section>;
  const ads=admin.allowed_menus.some(menu=>menu.path==='/admin/ads');
  const behavior=admin.log_dashboard?.allowed===true?admin.log_dashboard:null;
  const operations=admin.operations_dashboard?.allowed===true?admin.operations_dashboard:null;
  const canReview=admin.platform_enabled===true||admin.private_demo_review_allowed===true;
  const remaining=admin.allowed_menus.filter(menu=>menu.path!=='/admin/ads'&&!LEGACY_DASHBOARD_ROUTES.has(menu.path)
    &&(menu.path!=='/platform/review'||canReview));
  const hasDashboards=ads||!!behavior||!!operations;
  return <section className="p-card p-admin-overview"><h2>메인 플랫폼 관리자</h2><p>이 계정에 허용된 관리자 업무만 표시합니다.</p>
    {hasDashboards&&<><h3>대시보드</h3><div className="p-admin-dashboard-grid" aria-label="관리자 전용 대시보드">
      {ads&&<article className="p-admin-dashboard-tile"><h4>광고 대시보드</h4><p>광고 등록·게시 관리와 노출·클릭 현황을 확인합니다.</p><a className="p-admin-dashboard-link" href="/admin/ads">광고 대시보드 열기 →</a></article>}
      {behavior&&<DashboardTile label="사용자 행동 분석" description="Kibana 사용자 행동 분석 대시보드입니다." expected={BEHAVIOR_PATH} access={behavior}/>}
      {operations&&<DashboardTile label="운영 대시보드" description="Kibana 서비스 운영 지표 대시보드입니다." expected={OPERATIONS_PATH} access={operations}/>}
    </div></>}
    {remaining.length>0&&<><h3>기타 관리 업무</h3><div className="p-admin-menu">{remaining.map(menu=><a key={menu.path} href={menu.path}>{menu.label} →</a>)}</div></>}
    {!hasDashboards&&remaining.length===0&&<p>허용된 관리자 메뉴가 없습니다. 운영 권한을 확인해 주세요.</p>}
  </section>;
}

type RecoveryLink={purpose:'ID_HINT'|'PASSWORD_RESET';token:string}|{invalid:true}|null;
function readLink():RecoveryLink{
  const fragment=location.hash.slice(1);
  if(fragment)history.replaceState(null,'',location.pathname+location.search);
  if(!fragment)return null;
  const [purpose,id,secret,...extra]=fragment.split('/');
  if(extra.length||!['username','password'].includes(purpose)||!/^[0-9a-f-]{36}$/i.test(id||'')||!secret)return {invalid:true};
  return {purpose:purpose==='username'?'ID_HINT':'PASSWORD_RESET',token:id+'.'+secret};
}
export function PlatformRecovery(){
  const [link,setLink]=useState<RecoveryLink>(readLink),[purpose,setPurpose]=useState<'ID_HINT'|'PASSWORD_RESET'>('ID_HINT');
  const [email,setEmail]=useState(''),[password,setPassword]=useState(''),[confirmation,setConfirmation]=useState('');
  const [error,setError]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState(false),[done,setDone]=useState(false);
  const [now,setNow]=useState(Date.now()),[resendUntil,setResendUntil]=useState(()=>{try{return Number(sessionStorage.getItem('platform-recovery-resend-until'))||0;}catch{return 0;}});
  useEffect(()=>{const timer=window.setInterval(()=>setNow(Date.now()),1000);return()=>window.clearInterval(timer);},[]);
  const cooldown=Math.max(0,Math.ceil((resendUntil-now)/1000));
  useEffect(()=>{const update=()=>{setLink(readLink());setError('');setMessage('');setDone(false);};window.addEventListener('hashchange',update);return()=>window.removeEventListener('hashchange',update);},[]);
  async function submit(event:FormEvent){event.preventDefault();setBusy(true);setError('');
    try{
      if(link&&'purpose' in link){
        if(link.purpose==='PASSWORD_RESET'&&!passwordValid(password))throw new Error(`새 비밀번호는 ${PASSWORD_HELP}입니다.`);
        if(link.purpose==='PASSWORD_RESET'&&password!==confirmation)throw new Error('비밀번호 확인이 일치하지 않습니다.');
        const result=await call<{username?:string}>('/recovery/completions','POST',{...link,...(link.purpose==='PASSWORD_RESET'?{new_password:password,new_password_confirmation:confirmation}:{})});
        setMessage(link.purpose==='ID_HINT'?`가입 아이디: ${result.username}`:'비밀번호를 재설정했습니다. 새 비밀번호로 공통 로그인해 주세요.');setDone(true);setLink(null);
      }else{
        const result=await call<{message:string}>('/recovery/requests','POST',{purpose,email});setMessage(result.message);
        const until=Date.now()+60000;setNow(Date.now());setResendUntil(until);
        try{sessionStorage.setItem('platform-recovery-resend-until',String(until));}catch{/* Functional timestamp only; no email or token is stored. */}
      }
    }catch(e){setError((e as Error).message);}finally{setBusy(false);setPassword('');setConfirmation('');}
  }
  const invalid=link&&'invalid' in link;
  return <section className="p-card p-auth"><h2>메인 플랫폼 계정 찾기</h2><p>사용자·사업자가 함께 쓰는 공통 계정을 찾습니다. 가입 후 확인을 완료한 복구 이메일이 필요합니다.</p>
    {error&&<p className="p-alert error" role="alert">{error}</p>}{message&&<p className="p-alert" role="status">{message}</p>}
    {invalid?<><p className="p-alert error" role="alert">복구 링크를 확인해 주세요. 새로운 안내 메일을 요청할 수 있습니다.</p><button onClick={()=>setLink(null)}>복구 메일 다시 요청</button></>:!done&&<form onSubmit={e=>void submit(e)}><fieldset disabled={busy}>
      {!link?<><label htmlFor="platform-recovery-purpose">찾을 정보<select id="platform-recovery-purpose" aria-label="찾을 정보" value={purpose} onChange={e=>setPurpose(e.target.value as typeof purpose)}><option value="ID_HINT">아이디 찾기</option><option value="PASSWORD_RESET">비밀번호 재설정</option></select></label><label htmlFor="platform-recovery-email">확인된 복구 이메일<input id="platform-recovery-email" type="email" autoComplete="email" required maxLength={254} value={email} onChange={e=>setEmail(e.target.value)}/></label></>:
      'purpose' in link&&link.purpose==='PASSWORD_RESET'?<><label htmlFor="platform-reset-password">새 비밀번호 (8~20자)<input id="platform-reset-password" type="password" required autoComplete="new-password" value={password} onChange={e=>setPassword(e.target.value)}/></label><label htmlFor="platform-reset-confirmation">새 비밀번호 확인<input id="platform-reset-confirmation" type="password" required autoComplete="new-password" value={confirmation} onChange={e=>setConfirmation(e.target.value)}/></label><p className="p-field-help">{PASSWORD_HELP}. 흔하거나 아이디와 비슷한 비밀번호는 사용할 수 없습니다.</p></>:<p>확인 버튼을 누르면 가입 아이디를 표시합니다. 복구 링크는 한 번만 사용할 수 있습니다.</p>}
      <button className="p-primary" disabled={busy||(!link&&cooldown>0)}>{busy?'처리 중…':link?'계정 복구 확인':'안내 메일 요청'}</button>
      {!link&&<p className="p-field-help">{cooldown>0?`재요청까지 ${cooldown}초 · 아이디 찾기와 비밀번호 재설정은 같은 주소의 60초 제한을 공유합니다.`:'같은 복구 이메일로는 60초 간격으로 안내 메일을 요청할 수 있습니다.'}</p>}
    </fieldset></form>}
    <p className="p-muted">복구 이메일을 등록·확인하지 않았다면 이메일로 계정을 찾을 수 없습니다.</p><div className="p-auth-links"><a href="/platform/login">메인 플랫폼 로그인</a><a href="/platform/signup">회원가입</a></div>
  </section>;
}

export function PlatformLegal({path,policy}:{path:string;policy:Policy|null}){
  if(path==='/platform/privacy-contact')return <section className="p-card p-policy-page"><h2>개인정보 관련 문의</h2><p className="p-alert" role="status">개인정보 문의처 미확정</p><p>현재 팀·평가자 대상 로컬 시연입니다. 공개 문의 이메일·담당자·문의 URL은 아직 정해지지 않았습니다.</p><p>시연 중 문의는 안내받은 프로젝트 담당 팀원에게 전달해 주세요. 공식 문의처가 확정되면 이 페이지에서 안내합니다.</p><p>문의처가 없는 상태에서 개인정보를 보내는 입력 폼은 제공하지 않습니다.</p><a href="/platform/privacy">개인정보 처리방침 보기</a></section>;
  const privacy=path==='/platform/privacy';
  return <article className="p-card p-policy-page"><h2>{privacy?'개인정보 처리방침':'서비스 이용약관'}</h2><p className="p-muted">적용 문서 버전: {policy?.version||'불러오는 중'}</p><p className="p-demo-note">로컬 시연용 검토 문서입니다. 실제 운영 전 운영 주체·문의처·보유 및 파기 기준 등을 확정해 게시합니다.</p><div className="p-policy-text">{privacy?policy?.documents.privacy_notice:policy?.documents.terms}</div>{privacy&&<><h3>선택 복구 이메일</h3><div className="p-policy-text">{policy?.documents.email}</div><p>이메일 없이도 가입·로그인이 가능하며, 동의하지 않은 이메일 입력은 전송·저장하지 않습니다.</p><p><a href="/platform/privacy-contact">개인정보 관련 문의</a></p></>}</article>;
}
