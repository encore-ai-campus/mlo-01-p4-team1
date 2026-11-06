import {useEffect,useState,type FormEvent} from 'react';
import {call,raw,setCsrf,type Account,type Policy} from './api';
import {passwordValid,PASSWORD_HELP} from '../../shared/accountPolicy';

export function AccountSecurity({account,policy,onAccount}:{account:Account;policy:Policy|null;onAccount:(value:Account)=>void}){
  const [email,setEmail]=useState(''),[emailPassword,setEmailPassword]=useState(''),[consent,setConsent]=useState(false),[code,setCode]=useState('');
  const [current,setCurrent]=useState(''),[password,setPassword]=useState(''),[confirmation,setConfirmation]=useState(''),[removePassword,setRemovePassword]=useState(''),[withdrawPassword,setWithdrawPassword]=useState(''),[withdraw,setWithdraw]=useState('');
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState(''),[now,setNow]=useState(Date.now());
  useEffect(()=>{const timer=window.setInterval(()=>setNow(Date.now()),1000);return()=>window.clearInterval(timer);},[]);
  async function action(work:()=>Promise<void>,success:string,refresh=true){setBusy(true);setError('');setMessage('');try{await work();if(refresh)onAccount(await call<Account>('/me'));setMessage(success);}catch(e){setError((e as Error).message);}finally{setBusy(false);setCurrent('');setPassword('');setConfirmation('');setEmailPassword('');setRemovePassword('');setWithdrawPassword('');}}
  const seconds=account.email_pending_until?Math.max(0,Math.ceil((Date.parse(account.email_pending_until)-now)/1000)):0;
  function submit(event:FormEvent,work:()=>Promise<void>,success:string){event.preventDefault();void action(work,success);}
  return <section className="p-card p-security"><h2>계정·보안 관리</h2>
    {error&&<p className="p-alert error" role="alert">{error}</p>}{message&&<p className="p-alert" role="status">{message}</p>}
    <details open={Boolean(account.email_challenge_id)}><summary>복구 이메일 관리</summary><p>{account.email_status==='VERIFIED'?`${account.email_masked} · 확인 완료`:account.email_status==='PENDING'?'복구 이메일 확인 대기':'확인된 복구 이메일이 없습니다.'}</p>
      {account.email_challenge_id&&<form onSubmit={event=>submit(event,async()=>{onAccount(await call<Account>('/me/email/verify','POST',{challenge_id:account.email_challenge_id,code}));setCode('');},'복구 이메일 확인을 완료했습니다.')}>
        <label htmlFor="central-email-code">6자리 이메일 확인 코드<input id="central-email-code" inputMode="numeric" autoComplete="one-time-code" required pattern="[0-9]{6}" maxLength={6} value={code} onChange={e=>setCode(e.target.value)}/></label>
        <p className="p-field-help">남은 시간 {Math.floor(seconds/60)}:{String(seconds%60).padStart(2,'0')} · 최대 5회 확인</p><button disabled={busy||seconds===0}>이메일 확인</button>
      </form>}
      <form onSubmit={event=>submit(event,async()=>{await call('/me/email','POST',{email,current_password:emailPassword,email_consent:consent,policy_version:policy?.version});setEmail('');setConsent(false);},'확인 코드를 발송했습니다. 새 주소 확인 전에는 기존 복구 주소가 유지됩니다.')}>
        <label htmlFor="central-email">등록·변경할 복구 이메일<input id="central-email" type="email" autoComplete="email" required maxLength={254} value={email} onChange={e=>setEmail(e.target.value)}/></label>
        <label htmlFor="central-email-password">이메일 변경 확인용 현재 비밀번호<input id="central-email-password" type="password" autoComplete="current-password" required value={emailPassword} onChange={e=>setEmailPassword(e.target.value)}/></label>
        <p className="p-policy-text">{policy?.documents.email}</p><label className="p-check"><input type="checkbox" required checked={consent} onChange={e=>{setConsent(e.target.checked);if(!e.target.checked)setEmail('');}}/>[선택] 복구 이메일 수집·이용에 동의합니다.</label><button disabled={busy||!consent}>확인 코드 발송·재발송</button><p className="p-field-help">재발송은 60초 간격으로 가능합니다. 이메일 없이도 가입·로그인할 수 있습니다.</p>
      </form>
      <form onSubmit={event=>submit(event,async()=>{await call('/me/email','DELETE',{current_password:removePassword});},'복구 이메일과 선택 동의를 삭제했습니다.')}><label htmlFor="central-remove-email-password">이메일 삭제 확인용 현재 비밀번호<input id="central-remove-email-password" type="password" autoComplete="current-password" required value={removePassword} onChange={e=>setRemovePassword(e.target.value)}/></label><button disabled={busy||account.email_status==='ABSENT'}>이메일 삭제·동의 철회</button></form>
    </details>
    <details><summary>비밀번호 변경</summary><form onSubmit={event=>submit(event,async()=>{
      if(!passwordValid(password))throw new Error(`새 비밀번호는 ${PASSWORD_HELP}입니다.`);
      if(password!==confirmation)throw new Error('비밀번호 확인이 일치하지 않습니다.');
      await call('/me/password','POST',{current_password:current,new_password:password,new_password_confirmation:confirmation});
      const boot=await call<{session:{csrf_token:string}}>('/sessions/current');setCsrf(boot.session.csrf_token);
    },'비밀번호를 변경했습니다. 다른 로그인 세션은 만료됩니다.')}>
      <label htmlFor="central-current-password">현재 비밀번호<input id="central-current-password" type="password" autoComplete="current-password" required value={current} onChange={e=>setCurrent(e.target.value)}/></label>
      <label htmlFor="central-new-password">새 비밀번호 (8~20자)<input id="central-new-password" type="password" autoComplete="new-password" required value={password} onChange={e=>setPassword(e.target.value)}/></label>
      <p className="p-field-help">{PASSWORD_HELP}</p>
      <label htmlFor="central-new-confirmation">새 비밀번호 확인<input id="central-new-confirmation" type="password" autoComplete="new-password" required value={confirmation} onChange={e=>setConfirmation(e.target.value)}/></label><button disabled={busy}>비밀번호 변경</button>
    </form></details>
    <details><summary>전체 계정 탈퇴</summary><p>사용자·사업자 유형을 포함한 공통 계정을 탈퇴합니다. 소유 매장의 관리책임을 먼저 정리해야 합니다.</p><form onSubmit={event=>{event.preventDefault();void action(async()=>{if(withdraw!=='전체 계정 탈퇴')throw new Error('확인 문구를 정확히 입력해 주세요.');await call('/me/withdrawals','POST',{current_password:withdrawPassword,confirmation:'전체 계정 탈퇴'});location.assign('/platform');},'',false);}}><label htmlFor="central-withdraw-password">탈퇴 확인용 현재 비밀번호<input id="central-withdraw-password" type="password" autoComplete="current-password" required value={withdrawPassword} onChange={e=>setWithdrawPassword(e.target.value)}/></label><label htmlFor="central-withdraw-confirmation">확인 문구 ‘전체 계정 탈퇴’ 입력<input id="central-withdraw-confirmation" required value={withdraw} onChange={e=>setWithdraw(e.target.value)}/></label><button disabled={busy}>공통 계정 탈퇴</button></form></details>
  </section>;
}

