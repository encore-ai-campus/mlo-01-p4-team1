import {useState, type FormEvent} from 'react';
import {canonical} from './transport';
import {useAction, useResource} from './hooks';
import {ActionState, Empty, ListPagination, Notice, ResourceState} from './ui';
import {cleanSongs, dateLabel, songEligibility, statusLabel, validateSongs} from './viewModel';
import {createSongRequestClient, type Session, type SongCheck, type SongInput, type SongRequest, type SongRequestClient} from './client';
import './ownerSongRequests.css';

function SongRequestForm({client, session, onSubmitted}: {
  client: SongRequestClient;
  session: Session;
  onSubmitted: () => void;
}) {
  const [venueId, setVenueId] = useState(''), [items, setItems] = useState<SongInput[]>([{track_name: '', artist_name: '', youtube_url: ''}]);
  const [check, setCheck] = useState<{signature: string; result: SongCheck} | null>(null), [validation, setValidation] = useState('');
  const eligible = songEligibility(session, venueId || undefined);
  const quota = useResource(signal => eligible ? Promise.resolve(null) : client.getQuota(venueId || undefined, signal), [client, venueId, eligible]);
  const action = useAction(), checking = useAction();
  const body = cleanSongs(items, venueId || undefined), signature = canonical(body);
  const limit = Math.min(20, session.song_policy.per_submission_limit || 20, quota.value?.per_submission_limit || 20);
  const currentCheck = check?.signature === signature ? check.result : null;
  const catalogBlock = currentCheck?.items.some(item => item.duplicate_status === 'EXISTING_TRACK');
  const quotaBlock = quota.value?.remaining !== null && quota.value?.remaining !== undefined && quota.value.remaining < items.length;
  const disabled = !!eligible || !quota.value?.ready || action.pending || checking.pending;

  function update(index: number, field: keyof SongInput, value: string) {setItems(previous => previous.map((item, position) => position === index ? {...item, [field]: value} : item)); setValidation('');}
  function validate() {const error = validateSongs(items, limit); setValidation(error || ''); return !error;}
  function inspect() {
    if (!validate()) return;
    void checking.run(() => client.checkSongs(body), result => setCheck({signature, result}), '');
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (disabled || !validate() || !currentCheck || catalogBlock || quotaBlock) return;
    void action.run(() => client.submitSongs(body), () => {
      setItems([{track_name: '', artist_name: '', youtube_url: ''}]);
      setCheck(null);
      quota.reload();
      onSubmitted();
    }, '곡 신청이 접수되었습니다.');
  }
  return <section className="p-card owner-song-card" aria-labelledby="p4-song-title">
    <div className="p-card-heading owner-song-heading">
      <h2 id="p4-song-title">곡 추가 신청</h2>
      <span className="p-badge submitted">한 번에 최대 {limit}곡</span>
    </div>
    <p className="p-muted owner-song-intro">추가하고 싶은 곡을 담고 중복을 확인한 뒤 신청해 주세요.</p>
    <ResourceState {...quota} retry={quota.reload}/>
    {quota.value?.period_limit != null && quota.value.window_days !== null && <p className="p-muted">
      최근 {quota.value.window_days}일 최대 {quota.value.period_limit}곡{quota.value.remaining === null ? '' : ` · 남은 ${quota.value.remaining}곡`}
    </p>}
    {!eligible && !quota.loading && !quota.error && !quota.value?.ready && <Notice>현재 곡 신청 한도를 확인할 수 없습니다.</Notice>}
    <form className="owner-song-form" onSubmit={submit}>
      <label>신청 매장<select value={venueId} onChange={event => {setVenueId(event.target.value); setCheck(null);}} required={session.song_policy.require_approved_venue === true} disabled={!session.managed_venues.length || action.pending || checking.pending}>
        <option value="">매장을 선택해 주세요</option>{session.managed_venues.map(venue => <option key={venue.venue_id} value={venue.venue_id}>{venue.name}{venue.address ? ` · ${venue.address}` : ''}</option>)}
      </select></label>
      <Notice>{eligible}</Notice>
      <fieldset className="owner-song-fields" disabled={disabled}><legend>신청 곡 <span>{items.length}/{limit}</span></legend>
        {items.map((item, index) => <div className="owner-song-entry" key={index}>
          {items.length > 1 && <div className="owner-song-entry-heading">
            <strong>곡 {index + 1}</strong>
            <button type="button" className="p-text-button" onClick={() => setItems(previous => previous.filter((_, position) => position !== index))} aria-label={`${index + 1}번 곡 삭제`}>삭제</button>
          </div>}
          <div className="owner-song-inputs">
            <label>곡명<input required value={item.track_name} onChange={event => update(index, 'track_name', event.target.value)}/></label>
            <label>아티스트<input required value={item.artist_name} onChange={event => update(index, 'artist_name', event.target.value)}/></label>
            <label className="owner-song-url">YouTube 주소 <span className="p-muted">선택</span><input type="url" value={item.youtube_url || ''} placeholder="https://youtu.be/…" onChange={event => update(index, 'youtube_url', event.target.value)}/></label>
          </div>
        </div>)}
        <button type="button" className="owner-song-add" disabled={items.length >= limit} onClick={() => setItems(previous => [...previous, {track_name: '', artist_name: '', youtube_url: ''}])}><span aria-hidden="true">＋ </span>곡 추가</button>
      </fieldset>
      <Notice error>{validation}</Notice>
      {currentCheck && <div className="p4-check-results"><h3>중복 확인 결과</h3>
        {!currentCheck.catalog_checked && <Notice>음악 목록 대조가 완료되지 않았습니다. 운영자 검수에서 다시 확인합니다.</Notice>}
        <ul>{currentCheck.items.map((item, index) => <li key={index}>{item.track_name} · {item.artist_name} — {item.duplicate_status === 'NONE' ? '확인된 중복 없음' : item.duplicate_status === 'POSSIBLE' ? '중복 가능성 · 운영자 확인 필요' : '이미 등록된 곡'}</li>)}</ul>
      </div>}
      <Notice error>{catalogBlock ? '이미 등록된 곡을 신청 목록에서 제외해 주세요.' : quotaBlock ? '남은 신청 한도를 초과했습니다. 곡 수를 줄여 주세요.' : ''}</Notice>
      <div className="p-actions owner-song-submit"><button type="button" disabled={disabled} onClick={inspect}>중복 확인</button><button type="submit" className="p-primary" disabled={disabled || !currentCheck || !!catalogBlock || !!quotaBlock}>신청 제출</button></div>
      <ActionState {...checking}/><ActionState {...action}/>
    </form>
  </section>;
}

