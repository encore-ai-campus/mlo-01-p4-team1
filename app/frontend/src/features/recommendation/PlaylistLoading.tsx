import {Icon} from '../../components/Icons';
import './PlaylistLoading.css';

export function PlaylistLoading({compact}: {compact: boolean}) {
  return <div className={compact ? 'playlist-loading playlist-loading--compact' : 'playlist-loading'}>
    <div className="playlist-loading__lane" aria-hidden="true">
      <div className="playlist-loading__road"/>
      <div className="playlist-loading__walker">
        <span className="playlist-loading__shadow"/>
        <img className="playlist-loading__penguin" src="/penguin-guide-first.png" alt="" width={124} height={124}/>
      </div>
      <span className="playlist-loading__note"><Icon name="music"/></span>
    </div>
    <div className="playlist-loading__copy">
      <strong>{compact ? '새로운 음악을 찾고 있어요…' : '어울리는 음악을 고르고 있어요…'}</strong>
      <p>{compact ? '지금 선곡표를 보며 잠시 기다려 주세요.' : '매장의 분위기에 맞는 선곡표를 준비하고 있어요.'}</p>
    </div>
  </div>;
}
