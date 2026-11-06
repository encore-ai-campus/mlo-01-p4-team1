import {useRef,useState,type FormEvent} from 'react';
import {call,raw,type Policy} from './api';
import {BusinessNotice} from './BusinessNotice';
import {nicknameValid,passwordChecks,passwordValid,NICKNAME_HELP,PASSWORD_HELP} from '../../shared/accountPolicy';

type Check={value:string;state:'idle'|'checking'|'available'|'taken'|'error';message:string};
const emptyCheck:Check={value:'',state:'idle',message:''};
const usernameValid=(value:string)=>/^[A-Za-z0-9_]{4,20}$/.test(value);

function PasswordInput({label,value,onChange,confirmation=false,signup=false,describedBy,invalid=false}:{label:string;value:string;onChange:(value:string)=>void;confirmation?:boolean;signup?:boolean;describedBy?:string;invalid?:boolean}){
  const [visible,setVisible]=useState(false);
  const id=signup?(confirmation?'platform-confirmation':'platform-password'):'platform-login-password';
  return <><label htmlFor={id}>{label}</label><div className="p-input-action">
    <input id={id} aria-label={signup?(confirmation?'가입 비밀번호 확인':'가입 비밀번호'):'로그인 비밀번호'} aria-describedby={describedBy} aria-invalid={invalid||undefined}
      type={visible?'text':'password'} required autoComplete={signup?'new-password':'current-password'} value={value} onChange={e=>onChange(e.target.value)}/>
    <button type="button" aria-label={`${confirmation?'비밀번호 확인':'비밀번호'} ${visible?'숨기기':'보기'}`} aria-pressed={visible} onClick={()=>setVisible(!visible)}>{visible?'숨기기':'보기'}</button>
  </div></>;
}