type Saved={id:number;summary:string;track_count:number};
type Snapshot={summary:string;items:{track_id:string;track_name:string;artists_name:string}[]};
export function PersonalRecords(){
  const [items,setItems]=useState<Saved[]>([]),[snapshot,setSnapshot]=useState<Snapshot|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  async function refresh(){setItems((await raw<{items:Saved[]}>('/api/me/playlists')).items);}
  async function load(work:()=>Promise<void>){setBusy(true);setError('');try{await work();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <section className="p-card"><details onToggle={event=>{if(event.currentTarget.open)void load(refresh);}}><summary>muther. 개인 저장 자료</summary><p className="p-field-help">개인 계정에 저장한 선곡표입니다. 매장 공유 음악 상태와 별개입니다.</p>{error&&<p className="p-alert error" role="alert">{error}</p>}{!busy&&items.length===0&&<p>저장한 선곡표가 없습니다.</p>}{items.map(row=><div className="p-row" key={row.id}><span>{row.summary||'저장한 선곡표'} · {row.track_count}곡</span><button disabled={busy} onClick={()=>void load(async()=>{setSnapshot((await raw<{playlist:Snapshot}>(`/api/me/playlists/${row.id}`)).playlist);})}>보기</button><button disabled={busy} onClick={()=>void load(async()=>{await raw(`/api/me/playlists/${row.id}`,'DELETE');await refresh();setSnapshot(null);})}>삭제</button></div>)}{snapshot&&<><h3>{snapshot.summary}</h3><ol>{snapshot.items.map(item=><li key={item.track_id}>{item.track_name} · {item.artists_name}</li>)}</ol><button onClick={()=>setSnapshot(null)}>선곡표 닫기</button></>}</details></section>;
}
