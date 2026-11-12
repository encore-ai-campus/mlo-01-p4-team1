import {useEffect,useState,useRef,type FormEvent} from 'react';
import {Header} from '../../components/Header';
import {api,setCsrf} from '../../shared/api';
import './accounts.css';
import {passwordValid,nicknameValid,PASSWORD_HELP,NICKNAME_HELP} from '../../shared/accountPolicy';

type Policy={version:string;ready:boolean;local_preview:boolean;documents:Record<string,string>};
type Profile={username:string;nickname:string;store_type:string;email_masked:string;email_verified:boolean;email_pending:boolean;email_pending_until:string|null};
type Saved={id:number;created_at:string;summary:string;track_count:number};
type Snapshot={summary:string;items:{track_id:string;track_name:string;artists_name:string}[]};
export function useAccountBoot(){
  const [policy,setPolicy]=useState<Policy|null>(null),[error,setError]=useState('');
  useEffect(()=>{let active=true;
    void api<{csrf_token:string}>('/api/auth/me').then(me=>{setCsrf(me.csrf_token);return api<Policy>('/api/accounts/policy');})
      .then(value=>{if(active)setPolicy(value);}).catch(e=>{if(active)setError(e.message);});
    return()=>{active=false;};
  },[]);
  return {policy,error};
}
function Shell({title,children}:{title:string;children:React.ReactNode}){
  return <><Header login/><main className="account-layout"><section className={`card account-card${['회원가입','계정 찾기','아이디 확인','비밀번호 재설정'].includes(title)?' account-signup':''}${['회원가입','계정 찾기'].includes(title)?' account-figma-text':''}`}><p className="account-brand">MUSIC X WEATHER</p><h1>{title}</h1>{children}</section></main></>;
}
function Secret({label,value,onChange,autoComplete='current-password',id}:{label:string;value:string;onChange:(v:string)=>void;autoComplete?:string;id:string}){
  const [show,setShow]=useState(false);
  return <label htmlFor={id}>{label}<span className="password-field"><input id={id} type={show?'text':'password'} required maxLength={128} autoComplete={autoComplete} value={value} onChange={e=>onChange(e.target.value)}/><button type="button" aria-label={show?'비밀번호 숨기기':'비밀번호 표시'} aria-pressed={show} onClick={()=>setShow(!show)}>{show?'숨기기':'보기'}</button></span></label>;
}
function Notice({error,message}:{error?:string;message?:string}){return <>{error&&<p className="notice error" role="alert">{error}</p>}{message&&<p className="notice" role="status">{message}</p>}</>;}
function PolicyView({policy,kind,onClose}:{policy:Policy;kind:string;onClose:()=>void}){
  const close=useRef<HTMLButtonElement>(null);
  useEffect(()=>{const previous=document.activeElement as HTMLElement|null;close.current?.focus();
    const key=(e:KeyboardEvent)=>{if(e.key==='Escape'){e.preventDefault();onClose();}if(e.key==='Tab'){e.preventDefault();close.current?.focus();}};
    document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);previous?.focus();};
  },[onClose]);
  return <div className="account-dialog-backdrop" onClick={onClose}><section role="dialog" aria-modal="true" aria-label="약관 및 처리 안내" className="account-dialog" onClick={e=>e.stopPropagation()}><h2>약관 및 처리 안내</h2><p className="account-policy-text">{policy.documents[kind]}</p><small>적용 버전: {policy.version}</small><button ref={close} type="button" onClick={onClose}>닫기</button></section></div>;
}

