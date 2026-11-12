import {useState, type FormEvent} from 'react';
import {canonicalYoutubeVideoUrl} from '../recommendation/youtubeReview';
import {CatalogPublicationForm} from './CatalogPublicationForm';
import {createSongRequestClient, type AdminSongItem, type AdminTask, type SongRequestClient, type SongResultRecovery, type SongReview, type SongTags} from './client';
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

const recoveryLabels: Record<SongResultRecovery['decision'], string> = {
  REJECT: '반려',
  LINK_EXISTING: '기존 곡 연결',
  REQUEUE: '링크 수정 후 다시 처리',
};

function SongResultRecoveryForm({client, task, onRecovered}: {
  client: SongRequestClient;
  task: AdminTask;
  onRecovered: (decision: SongResultRecovery['decision']) => void;
}) {
  const [decision, setDecision] = useState<SongResultRecovery['decision'] | ''>('');
  const [reason, setReason] = useState('');
  const [trackId, setTrackId] = useState('');
  const [youtubeUrl, setYoutubeUrl] = useState('');
  const action = useAction();
  const url = canonicalYoutubeVideoUrl(youtubeUrl.trim());
  const originalUrl = canonicalYoutubeVideoUrl(task.source_ref);
  const canSubmit = !!decision && !!reason.trim() &&
    (decision !== 'LINK_EXISTING' || !!trackId.trim()) &&
    (decision !== 'REQUEUE' || (!!url && url !== originalUrl));

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit || !decision) return;
    const common = {expected_revision: task.revision, reason: reason.trim()};
    const body: SongResultRecovery | null = decision === 'LINK_EXISTING'
      ? {...common, decision, track_id: trackId.trim()}
      : decision === 'REQUEUE' ? (url ? {...common, decision, youtube_url: url} : null)
      : {...common, decision};
    if (body) void action.run(() => client.recoverSong(task.id, body), () => onRecovered(decision), '');
  }

  return <details className="p4-result-recovery">
    <summary>반려 · 기존 곡 연결 · 링크 수정</summary>
    <form onSubmit={submit}>
      <fieldset disabled={action.pending}>
        <legend>검수 결과 처리</legend>
        <label>처리 방법<select required value={decision} onChange={event => setDecision(event.target.value as typeof decision)}>
          <option value="">선택해 주세요</option>
          <option value="REJECT">{recoveryLabels.REJECT}</option>
          <option value="LINK_EXISTING">{recoveryLabels.LINK_EXISTING}</option>
          <option value="REQUEUE">{recoveryLabels.REQUEUE}</option>
        </select></label>
        {decision === 'LINK_EXISTING' && <>
          <label>기존 곡 ID<input required maxLength={128} value={trackId} onChange={event => setTrackId(event.target.value)}/></label>
          <p className="p4-meta"><a href="#music-list">음악 목록</a>에서 승인된 곡의 ID를 확인해 주세요. 같은 영상으로 확인된 곡만 연결할 수 있습니다.</p>
        </>}
        {decision === 'REQUEUE' && <>
          <label>수정한 YouTube 주소<input type="url" required maxLength={2048} value={youtubeUrl}
            onChange={event => setYoutubeUrl(event.target.value)}/></label>
          {url && (url === originalUrl ? <p className="p4-meta">이전 영상과 다른 주소를 입력해 주세요.</p>
            : <p><a href={url} target="_blank" rel="noopener noreferrer">수정한 영상 확인 ↗</a></p>)}
        </>}
        <label>신청자에게 공개할 사유<input required maxLength={1000} value={reason} onChange={event => setReason(event.target.value)}/></label>
        <div><button type="submit" disabled={!canSubmit}>{decision ? recoveryLabels[decision] : '검수 결과 저장'}</button></div>
      </fieldset>
      <ActionState {...action}/>
    </form>
  </details>;
}

