import {useState, type FormEvent} from 'react';
import {canonicalYoutubeVideoUrl} from '../recommendation/youtubeReview';
import {createSongRequestClient, type AdminSongItem, type AdminTask, type SongRequestClient, type SongReview, type SongTags} from './client';
import {useAction, useResource} from './hooks';
import {ActionState, Empty, ListPagination, Notice, ResourceState} from './ui';
import {statusLabel, tagLabel} from './viewModel';
import './songRequests.css';

function SongReviewForm({client, item, onReviewed}: {
  client: SongRequestClient;
  item: AdminSongItem;
  onReviewed: () => void;
}) {
  const approved = item.review_status === 'APPROVED';
  const [decision, setDecision] = useState<SongReview['decision']>(approved ? 'HOLD' : 'APPROVE');
  const [reason, setReason] = useState('');
  const [youtubeUrl, setYoutubeUrl] = useState(item.youtube_url || '');
  const [trackId, setTrackId] = useState('');
  const action = useAction();
  const url = canonicalYoutubeVideoUrl(youtubeUrl.trim()) || '';
  const validDecision = decision === 'APPROVE' ? !!url :
    decision === 'LINK_EXISTING' ? !!trackId.trim() : true;
  const canReverse = approved && !item.catalog_version && !['RECEIVED_PENDING_REVIEW', 'REGISTERED'].includes(item.pipeline_status ?? '');
  const canReview = item.review_status === 'PENDING' || item.review_status === 'HOLD' || canReverse;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canReview || !validDecision || !reason.trim() || (approved && decision !== 'HOLD' && decision !== 'REJECT')) return;
    const common = {expected_revision: item.revision, reason: reason.trim()};
    const body: SongReview = decision === 'APPROVE' ? {...common, decision, youtube_url: url} :
      decision === 'LINK_EXISTING' ? {...common, decision, track_id: trackId.trim()} : {...common, decision};
    void action.run(() => client.reviewSong(item.id, body), onReviewed, '');
  }

  return <article className="p4-card">
    <h3>{item.track_name} · {item.artist_name}</h3>
    <span className="p4-badge">{statusLabel(item.review_status)}</span>
    {item.pipeline_status && <p className="p4-meta">운영 처리 상태 {statusLabel(item.pipeline_status)}</p>}
    {item.public_reason && <p>{item.public_reason}</p>}
    {approved && url && <p><a href={url} target="_blank" rel="noopener noreferrer">승인한 YouTube 영상 확인 ↗</a></p>}
    {canReverse && <p className="p4-meta">링크를 바꾸려면 보류한 뒤 올바른 영상 주소로 다시 승인하세요.</p>}
    {canReview && <form onSubmit={submit}>
      <fieldset disabled={action.pending}>
        <legend>곡 검수</legend>
        <label>판정<select value={decision} onChange={event => setDecision(event.target.value as SongReview['decision'])}>
          {!approved && <option value="APPROVE">수집 대상으로 승인</option>}
          <option value="HOLD">보류</option>
          <option value="REJECT">반려</option>
          {!approved && <option value="LINK_EXISTING">기존 곡 연결</option>}
        </select></label>
        {decision === 'APPROVE' && <>
          <label>확인한 YouTube 주소<input type="url" required value={youtubeUrl} onChange={event => setYoutubeUrl(event.target.value)}/></label>
          <p className="p4-meta">영상에서 신청 곡과 아티스트가 일치하는지 확인한 뒤 승인하세요.</p>
          {url && <a href={url} target="_blank" rel="noopener noreferrer">YouTube에서 영상 확인 ↗</a>}
        </>}
        {decision === 'LINK_EXISTING' && <label>기존 곡 ID<input required value={trackId} onChange={event => setTrackId(event.target.value)}/></label>}
        <label>신청자에게 공개할 사유<textarea required value={reason} onChange={event => setReason(event.target.value)}/></label>
        <div><button type="submit" disabled={!validDecision || !reason.trim()}>검수 결과 저장</button></div>
      </fieldset>
      <ActionState {...action}/>
    </form>}
  </article>;
}

function SongTagOptions({label, options, selected, onToggle}: {
  label: string;
  options: string[];
  selected: string[];
  onToggle: (code: string) => void;
}) {
  return <fieldset>
    <legend>{label}</legend>
    <div className="p4-tag-options">
      {options.map(code => <button type="button" key={code} aria-pressed={selected.includes(code)}
        onClick={() => onToggle(code)}>{tagLabel(code)}</button>)}
    </div>
  </fieldset>;
}

