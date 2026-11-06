import type {Claim} from './WorkspacePanel';

export function BusinessJourney(){return <section className="p-card p-onboarding"><h2>사업자 계정에서 매장 운영까지</h2><ol className="p-journey"><li className="done"><b>1</b><strong>공통 회원가입</strong><span>메인 플랫폼 계정 준비</span></li><li><b>2</b><strong>사업자 정보 확인</strong><span>국세청 등록정보 대조</span></li><li><b>3</b><strong>매장 관리권한 심사</strong><span>해당 매장 관리권한 확인</span></li><li><b>4</b><strong>승인·권한 부여</strong><span>내 매장에서 운영 시작</span></li></ol><p>사업자 정보가 일치해도 매장 관리권한 심사는 별도로 진행합니다. 기존 매장의 직원·매니저는 아래 초대함에서 참여할 수 있습니다.</p><p className="p-demo-note">현재는 합성 사업자 시나리오로 2~4단계를 확인합니다. 실제 국세청 API 조회나 실사업자정보 제출은 제공하지 않습니다.</p></section>;}

export function claimGuidance(claim:Claim):string{
  if(claim.state==='APPROVED')return '승인이 완료되어 매장 권한이 부여되었습니다. 아래 ‘승인 매장 열기’로 운영을 시작하세요.';
  if(claim.state==='SUBMITTED')return '관리권한 심사를 기다리고 있습니다. 결과는 이 화면에서 확인할 수 있습니다.';
  if(claim.state==='NEEDS_MORE')return '관리자가 보완을 요청했습니다. 정보를 수정하고 사업자 정보를 다시 확인한 뒤 심사를 재요청해 주세요.';
  if(claim.state==='REJECTED')return '관리권한 심사에서 승인되지 않았습니다. 신청 정보와 관리권한을 확인한 뒤 새 신청을 진행해 주세요.';
  if(claim.state==='CANCELLED')return '취소된 신청입니다. 매장 연결이 필요하면 새 신청을 진행해 주세요.';
  if(claim.match_result==='UNAVAILABLE')return '사업자 정보를 확인하지 못했습니다. 입력 정보를 수정·저장해 다시 확인해 주세요.';
  if(claim.match_result==='NOT_MATCHED')return '사업자 정보가 일치하지 않습니다. 등록정보를 확인하고 수정해 주세요.';
  if(claim.business_status==='CLOSED')return '폐업 상태로 확인되어 심사를 요청할 수 없습니다. 사업자 상태를 확인해 주세요.';
  if(claim.match_result==='MATCHED'&&claim.business_status==='ACTIVE')return '사업자 정보 확인이 완료되었습니다. ‘매장 관리권한 심사 요청’을 눌러 다음 단계로 진행하세요.';
  return '사업자 정보를 확인하고 있습니다. 상태는 자동으로 갱신됩니다.';
}
