import {useEffect,useRef} from 'react';

export function BusinessNotice({open,onClose}:{open:boolean;onClose:()=>void}){
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{const element=dialog.current;if(!element)return;if(open&&!element.open)element.showModal();if(!open&&element.open)element.close();},[open]);
  return <dialog ref={dialog} className="p-business-dialog" aria-labelledby="business-notice-title" onCancel={event=>{event.preventDefault();onClose();}} onClick={event=>{if(event.target===event.currentTarget)onClose();}}>
    <div><p className="p-eyebrow">BUSINESS SIGNUP</p><h2 id="business-notice-title">매장 운영자 가입 안내</h2>
      <p>통합 플랫폼 계정으로 가입한 뒤 매장을 연결해 주세요.</p>
      <ol className="p-business-steps"><li><strong>공통 회원가입</strong><span>매장 운영자 유형을 선택해 가입합니다.</span></li><li><strong>매장 연결</strong><span>매장명과 지역으로 매장을 찾아 연결합니다.</span></li><li><strong>음악 운영</strong><span>뮤더에서 선곡하고 매장에 적용합니다.</span></li></ol>
      <p className="p-field-help">기존 매장의 직원·매니저는 초대를 수락해 참여합니다.</p>
      <p className="p-demo-note">로컬 시연에서는 실제 매장 소유 여부를 확인하지 않습니다.</p>
      <button type="button" className="p-primary" autoFocus onClick={onClose}>확인하고 가입 계속하기</button>
    </div>
  </dialog>;
}