export function Signup(){
  const boot=useAccountBoot();
  const [username,setUsername]=useState(''),[checked,setChecked]=useState(''),[availability,setAvailability]=useState('');
  const [nickname,setNickname]=useState(''),[password,setPassword]=useState(''),[confirmation,setConfirmation]=useState(''),[email,setEmail]=useState('');
  const [terms,setTerms]=useState(false),[notice,setNotice]=useState(false),[age,setAge]=useState(false),[emailConsent,setEmailConsent]=useState(false);
  const [error,setError]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState(false),[dialog,setDialog]=useState('');
  const policy=boot.policy;
  async function submit(e:FormEvent){e.preventDefault();if(!policy)return;setBusy(true);setError('');
    try{if(!passwordValid(password))throw new Error(`비밀번호는 ${PASSWORD_HELP}입니다.`);
      if(password!==confirmation)throw new Error('비밀번호 확인이 일치하지 않습니다.');
      if(nickname&&!nicknameValid(nickname))throw new Error(`별명은 ${NICKNAME_HELP}입니다.`);
      if(checked!==username.toLowerCase())throw new Error('아이디 중복 확인을 완료해 주세요.');
      const result=await api<{nickname:string;email_pending:boolean;email_deferred?:boolean}>('/api/accounts/signup','POST',{
        username,password,password_confirmation:confirmation,nickname,policy_version:policy.version,
        terms_accepted:terms,privacy_acknowledged:notice,age_confirmed:age,email_consent:emailConsent,...(emailConsent?{email}:{})});
      setMessage(`${result.nickname}님, 가입이 완료되었습니다. 로그인해 주세요.${result.email_pending?' 복구 이메일 확인은 로그인 후 마이페이지에서 완료할 수 있습니다.':result.email_deferred?' 메일 요청 제한으로 주소 등록을 보류했습니다. 로그인 후 마이페이지에서 다시 등록해 주세요.':''}`);
    }catch(e){setError((e as Error).message);}finally{setPassword('');setConfirmation('');setBusy(false);}
  }
  return <Shell title="회원가입"><Notice error={boot.error||error} message={message}/>{policy?.local_preview&&<p className="account-preview">로컬 검증 화면 · 운영 전 최종 약관과 처리 안내가 필요합니다.</p>}
    {policy?.ready===false&&<p role="status">회원 정책 게시를 준비하고 있습니다. 기본 선곡 서비스는 비회원으로 이용할 수 있습니다.</p>}
    {!message&&<form onSubmit={e=>void submit(e)}>
      <label htmlFor="signup-username">아이디 <small>영문·숫자·밑줄 4~20자 · 가입 후 변경 불가</small></label>
      <div className="account-inline"><input id="signup-username" autoComplete="username" required minLength={4} maxLength={20} pattern="[A-Za-z0-9_]+" value={username} onChange={e=>{setUsername(e.target.value);setChecked('');setAvailability('');}}/>
        <button type="button" disabled={busy||!policy} onClick={async()=>{setError('');try{const result=await api<{available:boolean;username:string}>('/api/accounts/availability','POST',{username});setChecked(result.available?result.username:'');setAvailability(result.available?'사용 가능한 아이디입니다.':'이미 사용 중인 아이디입니다.');}catch(e){setError((e as Error).message);}}}>중복 확인</button></div>
      <p role="status">{availability}</p>
      <Secret label="비밀번호 (8~20자)" id="signup-password" autoComplete="new-password" value={password} onChange={setPassword}/>
      <p className="account-help">{PASSWORD_HELP}</p><Secret label="비밀번호 확인" id="signup-confirm" autoComplete="new-password" value={confirmation} onChange={setConfirmation}/>
      <label htmlFor="signup-nickname">별명 (선택)<input id="signup-nickname" autoComplete="nickname" maxLength={10} value={nickname} onChange={e=>setNickname(e.target.value)}/><small>{NICKNAME_HELP}. 중복 불가. 비워두면 회원#### 형태로 생성합니다.</small></label>
      <p>매장 유형: <strong>카페</strong></p>
      <label className="account-check"><input type="checkbox" checked={terms} onChange={e=>setTerms(e.target.checked)} required/>[필수] 서비스 이용약관 동의</label><button type="button" className="account-text-button" onClick={()=>setDialog('terms')}>이용약관 자세히 보기</button>
      <label className="account-check"><input type="checkbox" checked={notice} onChange={e=>setNotice(e.target.checked)} required/>[확인] 기본 회원정보 처리 안내 확인</label><button type="button" className="account-text-button" onClick={()=>setDialog('privacy_notice')}>기본정보 처리 안내 보기</button>
      <label className="account-check"><input type="checkbox" checked={age} onChange={e=>setAge(e.target.checked)} required/>만 14세 이상입니다.</label>
      <label className="account-check"><input type="checkbox" checked={emailConsent} onChange={e=>{setEmailConsent(e.target.checked);if(!e.target.checked)setEmail('');}}/>[선택] 계정 복구용 이메일 수집·이용 동의</label><button type="button" className="account-text-button" onClick={()=>setDialog('email')}>이메일 이용 안내 보기</button>
      {emailConsent&&<label htmlFor="signup-email">복구 이메일<input id="signup-email" type="email" autoComplete="email" required maxLength={254} value={email} onChange={e=>setEmail(e.target.value)}/></label>}
      <p className="account-help">이메일 없이도 가입·로그인이 가능합니다. 복구 수단을 등록하지 않고 아이디·비밀번호를 잊으면 자동 복구를 제공할 수 없습니다.</p>
      <button className="primary" disabled={busy||!policy?.ready}>{busy?'가입 처리 중…':'회원가입'}</button>
    </form>}
    {policy?.ready&&<div className="account-links">
      {policy.documents.privacy&&<button type="button" className="account-text-button" onClick={()=>setDialog('privacy')}>개인정보 처리방침 전문</button>}
      {policy.documents.privacy_contact&&<button type="button" className="account-text-button" onClick={()=>setDialog('privacy_contact')}>개인정보 문의 안내</button>}
    </div>}
    <p className="account-links"><a href="/login">로그인</a><a href="/">비회원으로 이용</a></p>
    {policy&&dialog&&<PolicyView policy={policy} kind={dialog} onClose={()=>setDialog('')}/>}
  </Shell>;
}