/** Only public fields are rendered; pipeline/PC/database fields are intentionally ignored. */
export function SongHistoryList({requests}: {requests: SongRequest[]}) {
  if (!requests.length) return <Empty>아직 신청한 곡이 없습니다.</Empty>;
  return <div>{requests.map(request => <article className="owner-song-batch" key={request.id}><h3>{dateLabel(request.created_at)}</h3>
    <ul>{request.items.map(item => {
      const status = item.review_status;
      const badge = status === 'PENDING' ? 'submitted' : status === 'APPROVED' || status === 'LINKED' ? 'approved' : 'needs_more';
      const label = item.public_status === '접수됨' ? '검수 대기' : statusLabel(status);
      const detail = item.public_status && item.public_status !== '접수됨' ? statusLabel(item.public_status) : null;
      return <li className="p-row owner-song-history-row" key={item.id}>
        <div><strong>{item.track_name}</strong><small>{item.artist_name}</small></div>
        <span className={`p-badge ${badge}`}>{label}</span>
        {detail && detail !== label && <p className="p-muted owner-song-history-detail">{detail}</p>}
        {item.public_reason && <p className="p-muted owner-song-history-detail">{item.public_reason}</p>}
      </li>;
    })}</ul>
  </article>)}</div>;
}

function SongRequestHistory({client}: {client: SongRequestClient}) {
  const [page, setPage] = useState(0);
  const resource = useResource(signal => client.getSongRequests(page, signal), [client, page]);
  return <section className="p-card owner-song-card" aria-labelledby="owner-song-history"><div className="p-card-heading owner-song-heading"><h2 id="owner-song-history">내 곡 신청 현황</h2><button type="button" onClick={resource.reload}>새로고침</button></div>
    <ResourceState {...resource} retry={resource.reload}/>{resource.value && <SongHistoryList requests={resource.value.items}/>}
    <ListPagination label="내 곡 신청 현황 페이지" page={page} hasMore={resource.value?.has_more} loading={resource.loading} onPage={setPage}/>
  </section>;
}

export function OwnerSongRequests({client, session}: {client: SongRequestClient; session: Session}) {
  const [historyRevision, setHistoryRevision] = useState(0);
  return <div id="song-requests" className="owner-song-requests">
    <div className="owner-song-grid">
      <SongRequestForm client={client} session={session} onSubmitted={() => setHistoryRevision(value => value + 1)}/>
      <SongRequestHistory key={historyRevision} client={client}/>
    </div>
  </div>;
}

export function OwnerSongRequestsPortal() {
  const [client] = useState(createSongRequestClient);
  const session = useResource(signal => client.getSession(signal), [client]);
  return <>
    <ResourceState {...session} retry={session.reload}/>
    {session.value?.authenticated && session.value.managed_venues.length > 0
      && <OwnerSongRequests client={client} session={session.value}/>}
  </>;
}
