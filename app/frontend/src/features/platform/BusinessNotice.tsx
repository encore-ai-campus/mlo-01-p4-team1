import {useEffect,useRef} from 'react';

export function BusinessNotice({open,onClose}:{open:boolean;onClose:()=>void}){
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{const element=dialog.current;if(!element)return;if(open&&!element.open)element.showModal();if(!open&&element.open)element.close();},[open]);
  return <dialog ref={dialog} className="p-business-dialog" aria-labelledby="business-notice-title" onCancel={event=>{event.preventDefault();onClose();}} onClick={event=>{if(event.target===event.currentTarget)onClose();}}>
    <div><p className="p-eyebrow">BUSINESS SIGNUP</p><h2 id="business-notice-title">사업자 가입은 이렇게 진행됩니다</h2>
      <p>먼저 메인 플랫폼의 공통 계정을 만듭니다. 새 매장을 연결하려면 아래 절차를 이어서 진행합니다.</p>
      <ol className="p-business-steps"><li><strong>공통 회원가입</strong><span>사용자와 같은 아이디·비밀번호로 가입합니다.</span></li><li><strong>국세청 사업자 정보 확인</strong><span>가입 후 사업자 등록정보를 확인합니다.</span></li><li><strong>별도 매장 관리권한 심사</strong><span>해당 매장을 관리할 권한이 있는지 별도로 확인합니다.</span></li><li><strong>승인 후 매장 권한 부여</strong><span>승인된 매장에서 음악 운영과 팀 관리를 시작합니다.</span></li></ol>
      <p className="p-field-help">사업자 정보 확인만으로 매장 관리권한이 자동 부여되지는 않습니다. 기존 매장의 직원·매니저는 소유자의 초대를 수락해 참여합니다.</p>
      <p className="p-demo-note">현재 로컬 시연은 합성 사업자정보로 절차를 확인합니다. 실제 국세청 API 조회나 실사업자정보 입력은 진행하지 않습니다.</p>
      <button type="button" className="p-primary" autoFocus onClick={onClose}>확인하고 가입 계속하기</button>
    </div>
  </dialog>;
}