function readRecoveryLink(){
  const parts=location.hash.slice(1).split('/');
  if(location.hash)history.replaceState(null,'',location.pathname);
  return parts.length===3&&['username','password'].includes(parts[0])?{purpose:parts[0],id:parts[1],token:parts[2]}:null;
}
export function Recovery(){
  const boot=useAccountBoot();
  const [link,setLink]=useState(readRecoveryLink);
  const [purpose,setPurpose]=useState('username'),[email,setEmail]=useState(''),[password,setPassword]=useState(''),[confirmation,setConfirmation]=useState('');
  const [error,setError]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState(false),[done,setDone]=useState(false);
  useEffect(()=>{const update=()=>{setLink(readRecoveryLink());setDone(false);setError('');setMessage('');};
    window.addEventListener('hashchange',update);return()=>window.removeEventListener('hashchange',update);},[]);
  return <Shell title={link?(link.purpose==='username'?'아이디 확인':'비밀번호 재설정'):'계정 찾기'}><Notice error={boot.error||error} message={message}/>
    {!done&&<form onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{
      if(link){if(link.purpose==='password'&&!passwordValid(password))throw new Error(`비밀번호는 ${PASSWORD_HELP}입니다.`);
        if(link.purpose==='password'&&password!==confirmation)throw new Error('비밀번호 확인이 일치하지 않습니다.');const result=await api<{username?:string}>('/api/accounts/recovery/complete','POST',{...link,new_password:password,password_confirmation:confirmation});setMessage(link.purpose==='username'?`가입 아이디: ${result.username}`:'비밀번호를 재설정했습니다. 새 비밀번호로 로그인해 주세요.');setDone(true);}
      else{const result=await api<{message:string}>('/api/accounts/recovery','POST',{purpose,email});setMessage(result.message);}
    }catch(e){setError((e as Error).message);}finally{setPassword('');setConfirmation('');setBusy(false);}}}>
      {!link?<><label htmlFor="recovery-purpose">찾을 정보<select id="recovery-purpose" value={purpose} onChange={e=>setPurpose(e.target.value)}><option value="username">아이디 찾기</option><option value="password">비밀번호 재설정</option></select></label><label htmlFor="recovery-email">확인된 복구 이메일<input id="recovery-email" type="email" required value={email} onChange={e=>setEmail(e.target.value)}/></label></>:
        link.purpose==='password'?<><Secret label="새 비밀번호 (8~20자)" id="reset-password" value={password} onChange={setPassword} autoComplete="new-password"/><p className="account-help">{PASSWORD_HELP}</p><Secret label="새 비밀번호 확인" id="reset-confirm" value={confirmation} onChange={setConfirmation} autoComplete="new-password"/></>:<p>이 링크는 아이디 안내에만 사용되며 한 번 확인하면 만료됩니다.</p>}
      <button className="primary" disabled={busy||!boot.policy}>{busy?'처리 중…':link?'확인':'안내 메일 요청'}</button>
    </form>}<p className="account-links"><a href="/login">로그인</a><a href="/signup">회원가입</a></p>
  </Shell>;
}

