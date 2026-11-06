# 인증 전 로컬 검증 계약 v1

현재 수용 목표는 encore2~5 계정 인증 전 로컬 검증 완료입니다. 이 단계의 검증은 encore1을 포함한 실제 AWS 자격증명이나 API에 의존하지 않습니다. 5계정·5VPC·16EC2·3AZ·직접 Peering 10쌍의 설계를 유지합니다.

## 실행 경계

공개 의존성 다운로드, 고정 도구 설치, 합성 inventory/state, mock provider, 가짜 AWS CLI, localhost SDK 대역, 자기 run 전용 Docker 시험을 사용합니다. Terraform builtin `terraform_data`의 임시 local state 시험은 AWS provider/backend가 없는 시험 폴더에서만 실행합니다.

실제 AWS 로그인·profile 수정·STS/SSM·실제 backend 접근·refresh/plan/apply/import/destroy·이미지 게시·원격 배포·집 PC/Tailscale 변경은 이 단계에 포함하지 않습니다. 계정 인증 없이 가능한 구현·회귀검증을 계속합니다.

## 수용 기준

| 범위 | 명령과 완료 조건 |
|---|---|
| 준비 | `make prep-check prep-test`, 소유권·문서·schema·개인용 파일 추적 차단 |
| Foundation | `make foundation-unit foundation-integration`, 7개 mock root·16EC2·10Peering·정책 변형 거부·실제 exporter |
| Runtime | `make runtime-unit runtime-integration`, 16node render·Ansible·로컬 TLS 서비스·멱등 설치·checkpoint·백업/복원·rollback |
| Lifecycle | `make lifecycle-unit lifecycle-integration`, 대상·보존·승인·중복/동시 실행·응답 유실·재개 검증 |
| 공통 | `make integration-contract integration-execution integration-lifecycle preauth-check`, exporter→renderer·artifact·격리 credentials 대역·공통 잠금·종료/독립 복원 |
| 최종 통합 | 같은 최종 소스 상태에서 `make predeploy-check` 전체 통과, 필수 suite 누락/skip 없음 |

계획 교체·오래된 state/approval·잘못된 대상·영속 데이터 삭제·lease 상실·불명확 실행·부분 실패는 거부하거나 관측 기반 복구로 남겨야 합니다. 단순 mock 응답을 실제 컨테이너/DB 시험 대신 사용하지 않습니다.

결과는 `pass/fail/blocked/not_run`으로 구분하고 소스 SHA·변경 파일·명령·종료코드·도구/플랫폼·근거를 남깁니다. 환경 준비와 실제 suite 통과는 별도 상태입니다. Mac ARM64 통과는 Linux AMD64·AWS 3AZ·실제 SSM/IMDS·GPU 성공을 뜻하지 않습니다. 계정·네트워크·AMI·실제 앱 binding·비용·집 PC 항목은 실환경 인수로 남기며 `ready_for_aws=false`를 유지합니다.

## 로컬 자료와 공개 소스

개인용 지침·내부 보고서·프롬프트·상태/세션 기록은 `.local/` 또는 ignore된 도구 지침 파일에 보관합니다. `.gitignore`는 이미 추적한 파일을 제거하지 않으므로 index 추적 여부도 검사합니다. 공개 보안 gate는 강제 추가한 지침 파일과 Git bundle도 거부합니다. 과거 Git 이력의 기존 사본은 이 변경으로 삭제되지 않습니다.