export function SignupForm({policy}:{policy:Policy|null}){
  const [username,setUsername]=useState(''),[nickname,setNickname]=useState(''),[password,setPassword]=useState(''),[confirmation,setConfirmation]=useState('');
  const [initial,setInitial]=useState<'CONSUMER'|'BUSINESS'>(new URLSearchParams(location.search).get('type')==='BUSINESS'?'BUSINESS':'CONSUMER');
  const [businessNotice,setBusinessNotice]=useState(initial==='BUSINESS');
  const [email,setEmail]=useState(''),[emailConsent,setEmailConsent]=useState(false),[terms,setTerms]=useState(false),[notice,setNotice]=useState(false),[age,setAge]=useState(false);
  const [usernameCheck,setUsernameCheck]=useState<Check>(emptyCheck),[nicknameCheck,setNicknameCheck]=useState<Check>(emptyCheck);
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  const generations=useRef({username:0,nickname:0});
  const form=useRef<HTMLFormElement>(null);
  const passwordLength=Array.from(password).length;
  const checks=passwordChecks(password);
  const validPassword=passwordValid(password);
  const matches=Boolean(confirmation)&&password===confirmation;

  function change(field:'username'|'nickname',value:string){
    setError('');
    generations.current[field]++;
    if(field==='username'){setUsername(value);setUsernameCheck(emptyCheck);}
    else{setNickname(value);setNicknameCheck(emptyCheck);}
  }
  async function check(field:'username'|'nickname',value:string):Promise<boolean>{
    const update=field==='username'?setUsernameCheck:setNicknameCheck;
    const generation=++generations.current[field];
    if(!(field==='username'?usernameValid(value):nicknameValid(value))){
      update({value,state:'error',message:field==='username'?'영문·숫자·밑줄 4~20자로 입력해 주세요.':`별명은 ${NICKNAME_HELP}입니다.`});return false;
    }
    update({value,state:'checking',message:'중복 여부를 확인하고 있습니다.'});
    try{
      const result=await call<{available:boolean}>('/availability','POST',{[field]:value});
      if(generations.current[field]!==generation)return false;
      update({value,state:result.available?'available':'taken',message:result.available?`사용 가능한 ${field==='username'?'아이디':'별명'}입니다.`:`이미 사용 중인 ${field==='username'?'아이디':'별명'}입니다.`});
      return result.available;
    }catch(e){if(generations.current[field]===generation)update({value,state:'error',message:(e as Error).message});return false;}
  }
  async function submit(event:FormEvent){
    event.preventDefault();setError('');
    if(!validPassword){setError(`비밀번호는 ${PASSWORD_HELP}입니다.`);form.current?.querySelector<HTMLInputElement>('#platform-password')?.focus();return;}
    if(!matches){setError('비밀번호 확인이 일치하지 않습니다.');form.current?.querySelector<HTMLInputElement>('#platform-confirmation')?.focus();return;}
    if(!terms||!notice||!age){setError('이용약관 동의·처리 안내 확인·만 14세 이상 확인이 필요합니다.');return;}
    const consent=Boolean(email)&&emailConsent;
    if(consent&&!form.current?.querySelector<HTMLInputElement>('#platform-email')?.validity.valid){setError('복구 이메일 주소를 확인해 주세요.');return;}
    setBusy(true);
    try{
      if((usernameCheck.state!=='available'||usernameCheck.value!==username)&&!await check('username',username))throw new Error('아이디 중복 확인 결과를 확인해 주세요.');
      if(nickname&&(nicknameCheck.state!=='available'||nicknameCheck.value!==nickname)&&!await check('nickname',nickname))throw new Error('별명 중복 확인 결과를 확인해 주세요.');
      const result=await call<{next_action:{path:string}}>('/signups','POST',{
        username,password,password_confirmation:confirmation,nickname,initial_membership:initial,policy_version:policy?.version,
        acknowledgements:{terms,privacy_notice:notice,age14:age},email_consent:consent,...(consent?{email}:{})
      });
      setEmail('');setPassword('');setConfirmation('');location.assign(result.next_action.path);
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }

  return <form ref={form} onSubmit={e=>void submit(e)} noValidate>
    {error&&<p className="p-alert error" role="alert">{error}</p>}
    <fieldset disabled={busy}>
      <legend>먼저 이용할 유형</legend>
      <div className="p-type-options">{(['CONSUMER','BUSINESS'] as const).map(type=><label key={type} className={initial===type?'selected':''}>
        <input type="radio" name="initial-type" checked={initial===type} onChange={()=>{setInitial(type);if(type==='BUSINESS')setBusinessNotice(true);}}/><strong>{type==='CONSUMER'?'사용자':'사업자'}</strong><small>{type==='CONSUMER'?'사용자 서비스로':'muther. 매장 연결로'}</small>
      </label>)}</div>
      <p className="p-muted">가입 후 같은 계정에 다른 유형도 추가할 수 있습니다.</p>
      {initial==='BUSINESS'&&<button type="button" className="p-text-button" onClick={()=>setBusinessNotice(true)}>사업자 가입 절차 다시 보기</button>}
      <label htmlFor="platform-username">아이디</label>
      <div className="p-input-action"><input id="platform-username" aria-label="가입 아이디" required minLength={4} maxLength={20} pattern="[A-Za-z0-9_]{4,20}" autoComplete="username"
        aria-describedby="username-help username-check" aria-invalid={['taken','error'].includes(usernameCheck.state)||undefined} value={username} onChange={e=>change('username',e.target.value)}/>
        <button type="button" disabled={!policy||!usernameValid(username)||usernameCheck.state==='checking'} onClick={()=>void check('username',username)}>아이디 중복 확인</button>
      </div>
      <p id="username-help" className="p-field-help">영문·숫자·밑줄 4~20자 · 대소문자 구분 없음 · 가입 후 변경 불가</p>
      <p id="username-check" className={`p-field-status ${usernameCheck.state}`} role="status">{usernameCheck.message}</p>
      <PasswordInput label="비밀번호" signup value={password} onChange={value=>{setPassword(value);setError('');}} describedBy="password-help password-length" invalid={Boolean(password)&&!validPassword}/>
      <p id="password-help" className="p-field-help">{PASSWORD_HELP}<br/>흔하거나 아이디와 비슷한 비밀번호는 사용할 수 없습니다.</p>
      {password&&<p id="password-length" className={`p-field-status ${validPassword?'available':'error'}`} role="status">{validPassword?'비밀번호 입력 조건을 충족했습니다.':`현재 ${passwordLength}자 · ${[!checks.length?'8~20자 필요':'',!checks.characters?'공백 없이 영문·숫자·특수문자만 가능':'',!checks.uppercase?'대문자 1개 이상 필요':'',!checks.specials?'특수문자 2개 이상 필요':''].filter(Boolean).join(' · ')}`}</p>}
      <PasswordInput label="비밀번호 확인" signup confirmation value={confirmation} onChange={value=>{setConfirmation(value);setError('');}} describedBy="password-match" invalid={Boolean(confirmation)&&!matches}/>
      <p id="password-match" className={`p-field-status ${confirmation?(matches?'available':'error'):''}`} role="status">{confirmation?(matches?'비밀번호가 일치합니다.':'비밀번호 확인이 일치하지 않습니다.'):''}</p>
      <label htmlFor="platform-nickname">별명 <small>(선택)</small></label>
      <div className="p-input-action"><input id="platform-nickname" aria-label="가입 별명" aria-describedby="nickname-help nickname-check" aria-invalid={['taken','error'].includes(nicknameCheck.state)||undefined} value={nickname} maxLength={10} autoComplete="nickname" onChange={e=>change('nickname',e.target.value)}/>
        <button type="button" disabled={!policy||!nickname||nicknameCheck.state==='checking'} onClick={()=>void check('nickname',nickname)}>별명 중복 확인</button>
      </div>
      <p id="nickname-help" className="p-field-help">중복 불가 · {NICKNAME_HELP}<br/>비워두면 중복되지 않는 회원####으로 생성합니다.</p>
      <p id="nickname-check" className={`p-field-status ${nicknameCheck.state}`} role="status">{nicknameCheck.message}</p>
      <div className="p-optional-email">
        <label htmlFor="platform-email">복구 이메일 <small>(선택)</small></label>
        <input id="platform-email" aria-label="가입 복구 이메일" type="email" maxLength={254} autoComplete="email" placeholder="이메일 없이도 가입할 수 있습니다" aria-describedby="email-help" value={email} onChange={e=>{setEmail(e.target.value);if(!e.target.value)setEmailConsent(false);}}/>
        <p id="email-help" className="p-field-help">아이디 찾기·비밀번호 재설정에만 사용합니다. 가입·로그인에는 이메일 인증이 필요하지 않습니다.</p>
        {email&&<details><summary>복구 이메일 수집·이용 안내</summary><p>{policy?.documents.email}</p></details>}
        <p className="p-field-help">아래 선택 동의에 체크하면 가입 후 확인 코드를 보냅니다. 가입 후 계정·보안 관리 → 복구 이메일 관리에서 코드 입력 후 ‘이메일 확인’을 눌러 주세요. 미동의 주소는 전송·저장하지 않습니다.</p>
      </div>
      <details><summary>이용약관과 개인정보 처리 안내</summary><p>{policy?.documents.terms}</p><p>{policy?.documents.privacy_notice}</p></details>
      <label className="p-check"><input type="checkbox" required checked={terms} onChange={e=>setTerms(e.target.checked)}/>[필수] 서비스 이용약관에 동의합니다.</label>
      <label className="p-check"><input type="checkbox" required checked={notice} onChange={e=>setNotice(e.target.checked)}/>[필수 확인] 기본 회원정보 처리 안내를 확인했습니다.</label>
      <label className="p-check"><input type="checkbox" required checked={age} onChange={e=>setAge(e.target.checked)}/>[필수 확인] 만 14세 이상입니다.</label>
      <label className="p-check"><input type="checkbox" checked={emailConsent} onChange={e=>{setEmailConsent(e.target.checked);if(!e.target.checked)setEmail('');}}/>[선택] 복구 이메일 수집·이용에 동의합니다.</label>
      <button className="p-primary" disabled={busy||!policy}>{busy?'가입 확인 중…':'회원가입하고 시작하기'}</button>
    </fieldset>
    <p className="p-auth-links">이미 계정이 있나요? <a href="/platform/login">로그인</a></p>
    <BusinessNotice open={businessNotice} onClose={()=>setBusinessNotice(false)}/>
  </form>;
}

export function LoginForm({administrator=false}:{administrator?:boolean}){
  const [username,setUsername]=useState(''),[password,setPassword]=useState(''),[remember,setRemember]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  async function submit(event:FormEvent){event.preventDefault();setBusy(true);setError('');
    try{
      if(administrator){
        const result=await raw<{passkey_required:boolean;role:string;allowed_menus:{path:string}[]}>('/api/auth/login','POST',{username,password,remember:false});
        if(!result.passkey_required&&result.role!=='ADMIN')throw new Error('플랫폼 관리자 계정을 확인해 주세요.');
        location.assign('/platform/admin');
      }else{
        const result=await call<{next_action:{path:string}}>('/sessions','POST',{username,password,remember});
        location.assign(result.next_action.path);
      }
    }catch(e){setError((e as Error).message);setPassword('');}finally{setBusy(false);}
  }
  return <form onSubmit={e=>void submit(e)}>
    {error&&<p className="p-alert error" role="alert">{error}</p>}
    <fieldset disabled={busy}>
      <label htmlFor="platform-login-username">아이디<input id="platform-login-username" aria-label="로그인 아이디" required maxLength={20} autoComplete="username" value={username} onChange={e=>setUsername(e.target.value)}/></label>
      <PasswordInput label="비밀번호" value={password} onChange={setPassword}/>
      {!administrator&&<label className="p-check"><input type="checkbox" checked={remember} onChange={e=>setRemember(e.target.checked)}/>로그인 유지 (7일)</label>}
      <button className="p-primary" disabled={busy}>{busy?'로그인 중…':'로그인'}</button>
    </fieldset>
    <p className="p-muted">{administrator?'메인 플랫폼 운영자 계정입니다. 1차 로그인 후 추가 인증 상태를 안내합니다.':'사용자·사업자 공통 로그인입니다. 가입된 이용 유형을 확인해 이동합니다.'}</p>
    {!administrator&&<><div className="p-auth-links"><a href="/platform/account/recovery">아이디·비밀번호 찾기</a><a href="/platform/signup">회원가입</a></div><p className="p-admin-entry"><a href="/platform/admin/login">플랫폼 관리자 로그인</a></p></>}
  </form>;
}
