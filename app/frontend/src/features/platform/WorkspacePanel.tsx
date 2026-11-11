import {useEffect,useState,type FormEvent} from 'react';
import {call,etag,labels,ownerWrite,type Account} from './api';
import {OwnerSongRequests} from '../song-requests/SongRequests';
import {createSongRequestClient} from '../song-requests/client';
import {useResource} from '../song-requests/hooks';
export const LINKED_VENUE_FIXTURE = 'demo-linked-venue-000001';
const LINKED_VENUE_NAME = '그레인바운더리';

export type Claim={id:string;revision:number;content_revision:number;state:string;display_name:string;address:string;match_result:string;business_status:string;approved_workspace_id?:string|null;review_context:{fixture_id:string;applicant_label:string}};
type Workspace={id:string;revision:number;display_name:string;my_role:string;status:string};
type Invite={id:string;revision:number;workspace_display_name:string;role:string;state:string};
type Member={id:string;revision:number;nickname:string;role:string};
type Music={revision:number;preferences:{mood?:string;volume?:number};exclusions:string[];applied_playlist:string;playlists:{id:string;name:string}[]};
type CatalogVenue={venue_id:string;venue_name:string;road_address:string};
export function Badge({value}:{value:string}){return <span className={'p-badge '+value.toLowerCase()}>{labels[value]||value}</span>;}

