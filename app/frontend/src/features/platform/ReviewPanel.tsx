import {useEffect,useState} from 'react';
import {call,etag} from './api';
import {Badge,LINKED_VENUE_FIXTURE,type Claim} from './WorkspacePanel';
export function ReviewPanel({admin,claimId}:{admin:boolean;claimId?:string}){
  const [claims,setClaims]=useState<Claim[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  async function refresh(){
    if(claimId){
      const claim=await call<Claim>(`/admin/workspace-claims/${encodeURIComponent(claimId)}`);
      setClaims([claim]);
      return;
    }
    setClaims((await call<{items:Claim[]}>('/admin/workspace-claims')).items);
  }
  async function run(work:()=>Promise<void>){setBusy(true);setError('');try{await work();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  useEffect(()=>{if(admin)void run(refresh);},[admin,claimId]);
  return <><section className="p-flow"><span>① 합성 등록 정보 확인</span><b>→</b><span>② 별도 관리권한 심사</span><b>→</b><span>③ 매장 OWNER 부여</span></section>{error&&<div className="p-alert error" role="alert">{error} <a href="/admin/passkey">패스키 인증</a></div>}{message&&<p className="p-alert" role="status">{message}</p>}{!admin?<section className="p-card"><h2>관리자 인증이 필요합니다</h2><p>별도 매장 심사 권한과 최근 5분 이내 패스키 인증을 확인합니다.</p><div className="p-actions"><a className="p-link-button" href="/platform/admin/login">심사 관리자 로그인</a><a href="/admin/passkey">패스키 인증</a></div></section>:<section className="p-card"><div className="p-card-heading"><h2>매장 권한 신청</h2><button disabled={busy} onClick={()=>void run(refresh)}>새로고침</button></div>{claims.length===0&&<p className="p-muted">표시할 신청이 없습니다.</p>}{claims.map(claim=><article className="p-review" key={claim.id}><div><Badge value={claim.state}/><h3>{claim.display_name}</h3>{(claim.review_context.fixture_id===LINKED_VENUE_FIXTURE||claim.review_context.fixture_id.startsWith('catalog:'))&&<p className="p-muted">손님 카탈로그에 연결할 매장</p>}<p>{claim.address} · 신청 계정 {claim.review_context.applicant_label}</p><p className="p-muted">합성 확인 {claim.match_result} / {claim.business_status} · 내용 v{claim.content_revision}</p></div>{claim.state==='SUBMITTED'&&<div className="p-actions">{[['APPROVE','DEMO_SCENARIO_CONFIRMED','승인'],['REQUEST_MORE','CORRECTION_REQUIRED','보완 요청'],['REJECT','AUTHORITY_UNCONFIRMED','거절']].map(([decision,reason_code,label])=><button key={decision} disabled={busy} className={decision==='APPROVE'?'p-primary':''} onClick={()=>void run(async()=>{await call(`/admin/workspace-claims/${claim.id}/decisions`,'POST',{decision,reason_code},etag(claim.revision));await refresh();setMessage('심사 결과를 저장했습니다.');})}>{label}</button>)}</div>}</article>)}</section>}</>;
}