function FinalSongReviewForm({client, task, result, options, onRegistered}: {
  client: SongRequestClient;
  task: AdminTask;
  result: NonNullable<AdminTask['result']>;
  options: SongTags;
  onRegistered: (trackId: string) => void;
}) {
  const [tags, setTags] = useState<SongTags>({
    genre: result.tags.genre.filter(code => options.genre.includes(code)),
    mood: result.tags.mood.filter(code => options.mood.includes(code)),
  });
  const [album, setAlbum] = useState(result.metadata?.album_name ?? '');
  const [duration, setDuration] = useState(result.metadata?.duration_ms ? String(result.metadata.duration_ms / 1000) : '');
  const [explicit, setExplicit] = useState(result.metadata?.explicit == null ? '' : String(result.metadata.explicit));
  const action = useAction();
  const durationMs = Math.round(Number(duration) * 1000);
  const canSubmit = !!tags.genre.length && !!tags.mood.length && !!album.trim() && album.trim().length <= 300 &&
    durationMs >= 1 && durationMs <= 2147483647 && (explicit === 'true' || explicit === 'false');

  function toggle(kind: keyof SongTags, code: string) {
    setTags(current => ({...current, [kind]: current[kind].includes(code)
      ? current[kind].filter(value => value !== code) : [...current[kind], code]}));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    void action.run(async () => {
      const updated = await client.finalizeSong(task.id, {
        expected_revision: task.revision, tags, album_name: album.trim(), duration_ms: durationMs, explicit: explicit === 'true',
      });
      if (!updated.registration?.track_id) throw new Error('등록 상태를 확인하지 못했습니다. 처리 현황을 새로고침해 주세요.');
      return updated.registration.track_id;
    }, onRegistered, '');
  }

  return <form onSubmit={submit}>
    <fieldset disabled={action.pending}>
      <legend>최종 태그 검수</legend>
      <p className="p4-meta">장르와 분위기를 각각 하나 이상 선택해 주세요.</p>
      <div className="p4-final-fields">
        <SongTagOptions label="확정 장르" options={options.genre} selected={tags.genre} onToggle={code => toggle('genre', code)}/>
        <SongTagOptions label="확정 분위기" options={options.mood} selected={tags.mood} onToggle={code => toggle('mood', code)}/>
      </div>
      <div className="p4-final-fields p4-track-metadata">
        <label>앨범명<input required maxLength={300} value={album} onChange={event => setAlbum(event.target.value)}/></label>
        <label>곡 길이(초)<input type="number" required min="0.001" max="2147483.647" step="0.001"
          value={duration} onChange={event => setDuration(event.target.value)}/></label>
        <label>유해 콘텐츠 표시<select required value={explicit} onChange={event => setExplicit(event.target.value)}>
          <option value="">선택해 주세요</option><option value="false">없음</option><option value="true">있음</option>
        </select></label>
      </div>
      <div><button type="submit" disabled={!canSubmit}>최종 승인 및 음악 등록</button></div>
    </fieldset>
    <ActionState {...action}/>
  </form>;
}

function TagSummary({tags, prefix}: {tags: SongTags; prefix: string}) {
  return <>
    <div><dt>{prefix} 장르</dt><dd>{tags.genre.map(tagLabel).join(', ') || '부여된 태그 없음'}</dd></div>
    <div><dt>{prefix} 분위기</dt><dd>{tags.mood.map(tagLabel).join(', ') || '부여된 태그 없음'}</dd></div>
  </>;
}