function SongProcessingResults({client, revision, onRegistered, onShowTrack, onRecovered}: {
  client: SongRequestClient;
  revision: number;
  onRegistered: (trackId: string) => void;
  onShowTrack: (trackId: string) => void;
  onRecovered: () => void;
}) {
  const [page, setPage] = useState(0);
  const [registeredTrackId, setRegisteredTrackId] = useState('');
  const [recoveryNotice, setRecoveryNotice] = useState('');
  const [publication, setPublication] = useState<{version: string; videoReviewPending: boolean} | null>(null);
  const resource = useResource(signal => client.getAdminTasks(page, signal), [client, revision, page]);
  const tagOptions = resource.value?.tag_options;
  const recommendationGenres = resource.value?.recommendation_genres;
  function registered(trackId: string) {
    setRegisteredTrackId(trackId);
    setRecoveryNotice('');
    setPublication(null);
    onRegistered(trackId);
    resource.reload();
  }
  function recovered(decision: SongResultRecovery['decision']) {
    setRegisteredTrackId('');
    setPublication(null);
    setRecoveryNotice(decision === 'REQUEUE' ? '수정한 링크로 태깅을 다시 요청했습니다.'
      : decision === 'LINK_EXISTING' ? '기존 음악 목록의 곡에 연결했습니다.' : '곡 신청을 반려했습니다.');
    setPage(0);
    resource.reload();
    onRecovered();
  }
  function published(version: string, videoReviewPending: boolean) {
    setRegisteredTrackId('');
    setRecoveryNotice('');
    setPublication({version, videoReviewPending});
    resource.reload();
  }
  return <section className="p4-section">
    <div className="p4-section-heading">
      <h2>추론용 PC 처리 현황</h2>
      <button type="button" className="p4-secondary" onClick={resource.reload}>처리 현황 새로고침</button>
    </div>
    {registeredTrackId && <Notice>음악 목록에 등록했습니다. <a href="#music-list" onClick={() => onShowTrack(registeredTrackId)}>등록한 곡 보기</a></Notice>}
    <Notice>{recoveryNotice}</Notice>
    {publication && <Notice>추천 카탈로그 {publication.version}을 발행했습니다.
      {publication.videoReviewPending && <> <a href="/admin/music/youtube">YouTube 영상 검수</a>가 남아 있습니다.</>}
    </Notice>}
    <ResourceState {...resource} retry={resource.reload}/>
    {resource.value && (resource.value.items.length ? <ul className="p4-result-list">
      {resource.value.items.map(task => {
        const {result, registration} = task;
        const sourceUrl = canonicalYoutubeVideoUrl(task.source_ref);
        const trackId = registration?.track_id || task.linked_track_id;
        const canReview = task.is_current !== false && task.status === 'RECEIVED_PENDING_REVIEW' && !!result && !registration;
        const canPublish = task.is_current === true && task.status === 'REGISTERED' && !!registration && !registration.catalog_version;
        return <li key={`${task.id}:${task.revision}`} className="p4-card">
          <h3>{task.track_name} · {task.artist_name}</h3>
          <span className="p4-badge">{statusLabel(task.status)}</span>
          {sourceUrl && <p><a href={sourceUrl} target="_blank" rel="noopener noreferrer">태깅 대상 영상 확인 ↗</a></p>}
          {result && <dl className="p4-result-tags">
            <div><dt>모델 버전</dt><dd>{result.model_version}</dd></div>
            <TagSummary prefix="모델 추천" tags={result.tags}/>
          </dl>}
          {registration && <dl className="p4-result-tags"><TagSummary prefix="등록 시" tags={registration.tags}/></dl>}
          {trackId && <a href="#music-list" onClick={() => onShowTrack(trackId)}>음악 목록에서 보기</a>}
          {registration?.catalog_version && <p className="p4-meta">추천 카탈로그 {registration.catalog_version} 발행 완료 · <a href="/admin/music/youtube">YouTube 영상 검수</a></p>}
          {canPublish && (recommendationGenres && Object.keys(recommendationGenres).length
            ? <CatalogPublicationForm client={client} task={task} recommendationGenres={recommendationGenres} onPublished={published}/>
            : <p className="p4-meta">카탈로그 발행에 필요한 서비스 장르 목록을 불러오지 못했습니다.</p>)}
          {canReview && <>
            {tagOptions?.genre.length && tagOptions.mood.length
              ? <FinalSongReviewForm client={client} task={task} result={result} options={tagOptions} onRegistered={registered}/>
              : <p className="p4-meta">확정할 수 있는 태그 목록을 불러오지 못했습니다. 처리 현황을 새로고침해 주세요.</p>}
            <SongResultRecoveryForm client={client} task={task} onRecovered={recovered}/>
          </>}
        </li>;
      })}
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
    <SongProcessingResults client={client} revision={revision} onShowTrack={onShowTrack} onRecovered={resource.reload}
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
