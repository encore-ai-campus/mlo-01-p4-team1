# 개발 역할별 파일과 인터페이스 소유권

기준은 `aws-infra/contracts/ownership.v1.json`입니다. 정확한 파일 규칙이 디렉터리 prefix보다 우선합니다. 등록되지 않은 경로는 미배정이며 공통 기준 담당자와 먼저 조율합니다. 이는 작업 규약과 검증 규칙이며 IAM·파일 시스템 권한 격리를 뜻하지 않습니다.

## 공통 기준

README·Makefile·gitignore·공통 dependency lock, `contracts/`, `scripts/`, `tests/preparation/`, `docs/architecture/`의 설계도·안내와 현재 공통 인계 문서는 공통 기준 담당자가 관리합니다. 공통 변경은 통합 담당자가 별도 검토합니다.

공개 저장소에서는 공통 기준 담당자가 `app/`의 기본 소유권과 `.github/ISSUE_TEMPLATE/`, PR 템플릿 및 공개 보안 검사 workflow를 관리합니다. 팀원의 개별 기능 구현은 Issue·PR에서 해당 영역 담당자와 조율하며, 이 계약 자체는 운영 IAM이나 파일 시스템 권한을 부여하지 않습니다.

각 역할은 공통 기준을 고치지 않습니다. 변경이 필요하면 자기 `requests/`에 변경안과 소비 측 영향·호환성을 적습니다. 공동 승인이 필요한 논리 계약을 각 branch에서 서로 다른 값으로 바꾸지 않습니다.

## A 소유

- `aws-infra/foundation/` 구현·스크립트·tests·toolchain lock·파트 문서
- 로컬 `.local/agents/`의 A 기록·요청·인계 (Git 제외)
- `.github/workflows/infra-check.yml`, `infra-plan.yml`, `infra-apply.yml`

## B 소유

- `aws-infra/runtime/` Ansible·컨테이너·스크립트·tests·toolchain lock·파트 문서
- 로컬 `.local/agents/`의 B 기록·요청·인계 (Git 제외)
- `.github/workflows/runtime-check.yml`, `image-build.yml`, `app-deploy.yml`

## C 소유

- `aws-infra/lifecycle/` 종료·삭제 계획·실행 adapter·journal·실패/재개·tests·도구 lock·파트 문서
- 로컬 `.local/agents/`의 C 기록·요청·인계 (Git 제외)
- `.github/workflows/lifecycle-check.yml` AWS 인증 없는 로컬 검증

개인용 `AGENTS.md`와 `.local/agents/`는 Git 제외 대상입니다. 소유권 JSON에 남은 과거 기록 경로는 호환용 예약이며 공개 추적 허가가 아닙니다. 역할별 workflow는 서로 다른 파일이고 공통 Actions 설정 파일은 통합 담당자가 관리합니다. `.github/workflows/infra-destroy.yml`은 C 소유이며 실제 실행은 별도 승인과 입력 검증이 필요합니다. GitHub 사용자·팀이 정해지기 전 가짜 CODEOWNERS 계정을 만들지 않습니다.

C는 기존 A/B 소유 파일의 삭제 보호·state·종료/백업 코드를 수정하지 않습니다. C adapter가 소비할 인터페이스와 공통 실행 잠금·보호 해제 연계가 필요하면 자기 requests에 제안합니다. 변경 검토 전 실제 실행 경로는 차단하고 로컬 대역을 이용한 독립 구현·검증을 진행합니다.

## 기능별 경계

| 기능 | A 산출물 | B 산출물 |
|---|---|---|
| 연결 | VPC·Peering·route·SG·Endpoint·DNS | 호스트·컨테이너 통신 설정과 client 재시도 |
| 서버 | EC2·ENI·EBS·IMDS·instance profile | mount·권한·Docker·systemd·영속 데이터 |
| S3 | bucket·Endpoint·IAM·prefix·state 소유권 | S3 소비·backup·checkpoint·전송 임시 영역 사용 |
| SSM | Document·역할·대상 제한·versioned artifact 입력 | 실행 payload·설치/배포 순서·health·rollback |
| CI 인증 | OIDC provider·trust·plan/apply/runtime 역할 | 해당 역할을 참조하는 이미지/배포 workflow |
| 인계 | inventory-v1 exporter·인프라 실행 절차 | inventory adapter·runtime-plan-v1·서버 실행 절차 |

실제 배포가 필요한 SSM payload/secret/이미지 참조는 단순 문자열 shell 실행으로 연결하지 않습니다. A는 리소스·허용 입력을, B는 검증 가능한 versioned artifact와 실행 코드를 담당하며 호환 규격은 공통 변경 요청으로 조율합니다.

## 서로 기다리지 않는 개발

A는 mock provider로 실제 exporter를 시험하고 B는 공통 합성 inventory로 실제 renderer를 시험합니다. 상대 구현이 없어도 단위·자기 파트 통합 검증을 진행합니다. 공통 예제를 복사하는 것만으로 producer·consumer 구현을 완료한 것으로 표시하지 않습니다.

통합 담당자가 두 결과를 같은 기준에 모은 뒤 `make integration-contract`로 A exporter → B renderer → 양쪽 schema/의미 검증을 수행합니다. A/B가 상대 worktree를 직접 수정하거나 구현 파일을 복사해 소유권을 중복시키지 않습니다.

## 소유권 검사

첫 세션에 기록한 기준 SHA부터의 변경과 미커밋/미추적 파일을 함께 검사합니다.

```bash
.venv/bin/python scripts/check_preparation.py --owner A --base <공통-기준-SHA>
# B는 --owner B를 사용합니다.
# C는 --owner C를 사용합니다.
```

검사는 상대 파일·공통 파일·미배정 파일 변경을 거부합니다. 실제 inventory·출력·venv 등 ignore된 파일은 커밋하지 않으며 자동 생성물로 소유권 검사를 우회하지 않습니다.