function SongProcessingResults({client, revision, onRegistered, onShowTrack}: {
  client: SongRequestClient;
  revision: number;
  onRegistered: (trackId: string) => void;
  onShowTrack: (trackId: string) => void;
}) {
  const [page, setPage] = useState(0);
  const [registeredTrackId, setRegisteredTrackId] = useState('');
  const resource = useResource(signal => client.getAdminTasks(page, signal), [client, revision, page]);
  const tagOptions = resource.value?.tag_options;
  function registered(trackId: string) {
    setRegisteredTrackId(trackId);
    onRegistered(trackId);
    resource.reload();
  }
  return <section className="p4-section">
    <div className="p4-section-heading">
      <h2>추론용 PC 처리 현황</h2>
      <button type="button" className="p4-secondary" onClick={resource.reload}>처리 현황 새로고침</button>
    </div>
    {registeredTrackId && <Notice>음악 목록에 등록했습니다. <a href="#music-list" onClick={() => onShowTrack(registeredTrackId)}>등록한 곡 보기</a></Notice>}
    <ResourceState {...resource} retry={resource.reload}/>
    {resource.value && (resource.value.items.length ? <ul className="p4-result-list">
      {resource.value.items.map(task => <li key={`${task.id}:${task.revision}`} className="p4-card">
        <h3>{task.track_name} · {task.artist_name}</h3>
        <span className="p4-badge">{statusLabel(task.status)}</span>
        {task.result && <dl className="p4-result-tags">
          <div><dt>모델 버전</dt><dd>{task.result.model_version}</dd></div>
          <TagSummary prefix="모델 추천" tags={task.result.tags}/>
        </dl>}
        {task.registration && <>
          <dl className="p4-result-tags"><TagSummary prefix="확정" tags={task.registration.tags}/></dl>
          <a href="#music-list" onClick={() => onShowTrack(task.registration!.track_id)}>음악 목록에서 보기</a>
        </>}
        {task.status === 'RECEIVED_PENDING_REVIEW' && task.result && !task.registration &&
          (tagOptions?.genre.length && tagOptions.mood.length
            ? <FinalSongReviewForm client={client} task={task} result={task.result} options={tagOptions} onRegistered={registered}/>
            : <p className="p4-meta">확정할 수 있는 태그 목록을 불러오지 못했습니다. 처리 현황을 새로고침해 주세요.</p>)}
      </li>)}
    </ul> : <Empty>아직 처리 작업이 없습니다.</Empty>)}
    <ListPagination label="추론용 PC 처리 현황 페이지" page={page} hasMore={resource.value?.has_more} loading={resource.loading} onPage={setPage}/>
  </section>;
}

function SongReviewQueue({client, onRegistered, onShowTrack}: {
  client: SongRequestClient;
  onRegistered: (trackId: string) => void;
  onShowTrack: (trackId: string) => void;
}) {
  const [status, setStatus] = useState('PENDING');
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState(0);
  const resource = useResource(signal => client.getAdminSongItems(status, page, signal), [client, status, revision, page]);
  return <>
    <section className="p4-section">
      <div className="p4-section-heading">
        <h2>곡 신청 검수</h2>
        <button type="button" className="p4-secondary" onClick={resource.reload}>신청 목록 새로고침</button>
      </div>
      <p>승인한 곡은 추론용 PC 처리 대기열에 들어갑니다.</p>
      <label>검수 상태<select value={status} onChange={event => {setStatus(event.target.value); setPage(0);}}>
        <option value="">전체</option>
        {['PENDING', 'HOLD', 'APPROVED', 'REJECTED', 'LINKED'].map(value => <option key={value} value={value}>{statusLabel(value)}</option>)}
      </select></label>
      {!!revision && <Notice>곡 검수 결과를 저장했습니다.</Notice>}
      <ResourceState {...resource} retry={resource.reload}/>
      {resource.value && (resource.value.items.length ? <div className="p4-stack">
        {resource.value.items.map(item => <SongReviewForm key={`${item.id}:${item.revision}`} client={client} item={item}
          onReviewed={() => setRevision(value => value + 1)}/>)}
      </div> : <Empty>해당 상태의 곡 신청이 없습니다.</Empty>)}
      <ListPagination label="곡 신청 검수 페이지" page={page} hasMore={resource.value?.has_more} loading={resource.loading} onPage={setPage}/>
    </section>
    <SongProcessingResults client={client} revision={revision} onShowTrack={onShowTrack}
      onRegistered={trackId => {onRegistered(trackId); resource.reload();}}/>
  </>;
}

export function AdminSongRequests({onRegistered, onShowTrack}: {
  onRegistered: (trackId: string) => void;
  onShowTrack: (trackId: string) => void;
}) {
  const [client] = useState(createSongRequestClient);
  const session = useResource(signal => client.getSession(signal), [client]);
  const canReview = session.value?.authenticated && session.value.capabilities.includes('review_songs');
  return <div id="song-request-review" className="phase4-operations">
    <ResourceState {...session} retry={session.reload}/>
    {session.value && (canReview ? <SongReviewQueue client={client} onRegistered={onRegistered} onShowTrack={onShowTrack}/>
      : <Notice error>곡 신청을 검수할 권한이 없습니다.</Notice>)}
  </div>;
}