export function MyPage(){
  const boot=useAccountBoot();
  const [profile,setProfile]=useState<Profile|null>(null),[nickname,setNickname]=useState(''),[email,setEmail]=useState(''),[code,setCode]=useState('');
  const [password,setPassword]=useState(''),[newPassword,setNewPassword]=useState(''),[confirmation,setConfirmation]=useState(''),[withdraw,setWithdraw]=useState('');
  const [emailPassword,setEmailPassword]=useState(''),[removePassword,setRemovePassword]=useState(''),[withdrawPassword,setWithdrawPassword]=useState('');
  const [consent,setConsent]=useState(false),[dialog,setDialog]=useState(false),[saved,setSaved]=useState<Saved[]>([]),[snapshot,setSnapshot]=useState<Snapshot|null>(null);
  const [error,setError]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState(false),[now,setNow]=useState(Date.now());
  async function refresh(){const value=await api<Profile>('/api/me/profile');setProfile(value);setNickname(value.nickname);setSaved((await api<{items:Saved[]}>('/api/me/playlists')).items);}
  useEffect(()=>{if(boot.policy)void refresh().catch(e=>setError(e.message));},[boot.policy]);
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer);},[]);
  async function action(run:()=>Promise<unknown>,success:string){setBusy(true);setError('');setMessage('');try{await run();await refresh();setMessage(success);}catch(e){setError((e as Error).message);}finally{setPassword('');setEmailPassword('');setRemovePassword('');setWithdrawPassword('');setNewPassword('');setConfirmation('');setBusy(false);}}
  const seconds=profile?.email_pending_until?Math.max(0,Math.ceil((Date.parse(profile.email_pending_until)-now)/1000)):0;
  return <Shell title="마이페이지"><Notice error={boot.error||error} message={message}/>{!profile?<p><a href="/login">회원 로그인이 필요합니다.</a></p>:<>
    <p>{profile.username} · 카페</p>
    <form onSubmit={e=>{e.preventDefault();void action(()=>{if(!nicknameValid(nickname))throw new Error(`별명은 ${NICKNAME_HELP}입니다.`);return api('/api/me/profile','PATCH',{nickname});},'별명을 변경했습니다.');}}><h2>회원정보</h2><label htmlFor="profile-nickname">별명<input id="profile-nickname" required minLength={2} maxLength={10} value={nickname} onChange={e=>setNickname(e.target.value)}/></label><button disabled={busy}>별명 변경</button></form>
    <section><h2>복구 이메일</h2><p>{profile.email_verified?`${profile.email_masked} · 확인 완료`:'확인된 복구 이메일이 없습니다.'}</p>
      <form onSubmit={e=>{e.preventDefault();void action(()=>api('/api/me/email','POST',{email,current_password:emailPassword,email_consent:consent,policy_version:boot.policy?.version}),'확인 코드를 요청했습니다. 기존 복구 주소는 새 주소 확인까지 유지됩니다.');}}>
        <label htmlFor="profile-email">등록·변경할 이메일<input id="profile-email" type="email" required value={email} onChange={e=>setEmail(e.target.value)}/></label>
        <Secret label="현재 비밀번호" id="email-password" value={emailPassword} onChange={setEmailPassword}/>
        <label className="account-check"><input type="checkbox" checked={consent} onChange={e=>{setConsent(e.target.checked);if(!e.target.checked)setEmail('');}} required/>[선택] 복구 이메일 수집·이용 동의</label><button className="account-text-button" type="button" onClick={()=>setDialog(true)}>이메일 이용 안내 보기</button>
        <button disabled={busy||!consent}>확인 코드 발송·재발송</button><small>재발송은 60초 간격으로 가능합니다.</small>
      </form>
      {profile.email_pending&&<form onSubmit={e=>{e.preventDefault();void action(()=>api('/api/me/email/verify','POST',{code}),'복구 이메일 확인을 완료했습니다.');}}>
        <label htmlFor="email-code">6자리 확인 코드<input id="email-code" inputMode="numeric" autoComplete="one-time-code" required pattern="[0-9]{6}" maxLength={6} value={code} onChange={e=>setCode(e.target.value)}/></label><p>남은 시간 {Math.floor(seconds/60)}:{String(seconds%60).padStart(2,'0')} · 최대 5회 확인</p><button disabled={busy||seconds===0}>복구 이메일 확인</button></form>}
      <form onSubmit={e=>{e.preventDefault();void action(()=>api('/api/me/email','DELETE',{current_password:removePassword}),'복구 이메일과 선택 동의를 삭제했습니다. 이메일 계정 복구가 중단됩니다.');}}>
        <Secret label="이메일 삭제 확인용 현재 비밀번호" id="remove-email-password" value={removePassword} onChange={setRemovePassword}/><button disabled={busy||(!profile.email_verified&&!profile.email_pending)}>이메일 삭제·동의 철회</button>
      </form>
    </section>
    <form onSubmit={e=>{e.preventDefault();void action(()=>{if(!passwordValid(newPassword))throw new Error(`비밀번호는 ${PASSWORD_HELP}입니다.`);if(newPassword!==confirmation)throw new Error('비밀번호 확인이 일치하지 않습니다.');return api('/api/me/password','POST',{current_password:password,new_password:newPassword,password_confirmation:confirmation});},'비밀번호를 변경했습니다. 다른 기기에서는 다시 로그인해야 합니다.');}}><h2>비밀번호 변경</h2>
      <Secret label="현재 비밀번호" id="change-password-current" value={password} onChange={setPassword}/><Secret label="새 비밀번호 (8~20자)" id="change-password-new" value={newPassword} onChange={setNewPassword} autoComplete="new-password"/><small>{PASSWORD_HELP}</small><Secret label="새 비밀번호 확인" id="change-password-confirm" value={confirmation} onChange={setConfirmation} autoComplete="new-password"/><button disabled={busy}>비밀번호 변경</button>
    </form>
    <section><h2>저장한 선곡표</h2><p>선곡 화면에서 ‘내 선곡표에 저장’을 누른 결과를 기기 간 확인할 수 있습니다.</p>{saved.length===0&&<p>저장한 선곡표가 없습니다.</p>}<ul className="account-saved">{saved.map(row=><li key={row.id}><span>{row.summary||'저장한 선곡표'} · {row.track_count}곡</span><button onClick={()=>void api<{playlist:Snapshot}>(`/api/me/playlists/${row.id}`).then(result=>setSnapshot(result.playlist)).catch(e=>setError(e.message))}>보기</button><button disabled={busy} onClick={()=>void action(()=>api(`/api/me/playlists/${row.id}`,'DELETE'),'저장 이력을 삭제했습니다.')}>삭제</button></li>)}</ul>
      {snapshot&&<div className="account-snapshot"><h3>{snapshot.summary||'저장한 선곡표'}</h3><ol>{snapshot.items.map(item=><li key={item.track_id}>{item.track_name} · {item.artists_name}</li>)}</ol><button onClick={()=>setSnapshot(null)}>선곡표 닫기</button></div>}
    </section>
    <form onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{await api('/api/me/withdraw','POST',{current_password:withdrawPassword,confirmation:withdraw});location.assign('/?withdrawn=1');}catch(e){setError((e as Error).message);}finally{setWithdrawPassword('');setBusy(false);}}}><h2>회원탈퇴</h2><p>계정과 복구 정보, 본인 저장 이력이 삭제됩니다.</p><Secret label="탈퇴 확인용 현재 비밀번호" id="withdraw-password" value={withdrawPassword} onChange={setWithdrawPassword}/><label htmlFor="withdraw-confirm">확인 문구 ‘회원탈퇴’ 입력<input id="withdraw-confirm" required value={withdraw} onChange={e=>setWithdraw(e.target.value)}/></label><button className="account-danger" disabled={busy}>회원탈퇴</button></form>
    <button onClick={()=>void api('/api/auth/logout','POST',{}).then(()=>location.assign('/')).catch(e=>setError(e.message))}>로그아웃</button>
    {dialog&&boot.policy&&<PolicyView policy={boot.policy} kind="email" onClose={()=>setDialog(false)}/>}
  </>}</Shell>;
}