export function WorkspacePanel({account}:{account:Account}){
  const [claims,setClaims]=useState<Claim[]>([]),[stores,setStores]=useState<Workspace[]>([]),[invites,setInvites]=useState<Invite[]>([]);
  const [selected,setSelected]=useState<Workspace|null>(null),[music,setMusic]=useState<Music|null>(null),[members,setMembers]=useState<Member[]>([]);
  const [error,setError]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  const [receiver,setReceiver]=useState(''),[recipientCode,setRecipientCode]=useState(''),[role,setRole]=useState('STAFF'),[password,setPassword]=useState('');
  const [fixture,setFixture]=useState('catalog-search');
  const [searchRegion,setSearchRegion]=useState(''),[searchName,setSearchName]=useState(''),[searchResults,setSearchResults]=useState<CatalogVenue[]|null>(null),[catalogVenue,setCatalogVenue]=useState<CatalogVenue|null>(null);
  const linksCatalogVenue = fixture === LINKED_VENUE_FIXTURE;
  const catalogSearch = fixture === 'catalog-search' || fixture.startsWith('catalog:');
  const business=account.memberships.some(m=>m.type==='BUSINESS'&&m.status==='ACTIVE');
  const [songRequestClient]=useState(createSongRequestClient);
  const songAccess=useResource(async signal=>business
    ? {accountId:account.account_id,session:await songRequestClient.getSession(signal)}
    : null,[songRequestClient,business,account.account_id,stores]);
  const songSession=songAccess.value?.accountId===account.account_id ? songAccess.value.session : null;
  const canRequestSongs=songSession?.authenticated && songSession.managed_venues.length>0;
  async function refresh(){
    const incoming=await call<{items:Invite[]}>('/me/workspace-invitations');setInvites(incoming.items);
    if(business){const [c,w]=await Promise.all([call<{items:Claim[]}>('/me/workspace-claims'),call<{items:Workspace[]}>('/me/workspaces')]);setClaims(c.items);setStores(w.items);return w.items;}
    return [];
  }
  async function run(work:()=>Promise<unknown>){setBusy(true);setError('');setMessage('');try{await work();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  useEffect(()=>{void run(refresh);},[]);
  async function choose(store:Workspace){
    const [state,roster]=await Promise.all([
      call<Music>(`/workspaces/${store.id}/music`),
      store.my_role!=='STAFF'?call<{items:Member[]}>(`/workspaces/${store.id}/memberships`):Promise.resolve({items:[]}),
    ]);
    setMusic(state);setMembers(roster.items);setSelected(store);
  }
  async function saveClaim(event:FormEvent){event.preventDefault();await run(async()=>{
    const body = {
      display_name: catalogSearch ? catalogVenue?.venue_name : LINKED_VENUE_NAME,
      address: catalogSearch ? catalogVenue?.road_address : '카탈로그 등록 주소',
      applicant_relation: 'DELEGATE',
      fixture_id: fixture,
    };
    const row=await call<Claim>('/workspace-connections','POST',body);
    const updated=await refresh();
    const connected=updated.find(store=>store.id===row.approved_workspace_id);
    if(connected)await choose(connected);
    setMessage('매장을 연결했습니다.');
  });}
  async function musicWrite(body:unknown){
    if(!selected||!music)return;
    setMusic(await call<Music>(`/workspaces/${selected.id}/music`,'PATCH',body,etag(music.revision)));
  }
  return <>{error&&<div className="p-alert error" role="alert">{error}</div>}{message&&<div className="p-alert" role="status">{message}</div>}
  {business?<><section className="p-summary"><div><small>연결 매장</small><strong>{stores.length}</strong></div><div><small>연결 내역</small><strong>{claims.length}</strong></div><div><small>수락 대기 초대</small><strong>{invites.filter(i=>i.state==='PENDING').length}</strong></div></section>
  <section className="p-card"><h2>뮤더 선곡</h2><p>날씨에 맞는 선곡표를 만들고 승인한 뒤 매장에 적용할 수 있습니다.</p><div className="p-actions"><a className="p-link-button" href="/muther">뮤더에서 선곡 만들기</a>{canRequestSongs&&<a className="p-link-button owner-song-link" href="#song-requests">곡 추가 신청</a>}</div></section>
  <div className="p-columns"><section className="p-card"><div className="p-card-heading"><h2>내 매장</h2><button disabled={busy} onClick={()=>void run(refresh)}>새로고침</button></div>{stores.length===0?<p className="p-muted">아직 연결된 매장이 없습니다. 매장을 찾거나 기존 매장의 직원 초대를 수락하세요.</p>:stores.map(store=><button key={store.id} disabled={busy} className={'p-store '+(selected?.id===store.id?'selected':'')} onClick={()=>void run(()=>choose(store))}><span><strong>{store.display_name}</strong><small>{store.status==='ACTIVE'?'운영 가능':'이용 상태 확인 필요'}</small></span><Badge value={store.my_role}/></button>)}</section>
  <section className="p-card">
    <h2>내 매장 연결</h2>
    <form id="business-claim-form" onSubmit={e=>void saveClaim(e)}>
      <label>
        연결할 매장
        <select aria-label="연결할 매장" disabled={busy} value={catalogSearch?'catalog-search':fixture} onChange={e=>{setFixture(e.target.value);setCatalogVenue(null);setSearchResults(null);}}>
          <option value="catalog-search">기존 매장 검색</option>
          <option value={LINKED_VENUE_FIXTURE}>시연 매장 · {LINKED_VENUE_NAME}</option>
        </select>
      </label>
      {catalogSearch ? <>
        <p className="p-muted">지역과 매장명으로 연결할 매장을 찾으세요.</p>
        <label>시·군·구<input disabled={busy} value={searchRegion} onChange={e=>{setSearchRegion(e.target.value);setSearchResults(null);setCatalogVenue(null);setFixture('catalog-search');}} placeholder="예: 마포구" /></label>
        <label>매장명<input disabled={busy} value={searchName} onChange={e=>{setSearchName(e.target.value);setSearchResults(null);setCatalogVenue(null);setFixture('catalog-search');}} placeholder="두 글자 이상" /></label>
        <button type="button" disabled={busy||!searchRegion.trim()||searchName.trim().length<2} onClick={()=>void run(async()=>{setCatalogVenue(null);setFixture('catalog-search');setSearchResults(null);const query=new URLSearchParams({region:searchRegion.trim(),q:searchName.trim()});const result=await call<{items:CatalogVenue[]}>(`/claim-venues?${query}`);setSearchResults(result.items);})}>매장 검색</button>
        {searchResults!==null&&(searchResults.length===0?<p>일치하는 매장이 없습니다.</p>:<div aria-label="매장 검색 결과">{searchResults.map(venue=><button type="button" key={venue.venue_id} onClick={()=>{setCatalogVenue(venue);setFixture('catalog:'+venue.venue_id);}}>{venue.venue_name} · {venue.road_address}</button>)}</div>)}
        {catalogVenue&&<p>선택한 매장: {catalogVenue.venue_name} · {catalogVenue.road_address}</p>}
      </> : linksCatalogVenue && <p className="p-muted">{LINKED_VENUE_NAME}을 시연용 매장으로 연결합니다.</p>}
      <button className="p-primary" disabled={busy||(catalogSearch&&!catalogVenue)}>매장 연결하기</button>
    </form>
  </section></div>
  <section className="p-card"><h2>매장 연결 내역</h2>{claims.length===0?<p className="p-muted">연결된 매장이 없습니다.</p>:claims.map(claim=><article className="p-claim-progress" key={claim.id}>
    <div className="p-card-heading"><strong>{claim.display_name}</strong><Badge value={claim.state}/></div>
    <div className="p-actions">{claim.state==='APPROVED'&&stores.find(store=>store.id===claim.approved_workspace_id)&&<button className="p-primary" disabled={busy} onClick={()=>void run(()=>choose(stores.find(store=>store.id===claim.approved_workspace_id)!))}>연결 매장 열기</button>}</div>
  </article>)}</section>
  {selected&&music&&<section className="p-card"><div className="p-card-heading"><h2>{selected.display_name} · 음악 운영</h2><Badge value={selected.my_role}/></div><p>직원도 선곡표 최종 적용·공통 제외곡·음악 설정을 변경할 수 있습니다.</p><div className="p-playlists">{music.playlists.map(playlist=><article key={playlist.id}><div className="p-album">♫</div><h3>{playlist.name}</h3><p>{music.applied_playlist===playlist.id?'현재 매장에 적용됨':playlist.id.startsWith('demo-')?'권한 확인용 합성 선곡표':'점주가 승인한 선곡표'}</p><button disabled={busy} onClick={()=>void run(async()=>{await musicWrite({action:'APPLY',playlist_id:playlist.id});setMessage('선곡표를 매장에 적용했습니다.');})}>최종 승인 · 매장 적용</button></article>)}</div><div className="p-actions"><button disabled={busy} onClick={()=>void run(()=>musicWrite({action:'EXCLUSIONS',exclusions:music.exclusions.length?[]:['demo-track-1']}))}>합성 곡 1 제외 {music.exclusions.length?'해제':'적용'}</button><button disabled={busy} onClick={()=>void run(()=>musicWrite({action:'PREFERENCES',preferences:{mood:music.preferences.mood==='BRIGHT'?'CALM':'BRIGHT',volume:30}}))}>음악 분위기 변경</button><span className="p-muted">revision {music.revision} · {music.preferences.mood||'기본 분위기'} · 제외 {music.exclusions.length}곡</span></div><p className="p-muted">시연 매장에서 승인된 선곡표를 적용할 수 있습니다. 음원 재생 연결은 준비 중입니다.</p>
  {selected.my_role==='OWNER'&&<><hr/><h3>직원 · 매니저 초대</h3><p className="p-muted">직원이 발급한 15분 수신 코드를 입력합니다. 초대는 24시간 유효하며 직원이 직접 수락합니다.</p><form className="p-invite-form" onSubmit={e=>{e.preventDefault();void run(async()=>{try{await ownerWrite(`/workspaces/${selected.id}/invitations`,'POST',{recipient_code:recipientCode,role},selected.revision,password);setRecipientCode('');setMessage('초대를 보냈습니다. 대상 계정의 초대함에서 수락해 주세요.');}finally{setPassword('');}});}}><label>초대 수신 코드<input required autoComplete="off" value={recipientCode} onChange={e=>setRecipientCode(e.target.value)}/></label><label>역할<select value={role} onChange={e=>setRole(e.target.value)}><option value="STAFF">직원</option><option value="MANAGER">매니저</option></select></label><label>소유자 비밀번호 재확인<input required type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)}/></label><button disabled={busy}>초대 생성</button></form></>}
  {members.length>0&&<><hr/><h3>매장 구성원</h3>{members.map(member=><div className="p-row" key={member.id}><strong>{member.nickname}</strong><Badge value={member.role}/>{selected.my_role==='OWNER'&&member.role!=='OWNER'&&<><button disabled={busy||!password} onClick={()=>void run(async()=>{try{await ownerWrite(`/workspaces/${selected.id}/memberships/${member.id}`,'PATCH',{role:member.role==='STAFF'?'MANAGER':'STAFF'},member.revision,password);await choose(selected);}finally{setPassword('');}})}>역할 변경</button><button disabled={busy||!password} onClick={()=>void run(async()=>{try{await ownerWrite(`/workspaces/${selected.id}/memberships/${member.id}/revocations`,'POST',{reason_code:'STAFF_DEPARTURE'},member.revision,password);await choose(selected);}finally{setPassword('');}})}>권한 회수</button></>}</div>)}{selected.my_role==='OWNER'&&<p className="p-muted">구성원 변경은 위 소유자 비밀번호 재확인란 입력 후 사용할 수 있습니다.</p>}</>}
  </section>}
  {canRequestSongs&&<OwnerSongRequests client={songRequestClient} session={songSession}/>}
  </>:<section className="p-card"><h2>사업자 이용 유형을 추가하세요</h2><p>가입 계정을 유지하면서 muther.를 사용할 수 있습니다.</p><a href="/platform/account">이용 안내 확인하고 추가</a></section>}
  <section className="p-card"><h2>기존 매장에 직원으로 참여하기</h2><p>내 계정에 연결된 수신 코드를 소유자에게 전달하세요. 이메일 등록은 필요하지 않습니다.</p><div className="p-actions"><button disabled={busy} onClick={()=>void run(async()=>{const value=await call<{recipient_code:string}>('/me/invitation-receivers','POST',{});setReceiver(value.recipient_code);})}>초대 수신 코드 발급</button><button disabled={busy} onClick={()=>void run(refresh)}>초대함 새로고침</button></div>{receiver&&<p className="p-code"><code>{receiver}</code><small>15분 · 일회용 · 이 계정에 결합됨</small></p>}<h3>내 초대함</h3>{invites.length===0&&<p className="p-muted">받은 초대가 없습니다.</p>}{invites.map(invite=><div className="p-row" key={invite.id}><div><strong>{invite.workspace_display_name}</strong><small>{labels[invite.role]} 초대</small></div><Badge value={invite.state}/>{invite.state==='PENDING'&&(business?<><button disabled={busy} onClick={()=>void run(async()=>{await call(`/workspace-invitations/${invite.id}/acceptances`,'POST',{},etag(invite.revision));setReceiver('');await refresh();setMessage('매장 초대를 수락했습니다. 내 매장에서 매장을 선택해 운영을 시작하세요.');})}>초대 수락</button><button disabled={busy} onClick={()=>void run(async()=>{await call(`/workspace-invitations/${invite.id}/declines`,'POST',{},etag(invite.revision));await refresh();})}>거절</button></>:<a href="/platform/account">사업자 유형 먼저 추가</a>)}</div>)}</section></>;
}
