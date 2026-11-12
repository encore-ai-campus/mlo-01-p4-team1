import {useState, type FormEvent} from 'react';
import type {AdminTask, SongCatalogPublication, SongRequestClient} from './client';
import {useAction} from './hooks';
import {ActionState} from './ui';
import {tagLabel} from './viewModel';

const audioFields = {energy: '에너지', acousticness: '어쿠스틱 정도', valence: '음악적 긍정도'} as const;

function validAudioValue(value: string) {
  return value.trim() !== '' && Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= 1;
}

export function CatalogPublicationForm({client, task, recommendationGenres, onPublished}: {
  client: SongRequestClient;
  task: AdminTask;
  recommendationGenres: Record<string, string>;
  onPublished: (version: string, videoReviewPending: boolean) => void;
}) {
  const approvedGenres = task.registration?.tags.genre ?? [];
  const [genreGroups, setGenreGroups] = useState(() => approvedGenres.filter(code => Object.hasOwn(recommendationGenres, code)));
  const [includeAudio, setIncludeAudio] = useState(false);
  const [audioValues, setAudioValues] = useState({energy: '', acousticness: '', valence: ''});
  const [source, setSource] = useState('');
  const [mode, setMode] = useState('');
  const action = useAction();
  const needsMapping = approvedGenres.filter(code => !Object.hasOwn(recommendationGenres, code));
  const selectedGroups = Object.keys(recommendationGenres).filter(code => genreGroups.includes(code));
  const sourceText = source.trim();
  const audioComplete = Object.values(audioValues).every(validAudioValue) && !!sourceText && sourceText.length <= 300;
  const canSubmit = selectedGroups.length > 0 && (!includeAudio || audioComplete);

  function toggleGenre(code: string) {
    setGenreGroups(current => current.includes(code) ? current.filter(value => value !== code) : [...current, code]);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    const body: SongCatalogPublication = {expected_revision: task.revision, genre_groups: selectedGroups};
    if (includeAudio) {
      body.audio = {
        energy: Number(audioValues.energy),
        acousticness: Number(audioValues.acousticness),
        valence: Number(audioValues.valence),
        source: sourceText,
        ...(mode ? {mode: mode === 'major'} : {}),
      };
    }
    void action.run(async () => {
      const updated = await client.publishSong(task.id, body);
      const version = updated.registration?.catalog_version;
      if (!version) throw new Error('발행 결과를 확인하지 못했습니다. 처리 현황을 새로고침해 주세요.');
      return {version, videoReviewPending: updated.video_review_pending === true};
    }, ({version, videoReviewPending}) => onPublished(version, videoReviewPending), '');
  }

  return <form onSubmit={submit}>
    <fieldset disabled={action.pending}>
      <legend>추천 카탈로그 발행</legend>
      <fieldset>
        <legend>서비스 장르</legend>
        <p className="p4-meta">추천에 사용할 장르를 하나 이상 선택해 주세요.</p>
        {!!needsMapping.length && <p className="p4-meta">
          직접 확인이 필요한 등록 장르 {needsMapping.map(tagLabel).join(', ')}
        </p>}
        <div className="p4-tag-options">
          {Object.entries(recommendationGenres).map(([code, label]) =>
            <button type="button" key={code} aria-pressed={selectedGroups.includes(code)} onClick={() => toggleGenre(code)}>{label}</button>)}
        </div>
      </fieldset>
      <label>음향 특성 제공<select value={includeAudio ? 'include' : 'omit'} onChange={event => setIncludeAudio(event.target.value === 'include')}>
        <option value="omit">제공하지 않음</option><option value="include">검증된 값 입력</option>
      </select></label>
      {includeAudio && <fieldset>
        <legend>검증된 음향 특성</legend>
        <p className="p4-meta">확인한 에너지, 어쿠스틱 정도, 음악적 긍정도 값을 0~1 범위로 입력해 주세요.</p>
        <div className="p4-final-fields">
          {(Object.keys(audioFields) as (keyof typeof audioFields)[]).map(field => <label key={field}>{audioFields[field]}
            <input type="number" min="0" max="1" step="any" required value={audioValues[field]}
              onChange={event => setAudioValues(current => ({...current, [field]: event.target.value}))}/>
          </label>)}
          <label>조성<select value={mode} onChange={event => setMode(event.target.value)}>
            <option value="">제공하지 않음</option><option value="major">장조</option><option value="minor">단조</option>
          </select></label>
        </div>
        <label>음향 특성 출처<input required maxLength={300} value={source} onChange={event => setSource(event.target.value)}/></label>
      </fieldset>}
      <div><button type="submit" disabled={!canSubmit}>추천 카탈로그에 발행</button></div>
    </fieldset>
    <ActionState {...action}/>
  </form>;
}