function decode(value:string){return Uint8Array.from(atob(value.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));}
function encode(value:ArrayBuffer|null){return value?btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,''):null;}
export async function passkeyCeremony(mode:'register'|'authenticate',ticket:string){
  const options=await api<Record<string,unknown>>('/api/admin/passkeys/options','POST',{mode,enrollment_ticket:ticket});
  options.challenge=decode(options.challenge as string);
  for(const field of ['allowCredentials','excludeCredentials'])if(Array.isArray(options[field]))options[field]=(options[field] as {id:string}[]).map(c=>({...c,id:decode(c.id)}));
  let credential:PublicKeyCredential|null;
  if(mode==='register'){
    const user=options.user as Record<string,unknown>;user.id=decode(user.id as string);
    credential=await navigator.credentials.create({publicKey:options as unknown as PublicKeyCredentialCreationOptions}) as PublicKeyCredential|null;
  }else credential=await navigator.credentials.get({publicKey:options as unknown as PublicKeyCredentialRequestOptions}) as PublicKeyCredential|null;
  if(!credential)throw new Error('패스키 인증이 취소되었습니다.');
  const response=credential.response;
  const fields:Record<string,unknown>={clientDataJSON:encode(response.clientDataJSON)};
  if(mode==='register'){const r=response as AuthenticatorAttestationResponse;fields.attestationObject=encode(r.attestationObject);fields.transports=r.getTransports();}
  else{const r=response as AuthenticatorAssertionResponse;fields.authenticatorData=encode(r.authenticatorData);fields.signature=encode(r.signature);fields.userHandle=encode(r.userHandle);}
  return api<{landing_path:string}>('/api/admin/passkeys/verify','POST',{credential:{id:credential.id,rawId:encode(credential.rawId),type:'public-key',response:fields,clientExtensionResults:credential.getClientExtensionResults()},enrollment_ticket:ticket});
}
export function AdminPasskey(){
  useAccountBoot();
  const [ticket,setTicket]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  return <Shell title="관리자 패스키 인증"><p>관리자 계정의 패스키로 추가 인증해 주세요. 최초 등록·분실 복구에는 운영자의 본인 확인과 일회용 등록권이 필요합니다.</p><Notice error={error}/>
    <button className="primary" disabled={busy} onClick={async()=>{setBusy(true);setError('');try{const result=await passkeyCeremony('authenticate','');location.assign(result.landing_path);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>패스키로 인증</button>
    <form onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{const result=await passkeyCeremony('register',ticket);location.assign(result.landing_path);}catch(e){setError((e as Error).message);}finally{setTicket('');setBusy(false);}}}><label htmlFor="enrollment-ticket">최초 등록·분실 복구용 등록권<input id="enrollment-ticket" type="password" autoComplete="off" value={ticket} onChange={e=>setTicket(e.target.value)}/></label><button disabled={busy}>패스키 등록</button></form>
    <a href="/login">기본 인증 다시 진행</a>
  </Shell>;
}
